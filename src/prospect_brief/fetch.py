"""Polite async fetcher: blocklist, robots.txt, per-domain rate limit, declared UA, disk cache.

Hard rules 1, 6 and 7 live here. The blocklist is checked before any network call.
"""

from __future__ import annotations

import asyncio
import io
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from lxml import html as lxml_html
from pypdf import PdfReader

from .cache import DocumentCache, cache_key
from .config import Config
from .models import Document


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_blocked(url: str, blocklist: list[str]) -> bool:
    host = domain_of(url)
    return any(host == b or host.endswith("." + b) for b in blocklist)


class _RateLimiter:
    """One token bucket per domain."""

    def __init__(self, default_rps: float, per_domain: dict[str, float]):
        self.default_rps = default_rps
        self.per_domain = per_domain
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, domain: str) -> None:
        rps = self.default_rps
        for d, r in self.per_domain.items():
            if domain == d or domain.endswith("." + d):
                rps = r
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            t = max(now, self._next.get(domain, now))
            self._next[domain] = t + 1.0 / rps
            if t > now:
                await asyncio.sleep(t - now)


def html_to_text(raw: bytes | str, *, full: bool = False) -> tuple[str, str]:
    """Return (title, text). trafilatura for articles; lxml full text for filings/tables."""
    if isinstance(raw, bytes):
        raw_str = raw.decode("utf-8", errors="replace")
    else:
        raw_str = raw
    title = ""
    try:
        tree = lxml_html.fromstring(raw_str)
        t = tree.find(".//title")
        title = (t.text or "").strip() if t is not None else ""
    except Exception:
        tree = None
    text = ""
    if not full:
        text = trafilatura.extract(raw_str, include_comments=False, include_tables=True, favor_recall=True) or ""
    if (full or len(text) < 200) and tree is not None:
        for bad in tree.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        text = tree.text_content()
    return title, " ".join(text.split())


def pdf_to_text(raw: bytes) -> tuple[str, str]:
    reader = PdfReader(io.BytesIO(raw))
    title = ""
    try:
        title = (reader.metadata.title or "") if reader.metadata else ""
    except Exception:
        pass
    parts = []
    for page in reader.pages[:60]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return title, " ".join(" ".join(parts).split())


class Fetcher:
    def __init__(self, config: Config, cache: DocumentCache, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.cache = cache
        self.blocklist = config.blocklist
        self.limiter = _RateLimiter(float(config.get("fetch", "default_domain_rps", default=1.0)), config.get("fetch", "domain_rps", default={}) or {})
        self.sem = asyncio.Semaphore(int(config.get("fetch", "concurrency", default=5)))
        self.timeout = float(config.get("fetch", "timeout_seconds", default=25))
        self.client = httpx.AsyncClient(
            transport=transport,
            headers={"User-Agent": config.user_agent, "Accept": "text/html,application/pdf,application/json;q=0.9,*/*;q=0.8"},
            follow_redirects=True,
            timeout=self.timeout,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self.stats = {"cache_hits": 0, "fetched": 0, "blocked": 0, "robots_denied": 0, "errors": 0}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _robots_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                await self.limiter.wait(domain_of(url))
                r = await self.client.get(base + "/robots.txt")
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots file: allowed
            except Exception:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        if rp is None:
            return True
        return rp.can_fetch(self.config.user_agent, url) and rp.can_fetch("*", url)

    async def fetch(self, url: str, *, full_text: bool = False) -> Document:
        now = datetime.now(timezone.utc)
        key = cache_key(url)
        if is_blocked(url, self.blocklist):
            self.stats["blocked"] += 1
            return Document(url=url, final_url=url, cache_key=key, fetched_at=now, status=0, error="blocked_domain")
        cached = self.cache.get(url)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached
        async with self.sem:
            if not await self._robots_allowed(url):
                self.stats["robots_denied"] += 1
                return Document(url=url, final_url=url, cache_key=key, fetched_at=now, status=0, error="robots_disallow")
            await self.limiter.wait(domain_of(url))
            try:
                r = await self.client.get(url)
            except Exception as e:  # network errors are recorded, not raised
                self.stats["errors"] += 1
                return Document(url=url, final_url=url, cache_key=key, fetched_at=now, status=0, error=f"fetch_error:{type(e).__name__}")
        ctype = r.headers.get("content-type", "").lower()
        raw = r.content
        title, text = "", ""
        if r.status_code == 200:
            try:
                if "pdf" in ctype or url.lower().endswith(".pdf"):
                    title, text = pdf_to_text(raw)
                    ctype = ctype or "application/pdf"
                else:
                    title, text = html_to_text(raw, full=full_text)
            except Exception as e:
                self.stats["errors"] += 1
                return Document(url=url, final_url=str(r.url), cache_key=key, fetched_at=now, status=r.status_code, content_type=ctype, error=f"extract_error:{type(e).__name__}")
        doc = Document(
            url=url, final_url=str(r.url), cache_key=key, fetched_at=now, status=r.status_code,
            content_type=ctype, title=title, text=text, publisher=domain_of(str(r.url)),
            error=None if r.status_code == 200 else f"http_{r.status_code}",
        )
        self.stats["fetched"] += 1
        self.cache.put(doc, raw if r.status_code == 200 else None)
        return doc

    async def fetch_many(self, urls: list[str], *, full_text: bool = False) -> list[Document]:
        return list(await asyncio.gather(*(self.fetch(u, full_text=full_text) for u in urls)))
