"""PDF結合と30MB分割（設計6.7）。

「資料単位を保つ」制約と「単体が30MBを超える場合はページ分割する」例外が絡むと
分岐が複雑になるため、先に MergeUnit（分割してはならない最小単位）を確定させる
前処理を挟み、以降は単純な順序維持ビンパッキングに帰着させる。
"""

from __future__ import annotations

import io
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from . import config
from .models import Document, MergeUnit, Status
from .report import Logger


@dataclass
class MergeResult:
    files: list[Path] = field(default_factory=list)
    oversize_warnings: int = 0


class _UnitError(Exception):
    def __init__(self, unit: MergeUnit, reason: str) -> None:
        super().__init__(reason)
        self.unit = unit
        self.reason = reason


# --- 測定 -----------------------------------------------------------------

def _open(path: Path) -> PdfReader:
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        reader.decrypt("")
    return reader


def _measure(reader: PdfReader, start: int, end: int) -> int:
    """pages[start:end] を結合したときのバイト数を実測する。"""
    writer = PdfWriter()
    for page in reader.pages[start:end]:
        writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getbuffer().nbytes


def _write_range(reader: PdfReader, start: int, end: int, dest: Path) -> None:
    writer = PdfWriter()
    for page in reader.pages[start:end]:
        writer.add_page(page)
    with open(dest, "wb") as fh:
        writer.write(fh)


def _fit_end(reader: PdfReader, start: int, total: int, limit: int,
             avg_page_size: float) -> int | None:
    """start から何ページまで入れれば limit 以内に収まるかを求める。

    結合サイズはページ数に対して単調増加するため二分探索できる。平均ページサイズ
    による推定を起点に上限を指数的に広げ、測定回数を抑える。
    1ページだけで超過する場合は None を返す。
    """
    if _measure(reader, start, start + 1) > limit:
        return None

    guess = max(1, int(limit / avg_page_size)) if avg_page_size > 0 else 1
    hi = min(total, start + guess)
    while hi < total and _measure(reader, start, hi) <= limit:
        hi = min(total, start + max(1, hi - start) * 2)

    lo, best = start + 1, start + 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _measure(reader, start, mid) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


# --- 結合単位の確定 -------------------------------------------------------

def _split_oversized(doc: Document, chunks_dir: Path, log: Logger,
                     result: MergeResult) -> list[MergeUnit]:
    """単体で30MBを超える資料をページ単位で分割する（設計6.7.1）。"""
    assert doc.pdf_path is not None
    chunks_dir.mkdir(parents=True, exist_ok=True)
    reader = _open(doc.pdf_path)
    total_pages = len(reader.pages)
    total_size = doc.pdf_size or doc.pdf_path.stat().st_size
    avg = total_size / total_pages if total_pages else float(total_size)

    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total_pages:
        end = _fit_end(reader, start, total_pages, config.SIZE_LIMIT, avg)
        if end is None:
            end = start + 1
            result.oversize_warnings += 1
            log.write(
                f"警告 {doc.label}: {start + 1}ページ目が単独で30MBを超えるため"
                "これ以上分割できない（30MB超過を許容）"
            )
        ranges.append((start, end))
        start = end

    log.write(f"分割 {doc.label}（{total_pages}ページ / "
              f"{total_size / 1048576:.1f}MB）→ {len(ranges)}分割")

    units: list[MergeUnit] = []
    for index, (begin, finish) in enumerate(ranges, start=1):
        path = chunks_dir / f"{doc.order_key}_part{index:03d}.pdf"
        _write_range(reader, begin, finish, path)
        units.append(MergeUnit(
            order=doc.order + (0, index),
            doc=doc,
            pdf_path=path,
            size=path.stat().st_size,
            page_count=finish - begin,
            part=(index, len(ranges)),
        ))
    return units


def build_units(docs: list[Document], chunks_dir: Path, log: Logger,
                result: MergeResult) -> list[MergeUnit]:
    units: list[MergeUnit] = []
    ready = [d for d in docs if d.status == Status.CONVERTED and d.pdf_path]
    for doc in sorted(ready, key=lambda d: d.order):
        assert doc.pdf_path is not None
        size = doc.pdf_size or doc.pdf_path.stat().st_size
        if size <= config.SIZE_LIMIT:
            units.append(MergeUnit(
                order=doc.order, doc=doc, pdf_path=doc.pdf_path,
                size=size, page_count=doc.page_count or 0,
            ))
        else:
            units.extend(_split_oversized(doc, chunks_dir, log, result))
    return units


