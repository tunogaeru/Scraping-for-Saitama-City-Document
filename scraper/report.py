"""ログ（log.txt）と資料一覧（manifest.csv）の出力（設計7.4）。"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .models import Document


class Logger:
    """時系列ログをファイルへ逐次書き出す。

    作業ディレクトリ上のファイルに即座に書き出すため、異常終了しても
    そこまでのログが残る。正常終了時に出力フォルダへコピーする。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = open(path, "a", encoding="utf-8", newline="\n")

    def write(self, message: str, indent: int = 0) -> None:
        if self._fh.closed:      # 終了処理中の追記でも落とさない
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._fh.write(f"[{stamp}] {'  ' * indent}{message}\n")
        self._fh.flush()

    def section(self, title: str) -> None:
        if self._fh.closed:
            return
        self._fh.write("-" * 70 + "\n")
        self.write(title)

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


_MANIFEST_HEADER = [
    "順序", "状態", "資料名", "資料URL", "発見元ページ", "出自",
    "zip内パス", "サイズ", "ページ数", "収録先", "収録ページ", "エラー",
]


def write_manifest(docs: list[Document], path: Path) -> None:
    """資料一覧をCSVで出力する。

    UTF-8 BOM付きで書き出す。日本語WindowsのExcelはBOMなしUTF-8をCP932として
    解釈するため、BOMがないと文字化けする。
    """
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_MANIFEST_HEADER)
        for doc in sorted(docs, key=lambda d: d.order):
            writer.writerow([
                doc.order_key,
                doc.status.value,
                doc.original_name,
                doc.url or "",
                doc.source_page,
                doc.origin,
                doc.archive_member or "",
                doc.size if doc.size is not None else "",
                doc.page_count if doc.page_count is not None else "",
                doc.output_files(),
                doc.output_pages(),
                doc.error or "",
            ])
