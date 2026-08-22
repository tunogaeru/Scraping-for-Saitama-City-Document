"""zipの再帰展開（設計6.5）。"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from . import config, urlutils
from .models import Document, Status
from .report import Logger


def _member_name(info: zipfile.ZipInfo) -> str:
    """zipメンバ名を正しく復号する。

    日本語環境で作られたzipはCP932のファイル名をUTF-8フラグなしで格納することが
    多く、zipfileはそれをCP437として復号するため文字化けする。フラグが無い場合は
    CP437へ戻してCP932として読み直す。
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _is_safe(dest: Path, root: Path) -> bool:
    """展開先が作業ディレクトリ配下に収まるか（Zip Slip対策）。"""
    try:
        dest.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def expand_all(docs: list[Document], dest_dir: Path, seen: dict[str, Document],
               log: Logger) -> list[Document]:
    """全zipを再帰展開し、新たに得られた Document のリストを返す。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Document] = []
    queue = [d for d in docs
             if d.status == Status.DOWNLOADED and d.ext in config.EXT_ARCHIVE]

    while queue:
        parent = queue.pop(0)
        children = _expand_one(parent, dest_dir, seen, log)
        produced.extend(children)
        for child in children:
            if child.status == Status.EXTRACTED and child.ext in config.EXT_ARCHIVE:
                # web由来の資料は order の長さが2。zipを1階層展開するごとに1増える
                depth = len(child.order) - 2
                if depth >= config.MAX_ARCHIVE_DEPTH:
                    child.status = Status.FAILED_EXTRACT
                    child.error = f"zipの入れ子が上限（{config.MAX_ARCHIVE_DEPTH}階層）を超えた"
                    log.write(f"失敗:展開 {child.label} — {child.error}")
                else:
                    queue.append(child)

    return produced


def _expand_one(parent: Document, dest_dir: Path, seen: dict[str, Document],
                log: Logger) -> list[Document]:
    if parent.local_path is None or not parent.local_path.exists():
        parent.status = Status.FAILED_EXTRACT
        parent.error = "zipファイルが見つからない"
        return []

    try:
        archive = zipfile.ZipFile(parent.local_path)
    except (zipfile.BadZipFile, OSError) as exc:
        parent.status = Status.FAILED_EXTRACT
        parent.error = f"zipを開けない: {exc.__class__.__name__}"
        log.write(f"失敗:展開 {parent.label} — {parent.error}")
        return []

    children: list[Document] = []
    with archive:
        members = [(info, _member_name(info)) for info in archive.infolist()]
        members = [(i, n) for i, n in members if not i.is_dir()]
        members.sort(key=lambda item: item[1])

        index = 0
        for info, name in members:
            ext = urlutils.url_ext("http://x/" + name.replace("#", "_").replace("?", "_"))
            if ext not in config.EXT_TARGET:
                continue
            index += 1
            order = parent.order + (index,)
            child = Document(
                order=order,
                source_page=parent.source_page,
                url=parent.url,
                origin="zip",
                archive_member=name,
                original_name=name.rsplit("/", 1)[-1],
                ext=ext,
            )
            children.append(child)

            local = dest_dir / urlutils.local_filename(order, child.original_name, ext)
            if not _is_safe(local, dest_dir):
                child.status = Status.FAILED_EXTRACT
                child.error = "zip内のパスが作業ディレクトリ外を指している"
                log.write(f"失敗:展開 {child.label} — {child.error}")
                continue

            try:
                with archive.open(info) as src, open(local, "wb") as out:
                    digest = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = src.read(config.CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
                local.unlink(missing_ok=True)
                child.status = Status.FAILED_EXTRACT
                reason = "パスワード保護" if isinstance(exc, RuntimeError) else exc.__class__.__name__
                child.error = f"展開できない: {reason}"
                log.write(f"失敗:展開 {child.label} — {child.error}")
                continue

            if size == 0:
                local.unlink(missing_ok=True)
                child.status = Status.FAILED_EXTRACT
                child.error = "空のファイル"
                continue

            child.local_path = local
            child.size = size
            child.content_hash = digest.hexdigest()

            first = seen.get(child.content_hash)
            if first is not None:
                child.status = Status.SKIPPED_DUP_HASH
                child.error = f"{first.label} と同一内容"
                local.unlink(missing_ok=True)
                child.local_path = None
                log.write(f"除外(内容重複) {child.label} ← {first.label}")
            else:
                seen[child.content_hash] = child
                child.status = Status.EXTRACTED

    parent.status = Status.SKIPPED_ARCHIVE
    parent.error = f"zip本体（中の{len(children)}件を対象化）"
    log.write(f"展開 {parent.label} → {len(children)}件")
    return children