# --- ビンパッキング -------------------------------------------------------

def take_batch(pending: deque[MergeUnit], limit: int | None = None,
               margin: float | None = None) -> list[MergeUnit]:
    """順序を保ったまま、推定サイズが上限に収まる範囲で先頭から取り出す。"""
    # 既定値は呼び出し時に解決する（引数の既定値にすると import 時に固定される）
    limit = config.SIZE_LIMIT if limit is None else limit
    margin = config.PACK_MARGIN if margin is None else margin
    batch: list[MergeUnit] = []
    estimate = 0
    while pending:
        unit = pending[0]
        if batch and estimate + unit.size > limit * margin:
            break
        batch.append(pending.popleft())
        estimate += unit.size
    return batch


def pack(units: list[MergeUnit], limit: int | None = None,
         margin: float | None = None) -> list[list[MergeUnit]]:
    """単体テスト用の純粋なビンパッキング。"""
    pending = deque(units)
    batches = []
    while pending:
        batches.append(take_batch(pending, limit, margin))
    return batches


# --- 書き出し -------------------------------------------------------------

def _attempt_write(units: list[MergeUnit], dest: Path) -> list[tuple[MergeUnit, int, int]]:
    writer = PdfWriter()
    readers = []          # ページ参照が生きている間リーダーを保持する
    placements: list[tuple[MergeUnit, int, int]] = []
    cursor = 0

    for unit in units:
        try:
            reader = _open(unit.pdf_path)
            readers.append(reader)
            begin = cursor + 1
            for page in reader.pages:
                writer.add_page(page)
                cursor += 1
            placements.append((unit, begin, cursor))
        except Exception as exc:
            raise _UnitError(unit, f"{exc.__class__.__name__}: {exc}") from exc

    if hasattr(writer, "compress_identical_objects"):
        try:
            writer.compress_identical_objects()
        except Exception:
            pass                                  # 圧縮は失敗しても結合は続行する

    with open(dest, "wb") as fh:
        writer.write(fh)
    return placements


def _write_batch(units: list[MergeUnit], dest: Path,
                 log: Logger) -> tuple[list[MergeUnit], list[tuple[MergeUnit, int, int]]]:
    """1件の破損PDFのために全体が失敗しないよう、失敗した単位を除いて再構築する。"""
    remaining = list(units)
    while remaining:
        try:
            return remaining, _attempt_write(remaining, dest)
        except _UnitError as err:
            remaining = [u for u in remaining if u is not err.unit]
            err.unit.doc.status = Status.FAILED_MERGE
            err.unit.doc.error = f"結合できない: {err.reason}"
            log.write(f"失敗:結合 {err.unit.doc.label} — {err.reason}")
    dest.unlink(missing_ok=True)
    return [], []


def merge_and_split(docs: list[Document], out_dir: Path, chunks_dir: Path,
                    log: Logger) -> MergeResult:
    result = MergeResult()
    units = build_units(docs, chunks_dir, log, result)
    if not units:
        return result

    pending = deque(units)
    index = 1
    while pending:
        batch = take_batch(pending)
        dest = out_dir / config.MERGED_NAME_FMT.format(index)
        batch, placements = _write_batch(batch, dest, log)
        if not batch:
            continue

        # 結合後のサイズは元ファイルサイズの単純な合計と一致しないため、
        # 推定で詰めたあと必ず実測し、超過していれば末尾から差し戻す。
        while dest.stat().st_size > config.SIZE_LIMIT and len(batch) > 1:
            spilled = batch.pop()
            pending.appendleft(spilled)
            log.write(f"サイズ超過のため差し戻し {spilled.doc.label}{spilled.part_label}")
            batch, placements = _write_batch(batch, dest, log)
            if not batch:
                break
        if not batch:
            dest.unlink(missing_ok=True)
            continue

        size = dest.stat().st_size
        if size > config.SIZE_LIMIT:
            result.oversize_warnings += 1
            log.write(f"警告 {dest.name} が30MBを超過（{size / 1048576:.1f}MB）。"
                      "単一ページで上限を超える資料を含むため分割できない")

        for unit, begin, end in placements:
            unit.doc.status = Status.MERGED
            unit.doc.placements.append(f"{dest.name}:{begin}-{end}")

        log.write(f"出力 {dest.name}（{len(batch)}単位 / {size / 1048576:.1f}MB）")
        result.files.append(dest)
        index += 1

    return result
