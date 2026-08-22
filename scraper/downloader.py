"""ダウンロードと重複排除（設計6.4）。"""

from __future__ import annotations

from pathlib import Path

from . import urlutils
from .fetcher import Fetcher
from .models import Document, Status
from .report import Logger


def download_all(docs: list[Document], fetcher: Fetcher, dest_dir: Path,
                 log: Logger, progress) -> dict[str, Document]:
    """資料をダウンロードし、内容ハッシュで重複を排除する。

    order の昇順で処理するため、重複時に残るのは常に先に発見された方となり、
    結果が決定的になる。戻り値は hash -> 最初に取得した Document。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    seen: dict[str, Document] = {}
    pending = [d for d in docs if d.status == Status.DISCOVERED]
    total = len(pending)

    for index, doc in enumerate(sorted(pending, key=lambda d: d.order), start=1):
        assert doc.url is not None
        name = urlutils.local_filename(doc.order, doc.original_name, doc.ext)
        result = fetcher.download(doc.url, dest_dir / name)

        if not result.ok:
            doc.status = Status.FAILED_DOWNLOAD
            doc.error = result.reason
            log.write(f"失敗:取得 {doc.label} — {result.reason}")
        else:
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

        progress(index, total)

    return seen
