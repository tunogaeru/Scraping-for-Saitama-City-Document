"""資料を表すデータモデルと状態定義。

全フェーズはこの Document のリストを段階的に更新していく。各資料は最終的に
「収録済」か「未収録（理由付き）」のいずれかに到達し、後片付けとサマリー集計は
どちらもこの状態から導出される。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal


class Status(str, Enum):
    """資料の処理状態。"""

    DISCOVERED = "発見"            # クロールで発見、未ダウンロード
    DOWNLOADED = "取得済"          # ダウンロード完了
    EXTRACTED = "展開済"           # zip内から取り出した
    CONVERTED = "変換済"           # PDF化完了（元がPDFの場合も含む）
    MERGED = "収録済"              # 結合資料PDFに収録された（最終正常状態）

    SKIPPED_ROBOTS = "除外:robots"
    SKIPPED_DUP_URL = "除外:URL重複"
    SKIPPED_DUP_HASH = "除外:内容重複"
    SKIPPED_ARCHIVE = "除外:zip本体"
    SKIPPED_SCOPE = "除外:範囲外"

    FAILED_DOWNLOAD = "失敗:取得"
    FAILED_EXTRACT = "失敗:展開"
    FAILED_CONVERT = "失敗:変換"
    FAILED_MERGE = "失敗:結合"

    def __str__(self) -> str:      # Enum既定の "Status.MERGED" を避ける
        return self.value

    @property
    def is_failure(self) -> bool:
        return self.name.startswith("FAILED_")

    @property
    def is_skip(self) -> bool:
        return self.name.startswith("SKIPPED_")


#: 元ファイルの内容が結合資料PDFに含まれている（＝重複するので残さない）状態
COLLECTED_STATUSES = {Status.MERGED, Status.SKIPPED_DUP_HASH, Status.SKIPPED_DUP_URL}


@dataclass
class Document:
    """1件の資料。"""

    order: tuple[int, ...]                  # 結合順序キー（設計6.2.3）
    source_page: str                        # 発見元ページURL
    url: str | None = None                  # zip内ファイルは None
    origin: Literal["web", "zip"] = "web"
    archive_member: str | None = None       # zip内パス
    original_name: str = ""
    ext: str = ""                           # 小文字・ドット付き

    local_path: Path | None = None
    content_hash: str | None = None
    size: int | None = None
    pdf_path: Path | None = None
    pdf_size: int | None = None
    page_count: int | None = None

    status: Status = Status.DISCOVERED
    error: str | None = None
    placements: list[str] = field(default_factory=list)   # "結合資料_002.pdf:12-31"

    # -- 表示・集計用 -------------------------------------------------------

    @property
    def order_key(self) -> str:
        return "-".join(str(n) for n in self.order)

    @property
    def label(self) -> str:
        """ログ表示用の短い識別名。"""
        if (self.origin == "zip" and self.archive_member
                and self.archive_member != self.original_name):
            return f"{self.original_name}（zip内: {self.archive_member}）"
        return self.original_name or (self.url or "?")

    @property
    def is_collected(self) -> bool:
        """内容が結合資料PDFに含まれているか（＝元ファイルを残さない）。"""
        return self.status in COLLECTED_STATUSES

    @property
    def needs_keeping(self) -> bool:
        """未収録として _未収録ファイル に残すべきか。"""
        return self.status.is_failure

    def is_descendant_of(self, other: "Document") -> bool:
        return (
            len(self.order) > len(other.order)
            and self.order[: len(other.order)] == other.order
        )

    def output_files(self) -> str:
        return ";".join(p.split(":", 1)[0] for p in self.placements)

    def output_pages(self) -> str:
        return ";".join(p.split(":", 1)[1] for p in self.placements if ":" in p)


@dataclass
class MergeUnit:
    """結合時に分割してはならない最小単位（設計6.7）。"""

    order: tuple[int, ...]
    doc: Document
    pdf_path: Path
    size: int
    page_count: int
    part: tuple[int, int] | None = None      # (1, 3) = 3分割中の1つ目

    @property
    def part_label(self) -> str:
        if self.part is None:
            return ""
        return f"（{self.part[1]}分割中の{self.part[0]}）"


@dataclass
class Summary:
    """完了時サマリー（要件4.7）。"""

    pages_crawled: int = 0
    pages_failed: int = 0
    downloaded_ok: int = 0
    downloaded_ng: int = 0
    dup_skipped: int = 0
    robots_skipped: int = 0
    extracted: int = 0
    converted_ok: int = 0
    converted_ng: int = 0
    merged_docs: int = 0
    output_files: int = 0
    output_bytes: int = 0
    output_max_bytes: int = 0
    uncollected: int = 0
    oversize_warnings: int = 0
    interrupted: bool = False
    unit_label: str = "ページ"      # 汎用は「ページ」、DiscussCabinetは「フォルダ」
