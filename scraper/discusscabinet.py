"""DiscussCabinet（さいたま市議会 文書管理システム）専用クローラ。

このサイトは通常のWebサイトと構造が異なり、資料への <a href> リンクが存在しない。
画面遷移は全て「隠しフィールドを書き換えてフォームをPOST」する方式で、
セッションCookieを伴う。そのため汎用クローラ（crawler.py）では取得できない。

    GET  /saitama/list                         セッション確立・キャビネット一覧
    POST /saitama/list      cabinet_id=N       キャビネット直下
    POST /saitama/list      folder_id=X move=down   フォルダの中身
    POST /saitama/list      actions=next       次ページ（1ページ10件）
    POST /saitama/doc_view  docid=D            文書詳細（fileid・ファイル名を得る）
    POST /saitama/file_view fileid=F           ファイル本体

一覧と本体取得が不可分（fileidは詳細画面にしか出ない）なため、このモジュールは
汎用側のフェーズ1（クロール）とフェーズ2（ダウンロード）をまとめて担当する。
以降のフェーズ3〜6（zip展開・PDF変換・結合分割・出力）はそのまま再利用される。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from . import config, urlutils
from .fetcher import (
    DownloadResult, RateLimiter, new_session, request_with_retry, stream_to_file,
)
from .models import Document, Status
from .report import Logger

HOST = "www.discusscabinet.net"

#: 1ページあたりの表示件数を超えた場合のページ送り上限（無限ループ防止）
MAX_PAGES_PER_FOLDER = 500
MAX_FOLDER_DEPTH = 20

_RE_DOCID = re.compile(r"doSubmitWithDocid\('doc_view',\s*(\d+)\s*\)")
_RE_FOLDER = re.compile(r"setFolderid\('(\d+)'\s*,\s*'down'\)")
_RE_CABINET = re.compile(r"setCabinetid\('(\d+)'\)")
_RE_FILEID = re.compile(r"setFile\('(\d+)'\)")
_RE_COUNTS = re.compile(r"全文書数:\s*(\d+)\s*件\s*表示件数:\s*(\d+)\s*-\s*(\d+)")
_RE_DATE = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}")

#: 文書行に含まれる、件名ではないセルのテキスト
_ROW_NOISE = {"詳細", "添付", "件名", "日付"}


def matches(url: str) -> bool:
    """このURLをDiscussCabinet専用クローラで扱うべきか。"""
    return urlsplit(url).netloc.lower() == HOST


@dataclass
class Cabinet:
    id: str
    name: str


@dataclass
class FolderRef:
    id: str
    name: str


@dataclass
class DocRow:
    docid: str
    title: str
    date: str
    folder_path: str
    order: tuple[int, ...]


@dataclass
class Attachment:
    fileid: str
    filename: str


@dataclass
class CollectResult:
    documents: list[Document] = field(default_factory=list)
    rows: list[DocRow] = field(default_factory=list)
    folders_visited: int = 0
    folders_failed: int = 0
    downloaded_ok: int = 0
    downloaded_ng: int = 0
    interrupted: bool = False


# --- HTML解析（純粋関数。単体テスト対象）---------------------------------

def parse_form(html: str) -> dict[str, str]:
    """応答ページのフォーム状態を取り出す。次のPOSTはこれを土台にする。"""
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form")
    if form is None:
        return {}
    return {i.get("name"): (i.get("value") or "")
            for i in form.find_all("input") if i.get("name")}


def parse_cabinets(html: str) -> list[Cabinet]:
    """キャビネット一覧を取り出す。id=0 は「キャビネット一覧へ」なので除外する。"""
    soup = BeautifulSoup(html, "lxml")
    cabinets: list[Cabinet] = []
    seen: set[str] = set()
    for tag in soup.find_all(attrs={"onclick": True}):
        match = _RE_CABINET.search(tag.get("onclick"))
        if not match or match.group(1) == "0":
            continue
        cabinet_id = match.group(1)
        if cabinet_id in seen:
            continue
        seen.add(cabinet_id)
        cabinets.append(Cabinet(id=cabinet_id, name=tag.get_text(strip=True)))
    return cabinets


def parse_folders(html: str, current_folder_id: str) -> list[FolderRef]:
    """サブフォルダを取り出す。「上のフォルダへ」は move='up' なので混ざらない。"""
    soup = BeautifulSoup(html, "lxml")
    folders: list[FolderRef] = []
    seen: set[str] = set()
    for tag in soup.find_all(attrs={"onclick": True}):
        match = _RE_FOLDER.search(tag.get("onclick"))
        if not match:
            continue
        folder_id = match.group(1)
        if folder_id == current_folder_id or folder_id in seen:
            continue
        seen.add(folder_id)
        folders.append(FolderRef(id=folder_id, name=tag.get_text(strip=True)))
    return folders


def parse_doc_rows(html: str) -> list[tuple[str, str, str]]:
    """文書一覧テーブルから (docid, 件名, 日付) を取り出す。

    列構成はフォルダによって異なる（日付列を持たないフォルダがある）ため、
    列位置には依存させない。行の中から docid を拾い、残りのセルのうち
    日付形式のものを日付、最も長いテキストを件名として扱う。
    """
    soup = BeautifulSoup(html, "lxml")
    rows: list[tuple[str, str, str]] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        match = _RE_DOCID.search(str(tr))
        if match is None:
            continue
        docid = match.group(1)

        texts = [c.get_text(" ", strip=True) for c in cells]
        texts = [t for t in texts if t and t not in _ROW_NOISE]
        date = next((t for t in texts if _RE_DATE.match(t)), "")
        title = max((t for t in texts if t != date), key=len, default="")
        rows.append((docid, title, date))
    return rows


def parse_counts(html: str) -> tuple[int, int, int] | None:
    """「全文書数: N 件 表示件数: A - B」を (N, A, B) として返す。"""
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    match = _RE_COUNTS.search(text)
    if not match:
        return None
    return tuple(int(g) for g in match.groups())        # type: ignore[return-value]


def parse_detail(html: str) -> tuple[list[Attachment], dict[str, str]]:
    """文書詳細から添付ファイルとメタ情報を取り出す。"""
    soup = BeautifulSoup(html, "lxml")

    meta: dict[str, str] = {}
    for tr in soup.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th is None or td is None:
            continue
        meta[th.get_text(strip=True).rstrip(":：")] = td.get_text(" ", strip=True)

    attachments: list[Attachment] = []
    seen: set[str] = set()
    for tag in soup.find_all(attrs={"onclick": True}):
        match = _RE_FILEID.search(tag.get("onclick") or "")
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        attachments.append(Attachment(fileid=match.group(1),
                                      filename=tag.get_text(strip=True)))
    return attachments, meta


# --- クライアント ---------------------------------------------------------

class Client:
    def __init__(self, base_url: str, log: Logger, interval: float | None = None) -> None:
        parts = urlsplit(base_url)
        # /saitama/list → /saitama/ をアプリのルートとする
        root = parts.path.rsplit("/", 1)[0] or "/saitama"
        self.root = f"{parts.scheme}://{parts.netloc}{root}/"
        self.log = log
        self.limiter = RateLimiter(config.REQUEST_INTERVAL if interval is None else interval)
        self.session = new_session()
        self.state: dict[str, str] = {}

    @property
    def interval(self) -> float:
        return self.limiter.interval

    def _post(self, action: str, **overrides: str) -> str | None:
        data = dict(self.state)
        data.update(overrides)
        resp, error = request_with_retry(self.session, self.root + action, self.limiter,
                                         method="POST", data=data)
        if resp is None:
            self.log.write(f"失敗:POST {action} — {error}")
            return None
        html = resp.text
        resp.close()
        self.state = parse_form(html) or self.state
        return html

    def open(self) -> list[Cabinet]:
        """セッションを確立し、キャビネット一覧を返す。"""
        resp, error = request_with_retry(self.session, self.root + "list", self.limiter)
        if resp is None:
            raise RuntimeError(f"接続できません: {error}")
        html = resp.text
        resp.close()
        self.state = parse_form(html)
        return parse_cabinets(html)

    def list_folder(self, cabinet_id: str, folder_id: str
                    ) -> tuple[list[FolderRef], list[tuple[str, str, str]]] | None:
        """フォルダの中身を返す。文書はページ送りして全件集める。"""
        html = self._post("list", cabinet_id=cabinet_id, folder_id=folder_id,
                          move="down" if folder_id != "0" else "",
                          order="", actions="", start="1")
        if html is None:
            return None

        folders = parse_folders(html, folder_id)
        rows = parse_doc_rows(html)
        counts = parse_counts(html)

        pages = 1
        while counts and counts[2] < counts[0] and pages < MAX_PAGES_PER_FOLDER:
            html = self._post("list", actions="next", move="")
            if html is None:
                break
            next_counts = parse_counts(html)
            if next_counts is None or next_counts[1] == counts[1]:
                break                       # ページが進まなくなったら終了
            rows.extend(parse_doc_rows(html))
            counts = next_counts
            pages += 1

        return folders, rows

    def doc_detail(self, docid: str) -> tuple[list[Attachment], dict[str, str]] | None:
        html = self._post("doc_view", actions="doc_view", docid=str(docid))
        if html is None:
            return None
        return parse_detail(html)

    def download(self, fileid: str, dest: Path) -> DownloadResult:
        data = dict(self.state)
        data["fileid"] = fileid
        resp, error = request_with_retry(self.session, self.root + "file_view",
                                         self.limiter, method="POST", data=data,
                                         stream=True)
        if resp is None:
            return DownloadResult(ok=False, reason=error)
        # HTMLが返ってきた場合はエラー画面（取得失敗）
        content_type = resp.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            resp.close()
            return DownloadResult(ok=False, reason="ファイルではなくHTMLが返された")
        return stream_to_file(resp, dest)

    def close(self) -> None:
        self.session.close()


# --- 走査 -----------------------------------------------------------------

def walk(client: Client, cabinets: list[Cabinet], log: Logger, progress
         ) -> tuple[list[DocRow], int, int, bool]:
    """選択されたキャビネット配下を深さ優先で走査し、文書の一覧を作る。

    order は (キャビネット番号, 0, フォルダ番号, ..., 1, 文書番号) の形にする。
    タプルの辞書順により、画面表示と同じ「サブフォルダが先、文書が後」の並びになる。
    """
    rows: list[DocRow] = []
    visited: set[str] = set()
    stats = {"folders": 0, "failed": 0}
    interrupted = False

    def descend(cabinet: Cabinet, folder_id: str, order: tuple[int, ...], path: str,
                depth: int) -> None:
        if folder_id in visited or depth > MAX_FOLDER_DEPTH:
            return
        visited.add(folder_id)

        result = client.list_folder(cabinet.id, folder_id)
        if result is None:
            stats["failed"] += 1
            log.write(f"失敗:フォルダ取得 {path}")
            return
        folders, doc_rows = result
        stats["folders"] += 1
        log.write(f"フォルダ {path}（サブ{len(folders)} / 文書{len(doc_rows)}）")

        for index, sub in enumerate(folders):
            descend(cabinet, sub.id, order + (0, index), f"{path}{sub.name}/", depth + 1)

        for index, (docid, title, date) in enumerate(doc_rows):
            rows.append(DocRow(docid=docid, title=title, date=date,
                               folder_path=path, order=order + (1, index)))
        progress(stats["folders"], len(rows))

    try:
        for index, cabinet in enumerate(cabinets):
            log.write(f"キャビネット {cabinet.name}(id={cabinet.id}) を走査")
            descend(cabinet, "0", (index,), f"/{cabinet.name}/", 0)
    except KeyboardInterrupt:
        interrupted = True
        log.write("走査が中断されました（Ctrl+C）")

    rows.sort(key=lambda r: r.order)
    return rows, stats["folders"], stats["failed"], interrupted


def fetch_documents(client: Client, rows: list[DocRow], dest_dir: Path,
                    seen: dict[str, Document], log: Logger, progress
                    ) -> tuple[list[Document], bool]:
    """各文書の詳細を開いて添付ファイルを取得する。

    1文書に複数の添付がある場合は order の末尾に連番を足して個別の資料にする。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    documents: list[Document] = []
    interrupted = False

    try:
        for position, row in enumerate(rows, start=1):
            detail = client.doc_detail(row.docid)
            if detail is None:
                documents.append(_failed_doc(row, "文書詳細を開けない"))
                progress(position, len(rows))
                continue
            attachments, meta = detail

            if not attachments:
                documents.append(_failed_doc(row, "添付ファイルがない"))
                progress(position, len(rows))
                continue

            for index, attachment in enumerate(attachments, start=1):
                order = row.order + ((index,) if len(attachments) > 1 else ())
                doc = Document(
                    order=order,
                    source_page=f"{client.root}list  {row.folder_path}",
                    url=f"{client.root}file_view?docid={row.docid}&fileid={attachment.fileid}",
                    origin="web",
                    original_name=attachment.filename or f"{row.title}",
                    ext=urlutils.url_ext("http://x/" + attachment.filename),
                )
                doc.error = None
                documents.append(doc)

                name = urlutils.local_filename(order, doc.original_name, doc.ext)
                result = client.download(attachment.fileid, dest_dir / name)
                if not result.ok:
                    doc.status = Status.FAILED_DOWNLOAD
                    doc.error = result.reason
                    log.write(f"失敗:取得 {doc.label} — {result.reason}")
                    continue

                doc.local_path = result.path
                doc.content_hash = result.sha256
                doc.size = result.size
                assert result.sha256 is not None
                first = seen.get(result.sha256)
                if first is not None:
                    doc.status = Status.SKIPPED_DUP_HASH
                    doc.error = f"{first.label} と同一内容"
                    if doc.local_path:
                        doc.local_path.unlink(missing_ok=True)
                        doc.local_path = None
                    log.write(f"除外(内容重複) {doc.label} ← {first.label}")
                else:
                    seen[result.sha256] = doc
                    doc.status = Status.DOWNLOADED

            progress(position, len(rows))
    except KeyboardInterrupt:
        interrupted = True
        log.write("ダウンロードが中断されました（Ctrl+C）")

    return documents, interrupted


def _failed_doc(row: DocRow, reason: str) -> Document:
    doc = Document(order=row.order, source_page=row.folder_path, url=None,
                   original_name=row.title, ext="")
    doc.status = Status.FAILED_DOWNLOAD
    doc.error = reason
    return doc


def rows_to_documents(rows: list[DocRow], root: str) -> list[Document]:
    """下見用。ダウンロードせずに一覧だけを Document 化する。"""
    documents = []
    for row in rows:
        doc = Document(
            order=row.order,
            source_page=f"{root}list  {row.folder_path}",
            url=f"{root}doc_view?docid={row.docid}",
            original_name=row.title,
            ext="",
        )
        doc.error = f"日付 {row.date}" if row.date else None
        documents.append(doc)
    return documents
