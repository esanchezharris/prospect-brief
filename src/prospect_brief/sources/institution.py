"""The institution's own domain: gift announcements, board and council listings, alumni magazine,
named spaces. Uses the search provider restricted to the institution's domain."""

from __future__ import annotations

from ..config import Config
from ..models import SearchResult
from ..search import SearchProvider


def institution_domain(config: Config, institution: str) -> str | None:
    m = config.get("institution_domains", default={}) or {}
    for k, v in m.items():
        if k.lower() == institution.lower():
            return v
    return None


async def collect_institution_urls(search: SearchProvider, config: Config, subject: str, institution: str, *, max_queries: int = 4) -> tuple[list[SearchResult], str | None]:
    domain = institution_domain(config, institution)
    if not domain:
        return [], None
    surname = subject.split()[-1]
    queries = [f'"{subject}" gift', f'"{subject}" board OR council OR trustee', f'"{subject}" alumni OR alumnus OR alumna', f'"{surname}" named OR endowed OR center OR scholarship']
    out: list[SearchResult] = []
    seen: set[str] = set()
    for q in queries[:max_queries]:
        try:
            results = await search.search(q, max_results=5, include_domains=[domain])
        except TypeError:
            results = await search.search(f"{q} site:{domain}", max_results=5)
        except Exception:
            continue
        for r in results:
            if domain in r.url and r.url not in seen:
                seen.add(r.url)
                out.append(r)
    return out, domain
