"""DiscussCabinet専用クローラのHTML解析テスト。

フィクスチャは実際のサイトから取得したマークアップを最小構成に写したもの。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import discusscabinet as dc

CABINET_PAGE = """
<html><body>
<form id="command" action="list" method="post">
<input type="hidden" name="top_tab" value="list"/>
<input type="hidden" name="cabinet_id" value=""/>
<input type="hidden" name="folder_id" value=""/>
<input type="hidden" name="actions" value=""/>
<input type="hidden" name="start" value="1"/>
<input type="hidden" name="docid"/>
<button onclick="javascript:doSubmitTop('news');">新着情報</button>
<button onclick="javascript:setCabinetid('1');doSubmit('list');">本会議</button>
<button onclick="javascript:setCabinetid('2');doSubmit('list');">委員会</button>
<button onclick="javascript:setCabinetid('721');doSubmit('list');">マニュアル</button>
</form></body></html>
"""

FOLDER_PAGE = """
<html><body>
<form action="list" method="post">
<input type="hidden" name="cabinet_id" value="1"/>
<input type="hidden" name="folder_id" value="212607"/>
</form>
<button onclick="javascript:setFolderid('212607','up');doSubmit('list');">上のフォルダへ</button>
<button onclick="javascript:setCabinetid(0);doSubmit('list');">キャビネット一覧へ</button>
<button onclick="javascript:setFolderid('212688','down');doSubmit('list');">議案書等</button>
<button onclick="javascript:setFolderid('215037','down');doSubmit('list');">代表質問通告書</button>
</body></html>
"""

DOC_LIST_PAGE = """
<html><body>
<form action="list" method="post"><input type="hidden" name="folder_id" value="214308"/></form>
<p>全文書数:    14 件    表示件数:    1 - 10 </p>
<table>
<tr><th></th><th>添付</th><th><span>件名</span></th><th><span>日付</span></th></tr>
<tr>
  <td><button onclick="javascript:doSubmitWithDocid('doc_view',14603)">詳細</button></td>
  <td><img alt="クリップアイコン" src="images/Kurippu_icon_g.png"/></td>
  <td>01_R080203議事日程（第１号）</td>
  <td>2026/02/03</td>
</tr>
<tr>
  <td><button onclick="javascript:doSubmitWithDocid('doc_view',14604)">詳細</button></td>
  <td></td>
  <td>02_R080203会期予定表</td>
  <td>2026/02/03</td>
</tr>
</table>
<button id="btn_nextpage" onclick="javascript:doPage('next')"></button>
</body></html>
"""

DETAIL_PAGE = """
<html><body>
<form action="file_view" method="post">
<input type="hidden" name="cabinet_id" value="1"/>
<input type="hidden" name="folder_id" value="212769"/>
<input type="hidden" name="docid" value="14580"/>
<input type="hidden" name="fileid" value="0"/>
</form>
<table>
<tr><th>文書ID:</th><td>14580</td></tr>
<tr><th>ファイルID:</th><td>16245</td></tr>
<tr><th>件名:</th><td> 令和８年２月定例会提出議案一覧 </td></tr>
<tr><th>日付:</th><td>2026/01/28</td></tr>
<tr><th>ファイル名:</th><td>
  <a href="#" onClick="setFile('16245');doSubmitWithNewWin('file_view');return false;">
  令和８年２月定例会提出議案一覧.pdf</a></td></tr>
<tr><th>サイズ:</th><td>638KB</td></tr>
</table>
</body></html>
"""


# 日付列を持たないフォルダ（マニュアル配下）。列構成はフォルダごとに異なる。
DOC_LIST_NO_DATE = """
<html><body>
<p>全文書数: 1 件 表示件数: 1 - 1 </p>
<table>
<tr><th>添付</th><th>件名</th><th></th></tr>
<tr>
  <td><img src="images/Kurippu_icon_g.png"/></td>
  <td>DiscussCabinet ユーザマニュアル</td>
  <td><button onclick="javascript:doSubmitWithDocid('doc_view',99001)">詳細</button></td>
