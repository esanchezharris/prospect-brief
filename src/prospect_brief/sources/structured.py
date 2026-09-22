"""Helpers shared by the structured collectors: a cached JSON GET through the polite fetcher, and a
way to turn a structured record into a Document whose text the normal checks can run against.

The structured collectors build claims in code, not with a model: every claim's quote is a
substring of the rendered record text, so checks (a) and (b) pass by construction and the
footnote links to the official filing. The rendered text is cached like any other document."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from ..cache import cache_key
from ..fetch import Fetcher, domain_of
from ..models import Document, Evidence


async def get_json(fetcher: Fetcher, url: str, *, headers: dict | None = None) -> dict | list | None:
    cached = fetcher.cache.get_json("structured", url)
    if cached is not None:
        return cached
    await fetcher.limiter.wait(domain_of(url))
    try:
        r = await fetcher.client.get(url, headers=headers or {})
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    fetcher.cache.put_json("structured", url, data)
    return data


async def get_text(fetcher: Fetcher, url: str) -> str | None:
    cached = fetcher.cache.get_json("structured", url)
    if cached is not None:
        return cached.get("text")
    await fetcher.limiter.wait(domain_of(url))
    try:
        r = await fetcher.client.get(url)
        if r.status_code != 200:
            return None
    except Exception:
        return None
    fetcher.cache.put_json("structured", url, {"text": r.text})
    return r.text


def record_document(fetcher: Fetcher, *, url: str, title: str, text: str, publisher: str, published_date: str | None) -> Document:
    """Store a rendered structured record as a cached Document."""
    doc = Document(url=url, final_url=url, cache_key=cache_key(url), fetched_at=datetime.now(timezone.utc), status=200, content_type="text/plain; structured",
                   title=title, text=text, publisher=publisher, published_date=published_date)
    fetcher.cache.put(doc)
    return doc


def code_claim(doc: Document, *, id: str, claim: str, section: str, claim_type: str, quote: str) -> Evidence:
    assert quote in doc.text, "structured quotes must be substrings of the rendered record"
    return Evidence(id=id, claim=claim, section=section, claim_type=claim_type, supporting_quote=quote[:300], source_url=doc.final_url, source_title=doc.title,
                    publisher=doc.publisher, published_date=doc.published_date, accessed_at=doc.fetched_at, cache_key=doc.cache_key, status="flagged")
