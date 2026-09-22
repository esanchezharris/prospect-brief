"""Signal watch orchestration: search EFTS → fetch filings → passages → extract → checks → dedupe → rank → render."""

from __future__ import annotations

import csv
import re
import shlex
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..cache import DocumentCache
from ..config import Config
from ..fetch import Fetcher
from ..llm import LLMProvider
from ..models import FilingHit, Signal
from ..render import TEMPLATES
from ..textnorm import normalize
from .efts import EftsClient
from .extract import check_extraction, extract_passages, find_passages, frame_event, resolve_full_name

FORM_RANK = {"S-1": 0, "8-K": 1, "DEF14A": 2, "DEF 14A": 2, "10-K": 3}


def brief_command(name: str, company: str, institution: str) -> str:
    return f"prospect-brief run {shlex.quote(name)} --anchor employer={shlex.quote(company)} --anchor school={shlex.quote(institution)} --institution {shlex.quote(institution)}"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def dedupe_and_rank(signals: list[Signal]) -> list[Signal]:
    merged: dict[str, Signal] = {}
    for s in signals:
        key = normalize(s.person_name) + "|" + normalize(s.company)
        if key in merged:
            m = merged[key]
            for u in s.filing_urls:
                if u not in m.filing_urls:
                    m.filing_urls.append(u)
            # keep the higher-ranked form's framing
            if (FORM_RANK.get(s.form.split("/")[0], 9), -s.confidence) < (FORM_RANK.get(m.form.split("/")[0], 9), -m.confidence):
                s.filing_urls = m.filing_urls
                merged[key] = s
        else:
            merged[key] = s
    out = list(merged.values())
    out.sort(key=lambda s: (FORM_RANK.get(s.form.split("/")[0], 9), -s.confidence, s.filing_date), reverse=False)
    # filing_date descending within the same form and confidence
    out.sort(key=lambda s: (FORM_RANK.get(s.form.split("/")[0], 9), -s.confidence, -int(s.filing_date.replace("-", "") or 0)))
    return out


def render_signals(signals: list[Signal], dropped: list[Signal], *, institution: str, days: int, forms: list[str], summary: dict, out_html: Path, out_csv: Path) -> None:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)
    html = env.get_template("signals.html.j2").render(signals=signals, dropped=dropped, institution=institution, days=days, forms=forms, summary=summary, generated_at=datetime.now(timezone.utc))
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html)
    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "company", "ticker", "role", "event", "filing_date", "form", "affiliation_as_stated", "quote", "confidence", "filing_urls", "brief_command"])
        for s in signals:
            w.writerow([s.person_name, s.company, s.ticker or "", s.role, s.event, s.filing_date, s.form, s.affiliation_as_stated, s.quote, f"{s.confidence:.2f}", " ".join(s.filing_urls), s.command])


async def run_signals(*, institution: str, days: int, forms: list[str], config: Config, llm: LLMProvider, max_filings: int = 200, transport: httpx.AsyncBaseTransport | None = None, log=print) -> tuple[Path, Path]:
    t0 = time.monotonic()
    if len(institution.strip()) < 8 or " " not in institution.strip():
        raise ValueError("institution must be a full name phrase, never a bare acronym like USC")
    end = date.today()
    start = end - timedelta(days=days)
    cache = DocumentCache(config.cache_dir)
    fetcher = Fetcher(config, cache, transport=transport)
    fetcher.client.headers["User-Agent"] = f"prospect-brief {config.contact_email}"
    efts = EftsClient(fetcher.client, cache, limiter=fetcher.limiter)
    summary = {"filings_searched": 0, "filings_fetched": 0, "passages_found": 0, "bios_kept": 0, "noise_dropped": 0}
    try:
        hits: list[FilingHit] = []
        for form in forms:
            found = await efts.search(institution, form, start, end)
            log(f"[efts] {form}: {len(found)} filings")
            hits.extend(found)
        # rank 8-K item 5.02 (officer/director changes) first within 8-Ks
        hits.sort(key=lambda h: (FORM_RANK.get(h.root_form, 9), 0 if "5.02" in h.items else 1, h.file_date), reverse=False)
        summary["filings_searched"] = len(hits)
        hits = hits[:max_filings]
        docs = await fetcher.fetch_many([h.url for h in hits], full_text=True)
    finally:
        await fetcher.aclose()
    summary["filings_fetched"] = sum(1 for d in docs if d.ok)

    kept: list[Signal] = []
    dropped: list[Signal] = []
    for hit, doc in zip(hits, docs):
        if not doc.ok:
            continue
        passages = find_passages(doc.text, institution)
        summary["passages_found"] += len(passages)
        if not passages:
            continue
        try:
            extractions = extract_passages(llm, hit, institution, passages)
        except Exception as e:
            log(f"[extract] error on {hit.adsh}: {type(e).__name__}: {e}")
            continue
        for x in extractions:
            chk = check_extraction(x, doc.text, partial_ratio_min=int(config.get("verify", "quote_partial_ratio_min", default=92)), company=hit.company)
            full_name, _ = resolve_full_name(x.person_name, doc.text) if chk.passed else (x.person_name.strip(), False)
            sig = Signal(
                person_name=full_name, company=hit.company, ticker=hit.ticker, role=x.role.strip(), event=frame_event(hit, x.role),
                filing_date=hit.file_date, form=hit.form, affiliation_as_stated=x.affiliation_as_stated.strip(), quote=x.quote.strip()[:300],
                confidence=float(x.confidence), filing_urls=[hit.url], status="verified" if chk.passed else "dropped", drop_reason=chk.reason,
            )
            if chk.passed:
                sig.command = brief_command(sig.person_name, sig.company, institution)
                kept.append(sig)
            else:
                dropped.append(sig)
    signals = dedupe_and_rank(kept)
    summary["bios_kept"] = len(signals)
    summary["noise_dropped"] = len(dropped)
    summary["seconds"] = round(time.monotonic() - t0, 1)
    summary["cost_usd"] = round(sum(u.cost_usd for u in llm.usage()), 4)
    stamp = end.isoformat()
    out_html = config.signals_dir / f"{slugify(institution)}-{stamp}.html"
    out_csv = config.signals_dir / f"{slugify(institution)}-{stamp}.csv"
    render_signals(signals, dropped, institution=institution, days=days, forms=forms, summary=summary, out_html=out_html, out_csv=out_csv)
    log(
        f"[done] {summary['filings_searched']} filings searched, {summary['passages_found']} passages found, "
        f"{summary['bios_kept']} bios kept, {summary['noise_dropped']} noise dropped, {summary['seconds']}s, estimated cost ${summary['cost_usd']:.2f}"
    )
    return out_html, out_csv