</tr>
</table>
</body></html>
"""


class TestMatches(unittest.TestCase):
    def test_host_routing(self):
        self.assertTrue(dc.matches("https://www.discusscabinet.net/saitama/list"))
        self.assertTrue(dc.matches("https://WWW.DISCUSSCABINET.NET/saitama/list"))
        self.assertFalse(dc.matches("https://www.city.saitama.lg.jp/001/index.html"))


class TestParsers(unittest.TestCase):
    def test_form_state(self):
        state = dc.parse_form(CABINET_PAGE)
        self.assertEqual(state["top_tab"], "list")
        self.assertEqual(state["start"], "1")
        self.assertEqual(state["docid"], "")     # value属性なしは空文字にする

    def test_cabinets(self):
        cabinets = dc.parse_cabinets(CABINET_PAGE)
        self.assertEqual([(c.id, c.name) for c in cabinets],
                         [("1", "本会議"), ("2", "委員会"), ("721", "マニュアル")])

    def test_cabinet_zero_excluded(self):
        # setCabinetid(0) は「キャビネット一覧へ」の戻りボタン
        self.assertEqual(dc.parse_cabinets(FOLDER_PAGE), [])

    def test_folders_exclude_up_button(self):
        folders = dc.parse_folders(FOLDER_PAGE, "212607")
        self.assertEqual([(f.id, f.name) for f in folders],
                         [("212688", "議案書等"), ("215037", "代表質問通告書")])

    def test_doc_rows(self):
        rows = dc.parse_doc_rows(DOC_LIST_PAGE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("14603", "01_R080203議事日程（第１号）", "2026/02/03"))
        self.assertEqual(rows[1][0], "14604")

    def test_header_row_not_treated_as_document(self):
        self.assertNotIn("添付", [r[1] for r in dc.parse_doc_rows(DOC_LIST_PAGE)])

    def test_folder_without_date_column(self):
        """日付列を持たない3列構成のフォルダでも件名を取り出せること。"""
        rows = dc.parse_doc_rows(DOC_LIST_NO_DATE)
        self.assertEqual(rows, [("99001", "DiscussCabinet ユーザマニュアル", "")])

    def test_detail_button_text_not_used_as_title(self):
        for _, title, _ in dc.parse_doc_rows(DOC_LIST_PAGE):
            self.assertNotEqual(title, "詳細")

    def test_counts(self):
        self.assertEqual(dc.parse_counts(DOC_LIST_PAGE), (14, 1, 10))

    def test_counts_absent(self):
        self.assertIsNone(dc.parse_counts(FOLDER_PAGE))

    def test_detail(self):
        attachments, meta = dc.parse_detail(DETAIL_PAGE)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].fileid, "16245")
        self.assertTrue(attachments[0].filename.endswith(".pdf"))
        self.assertEqual(meta["ファイルID"], "16245")
        self.assertEqual(meta["サイズ"], "638KB")

    def test_detail_no_attachment(self):
        attachments, _ = dc.parse_detail(FOLDER_PAGE)
        self.assertEqual(attachments, [])


class TestOrdering(unittest.TestCase):
    """order は (キャビネット, 0, フォルダ番号..., 1, 文書番号) の形。"""

    def test_subfolders_sort_before_own_documents(self):
        own_doc = (0, 1, 0)              # このフォルダの1件目の文書
        sub_doc = (0, 0, 0, 1, 0)        # サブフォルダの中の文書
        self.assertLess(sub_doc, own_doc)

    def test_document_order_within_folder(self):
        self.assertLess((0, 1, 0), (0, 1, 1))

    def test_multiple_attachments_stay_adjacent(self):
        first, second = (0, 1, 3, 1), (0, 1, 3, 2)
        next_doc = (0, 1, 4)
        self.assertLess(first, second)
        self.assertLess(second, next_doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
