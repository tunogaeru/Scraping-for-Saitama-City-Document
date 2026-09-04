"""HTTP層。レート制御・リトライ・robots.txt判定を一元化する（設計6.3）。

ページ取得と資料ダウンロードで同一インスタンスを共有することで、1.5秒間隔の
レート制御が全リクエストに一元的にかかる。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

from . import config
from .report import Logger


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": config.USER_AGENT,
        "Accept-Language": "ja,en;q=0.8",
    })
    return session


class RateLimiter:
    """全リクエストに一定間隔を強制する。複数のクライアントで共有できる。"""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        remaining = self.interval - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()


def request_with_retry(session: requests.Session, url: str, limiter: RateLimiter,
                       *, method: str = "GET", stream: bool = False,
                       data: dict | None = None,
                       ) -> tuple[requests.Response | None, str | None]:
    """リトライ付きでリクエストする。4xx（429を除く）は再試行しない。"""
    timeout = (config.CONNECT_TIMEOUT, config.READ_TIMEOUT)
    last_error = "不明なエラー"
    for attempt in range(config.MAX_RETRIES + 1):
        if attempt:
            backoff = config.RETRY_BACKOFF[min(attempt - 1, len(config.RETRY_BACKOFF) - 1)]
            time.sleep(backoff)
        limiter.wait()
        try:
            resp = session.request(method, url, stream=stream, data=data,
                                   timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            last_error = f"通信エラー: {exc.__class__.__name__}"
            continue

        if resp.status_code in config.RETRY_STATUS:
            last_error = f"HTTP {resp.status_code}"
            resp.close()
            continue
        if resp.status_code >= 400:
            resp.close()
            return None, f"HTTP {resp.status_code}"
        return resp, None
    return None, last_error


def stream_to_file(resp: requests.Response, dest: Path) -> "DownloadResult":
    """レスポンス本文をチャンク単位で保存し、同時にSHA-256を計算する。"""
    digest = hashlib.sha256()
    size = 0
    try:
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(config.CHUNK_SIZE):
                if not chunk:
                    continue
                fh.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except (requests.RequestException, OSError) as exc:
        dest.unlink(missing_ok=True)
        return DownloadResult(ok=False, reason=f"保存失敗: {exc.__class__.__name__}")
    finally:
        resp.close()

    if size == 0:
        dest.unlink(missing_ok=True)
        return DownloadResult(ok=False, reason="空のファイル")
    return DownloadResult(ok=True, path=dest, sha256=digest.hexdigest(), size=size,
                          content_type=resp.headers.get("Content-Type", ""))


@dataclass
class PageResult:
    ok: bool
    url: str
    final_url: str = ""
    is_html: bool = False
    content: bytes | None = None
    charset: str | None = None
    content_type: str = ""
    disposition: str | None = None
    reason: str | None = None


@dataclass
class DownloadResult:
    ok: bool
    path: Path | None = None
    sha256: str | None = None
    size: int = 0
    content_type: str = ""
    reason: str | None = None


class Fetcher:
    def __init__(self, base_url: str, log: Logger, interval: float | None = None) -> None:
        self.log = log
        # 既定値は呼び出し時に解決する（引数の既定値にすると import 時に固定される）
        self.limiter = RateLimiter(config.REQUEST_INTERVAL if interval is None else interval)
        self.session = new_session()
        self._robots = RobotFileParser()
        self.robots_loaded = False
        self._load_robots(base_url)

    @property
    def interval(self) -> float:
        return self.limiter.interval

    def _wait(self) -> None:
        self.limiter.wait()

    # -- robots.txt ---------------------------------------------------------

    def _load_robots(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        self._robots.set_url(robots_url)
        self._wait()
        try:
            resp = self.session.get(
                robots_url, timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT))
        except requests.RequestException as exc:
            self._robots.allow_all = True
            self.log.write(f"robots.txt 取得失敗（制限なしとして扱う）: {exc.__class__.__name__}")
            return

        if resp.status_code == 200:
            self._robots.parse(resp.text.splitlines())
            self.robots_loaded = True
            delay = self._crawl_delay()
            if delay and delay > self.interval:
                self.interval = delay
                self.log.write(f"robots.txt 取得成功 Crawl-delay={delay}秒 → 間隔 {self.interval}秒")
            else:
                self.log.write(f"robots.txt 取得成功 Crawl-delay=なし → 間隔 {self.interval}秒")
        else:
            # 404等は「制限なし」として扱うのがRFC上の慣行
            self._robots.allow_all = True
            self.log.write(f"robots.txt なし（HTTP {resp.status_code}）→ 制限なしとして扱う")

    def _crawl_delay(self) -> float | None:
        try:
            value = self._robots.crawl_delay(config.USER_AGENT)
        except Exception:
            return None
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def allowed(self, url: str) -> bool:
        try:
            return self._robots.can_fetch(config.USER_AGENT, url)
        except Exception:
            return True

    # -- リクエスト ---------------------------------------------------------

    def _request(self, url: str, stream: bool) -> tuple[requests.Response | None, str | None]:
        return request_with_retry(self.session, url, self.limiter, stream=stream)

    # -- ページ取得 ---------------------------------------------------------

    def get_page(self, url: str) -> PageResult:
        """ページを取得する。

        HTMLでなかった場合は本文を読まずに閉じ、is_html=False で返す。
        呼び出し側（crawler）がそれを資料として登録し直す（設計6.2.2）。
        """
        resp, error = self._request(url, stream=True)
        if resp is None:
            return PageResult(ok=False, url=url, reason=error)

        content_type = resp.headers.get("Content-Type", "")
        disposition = resp.headers.get("Content-Disposition")
        base_type = content_type.split(";", 1)[0].strip().lower()
        is_html = base_type in ("text/html", "application/xhtml+xml", "")

        if not is_html or disposition:
            final = resp.url
            resp.close()
            return PageResult(ok=True, url=url, final_url=final, is_html=False,
                              content_type=content_type, disposition=disposition)

        try:
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(config.CHUNK_SIZE):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total > config.MAX_HTML_BYTES:
                    break
            body = b"".join(chunks)
        except requests.RequestException as exc:
            resp.close()
            return PageResult(ok=False, url=url, reason=f"本文読み込み失敗: {exc.__class__.__name__}")
        finally:
            resp.close()

        charset = None
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip(' "\'')

        return PageResult(ok=True, url=url, final_url=resp.url, is_html=True,
                          content=body, charset=charset, content_type=content_type)

    # -- ダウンロード -------------------------------------------------------

    def download(self, url: str, dest: Path) -> DownloadResult:
        """資料をストリーミング保存する。書き込みと同時にSHA-256を計算する。"""
        resp, error = self._request(url, stream=True)
        if resp is None:
            return DownloadResult(ok=False, reason=error)
        return stream_to_file(resp, dest)

    def close(self) -> None:
        self.session.close()
