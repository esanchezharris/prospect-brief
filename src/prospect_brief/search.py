"""Search providers. Tavily is the default; Brave or Exa would be one more class here."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

import httpx

from .fetch import is_blocked
from .models import SearchResult


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, max_results: int = 6, topic: str = "general", include_domains: list[str] | None = None) -> list[SearchResult]: ...

    async def aclose(self) -> None: ...


class TavilyProvider:
    name = "tavily"
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None = None, *, exclude_domains: list[str] | None = None, user_agent: str = "prospect-brief"):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("TAVILY_API_KEY is not set in .env")
        # Tavily caps exclude_domains at 150; the blocklist is well under that.
        self.exclude_domains = [d for d in (exclude_domains or []) if "." in d and not d.endswith(".example")][:150]
        self.client = httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": user_agent})
        self.credits_used = 0

    async def search(self, query: str, *, max_results: int = 6, topic: str = "general", include_domains: list[str] | None = None) -> list[SearchResult]:
        body = {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "topic": topic,
        }
        if include_domains:
            body["include_domains"] = include_domains
        else:
            body["exclude_domains"] = self.exclude_domains
        r = await self.client.post(self.URL, json=body)
        r.raise_for_status()
        data = r.json()
        self.credits_used += int((data.get("usage") or {}).get("credits", 1))
        out = []
        for item in data.get("results", []):
            url = item.get("url", "")
            if not url or is_blocked(url, self.exclude_domains):
                continue
            out.append(SearchResult(url=url, title=item.get("title", ""), snippet=item.get("content", ""), published_date=item.get("published_date"), query=query))
        return out

    async def aclose(self) -> None:
        await self.client.aclose()


class MockSearchProvider:
    """Serves a local fixture corpus. Each fixture file has a manifest entry with keywords."""

    name = "mock"

    def __init__(self, manifest: list[dict]):
        self.manifest = manifest
        self.queries: list[str] = []

    @classmethod
    def from_dir(cls, corpus_dir: Path) -> "MockSearchProvider":
        import json

        return cls(json.loads((corpus_dir / "manifest.json").read_text()))

    async def search(self, query: str, *, max_results: int = 6, topic: str = "general", include_domains: list[str] | None = None) -> list[SearchResult]:
        self.queries.append(query)
        q = query.lower()
        scored = []
        for entry in self.manifest:
            if include_domains and not any(d in entry["url"] for d in include_domains):
                continue
            score = sum(1 for kw in entry.get("keywords", []) if kw.lower() in q)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda t: -t[0])
        return [SearchResult(url=e["url"], title=e.get("title", ""), snippet=e.get("snippet", ""), query=query) for _, e in scored[:max_results]]

    async def aclose(self) -> None:
        return None
