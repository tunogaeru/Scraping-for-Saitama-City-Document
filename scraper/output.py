"""出力フォルダの構築と後片付け（設計6.8）。"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from . import config, winenv
from .models import Document, Status
from .report import Logger


def make_work_dir() -> Path:
    """中間ファイル用の一時作業ディレクトリを作る（要件4.6）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(tempfile.mkdtemp(prefix=f"{config.WORK_PREFIX}{stamp}_"))
    for name in ("downloads", "extracted", "converted", "chunks"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def make_output_dir(desktop: Path, suffix: str = "") -> Path:
    """デスクトップに新規フォルダを作る。同名があれば連番を付す。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = desktop / f"{config.OUTPUT_PREFIX}_{stamp}{suffix}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = desktop / f"{base.name}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def _unique_dest(directory: Path, name: str) -> Path:
    dest = directory / name
    if not dest.exists():
        return dest
    stem, dot, ext = name.rpartition(".")
    stem = stem or name
    ext = f".{ext}" if dot else ""
    index = 2
    while True:
        candidate = directory / f"{stem}_{index}{ext}"
        if not candidate.exists():
            return candidate
        index += 1


def finalize(docs: list[Document], out_dir: Path, log: Logger) -> int:
    """未収録ファイルのみを出力フォルダへ移す（要件4.6）。

    結合資料PDFに取り込まれた元ファイル・変換済みPDFは内容が重複するため
    移動しない（作業ディレクトリの削除により消える）。
    """
    keep: list[Document] = []

    for doc in sorted(docs, key=lambda d: d.order):
        if doc.status == Status.SKIPPED_ARCHIVE:
            descendants = [o for o in docs if o.is_descendant_of(doc)]
            # 内容重複で除外された中身も、別の資料として結合資料に入っている
            collected = any(o.is_collected for o in descendants)
            # 展開自体に失敗して実ファイルが残らなかった中身がある場合、
            # zip本体が唯一の入手元になるため残す
            unrecoverable = any(
                o.status.is_failure and not (o.local_path and o.local_path.exists())
                for o in descendants
            )
            if (not collected or unrecoverable) and doc.local_path and doc.local_path.exists():
                keep.append(doc)
            continue
        if doc.needs_keeping and doc.local_path and doc.local_path.exists():
            keep.append(doc)

    if not keep:
        return 0

    target = out_dir / config.UNCOLLECTED_DIR
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for doc in keep:
        assert doc.local_path is not None
        name = f"{doc.order_key}_{doc.local_path.name.split('_', 1)[-1]}"
        dest = _unique_dest(target, name)
        try:
            shutil.move(str(doc.local_path), str(dest))
            doc.local_path = dest
            moved += 1
        except OSError as exc:
            log.write(f"未収録ファイルの移動に失敗 {doc.label} — {exc}")
    log.write(f"未収録ファイル {moved}件を {config.UNCOLLECTED_DIR} へ配置")
    return moved


def cleanup_work_dir(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)


def check_desktop(desktop: Path) -> str | None:
    """デスクトップに書き込めるか確認する。問題があればメッセージを返す。"""
    if not desktop.exists():
        return f"デスクトップフォルダが見つかりません: {desktop}"
    if not winenv.is_writable_dir(desktop):
        return f"デスクトップフォルダに書き込めません: {desktop}"
    return None
