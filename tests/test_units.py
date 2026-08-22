"""単体・結合テスト（設計9）。

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from pypdf import PdfReader

from scraper import archive, config, pdfmerge, urlutils
from scraper.models import Document, MergeUnit, Status


# --- テスト用のPDF生成 -----------------------------------------------------

def make_pdf(path: Path, pages: int = 1, px: int = 200) -> Path:
    """ランダムノイズ画像から実サイズを持つPDFを作る（圧縮が効きにくい）。"""
    images = [
        Image.frombytes("RGB", (px, px), os.urandom(px * px * 3))
        for _ in range(pages)
    ]
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    return path


def make_doc(order: tuple[int, ...], pdf: Path, name: str = "") -> Document:
    doc = Document(order=order, source_page="http://e.test/", url=f"http://e.test/{name}",
                   original_name=name or pdf.name, ext=".pdf")
    doc.local_path = pdf
    doc.pdf_path = pdf
    doc.pdf_size = pdf.stat().st_size
    doc.page_count = len(PdfReader(str(pdf)).pages)
    doc.status = Status.CONVERTED
    return doc


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# --- URL正規化 -------------------------------------------------------------

class TestNormalize(unittest.TestCase):
    def test_relative_resolution(self):
        base = "https://e.test/docs/a/index.html"
        self.assertEqual(urlutils.normalize("b.html", base), "https://e.test/docs/a/b.html")
        self.assertEqual(urlutils.normalize("../c.html", base), "https://e.test/docs/c.html")
        self.assertEqual(urlutils.normalize("/d.html", base), "https://e.test/d.html")

    def test_dot_segments(self):
        self.assertEqual(urlutils.normalize("https://e.test/a/./b/../c"), "https://e.test/a/c")
        self.assertEqual(urlutils.normalize("https://e.test/../../x"), "https://e.test/x")

    def test_fragment_and_case(self):
        self.assertEqual(urlutils.normalize("HTTPS://E.TEST/A#frag"), "https://e.test/A")

    def test_default_port_stripped(self):
        self.assertEqual(urlutils.normalize("https://e.test:443/a"), "https://e.test/a")
        self.assertEqual(urlutils.normalize("http://e.test:80/a"), "http://e.test/a")
        self.assertEqual(urlutils.normalize("https://e.test:8443/a"), "https://e.test:8443/a")

    def test_tracking_params_removed_others_kept(self):
        self.assertEqual(
            urlutils.normalize("https://e.test/a?id=3&utm_source=x&page=2"),
            "https://e.test/a?id=3&page=2")

    def test_query_untouched_when_nothing_removed(self):
        url = "https://e.test/a?b=1&b=2&c="
        self.assertEqual(urlutils.normalize(url), url)

    def test_rejected_schemes(self):
        for bad in ("javascript:void(0)", "mailto:a@b.c", "#top", "", "   "):
            self.assertIsNone(urlutils.normalize(bad))

    def test_dedup_key_ignores_scheme(self):
        self.assertEqual(urlutils.dedup_key("http://e.test/a?b=1"),
                         urlutils.dedup_key("https://e.test/a?b=1"))


class TestScope(unittest.TestCase):
    def test_prefix(self):
        cases = {
            "https://e.test": "/",
            "https://e.test/": "/",
            "https://e.test/docs/": "/docs/",
            "https://e.test/docs": "/docs/",
            "https://e.test/docs/index.html": "/docs/",
        }
        for url, expected in cases.items():
            self.assertEqual(urlutils.scope_prefix(url), expected, url)

    def test_in_scope(self):
        prefix = urlutils.scope_prefix("https://e.test/docs")
        ok = ["https://e.test/docs", "https://e.test/docs/", "https://e.test/docs/2024/a.html"]
        ng = ["https://e.test/docs2/a.html", "https://e.test/other/", "https://x.test/docs/a"]
        for url in ok:
            self.assertTrue(urlutils.in_scope(url, "e.test", prefix), url)
        for url in ng:
            self.assertFalse(urlutils.in_scope(url, "e.test", prefix), url)


class TestFilenames(unittest.TestCase):
    def test_illegal_chars_replaced(self):
        self.assertEqual(urlutils.sanitize_stem('a:b*c?d"e<f>g|h'), "a_b_c_d_e_f_g_h")

    def test_reserved_names_prefixed(self):
        self.assertEqual(urlutils.sanitize_stem("CON"), "_CON")
        self.assertEqual(urlutils.sanitize_stem("com1.txt"), "_com1")

    def test_trailing_dot_and_space(self):
        self.assertEqual(urlutils.sanitize_stem("name.  "), "name")

    def test_percent_decoded(self):
        self.assertEqual(urlutils.sanitize_stem("%E8%B3%87%E6%96%99.pdf"), "資料")

    def test_length_capped(self):
        self.assertLessEqual(len(urlutils.sanitize_stem("あ" * 200)), 60)

    def test_never_empty(self):
        self.assertEqual(urlutils.sanitize_stem("..."), "file")

    def test_local_filename_orders(self):
        self.assertEqual(urlutils.local_filename((3, 7), "報告書.pdf", ".pdf"), "3-7_報告書.pdf")
        self.assertEqual(urlutils.local_filename((3, 7, 1), "a.docx", ".docx"), "3-7-1_a.docx")

    def test_url_ext(self):
        self.assertEqual(urlutils.url_ext("https://e.test/a/b.PDF"), ".pdf")
        self.assertEqual(urlutils.url_ext("https://e.test/a/b.tar.gz"), ".gz")
        self.assertEqual(urlutils.url_ext("https://e.test/a/download?id=1"), "")
        self.assertEqual(urlutils.url_ext("https://e.test/a/.htaccess"), "")

    def test_guess_ext_from_headers(self):
        self.assertEqual(urlutils.guess_ext("application/pdf; charset=x", None, "u"), ".pdf")
        self.assertEqual(
            urlutils.guess_ext("application/octet-stream",
                               'attachment; filename="報告.xlsx"', "u"), ".xlsx")
        self.assertEqual(urlutils.guess_ext("text/html", None, "u"), "")


# --- ビンパッキング --------------------------------------------------------

class TestPacking(unittest.TestCase):
    @staticmethod
    def unit(size: int, order: int) -> MergeUnit:
        doc = Document(order=(order,), source_page="", original_name=str(order))
        return MergeUnit(order=(order,), doc=doc, pdf_path=Path("x"), size=size, page_count=1)

    def test_empty(self):
        self.assertEqual(pdfmerge.pack([]), [])

    def test_single_unit_alone_when_over_limit(self):
        units = [self.unit(100, 1)]
        self.assertEqual(len(pdfmerge.pack(units, limit=10, margin=1.0)), 1)

    def test_order_preserved(self):
        units = [self.unit(4, i) for i in range(6)]
        batches = pdfmerge.pack(units, limit=10, margin=1.0)
        flat = [u.order[0] for batch in batches for u in batch]
        self.assertEqual(flat, list(range(6)))

    def test_respects_limit(self):
        units = [self.unit(4, i) for i in range(6)]
        for batch in pdfmerge.pack(units, limit=10, margin=1.0):
            self.assertLessEqual(sum(u.size for u in batch), 10)

    def test_boundary_exact(self):
        units = [self.unit(5, 0), self.unit(5, 1), self.unit(1, 2)]
        batches = pdfmerge.pack(units, limit=10, margin=1.0)
        self.assertEqual([len(b) for b in batches], [2, 1])

    def test_take_batch_always_consumes(self):
        pending = deque([self.unit(999, 0), self.unit(1, 1)])
        batch = pdfmerge.take_batch(pending, limit=10, margin=1.0)
        self.assertEqual(len(batch), 1)
        self.assertEqual(len(pending), 1)


# --- 結合・分割（実PDF）---------------------------------------------------

class TestMerge(TempDirCase):
    def setUp(self):
        super().setUp()
        self._limit = config.SIZE_LIMIT
        self.out = self.tmp / "out"
        self.chunks = self.tmp / "chunks"
        self.out.mkdir()

    def tearDown(self):
        config.SIZE_LIMIT = self._limit
        super().tearDown()

    class _Log:
        def __init__(self): self.lines = []
        def write(self, message, indent=0): self.lines.append(message)
        def section(self, title): self.lines.append(title)
        def close(self): pass

    def test_all_outputs_within_limit_and_order_preserved(self):
        docs = []
        for i in range(8):
            pdf = make_pdf(self.tmp / f"src{i}.pdf", pages=2)
            docs.append(make_doc((1, i), pdf, f"doc{i}.pdf"))
        total = sum(d.pdf_size for d in docs)
        config.SIZE_LIMIT = total // 3          # 3ファイル程度に分かれる想定

        log = self._Log()
        result = pdfmerge.merge_and_split(docs, self.out, self.chunks, log)

        self.assertGreater(len(result.files), 1)
        for path in result.files:
            self.assertLessEqual(path.stat().st_size, config.SIZE_LIMIT, path.name)
        for doc in docs:
            self.assertEqual(doc.status, Status.MERGED, doc.original_name)
            self.assertTrue(doc.placements)
        # 収録先ファイルは発見順に単調増加する
        files = [d.placements[0].split(":")[0] for d in docs]
        self.assertEqual(files, sorted(files))

    def test_single_file_when_small(self):
        docs = [make_doc((1, i), make_pdf(self.tmp / f"s{i}.pdf"), f"s{i}.pdf") for i in range(3)]
        result = pdfmerge.merge_and_split(docs, self.out, self.chunks, self._Log())
        self.assertEqual([p.name for p in result.files], ["結合資料_001.pdf"])
        self.assertEqual(len(PdfReader(str(result.files[0])).pages), 3)

    def test_oversized_document_is_page_split(self):
        big = make_pdf(self.tmp / "big.pdf", pages=12)
        docs = [make_doc((1, 0), big, "big.pdf")]
        config.SIZE_LIMIT = big.stat().st_size // 3

        result = pdfmerge.merge_and_split(docs, self.out, self.chunks, self._Log())

        self.assertGreaterEqual(len(result.files), 3)
        for path in result.files:
            self.assertLessEqual(path.stat().st_size, config.SIZE_LIMIT, path.name)
        # 全ページが失われていない
        self.assertEqual(sum(len(PdfReader(str(p)).pages) for p in result.files), 12)
        self.assertEqual(docs[0].status, Status.MERGED)
        self.assertEqual(len(docs[0].placements), len(result.files))

    def test_single_page_over_limit_is_tolerated_with_warning(self):
        pdf = make_pdf(self.tmp / "huge.pdf", pages=2, px=400)
        docs = [make_doc((1, 0), pdf, "huge.pdf")]
        config.SIZE_LIMIT = 1024                # 1ページでも必ず超える

        result = pdfmerge.merge_and_split(docs, self.out, self.chunks, self._Log())

        self.assertEqual(len(result.files), 2)  # ページごとに1ファイル
        self.assertGreater(result.oversize_warnings, 0)

    def test_corrupt_pdf_is_dropped_not_fatal(self):
        good1 = make_doc((1, 0), make_pdf(self.tmp / "g1.pdf"), "g1.pdf")
        bad_path = self.tmp / "bad.pdf"
        bad_path.write_bytes(b"%PDF-1.4 broken")
        bad = make_doc((1, 1), make_pdf(self.tmp / "g2.pdf"), "bad.pdf")
        bad.pdf_path = bad_path                 # 検証をすり抜けた破損PDFを模す
        bad.pdf_size = bad_path.stat().st_size
        good2 = make_doc((1, 2), make_pdf(self.tmp / "g3.pdf"), "g3.pdf")

        result = pdfmerge.merge_and_split([good1, bad, good2], self.out, self.chunks,
                                          self._Log())

        self.assertEqual(len(result.files), 1)
        self.assertEqual(good1.status, Status.MERGED)
        self.assertEqual(good2.status, Status.MERGED)
        self.assertEqual(bad.status, Status.FAILED_MERGE)


# --- zip -------------------------------------------------------------------

class TestArchive(TempDirCase):
    def test_cp932_member_name_decoded(self):
        # Windowsのエクスプローラが作るzipはCP932のバイト列をUTF-8フラグなしで
        # 格納する。zipfileはそれをCP437として復号するため文字化けした状態で
        # info.filename に入る。その状態を直接組み立てて検証する。
        info = zipfile.ZipInfo()
        info.filename = "資料.pdf".encode("cp932").decode("cp437")
        info.flag_bits = 0
        self.assertEqual(archive._member_name(info), "資料.pdf")

    def test_utf8_member_name_kept(self):
        info = zipfile.ZipInfo("報告書.pdf")
        info.flag_bits = 0x800
        self.assertEqual(archive._member_name(info), "報告書.pdf")

    def test_undecodable_name_falls_back(self):
        info = zipfile.ZipInfo("plain.pdf")
        info.flag_bits = 0
        self.assertEqual(archive._member_name(info), "plain.pdf")

    def test_zip_slip_rejected(self):
        root = self.tmp / "work"
        root.mkdir()
        self.assertFalse(archive._is_safe(root / ".." / ".." / "evil.pdf", root))
        self.assertTrue(archive._is_safe(root / "ok.pdf", root))


# --- 状態と後片付けの判定 --------------------------------------------------

class TestStatusRules(unittest.TestCase):
    def test_collected_statuses_are_not_kept(self):
        for status in (Status.MERGED, Status.SKIPPED_DUP_HASH):
            doc = Document(order=(1,), source_page="", status=status)
            self.assertTrue(doc.is_collected)
            self.assertFalse(doc.needs_keeping)

    def test_failures_are_kept(self):
        for status in (Status.FAILED_DOWNLOAD, Status.FAILED_EXTRACT,
                       Status.FAILED_CONVERT, Status.FAILED_MERGE):
            doc = Document(order=(1,), source_page="", status=status)
            self.assertTrue(doc.needs_keeping)
            self.assertFalse(doc.is_collected)

    def test_descendant_detection(self):
        parent = Document(order=(3, 7), source_page="")
        child = Document(order=(3, 7, 1), source_page="")
        sibling = Document(order=(3, 8), source_page="")
        self.assertTrue(child.is_descendant_of(parent))
        self.assertFalse(sibling.is_descendant_of(parent))
        self.assertFalse(parent.is_descendant_of(parent))

    def test_order_sorting_places_zip_members_after_parent(self):
        orders = [(3, 8), (3, 7), (3, 7, 2), (3, 7, 1)]
        self.assertEqual(sorted(orders), [(3, 7), (3, 7, 1), (3, 7, 2), (3, 8)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
