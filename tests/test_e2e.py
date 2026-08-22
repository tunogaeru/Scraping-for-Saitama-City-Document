"""結合テスト（設計9.2）。

ローカルHTTPサーバに多様な構成のテストサイトを立て、クロールから出力までを
通しで実行する。LibreOfficeは環境に無い場合スタブで代替する。

    python -m unittest tests.test_e2e -v
"""

from __future__ import annotations

import http.server
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from scraper import archive, config, crawler, downloader, output, pdfmerge, winenv
from scraper.converter import Converter
from scraper.fetcher import Fetcher
from scraper.models import Status
from scraper.report import write_manifest

from test_units import make_pdf


class _Handler(http.server.SimpleHTTPRequestHandler):
    """拡張子のない資料リンクを再現するため Content-Type を細工する。"""

    def guess_type(self, path):
        if Path(path).name == "dl":
            return "application/pdf"
        return super().guess_type(path)

    def log_message(self, *args):
        pass


class _Log:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, message, indent=0):
        self.lines.append(message)

    def section(self, title):
        self.lines.append(title)

    def close(self):
        pass


def _build_site(root: Path) -> None:
    (root / "docs" / "sub").mkdir(parents=True)
    (root / "docs" / "secret").mkdir()
    (root / "files").mkdir()
    (root / "other").mkdir()

    (root / "robots.txt").write_text("User-agent: *\nDisallow: /docs/secret/\n")

    # 範囲外・別ホスト・<img src> は収集されてはならない
    (root / "docs" / "index.html").write_text(
        "<html><body>"
        '<a href="a.pdf">資料A</a>'
        '<a href="photo.png">写真</a>'
        '<img src="logo.png">'
        '<a href="/files/shared.pdf">共有資料</a>'
        '<a href="secret/hidden.pdf">秘密</a>'
        '<a href="sub/">下位ページ</a>'
        '<a href="sjis.html">SJISページ</a>'
        '<a href="/other/index.html">範囲外</a>'
        '<a href="https://example.invalid/x.pdf">外部</a>'
        "</body></html>", encoding="utf-8")

    (root / "docs" / "sub" / "index.html").write_text(
        "<html><body>"
        '<a href="b.pdf">資料B</a>'
        '<a href="dup.pdf">重複資料</a>'
        '<a href="pack.zip">まとめ</a>'
        '<a href="dl">拡張子なし</a>'
        '<a href="notes.txt">メモ</a>'
        '<a href="../">戻る（循環）</a>'
        "</body></html>", encoding="utf-8")

    (root / "docs" / "sjis.html").write_bytes(
        '<html><head><meta charset="Shift_JIS"></head><body>'
        '<a href="c.pdf">資料C（日本語ページ）</a>'
        "</body></html>".encode("shift_jis"))

    (root / "other" / "index.html").write_text(
        '<html><body><a href="ng.pdf">巡回されてはならない</a></body></html>')

    make_pdf(root / "docs" / "a.pdf")
    make_pdf(root / "docs" / "secret" / "hidden.pdf")
    make_pdf(root / "docs" / "sub" / "b.pdf")
    make_pdf(root / "docs" / "c.pdf")
    make_pdf(root / "docs" / "sub" / "dl")
    make_pdf(root / "files" / "shared.pdf")
    make_pdf(root / "other" / "ng.pdf")
    shutil.copy(root / "docs" / "a.pdf", root / "docs" / "sub" / "dup.pdf")

    (root / "docs" / "sub" / "notes.txt").write_text("メモ本文", encoding="utf-8")
    Image.new("RGBA", (40, 30), (255, 0, 0, 128)).save(root / "docs" / "photo.png")
    Image.new("RGB", (10, 10), (0, 0, 0)).save(root / "docs" / "logo.png")

    # zip の中に zip を入れ、再帰展開を確認する
    inner = root / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("奥.pdf", (root / "docs" / "c.pdf").read_bytes())
    with zipfile.ZipFile(root / "docs" / "sub" / "pack.zip", "w") as zf:
        zf.writestr("中身.pdf", make_pdf(root / "_tmp.pdf").read_bytes())
        zf.writestr("readme.md", "対象外の拡張子".encode("utf-8"))
        zf.writestr("nested/inner.zip", inner.read_bytes())
    inner.unlink()
    (root / "_tmp.pdf").unlink()


