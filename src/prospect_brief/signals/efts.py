"""SEC EDGAR full-text search client. Response shape confirmed live on Sep 21, 2026 (see CLAUDE.md)."""

from __future__ import annotations

import re
from datetime import date

import httpx

from ..cache import DocumentCache
from ..models import FilingHit

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
FORM_PARAM = {"S-1": "S-1", "8-K": "8-K", "DEF14A": "DEF14A", "DEF 14A": "DEF14A", "10-K": "10-K"}
_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6}(?:,\s*[A-Z][A-Z0-9.\-]{0,6})*)\)")
_CIK_RE = re.compile(r"\s*\(CIK\s+\d+\)\s*$")


def parse_display_name(name: str) -> tuple[str, str | None]:
    """'Eloxx Pharmaceuticals, Inc.  (ELOX)  (CIK 0001035354)' -> ('Eloxx Pharmaceuticals, Inc.', 'ELOX')."""
    base = _CIK_RE.sub("", name).strip()
    ticker = None
    m = _TICKER_RE.search(base)
    if m:
        ticker = m.group(1).split(",")[0].strip()
        base = base[: m.start()].strip()
    return " ".join(base.split()), ticker


def hit_from_json(h: dict) -> FilingHit | None:
    src = h.get("_source", {})
    _id = h.get("_id", "")
    if ":" not in _id or not src.get("ciks"):
        return None
    adsh, filename = _id.split(":", 1)
    display = (src.get("display_names") or [""])[0]
    company, ticker = parse_display_name(display)
    root = (src.get("root_forms") or [src.get("form", "")])[0]
    return FilingHit(
        adsh=src.get("adsh", adsh), filename=filename, form=src.get("form", root), root_form=root, file_date=src.get("file_date", ""),
        display_name=display, company=company, ticker=ticker, cik=src["ciks"][0], items=list(src.get("items") or []),
    )


class EftsClient:
    def __init__(self, client: httpx.AsyncClient, cache: DocumentCache, *, limiter=None):
        self.client = client
        self.cache = cache
        self.limiter = limiter
        self.requests = 0

    async def _get(self, params: dict) -> dict:
        key = EFTS_URL + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        cached = self.cache.get_json("efts", key)
        if cached is not None:
            return cached
        if self.limiter:
            await self.limiter.wait("efts.sec.gov")
        r = await self.client.get(EFTS_URL, params=params)
        r.raise_for_status()
        self.requests += 1
        data = r.json()
        self.cache.put_json("efts", key, data)
        return data

    async def search(self, phrase: str, form: str, start: date, end: date, *, max_hits: int = 1000) -> list[FilingHit]:
        """Exact-phrase search for one form family, following pagination."""
        hits: list[FilingHit] = []
        offset = 0
        while True:
            params = {"q": f'"{phrase}"', "forms": FORM_PARAM.get(form, form), "dateRange": "custom", "startdt": start.isoformat(), "enddt": end.isoformat()}
            if offset:
                params["from"] = offset
            data = await self._get(params)
            page = data.get("hits", {}).get("hits", [])
            total = int(data.get("hits", {}).get("total", {}).get("value", 0))
            for h in page:
                fh = hit_from_json(h)
                if fh:
                    hits.append(fh)
            offset += len(page)
            if not page or offset >= total or offset >= max_hits:
                break
        return hits
