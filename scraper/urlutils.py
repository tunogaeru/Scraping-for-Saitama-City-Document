"""URL正規化・スコープ判定・ローカルファイル名生成（設計6.1）。

このモジュールの関数は全て副作用を持たない純粋関数であり、単体テストの主対象。
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import (
    parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit,
)

from . import config
from .winenv import RESERVED_NAMES

_IGNORED_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "about:", "ftp:")
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: Content-Type から拡張子への対応（拡張子なしリンクのフォールバック用）
_CONTENT_TYPE_EXT = {
    "application/pdf": ".pdf",
    "application/x-pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
}


# --- 正規化 ---------------------------------------------------------------

def _remove_dot_segments(path: str) -> str:
    """RFC 3986 5.2.4 のアルゴリズムで '.' / '..' を解決する。"""
    buf = path
    out: list[str] = []
    while buf:
        if buf.startswith("../"):
            buf = buf[3:]
        elif buf.startswith("./"):
            buf = buf[2:]
        elif buf.startswith("/./"):
            buf = "/" + buf[3:]
        elif buf == "/.":
            buf = "/"
        elif buf.startswith("/../"):
            buf = "/" + buf[4:]
            if out:
                out.pop()
        elif buf == "/..":
            buf = "/"
            if out:
                out.pop()
        elif buf in (".", ".."):
            buf = ""
        else:
            start = 1 if buf.startswith("/") else 0
            idx = buf.find("/", start)
            if idx == -1:
                out.append(buf)
                buf = ""
            else:
                out.append(buf[:idx])
                buf = buf[idx:]
    return "".join(out)


def _strip_tracking(query: str) -> str:
    """トラッキングパラメータのみを除去する。

    残ったパラメータの順序は変更しない（順序に依存するサーバー実装があるため）。
    除去対象が1つもなければ原文をそのまま返し、再エンコードによる差異を防ぐ。
    """
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in config.TRACKING_PARAMS]
    if len(kept) == len(pairs):
        return query
    return urlencode(kept)


def normalize(url: str, base: str | None = None) -> str | None:
    """URLを正規化する。対象外なら None を返す。"""
    if not url:
        return None
    url = url.strip().replace("\n", "").replace("\r", "").replace("\t", "")
    if not url or url.startswith("#"):
        return None
    if url.lower().startswith(_IGNORED_SCHEMES):
        return None

    if base:
        try:
            url = urljoin(base, url)
        except ValueError:
            return None

    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None

    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    if not netloc:
        return None

    path = _remove_dot_segments(parts.path) or "/"
    query = _strip_tracking(parts.query)
    return urlunsplit((scheme, netloc, path, query, ""))


def dedup_key(url: str) -> tuple[str, str, str]:
    """重複判定キー。スキームを含めず http/https の同一ページを同一視する。"""
    parts = urlsplit(url)
    return (parts.netloc.lower(), parts.path, parts.query)


# --- スコープ判定 ---------------------------------------------------------

def scope_prefix(base_url: str) -> str:
    """入力URLから「配下」を表すパスプレフィックスを求める。

    - https://e.com/          → "/"
    - https://e.com/docs/     → "/docs/"
    - https://e.com/docs      → "/docs/"
    - https://e.com/docs/i.html → "/docs/"   （ファイル指定はそのディレクトリ）
    """
    path = urlsplit(base_url).path
    if path in ("", "/"):
        return "/"
    if path.endswith("/"):
        return path
    last = path.rsplit("/", 1)[-1]
    if "." in last:
        return path.rsplit("/", 1)[0] + "/"
    return path + "/"


def in_scope(url: str, netloc: str, prefix: str) -> bool:
    """ページ巡回の対象範囲内か（同一ホスト かつ プレフィックス配下）。"""
    parts = urlsplit(url)
    if parts.netloc.lower() != netloc.lower():
        return False
    return parts.path == prefix.rstrip("/") or parts.path.startswith(prefix)


def same_host(url: str, netloc: str) -> bool:
    return urlsplit(url).netloc.lower() == netloc.lower()


# --- 拡張子・ファイル名 ---------------------------------------------------

def url_ext(url: str) -> str:
    """URLパス末尾から拡張子（小文字・ドット付き）を得る。なければ ""。"""
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    name = unquote(name)
    head, dot, tail = name.rpartition(".")
    if not dot or not head:
        return ""
    ext = "." + tail.lower()
    if len(ext) > 6 or not tail.isalnum():
        return ""
    return ext


def url_basename(url: str) -> str:
    """URLから表示用のファイル名を得る。"""
    parts = urlsplit(url)
    name = unquote(parts.path.rsplit("/", 1)[-1])
    if not name:
        name = parts.netloc + parts.path.replace("/", "_")
    if parts.query and "." not in name:
        name = f"{name}_{parts.query[:40]}"
    return name or "index"


def filename_from_disposition(header: str | None) -> str | None:
    """Content-Disposition の filename / filename* を取り出す。"""
    if not header:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", header, re.I)
    if m:
        charset = m.group(1).strip() or "utf-8"
        try:
            return unquote(m.group(2).strip(), encoding=charset, errors="replace")
        except LookupError:
            return unquote(m.group(2).strip(), errors="replace")
    m = re.search(r'filename\s*=\s*"([^"]+)"', header, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", header, re.I)
    if m:
        return m.group(1).strip()
    return None


def guess_ext(content_type: str | None, disposition: str | None, url: str) -> str:
    """拡張子を持たないリンクの実体を判定する（設計6.2.2）。"""
    name = filename_from_disposition(disposition)
    if name:
        ext = url_ext("http://x/" + quote(name, safe=""))
        if ext in config.EXT_TARGET:
            return ext
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        ext = _CONTENT_TYPE_EXT.get(base)
        if ext:
            return ext
    ext = url_ext(url)
    return ext if ext in config.EXT_TARGET else ""


def sanitize_stem(name: str, limit: int = 60) -> str:
    """Windowsで安全なファイル名の幹（拡張子なし）に整える。"""
    name = unquote(name)
    name = unicodedata.normalize("NFC", name)
    name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    name = _ILLEGAL_CHARS.sub("_", name)
    name = name.strip().rstrip(". ")
    if name.upper() in RESERVED_NAMES:
        name = "_" + name
    name = name[:limit].rstrip(". ")
    return name or "file"


def local_filename(order: tuple[int, ...], original_name: str, ext: str) -> str:
    """作業ディレクトリ上でのファイル名を生成する（設計6.1.4）。

    順序キーを先頭に付けることで一意性とソート可能性を同時に確保する。
    元のURL・元ファイル名は Document と manifest.csv に保持するため情報は失われない。
    """
    seq = "-".join(str(n) for n in order)
    return f"{seq}_{sanitize_stem(original_name)}{ext}"