def _make_soffice_stub(path: Path) -> Path:
    """LibreOffice が無い環境向けのスタブ。--outdir の扱いを含めて検証できる。"""
    script = path / "soffice_stub.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from PIL import Image\n"
        "args = sys.argv[1:]\n"
        "outdir = Path(args[args.index('--outdir') + 1])\n"
        "src = Path(args[-1])\n"
        "outdir.mkdir(parents=True, exist_ok=True)\n"
        "Image.new('RGB', (100, 140), (255, 255, 255)).save(outdir / (src.stem + '.pdf'))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    launcher = path / "soffice"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.site = cls.tmp / "site"
        cls.site.mkdir()
        _build_site(cls.site)

        handler = lambda *a, **kw: _Handler(*a, directory=str(cls.site), **kw)  # noqa: E731
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        cls.soffice = winenv.find_soffice() or _make_soffice_stub(cls.tmp)
        cls._interval = config.REQUEST_INTERVAL
        config.REQUEST_INTERVAL = 0.0

    @classmethod
    def tearDownClass(cls):
        config.REQUEST_INTERVAL = cls._interval
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def test_full_pipeline(self):
        base = f"http://127.0.0.1:{self.port}/docs/"
        work = self.tmp / "work"
        for name in ("downloads", "extracted", "converted", "chunks"):
            (work / name).mkdir(parents=True, exist_ok=True)
        out_dir = self.tmp / "out"
        out_dir.mkdir(exist_ok=True)
        log = _Log()

        fetcher = Fetcher(base, log, interval=0.0)
        result = crawler.crawl(fetcher, base, log, lambda *a: None)
        docs = result.documents
        names = {d.original_name for d in docs}

        # --- クロール範囲 --------------------------------------------------
        # /docs/ , /docs/sub/ , /docs/sjis.html の3ページ
        self.assertEqual(result.pages_crawled, 3)
        self.assertNotIn("ng.pdf", names, "範囲外の /other/ が巡回された")
        self.assertEqual(result.external_docs, 1, "外部ホストの資料が除外されていない")
        self.assertNotIn("logo.png", names, "<img src> の画像を収集してはならない")
        self.assertIn("photo.png", names, "<a href> の画像は収集する")

        # 資料はパス範囲の制約を受けない（同一ホストなら /files/ 配下も対象）
        self.assertIn("shared.pdf", names)
        # robots.txt の Disallow 対象は除外される
        hidden = next(d for d in docs if d.original_name == "hidden.pdf")
        self.assertEqual(hidden.status, Status.SKIPPED_ROBOTS)
        # Shift_JIS ページのリンクも解析できている
        self.assertIn("c.pdf", names)
        # 拡張子なしリンクを Content-Type から資料と判定できている
        dl = next(d for d in docs if d.original_name == "dl")
        self.assertEqual(dl.ext, ".pdf")

        # --- ダウンロードと内容重複排除 ------------------------------------
        seen = downloader.download_all(docs, fetcher, work / "downloads", log,
                                       lambda *a: None)
        fetcher.close()
        dup = next(d for d in docs if d.original_name == "dup.pdf")
        self.assertEqual(dup.status, Status.SKIPPED_DUP_HASH)
        self.assertIsNone(dup.local_path, "重複ファイルは削除されているはず")

        # --- zip の再帰展開 -------------------------------------------------
        produced = archive.expand_all(docs, work / "extracted", seen, log)
        docs.extend(produced)
        pack = next(d for d in docs if d.original_name == "pack.zip")
        self.assertEqual(pack.status, Status.SKIPPED_ARCHIVE)
        produced_names = {d.original_name for d in produced}
        self.assertIn("中身.pdf", produced_names)
        self.assertIn("inner.zip", produced_names)
        self.assertIn("奥.pdf", produced_names, "入れ子のzipが再帰展開されていない")
        self.assertNotIn("readme.md", produced_names, "対象外拡張子を取り込んでいる")
        # 奥.pdf は c.pdf と同一内容なので重複排除される
        self.assertEqual(
            next(d for d in produced if d.original_name == "奥.pdf").status,
            Status.SKIPPED_DUP_HASH)

        # --- 変換 -----------------------------------------------------------
        Converter(self.soffice, work, log).convert_all(docs, lambda *a: None)
        for name in ("a.pdf", "b.pdf", "c.pdf", "dl", "shared.pdf", "photo.png",
                     "notes.txt", "中身.pdf"):
            doc = next(d for d in docs if d.original_name == name)
            self.assertEqual(doc.status, Status.CONVERTED, f"{name}: {doc.error}")
            self.assertIsNotNone(doc.pdf_path)
            self.assertGreaterEqual(doc.page_count or 0, 1)

        # --- 結合・分割 ------------------------------------------------------
        merged = pdfmerge.merge_and_split(docs, out_dir, work / "chunks", log)
        self.assertGreaterEqual(len(merged.files), 1)
        for path in merged.files:
            self.assertLessEqual(path.stat().st_size, config.SIZE_LIMIT)

        collected = [d for d in docs if d.status == Status.MERGED]
        self.assertEqual(len(collected), 8)
        # 収録順は発見順（order）どおり
        self.assertEqual([d.order for d in sorted(collected, key=lambda d: d.order)],
                         sorted(d.order for d in collected))

        # --- 出力・後片付け --------------------------------------------------
        kept = output.finalize(docs, out_dir, log)
        write_manifest(docs, out_dir / config.MANIFEST_NAME)

        self.assertEqual(kept, 0, "失敗が無いのに未収録ファイルが出ている")
        self.assertFalse((out_dir / config.UNCOLLECTED_DIR).exists(),
                         "該当0件なら _未収録ファイル は作成しない")
        self.assertTrue((out_dir / config.MANIFEST_NAME).exists())

        manifest = (out_dir / config.MANIFEST_NAME).read_text(encoding="utf-8-sig")
        self.assertIn("結合資料_001.pdf", manifest)
        self.assertIn("除外:robots", manifest)
        self.assertIn("除外:内容重複", manifest)

    def test_uncollected_files_are_kept(self):
        """変換に失敗した資料は _未収録ファイル に残る（要件4.6）。"""
        work = self.tmp / "work2"
        (work / "downloads").mkdir(parents=True, exist_ok=True)
        out_dir = self.tmp / "out2"
        out_dir.mkdir(exist_ok=True)

        broken = work / "downloads" / "1-1_broken.xlsx"
        broken.write_bytes(b"not really a spreadsheet")
        from scraper.models import Document
        doc = Document(order=(1, 1), source_page="http://x/", url="http://x/broken.xlsx",
                       original_name="broken.xlsx", ext=".xlsx")
        doc.local_path = broken
        doc.status = Status.FAILED_CONVERT
        doc.error = "変換できない"

        kept = output.finalize([doc], out_dir, _Log())

        self.assertEqual(kept, 1)
        remaining = list((out_dir / config.UNCOLLECTED_DIR).iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].name.endswith("broken.xlsx"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
