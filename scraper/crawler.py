"""クロール（設計6.2）。

幅優先探索でページを巡回し、資料リンクを Document として登録する。
ページの発見順が結合順序に直結するため、探索順序を決定的に保つ。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from . import config, urlutils
from .fetcher import Fetcher
from .models import Document, Status
from .report import Logger


@dataclass
class CrawlResult:
    documents: list[Document] = field(default_factory=list)
    pages_crawled: int = 0
    pages_failed: int = 0
    robots_skipped: int = 0
    external_docs: int = 0
    interrupted: bool = False


def _extract_links(soup: BeautifulSoup) -> tuple[list[str], list[str]]:
    """(資料・ページ候補となる<a href>, ページのみの候補) を返す。

    資料の判定対象は要件4.3により <a href> のみ。<img src> は走査しない
    （サイトのロゴ・アイコン等を収集してしまうため）。
    frame / iframe は「ページ」としてのみ辿る。フレーム構成のサイトで
    巡回が一切進まなくなるのを防ぐためで、画像収集の制限には影響しない。
    """
    anchors = [a.get("href") for a in soup.find_all("a") if a.get("href")]
    frames = [f.get("src") for f in soup.find_all(["frame", "iframe"]) if f.get("src")]
    return anchors, frames


def _decode_base(soup: BeautifulSoup, fallback: str) -> str:
    tag = soup.find("base", href=True)
    if tag:
        resolved = urlutils.normalize(tag["href"], fallback)
        if resolved:
            return resolved
    return fallback


def crawl(fetcher: Fetcher, base_url: str, log: Logger, progress) -> CrawlResult:
    """base_url 配下を巡回する。progress(pages, docs, queued) が進捗通知。"""
    netloc = urlsplit(base_url).netloc
    prefix = urlutils.scope_prefix(base_url)
    result = CrawlResult()

    queue: deque[tuple[str, tuple[int, ...]]] = deque([(base_url, (0, 0))])
    visited = {urlutils.dedup_key(base_url)}
    documents: dict[tuple[str, str, str], Document] = {}
    page_seq = 0

    def register(url: str, order: tuple[int, ...], ext: str, source: str) -> None:
        key = urlutils.dedup_key(url)
        if key in documents:
            return
        doc = Document(
            order=order,
            source_page=source,
            url=url,
            origin="web",
            original_name=urlutils.url_basename(url),
            ext=ext,
        )
        if not fetcher.allowed(url):
            doc.status = Status.SKIPPED_ROBOTS
            doc.error = "robots.txt により除外"
            result.robots_skipped += 1
            log.write(f"除外(robots) {doc.label}", indent=1)
        else:
            log.write(f"資料発見 {doc.label}", indent=1)
        documents[key] = doc

    try:
        while queue:
            if config.MAX_PAGES is not None and result.pages_crawled >= config.MAX_PAGES:
                log.write(f"ページ数上限 {config.MAX_PAGES} に達したためクロールを終了")
                break

            url, order = queue.popleft()

            if not fetcher.allowed(url):
                result.robots_skipped += 1
                log.write(f"除外(robots) ページ {url}")
                continue

            page = fetcher.get_page(url)
            if not page.ok:
                result.pages_failed += 1
                log.write(f"失敗:ページ取得 {url} — {page.reason}")
                continue

            # Content-Type が非HTML → 資料として登録し直す（設計6.2.2）
            if not page.is_html:
                ext = urlutils.guess_ext(page.content_type, page.disposition, url)
                if ext:
                    register(url, order, ext, url)
                else:
                    log.write(f"対象外の形式 {url} ({page.content_type})")
                continue

            page_seq += 1
            result.pages_crawled += 1
            log.write(f"ページ取得 ({page_seq}) {url}")

            soup = BeautifulSoup(page.content or b"", "lxml", from_encoding=page.charset)
            join_base = _decode_base(soup, page.final_url or url)
            anchors, frames = _extract_links(soup)

            for link_seq, href in enumerate(anchors):
                target = urlutils.normalize(href, join_base)
                if target is None:
                    continue
                ext = urlutils.url_ext(target)

                if ext in config.EXT_TARGET:
                    # 資料はパス範囲の制約を受けない。/docs/ 配下のページから
                    # /files/ 配下のPDFを参照する構成が一般的なため。
                    # ただし別ホストの資料は収集しない。
                    if not urlutils.same_host(target, netloc):
                        result.external_docs += 1
                        log.write(f"除外(外部ホスト) {target}", indent=1)
                        continue
                    register(target, (page_seq, link_seq), ext, url)
                    continue

                if ext in config.EXT_HTML and urlutils.in_scope(target, netloc, prefix):
                    key = urlutils.dedup_key(target)
                    if key not in visited:
                        visited.add(key)
                        queue.append((target, (page_seq, link_seq)))

            for frame_seq, src in enumerate(frames):
                target = urlutils.normalize(src, join_base)
                if target is None or not urlutils.in_scope(target, netloc, prefix):
                    continue
                key = urlutils.dedup_key(target)
                if key not in visited:
                    visited.add(key)
                    queue.append((target, (page_seq, 10_000 + frame_seq)))

            progress(result.pages_crawled, len(documents), len(queue))

    except KeyboardInterrupt:
        result.interrupted = True
        log.write("クロールが中断されました（Ctrl+C）")

    result.documents = sorted(documents.values(), key=lambda d: d.order)
    return result
