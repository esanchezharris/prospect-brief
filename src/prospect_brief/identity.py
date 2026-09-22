"""Identity resolution: a few searches plus Wikipedia/Wikidata lookups produce an identity card
that the user confirms before any research starts (hard rule 4).

API shapes confirmed live on Sep 21, 2026:
- https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=..&format=json -> query.search[] {title, snippet}
- https://en.wikipedia.org/api/rest_v1/page/summary/<Title> -> {title, description, extract, content_urls.desktop.page}
- https://www.wikidata.org/w/api.php?action=wbsearchentities&search=..&language=en&format=json&type=item -> search[] {id, label, description}
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlparse

from .cache import DocumentCache
from .llm import LLMProvider, UNTRUSTED_INPUT_NOTICE
from .models import IdentityCard, SearchResult
from .search import SearchProvider

SYSTEM = f"""You resolve the identity of one person for a university development office before any
research is done. The most dangerous failure is mixing up two people with the same name.
{UNTRUSTED_INPUT_NOTICE}

From the search results and encyclopedia entries provided, build an identity card for the subject
described by the anchors. Use only what the sources say. List every other person with the same or
a very similar name that appears in the sources and say how they differ. Set can_separate=false
if the anchors do not clearly distinguish the subject from those namesakes. Do not include home
address, contact details, family members outside public philanthropy, health, religion, ethnicity,
immigration status or criminal history."""


async def _get_json(fetcher, url: str) -> dict | None:
    cache: DocumentCache = fetcher.cache
    cached = cache.get_json("lookups", url)
    if cached is not None:
        return cached
    try:
        await fetcher.limiter.wait(urlparse(url).hostname or "")
        r = await fetcher.client.get(url)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    cache.put_json("lookups", url, data)
    return data


async def wiki_lookups(fetcher, name: str) -> dict:
    out: dict = {"wikipedia_search": [], "wikipedia_summary": None, "wikidata": []}
    q = quote(name)
    ws = await _get_json(fetcher, f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json&srlimit=5&srprop=snippet")
    if ws:
        for r in ws.get("query", {}).get("search", []):
            out["wikipedia_search"].append({"title": r.get("title"), "snippet": r.get("snippet", "")})
    first = next((r["title"] for r in out["wikipedia_search"] if r["title"].lower() == name.lower()), None)
    if first:
        summ = await _get_json(fetcher, f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(first.replace(' ', '_'))}")
        if summ:
            out["wikipedia_summary"] = {"title": summ.get("title"), "description": summ.get("description"), "extract": (summ.get("extract") or "")[:1500], "url": (summ.get("content_urls") or {}).get("desktop", {}).get("page")}
    wd = await _get_json(fetcher, f"https://www.wikidata.org/w/api.php?action=wbsearchentities&search={q}&language=en&format=json&limit=7&type=item")
    if wd:
        for r in wd.get("search", []):
            out["wikidata"].append({"id": r.get("id"), "label": r.get("label"), "description": r.get("description")})
    return out


async def resolve_identity(*, llm: LLMProvider, search: SearchProvider, fetcher, subject: str, anchors: dict[str, str], institution: str, log=print) -> IdentityCard:
    queries = [f'"{subject}"', f'"{subject}" {" ".join(anchors.values())}'] + [f'"{subject}" {v}' for v in list(anchors.values())[:3]]
    results: list[SearchResult] = []
    seen: set[str] = set()
    for q in queries[:5]:
        try:
            for r in await search.search(q, max_results=5):
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)
        except Exception as e:
            log(f"[identity] search error: {type(e).__name__}")
    wiki = await wiki_lookups(fetcher, subject)
    user = (
        f"Subject: {subject}\nAnchors given by the user: " + "; ".join(f"{k}={v}" for k, v in anchors.items()) + f"\nInstitution: {institution}\n\n"
        "<document>\nSearch results:\n" + "\n".join(f"- {r.title} | {r.url}\n  {r.snippet[:300]}" for r in results)
        + "\n\nEncyclopedia lookups:\n" + json.dumps(wiki, indent=1)[:6000] + "\n</document>\n\nBuild the identity card."
    )
    return llm.structured(purpose="identity", role="writer", system=SYSTEM, user=user, schema=IdentityCard, max_tokens=3000)


def format_card(card: IdentityCard, anchors: dict[str, str]) -> str:
    lines = [f"Identity card: {card.full_name}"]
    if card.name_variants:
        lines.append(f"  Also seen as: {', '.join(card.name_variants)}")
    if card.current_role:
        lines.append(f"  Current role: {card.current_role}")
    if card.city:
        lines.append(f"  City: {card.city}")
    if card.spouse:
        lines.append(f"  Spouse (named jointly in public philanthropy): {card.spouse}")
    if card.education:
        lines.append(f"  Education: {'; '.join(card.education)}")
    lines.append("  Anchor facts:")
    for f in card.anchor_facts:
        lines.append(f"    - {f.fact}  <{f.source_url}>")
    if card.namesakes:
        lines.append("  Other people with this name:")
        for n in card.namesakes:
            lines.append(f"    - {n.description}" + (f"  <{n.source_url}>" if n.source_url else ""))
    lines.append(f"  Can be separated from namesakes with the given anchors: {'yes' if card.can_separate else 'NO'}")
    lines.append(f"  {card.reasoning}")
    return "\n".join(lines)


def confirmed_anchors(card: IdentityCard, anchors: dict[str, str]) -> dict[str, str]:
    """Anchors used by check (d): the user's anchors plus employer/city/education from the card."""
    out = dict(anchors)
    if card.employer and "employer" not in out:
        out["employer"] = card.employer
    if card.city and "city" not in out:
        out["city"] = card.city
    if card.spouse and "spouse" not in out:
        out["spouse"] = card.spouse
    for i, edu in enumerate(card.education[:2]):
        out.setdefault(f"school{i or ''}", edu)
    return out
