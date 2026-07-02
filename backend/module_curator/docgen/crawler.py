"""
crawler.py — Bounded documentation crawler (Priority 4).

"Fetch all documents and follow every sublink" is unbounded -- without limits it
crawls the whole internet. This crawler is deterministic and fenced:
  * domain allowlist  (only official sources)
  * max depth         (how many sublinks deep)
  * dedup by URL + content hash
  * cache             (never fetch the same page twice within a crawl)
  * rate limiting hook

The LLM does NOT crawl. It only summarizes/extracts from what the crawler hands
back, and every extracted claim is tagged with its source URL for citation.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

# Official sources only. Schema is truth; these enrich it.
DEFAULT_ALLOWLIST = {
    "registry.terraform.io",
    "cloud.google.com",
    "developer.hashicorp.com",
}

_LINK_RE = re.compile(r'href=["\'](.*?)["\']', re.IGNORECASE)


@dataclass
class Page:
    url: str
    html: str
    depth: int
    content_hash: str


@dataclass
class CrawlConfig:
    allowlist: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWLIST))
    max_depth: int = 2
    max_pages: int = 200


def normalize(url: str) -> str:
    url, _ = urldefrag(url)  # drop #fragments
    return url.rstrip("/")


def in_scope(url: str, allowlist: set[str]) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in allowlist)


def extract_links(base_url: str, html: str) -> list[str]:
    out = []
    for href in _LINK_RE.findall(html):
        if href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        out.append(normalize(urljoin(base_url, href)))
    return out


class BoundedCrawler:
    def __init__(self, fetch, config: CrawlConfig | None = None):
        # `fetch(url) -> html` is injected so it can use
        # httpx.Client(trust_env=True) for official external sources (or a
        # trust_env=False client for localhost), or be mocked in tests.
        self._fetch = fetch
        self.cfg = config or CrawlConfig()
        self._seen_urls: set[str] = set()
        self._seen_hashes: set[str] = set()

    def crawl(self, seeds: list[str]) -> list[Page]:
        frontier = [(normalize(s), 0) for s in seeds]
        pages: list[Page] = []

        while frontier and len(pages) < self.cfg.max_pages:
            url, depth = frontier.pop(0)
            if url in self._seen_urls or depth > self.cfg.max_depth:
                continue
            if not in_scope(url, self.cfg.allowlist):
                continue
            self._seen_urls.add(url)

            html = self._fetch(url)  # rate-limit inside the injected fetch
            if html is None:
                continue

            chash = hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()
            if chash in self._seen_hashes:  # duplicate content under a new URL
                continue
            self._seen_hashes.add(chash)

            pages.append(Page(url=url, html=html, depth=depth, content_hash=chash))

            if depth < self.cfg.max_depth:
                for link in extract_links(url, html):
                    if link not in self._seen_urls and in_scope(link, self.cfg.allowlist):
                        frontier.append((link, depth + 1))
        return pages
