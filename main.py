"""さいたま市文書スクレイピング・PDF統合ツール。

コマンドプロンプトで `python main.py` を実行してください。

引数を付けずに実行すると、URLの入力を求めたうえで全処理を行います。
初めて対象にするサイトでは、まず --dry-run で収集対象を下見することを勧めます。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from scraper import (
    archive, cli, config, crawler, discusscabinet, downloader, output, pdfmerge,
    urlutils, winenv,
)
from scraper.converter import Converter
from scraper.fetcher import Fetcher
from scraper.models import Document, Status, Summary
from scraper.report import Logger, write_manifest

PHASES = 6


@dataclass
class Estimate:
    """本番実行の所要時間見積り（1資料あたりの秒数と、その内訳の説明）。"""
    seconds_each: float
    note: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="指定URL配下の資料を収集し、PDFに結合します。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="引数なしで実行すると、URLの入力を求めたうえで全処理を行います。",
    )
    parser.add_argument("url", nargs="?",
                        help="対象URL。省略すると実行後に入力を求めます")
    parser.add_argument("--dry-run", action="store_true",
                        help="下見。クロールのみ行い、収集対象の一覧を出力します"
                             "（ダウンロード・変換・結合は行いません）")
    parser.add_argument("--max-pages", type=int, metavar="N",
                        help="巡回するページ数の上限。初回の試験実行に使います")
    parser.add_argument("--cabinet", metavar="名前",
                        help="DiscussCabinet専用。対象キャビネットを名前で指定します"
                             "（例: 本会議）。省略すると実行時に選択します")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None,
         printer: cli.Printer | None = None) -> int:
    """引数なしで実行するとコンソール版。

    printer を差し替えると入出力先を変更できる（web.py のブラウザ版が利用する）。
    """
    args = parse_args(argv)
    winenv.setup_console()
    printer = printer or cli.Printer()
    printer.header()

    if args.max_pages is not None:
        config.MAX_PAGES = args.max_pages
        printer.line(f"巡回ページ数の上限: {args.max_pages}ページ")
    if args.dry_run:
        printer.line("下見モード: クロールのみ行い、ダウンロードは行いません")

    # 事前チェックはクロール前に行う。数時間かけて収集した後に変換不能と
    # 判明する事態を避けるため、この順序を守る（設計7.2）。
    # 下見モードは変換を行わないためLibreOfficeを必要としない。
    soffice = winenv.find_soffice()
    if soffice is None and not args.dry_run:
        printer.error(
            "LibreOffice が見つかりません。\n"
            "  Word・Excel・PowerPoint をPDFに変換するために必要です。\n"
            "  https://ja.libreoffice.org/ からインストールし、\n"
            "  soffice.exe のあるフォルダを環境変数 PATH に追加してください。\n"
            "  （既定のインストール先: C:\\Program Files\\LibreOffice\\program）"
        )
        printer.line()
        printer.line("このまま続行すると、PDFと画像だけを収集します。")
        printer.line("Word・Excel・PowerPoint は変換できず「未収録ファイル」に残ります。")
        if not printer.confirm("LibreOffice なしで続行しますか？"):
            printer.line("中止しました。収集対象の確認だけなら --dry-run で実行できます。")
            printer.wait_exit()
            return 1
        printer.line("LibreOffice なしで続行します。")
    if soffice is not None:
        printer.line(f"LibreOffice: {soffice}")

    desktop = winenv.desktop_dir()
    problem = output.check_desktop(desktop)
    if problem:
        printer.error(problem)
        printer.wait_exit()
        return 1
    printer.line(f"出力先デスクトップ: {desktop}")
    printer.line()

    if args.url:
        base_url = urlutils.normalize(args.url)
        if base_url is None:
            printer.error(f"URLの形式が正しくありません: {args.url}")
            return 1
        printer.line(f"対象URL: {base_url}")
    else:
        base_url = printer.prompt_url()
        if base_url is None:
            printer.line("中止しました。")
            return 1

    work_dir = output.make_work_dir()
    log = Logger(work_dir / config.LOG_NAME)
    log.write(f"開始 URL={base_url} dry_run={args.dry_run} max_pages={config.MAX_PAGES}")

    keep_work = False
    try:
        return _run(printer, log, base_url, desktop, work_dir, soffice,
                    args.dry_run, args.cabinet)
    except KeyboardInterrupt:
        keep_work = True
        printer.error("処理が中断されました。")
        log.write("処理が中断されました（Ctrl+C）")
        return 1
    except Exception as exc:                     # noqa: BLE001 - 最終防衛線
        keep_work = True
        printer.error(f"予期しないエラーが発生しました: {exc.__class__.__name__}: {exc}")
        log.write(f"予期しないエラー: {exc.__class__.__name__}: {exc}")
        return 1
    finally:
        log.close()
        if keep_work:
            printer.line(f"調査用に作業ディレクトリを残しています: {work_dir}")
            printer.wait_exit()


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return "1分未満"
    if seconds < 3600:
        return f"約 {seconds / 60:.0f}分"
    return f"約 {seconds / 3600:.1f}時間"


def _dry_run_report(printer: cli.Printer, log: Logger, documents: list[Document],
                    desktop: Path, work_dir: Path, seconds_each: float,
                    note: str = "") -> int:
    """下見の結果を表示し、収集対象の一覧をデスクトップに書き出す。"""
    out_dir = output.make_output_dir(desktop, suffix="_下見")
    write_manifest(documents, out_dir / config.MANIFEST_NAME)
    log.write(f"下見結果を出力 {out_dir}")
    log.close()
    shutil.copy2(work_dir / config.LOG_NAME, out_dir / config.LOG_NAME)
    output.cleanup_work_dir(work_dir)

    target = [d for d in documents if d.status == Status.DISCOVERED]
    skipped = len(documents) - len(target)
    by_ext = Counter(d.ext for d in target)

    printer.line()
    printer.line("=" * 70)
    printer.line(" 下見の結果")
    printer.line("=" * 70)
    printer.line(f"収集対象の資料: {len(target)}件"
                 + (f"（robots.txt等で除外 {skipped}件）" if skipped else ""))
    printer.line()
    if set(by_ext) == {""}:
        # DiscussCabinetの下見は詳細画面を開かないため、この時点では形式が不明
        printer.line("  ファイル形式は本番実行時に判明します（下見では詳細画面を開きません）")
    else:
        for ext, count in sorted(by_ext.items(), key=lambda kv: (-kv[1], kv[0])):
            printer.line(f"  {ext or '(形式不明)':<16} {count:5d} 件")
    printer.line()

    printer.line(f"本番実行の予想所要時間: {_format_duration(len(target) * seconds_each)}"
                 + (f"（{note}）" if note else ""))
    printer.line()
    printer.line("収集対象の一覧を書き出しました。内容を確認してください:")
    printer.line(f"  {out_dir / config.MANIFEST_NAME}")
    printer.line(f"  {out_dir / config.LOG_NAME}")
    printer.line()
    printer.line("問題なければ、--dry-run を外して本番実行してください。")
    printer.wait_exit()
    return 0


def _run(printer: cli.Printer, log: Logger, base_url: str, desktop: Path,
         work_dir: Path, soffice: Path | None, dry_run: bool = False,
         cabinet_filter: str | None = None) -> int:
    """フェーズ1〜2はサイトに応じて切り替え、フェーズ3〜6は共通で処理する。"""
    summary = Summary()

    if discusscabinet.matches(base_url):
        collected = _collect_discusscabinet(printer, log, base_url, work_dir,
                                            summary, dry_run, cabinet_filter)
    else:
        collected = _collect_generic(printer, log, base_url, work_dir, summary, dry_run)

    if collected is None:
        printer.wait_exit()
        return 1
    documents, seen, estimate = collected

    if dry_run:
        return _dry_run_report(printer, log, documents, desktop, work_dir,
                               estimate.seconds_each, estimate.note)

    return _process(printer, log, documents, seen, desktop, work_dir, soffice, summary)


def _collect_generic(printer: cli.Printer, log: Logger, base_url: str, work_dir: Path,
                     summary: Summary, dry_run: bool
                     ) -> tuple[list[Document], dict, Estimate] | None:
    """汎用クローラでフェーズ1〜2を行う。"""
    printer.line()
    printer.line(f"対象範囲: {base_url} 配下"
                 f"（ホスト {base_url.split('/')[2]} のみ）")

    fetcher = Fetcher(base_url, log)
    printer.line(f"リクエスト間隔: {fetcher.interval:.1f}秒"
                 + ("（robots.txt 取得成功）" if fetcher.robots_loaded else "（robots.txt なし）"))

    # --- フェーズ1: クロール ---------------------------------------------
    printer.phase(1, PHASES, "クロール中...")
    log.section("フェーズ1: クロール")

    def on_crawl(pages: int, docs: int, queued: int) -> None:
        printer.progress(f"  ページ {pages:6d} 件 | 資料発見 {docs:6d} 件 | 未訪問 {queued:6d} 件")
        if pages and pages % config.PROGRESS_NOTICE_EVERY == 0:
            printer.line(f"  {pages}ページを巡回中です。中断する場合は Ctrl+C を押してください。")

    result = crawler.crawl(fetcher, base_url, log, on_crawl)
    documents: list[Document] = result.documents
    summary.pages_crawled = result.pages_crawled
    summary.pages_failed = result.pages_failed
    summary.robots_skipped = result.robots_skipped
    summary.interrupted = result.interrupted
    printer.phase_done(1, PHASES,
                       f"{result.pages_crawled}ページを巡回、{len(documents)}件の資料を発見しました。")
    if result.external_docs:
        printer.line(f"  ※ 別ホスト上の資料 {result.external_docs}件は対象外としました。")
    log.write(f"クロール完了 ページ{result.pages_crawled} / 資料{len(documents)}")

    if result.interrupted and not _confirm_continue(printer, log, work_dir):
        return None

    pending = [d for d in documents if d.status == Status.DISCOVERED]
    if not pending:
        printer.error("ダウンロード可能な資料が1件も見つかりませんでした。"
                      "\n  URLが正しいか、対象ページに資料へのリンクがあるかを確認してください。"
                      "\n  資料のリンクがJavaScriptで生成されている場合、このツールでは取得できません。")
        printer.line(f"  巡回の記録: {work_dir / config.LOG_NAME}")
        log.write("資料0件のため終了")
        return None

    if dry_run:
        fetcher.close()
        return documents, {}, Estimate(
            fetcher.interval,
            f"{fetcher.interval:.0f}秒間隔 × {len(pending)}件のダウンロード")

    printer.line("  ダウンロード予想所要時間: "
                 f"{_format_duration(len(pending) * fetcher.interval)}"
                 f"（{fetcher.interval:.0f}秒間隔 × {len(pending)}件）")

    # --- フェーズ2: ダウンロード -----------------------------------------
    printer.phase(2, PHASES, "ダウンロード中...")
    log.section("フェーズ2: ダウンロード")

    def on_download(done: int, total: int) -> None:
        printer.progress(f"  {done}/{total}")

    seen = downloader.download_all(documents, fetcher, work_dir / "downloads",
                                   log, on_download)
    fetcher.close()
    summary.downloaded_ok = sum(1 for d in documents if d.status == Status.DOWNLOADED)
    summary.downloaded_ng = sum(1 for d in documents if d.status == Status.FAILED_DOWNLOAD)
    summary.dup_skipped = sum(1 for d in documents if d.status == Status.SKIPPED_DUP_HASH)
    printer.phase_done(2, PHASES,
                       f"{summary.downloaded_ok}件を取得、{summary.downloaded_ng}件が失敗しました。")
    return documents, seen, Estimate(fetcher.interval)


def _confirm_continue(printer: cli.Printer, log: Logger, work_dir: Path) -> bool:
    printer.line()
    if printer.confirm("中断されました。収集済みの資料で処理を続行しますか？"):
        return True
    printer.line("中止しました。")
    printer.line(f"作業ディレクトリ: {work_dir}")
    log.write("利用者の選択により中止")
    return False


def _collect_discusscabinet(printer: cli.Printer, log: Logger, base_url: str,
                            work_dir: Path, summary: Summary, dry_run: bool,
                            cabinet_filter: str | None
                            ) -> tuple[list[Document], dict, Estimate] | None:
    """DiscussCabinet専用クローラでフェーズ1〜2を行う。"""
    printer.line()
    printer.line("DiscussCabinet（さいたま市議会 文書管理システム）として接続します。")
    summary.unit_label = "フォルダ"

    client = discusscabinet.Client(base_url, log)
    try:
        cabinets = client.open()
    except RuntimeError as exc:
        printer.error(str(exc))
        return None
    if not cabinets:
        printer.error("キャビネットが見つかりませんでした。サイトの構成が変わった可能性があります。")
        return None

    selected = _select_cabinets(printer, cabinets, cabinet_filter)
    if not selected:
        printer.line("中止しました。")
        return None
    log.write("対象キャビネット: " + " / ".join(c.name for c in selected))
    printer.line(f"リクエスト間隔: {client.interval:.1f}秒")

    # --- フェーズ1: フォルダ走査 -----------------------------------------
    printer.phase(1, PHASES, "フォルダを走査中...")
    log.section("フェーズ1: フォルダ走査")

    def on_walk(folders: int, docs: int) -> None:
        printer.progress(f"  フォルダ {folders:5d} 件 | 文書 {docs:6d} 件")

    rows, folders, failed, interrupted = discusscabinet.walk(
        client, selected, log, on_walk)
    summary.pages_crawled = folders
    summary.pages_failed = failed
    summary.interrupted = interrupted
    printer.phase_done(1, PHASES,
                       f"{folders}フォルダを走査、{len(rows)}件の文書を発見しました。")

    if interrupted and not _confirm_continue(printer, log, work_dir):
        return None
    if not rows:
        printer.error("文書が1件も見つかりませんでした。")
        return None

    if dry_run:
        client.close()
        # 本番は1文書につき詳細画面と本体の2リクエストになる
        return discusscabinet.rows_to_documents(rows, client.root), {}, Estimate(
            client.interval * 2,
            f"1文書につき詳細画面と本体の2リクエスト（{client.interval:.0f}秒間隔）"
            f" × {len(rows)}件")

    printer.line("  取得予想所要時間: "
                 f"{_format_duration(len(rows) * client.interval * 2)}"
                 f"（1文書につき詳細画面と本体の2リクエスト × {len(rows)}件）")

    # --- フェーズ2: 文書取得 ----------------------------------------------
    printer.phase(2, PHASES, "文書を取得中...")
    log.section("フェーズ2: 文書取得")

    def on_fetch(done: int, total: int) -> None:
        printer.progress(f"  {done}/{total}")

    seen: dict = {}
    documents, interrupted = discusscabinet.fetch_documents(
        client, rows, work_dir / "downloads", seen, log, on_fetch)
    client.close()

    summary.downloaded_ok = sum(1 for d in documents if d.status == Status.DOWNLOADED)
    summary.downloaded_ng = sum(1 for d in documents if d.status == Status.FAILED_DOWNLOAD)
    summary.dup_skipped = sum(1 for d in documents if d.status == Status.SKIPPED_DUP_HASH)
    printer.phase_done(2, PHASES,
                       f"{summary.downloaded_ok}件を取得、{summary.downloaded_ng}件が失敗しました。")

    if interrupted and not _confirm_continue(printer, log, work_dir):
        return None
    return documents, seen, Estimate(client.interval)


def _select_cabinets(printer: cli.Printer, cabinets: list, filter_name: str | None):
    """対象キャビネットを決める。--cabinet 指定があれば対話をとばす。"""
    if filter_name:
        matched = [c for c in cabinets if filter_name in c.name]
        if not matched:
            printer.error(f"該当するキャビネットがありません: {filter_name}\n"
                          "  候補: " + " / ".join(c.name for c in cabinets))
            return []
        return matched

    printer.line()
    printer.line("キャビネットを選択してください（全体を対象にすると長時間かかります）:")
    printer.line("   0) すべて")
    for index, cabinet in enumerate(cabinets, start=1):
        printer.line(f"  {index:2d}) {cabinet.name}")
    choice = printer.choose(len(cabinets))
    if choice is None:
        return []
    return list(cabinets) if choice == 0 else [cabinets[choice - 1]]


def _process(printer: cli.Printer, log: Logger, documents: list[Document], seen: dict,
             desktop: Path, work_dir: Path, soffice: Path | None, summary: Summary) -> int:
    """フェーズ3〜6。どちらのクローラを使った場合も共通。"""
    # --- フェーズ3: zip展開 ----------------------------------------------
    printer.phase(3, PHASES, "zip展開中...")
    log.section("フェーズ3: zip展開")
    zip_count = sum(1 for d in documents if d.ext in config.EXT_ARCHIVE
                    and d.status == Status.DOWNLOADED)
    produced = archive.expand_all(documents, work_dir / "extracted", seen, log)
    documents.extend(produced)
    summary.extracted = sum(1 for d in produced if d.status == Status.EXTRACTED)
    summary.dup_skipped = sum(1 for d in documents if d.status == Status.SKIPPED_DUP_HASH)
    printer.phase_done(3, PHASES,
                       f"{zip_count}件のzipから{summary.extracted}件の資料を取り出しました。")

    # --- フェーズ4: PDF変換 ----------------------------------------------
    printer.phase(4, PHASES, "PDF変換中...")
    log.section("フェーズ4: PDF変換")

    def on_convert(done: int, total: int) -> None:
        printer.progress(f"  {done}/{total}")

    Converter(soffice, work_dir, log).convert_all(documents, on_convert)
    summary.converted_ok = sum(1 for d in documents if d.status == Status.CONVERTED)
    summary.converted_ng = sum(1 for d in documents if d.status == Status.FAILED_CONVERT)
    printer.phase_done(4, PHASES,
                       f"{summary.converted_ok}件を変換、{summary.converted_ng}件が失敗しました。")

    # --- フェーズ5: 結合・分割 -------------------------------------------
    printer.phase(5, PHASES, "結合・分割中...")
    log.section("フェーズ5: 結合・分割")
    out_dir = output.make_output_dir(desktop)
    log.write(f"出力フォルダ {out_dir}")
    merged = pdfmerge.merge_and_split(documents, out_dir, work_dir / "chunks", log)
    summary.output_files = len(merged.files)
    summary.oversize_warnings = merged.oversize_warnings
    sizes = [path.stat().st_size for path in merged.files]
    summary.output_bytes = sum(sizes)
    summary.output_max_bytes = max(sizes, default=0)
    summary.merged_docs = sum(1 for d in documents if d.status == Status.MERGED)
    printer.phase_done(5, PHASES, f"{summary.output_files}ファイルに分割しました。")

    # --- フェーズ6: 出力 --------------------------------------------------
    printer.phase(6, PHASES, "出力中...")
    log.section("フェーズ6: 出力")
    summary.uncollected = output.finalize(documents, out_dir, log)
    write_manifest(documents, out_dir / config.MANIFEST_NAME)
    log.write(f"完了 収録 {summary.merged_docs}件 / 未収録 {summary.uncollected}件 / "
              f"出力 {summary.output_files}ファイル")
    log.close()
    shutil.copy2(work_dir / config.LOG_NAME, out_dir / config.LOG_NAME)
    output.cleanup_work_dir(work_dir)

    printer.summary(summary, out_dir, None)
    printer.wait_exit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
