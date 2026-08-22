"""設定値・定数。マジックナンバーは全てここに集約する。"""

from __future__ import annotations

# --- サイズ ---------------------------------------------------------------
SIZE_LIMIT = 30 * 1024 * 1024   # 30MB（要件4.5）
PACK_MARGIN = 0.93              # ビンパッキングの推定マージン（設計6.7.3）

# --- クロール -------------------------------------------------------------
REQUEST_INTERVAL = 3.0          # 秒。全HTTPリクエストに適用（要件4.2）
MAX_PAGES: int | None = None    # None = 無制限（要件No.3）
MAX_ARCHIVE_DEPTH = 5           # zip再帰展開の上限（要件4.3）
MAX_HTML_BYTES = 20 * 1024 * 1024   # HTMLとして読み込む上限
PROGRESS_NOTICE_EVERY = 1000    # Nページごとに中断方法を案内する

USER_AGENT = (
    "SaitamaDocCollector/0.1 "
    "(+https://github.com/; document collection tool)"
)

# --- HTTP -----------------------------------------------------------------
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
MAX_RETRIES = 3
RETRY_BACKOFF = [3.0, 6.0, 12.0]        # リトライ待機（秒）
RETRY_STATUS = {429, 500, 502, 503, 504}
CHUNK_SIZE = 1024 * 1024

# --- 変換 -----------------------------------------------------------------
SOFFICE_TIMEOUT = 180           # 秒。1ファイルあたりの変換タイムアウト
IMAGE_DEFAULT_DPI = 96

# --- 対象拡張子（要件4.3）------------------------------------------------
EXT_PDF = {".pdf"}
EXT_OFFICE = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
EXT_TEXT = {".txt", ".csv"}
EXT_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}
EXT_ARCHIVE = {".zip"}
EXT_TARGET = EXT_PDF | EXT_OFFICE | EXT_TEXT | EXT_IMAGE | EXT_ARCHIVE

# 拡張子なし("")も「ページかもしれない」として巡回対象に含める
EXT_HTML = {"", ".html", ".htm", ".shtml", ".xhtml", ".php", ".asp", ".aspx", ".jsp", ".cgi"}

# --- URL正規化 ------------------------------------------------------------
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "yclid", "_ga", "mc_cid", "mc_eid",
}

# --- 出力 -----------------------------------------------------------------
OUTPUT_PREFIX = "さいたま市文書"
MERGED_NAME_FMT = "結合資料_{:03d}.pdf"
UNCOLLECTED_DIR = "_未収録ファイル"
LOG_NAME = "log.txt"
MANIFEST_NAME = "manifest.csv"
WORK_PREFIX = "saitama_doc_"
