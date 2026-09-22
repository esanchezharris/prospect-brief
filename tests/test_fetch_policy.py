import pytest

from prospect_brief.cache import DocumentCache
from prospect_brief.fetch import Fetcher, is_blocked
from prospect_brief.testing import fixture_transport


def test_blocklist_matches_subdomains(config):
    assert is_blocked("https://www.linkedin.com/in/someone", config.blocklist)
    assert is_blocked("https://m.facebook.com/x", config.blocklist)
    assert is_blocked("https://blocked.example/profiles/x.html", config.blocklist)
    assert not is_blocked("https://fixture.example/news/x.html", config.blocklist)


async def test_blocked_domain_is_never_requested(config):
    t = fixture_transport()
    f = Fetcher(config, DocumentCache(config.cache_dir), transport=t)
    doc = await f.fetch("https://blocked.example/profiles/dorian-vexley-marsh.html")
    await f.aclose()
    assert doc.error == "blocked_domain" and not doc.ok
    assert not any("blocked.example" in u for u in t.requests)


async def test_robots_disallow_is_honored(config):
    t = fixture_transport()
    f = Fetcher(config, DocumentCache(config.cache_dir), transport=t)
    doc = await f.fetch("https://fixture.example/private/board-notes.html")
    await f.aclose()
    assert doc.error == "robots_disallow"
    assert "https://fixture.example/robots.txt" in t.requests
    assert not any("/private/" in u for u in t.requests)


async def test_fetch_caches_and_declares_user_agent(config):
    t = fixture_transport()
    f = Fetcher(config, DocumentCache(config.cache_dir), transport=t)
    url = "https://fixture.example/news/2025/03/vexley-marsh-gift.html"
    d1 = await f.fetch(url)
    d2 = await f.fetch(url)
    await f.aclose()
    assert d1.ok and "$12 million" in d1.text and d1.title.startswith("Dorian Vexley-Marsh gives")
    assert d2.cache_key == d1.cache_key and f.stats["cache_hits"] == 1
    assert t.requests.count(url) == 1
    assert "prospect-brief" in f.client.headers["User-Agent"]
    assert (config.cache_dir / "docs" / f"{d1.cache_key}.json").exists()


async def test_pdf_is_extracted(config):
    f = Fetcher(config, DocumentCache(config.cache_dir), transport=fixture_transport())
    doc = await f.fetch("https://vexleymarshfoundation.example/annual-report-2024.pdf")
    await f.aclose()
    assert doc.ok and "$48,300,000" in doc.text
