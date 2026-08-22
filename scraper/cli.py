"""コンソール入出力（設計8）。"""

from __future__ import annotations

import sys
from pathlib import Path

from . import urlutils
from .models import Summary

_WIDTH = 70
_RULE = "=" * _WIDTH


def _mb(value: int) -> str:
    return f"{value / 1048576:.1f} MB"


class Printer:
    """進捗行は \\r で同一行を更新し、スクロールで画面が流れないようにする。"""

    def __init__(self) -> None:
        self._live = False
        self._ticks = 0
        try:
            self._tty = bool(sys.stdout.isatty())
        except (AttributeError, ValueError):
            self._tty = False

    def line(self, text: str = "") -> None:
        self._clear()
        print(text)

    def header(self) -> None:
        self.line(_RULE)
        self.line(" さいたま市文書 収集ツール")
        self.line(_RULE)

    def progress(self, text: str) -> None:
        if not self._tty:
            # ファイルへリダイレクトされている場合、\r は上書きにならず1行に
            # 連結されてしまうため、間引いて改行付きで出力する
            self._ticks += 1
            if self._ticks % 25 == 1:
                print(text)
            return
        sys.stdout.write("\r" + text.ljust(_WIDTH))
        sys.stdout.flush()
        self._live = True

    def _clear(self) -> None:
        if self._live:
            sys.stdout.write("\r" + " " * _WIDTH + "\r")
            sys.stdout.flush()
            self._live = False

    def phase(self, index: int, total: int, title: str) -> None:
        self.line()
        self.line(f"[{index}/{total}] {title}")

    def phase_done(self, index: int, total: int, text: str) -> None:
        self._clear()
        self.line(f"[{index}/{total}] 完了: {text}")

    def error(self, text: str) -> None:
        self._clear()
        self.line()
        self.line(f"エラー: {text}")

    # -- 入力 ---------------------------------------------------------------

    def prompt_url(self) -> str | None:
        """正しいURLが入力されるまで繰り返す。空入力・Ctrl+C で None。"""
        while True:
            try:
                raw = input("対象URLを入力してください: ").strip()
            except (EOFError, KeyboardInterrupt):
                self.line()
                return None
            if not raw:
                self.line("URLが入力されていません。中止する場合は Ctrl+C を押してください。")
                continue
            if not raw.lower().startswith(("http://", "https://")):
                self.line("http:// または https:// で始まるURLを入力してください。")
                continue
            normalized = urlutils.normalize(raw)
            if normalized is None:
                self.line("URLの形式が正しくありません。入力し直してください。")
                continue
            return normalized

    def choose(self, maximum: int, default: int = 0) -> int | None:
        """0〜maximum の番号を選ばせる。中止なら None。"""
        while True:
            try:
                answer = input(f"選択 (0-{maximum}, 既定 {default}): ").strip()
            except (EOFError, KeyboardInterrupt):
                self.line()
                return None
            if not answer:
                return default
            if answer.isdigit() and 0 <= int(answer) <= maximum:
                return int(answer)
            self.line(f"0 から {maximum} の番号を入力してください。")

    def confirm(self, question: str) -> bool:
        while True:
            try:
                answer = input(f"{question} (Y/N): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                self.line()
                return False
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def wait_exit(self) -> None:
        """ダブルクリック実行でウィンドウが即座に閉じ、サマリーを読めなくなるのを防ぐ。"""
        if not sys.stdin or not sys.stdin.isatty():
            return
        try:
            input("\n何かキーを押すと終了します...")
        except (EOFError, KeyboardInterrupt):
            pass

    # -- サマリー -----------------------------------------------------------

    def summary(self, summary: Summary, out_dir: Path, work_dir: Path | None) -> None:
        self.line()
        self.line(_RULE)
        title = "処理が完了しました" if not summary.interrupted else "処理が完了しました（クロールは中断）"
        self.line(f" {title}")
        self.line(_RULE)
        self.line(f"出力先: {out_dir}")
        self.line()
        self.line(f"  巡回{summary.unit_label}数      {summary.pages_crawled:6d}"
                  + (f"（取得失敗 {summary.pages_failed}）" if summary.pages_failed else ""))
        self.line(f"  ダウンロード      {summary.downloaded_ok:6d} 件成功 / "
                  f"{summary.downloaded_ng:4d} 件失敗")
        if summary.dup_skipped:
            self.line(f"  内容重複による除外{summary.dup_skipped:6d} 件")
        if summary.robots_skipped:
            self.line(f"  robots.txtで除外  {summary.robots_skipped:6d} 件")
        if summary.extracted:
            self.line(f"  zipから展開       {summary.extracted:6d} 件")
        self.line(f"  PDF変換           {summary.converted_ok:6d} 件成功 / "
                  f"{summary.converted_ng:4d} 件失敗")
        self.line(f"  結合資料PDF       {summary.output_files:6d} ファイル"
                  f"（合計 {_mb(summary.output_bytes)} / 最大 {_mb(summary.output_max_bytes)}）")
        if summary.uncollected:
            self.line(f"  未収録ファイル    {summary.uncollected:6d} 件 → _未収録ファイル フォルダ")

        self.line()
        if summary.oversize_warnings:
            self.line(f"※ {summary.oversize_warnings}件が30MBを超えています。"
                      "1ページ単体で上限を超える資料のため分割できませんでした。")
        if summary.downloaded_ng or summary.converted_ng or summary.uncollected:
            self.line("失敗した資料があります。詳細は log.txt / manifest.csv を確認してください。")
        else:
            self.line("全ての資料を収録しました。")
        if work_dir is not None:
            self.line(f"作業ディレクトリを残しています: {work_dir}")
