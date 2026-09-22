"""Brief pipeline orchestration: intake → plan → search → fetch → extract → verify → write → render."""

from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .cache import DocumentCache
from .config import Config
from .extract import extract_evidence
from .fetch import Fetcher, is_blocked
from .identity import confirmed_anchors, format_card, resolve_identity
from .llm import LLMProvider
from .models import Brief, BriefSection, Document, Evidence, IdentityCard, RunReport


class IdentityUnresolved(Exception):
    """Raised when the subject cannot be separated from namesakes with the given anchors."""
from .plan import make_plan
from .render import write_outputs
from .search import SearchProvider
from .sources.edgar import collect_def14a, collect_edgar
from .sources.institution import collect_institution_urls
from .sources.propublica import collect_propublica
from .textnorm import normalize
from .trust import apply_identity, check_entailment, corroborate_and_conflict, mark_stale, source_tier
from .verify import is_out_of_scope, is_wealth_estimate, looks_like_instruction, quote_in_source, quote_references_subject, specifics_in_quote
from .write import write_brief


def make_run_id(subject: str) -> str:
    import unicodedata

    ascii_name = unicodedata.normalize("NFKD", subject).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:40] or "subject"
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{slug}"


def mentions_subject(text: str, subject: str) -> bool:
    """M1 sanity filter: the document must mention the subject's full name (or surname plus a given name)."""
    t = normalize(text)
    parts = [p for p in normalize(subject).split() if len(p) > 1]
    if not parts:
        return False
    if " ".join(parts) in t:
        return True
    return parts[-1] in t and parts[0] in t


def subject_name_variants(subject: str) -> list[str]:
    parts = subject.replace(",", " ").split()
    out = [subject]
    if len(parts) >= 2:
        out += [parts[0], parts[-1], f"{parts[0]} {parts[-1]}"]
    return out


def verify_evidence(items: list[Evidence], docs_by_key: dict[str, Document], config: Config, subject: str = "", name_variants: list[str] | None = None) -> None:
    thr = int(config.get("verify", "quote_partial_ratio_min", default=92))
    exempt = (subject_name_variants(subject) + list(name_variants or [])) if subject else []
    for e in items:
        w = is_wealth_estimate(e.claim, e.supporting_quote)
        if not w.passed:
            e.status, e.drop_reason = "dropped", w.reason
            continue
        o = is_out_of_scope(e.claim, e.supporting_quote)
        if not o.passed:
            e.status, e.drop_reason = "dropped", o.reason
            continue
        doc = docs_by_key.get(e.cache_key)
        a = quote_in_source(e.supporting_quote, doc.text if doc else "", partial_ratio_min=thr)
        e.checks.quote = a.passed
        if not a.passed:
            e.status, e.drop_reason = "dropped", a.reason
            continue
        span = (a.detail["start"], a.detail["end"]) if "start" in a.detail else None
        g = looks_like_instruction(e.supporting_quote, doc.text if doc else "", span)
        if not g.passed:
            e.status, e.drop_reason = "dropped", g.reason
            continue
        b = specifics_in_quote(e.claim, e.supporting_quote, exempt)
        e.checks.specifics = b.passed
        if not b.passed:
            e.status, e.drop_reason = "dropped", b.reason
            continue
        if subject and not e.claim_type.startswith("foundation") and e.publisher not in ("sec.gov", "projects.propublica.org"):
            r = quote_references_subject(e.supporting_quote, subject, exempt)
            if not r.passed:
                e.status, e.drop_reason = "dropped", r.reason
                continue
        e.status = "verified"


async def run_brief(
    *, subject: str, anchors: dict[str, str], institution: str, config: Config, llm: LLMProvider, search: SearchProvider,
    run_dir: Path, transport: httpx.AsyncBaseTransport | None = None, log=print, confirm=None, yes: bool = True,
) -> tuple[Brief, Path]:
    t0 = time.monotonic()
    started = datetime.now(timezone.utc)
    run_id = run_dir.name
    report = RunReport(run_id=run_id, subject=subject, institution=institution, anchors=anchors, started_at=started)
    budgets = config.get("budgets", default={})
    max_docs = int(budgets.get("max_documents", 50))
    wall = float(budgets.get("wall_clock_seconds", 360))

    def time_left() -> float:
        return wall - (time.monotonic() - t0)

    cache = DocumentCache(config.cache_dir)
    fetcher = Fetcher(config, cache, transport=transport)

    # 0. identity (hard rule 4): resolve, show the card, confirm, stop if namesakes cannot be separated
    try:
        card: IdentityCard = await resolve_identity(llm=llm, search=search, fetcher=fetcher, subject=subject, anchors=anchors, institution=institution, log=log)
    except Exception as e:
        await fetcher.aclose()
        raise
    log(format_card(card, anchors))
    if not card.can_separate:
        await fetcher.aclose()
        raise IdentityUnresolved("The subject could not be separated from a namesake with the given anchors. Add another anchor (employer=, city=, school=, company=, spouse=) and run again.")
    if not yes and confirm is not None and not confirm(card):
        await fetcher.aclose()
        raise IdentityUnresolved("Identity not confirmed by the user.")
    id_anchors = confirmed_anchors(card, anchors)
    report.counts["identity_anchors"] = len(id_anchors)

    # 1. plan
    plan = make_plan(llm, config, subject, anchors, institution)
    report.counts["queries_planned"] = len(plan.queries)
    log(f"[plan] {len(plan.queries)} queries")

    # 2. search
    per_query = int(config.get("search", "results_per_query", default=6))
    urls: dict[str, str] = {}  # url -> section hint
    for q in plan.queries:
        if time_left() < 60:
            report.notes.append("search stopped early: wall clock budget")
            break
        try:
            results = await search.search(q.query, max_results=per_query, topic=q.topic)
        except Exception as e:
            report.notes.append(f"search error for {q.query!r}: {type(e).__name__}")
            continue
        report.searches_run += 1
        for r in results:
            if r.url not in urls and not is_blocked(r.url, config.blocklist):
                urls[r.url] = q.section
    # 2b. the institution's own domain (M3)
    inst_results, inst_domain = await collect_institution_urls(search, config, subject, institution)
    for r in inst_results:
        urls.setdefault(r.url, "institution")
    report.counts["institution_domain_urls"] = len(inst_results)
    report.counts["urls_found"] = len(urls)
    log(f"[search] {report.searches_run} searches, {len(urls)} unique urls ({len(inst_results)} from {inst_domain or 'no institution domain configured'})")

    # 3. fetch
    docs = await fetcher.fetch_many(list(urls)[:max_docs])
    report.counts.update({f"fetch_{k}": v for k, v in fetcher.stats.items()})
    good = [d for d in docs if d.ok]
    on_subject = [d for d in good if mentions_subject(d.text, subject)]
    report.counts["documents_fetched_ok"] = len(good)
    report.counts["documents_mentioning_subject"] = len(on_subject)
    log(f"[fetch] {len(good)} readable, {len(on_subject)} mention the subject")

    # 3b. structured sources (M3): SEC EDGAR insider filings + proxy statements, ProPublica foundations
    preverified: dict[str, str] = {}
    card_orgs = [f.fact for f in card.anchor_facts]
    try:
        edgar_docs, edgar_ev, edgar_notes, targets = await collect_edgar(fetcher, subject=subject, anchors=id_anchors, card_orgs=card_orgs, log=log)
        proxy_docs, proxy_notes = await collect_def14a(fetcher, targets=targets, subject=subject, log=log) if targets else ([], [])
        report.notes.extend(edgar_notes + proxy_notes)
    except Exception as e:
        edgar_docs, edgar_ev, proxy_docs = [], [], []
        report.notes.append(f"SEC EDGAR collection error: {type(e).__name__}: {e}")
    for d in edgar_docs:
        preverified[d.cache_key] = "sec_reporting_owner_record_for_anchored_issuer"
    docs.extend(edgar_docs + proxy_docs)
    on_subject.extend(d for d in proxy_docs if mentions_subject(d.text, subject))
    report.counts["sec_insider_filings"] = len(edgar_docs)
    report.counts["sec_proxy_statements"] = len(proxy_docs)

    # 4. extract: a few documents at a time, bounded by the wall clock
    evidence: list[Evidence] = list(edgar_ev)
    workers = int(config.get("budgets", "extract_concurrency", default=4))
    done_docs = 0

    def _one(i: int, d: Document) -> list[Evidence]:
        return extract_evidence(llm, config, d, subject, anchors, institution, id_prefix=f"e{i}-")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        queue = list(enumerate(on_subject, 1))
        while queue or pending:
            while queue and len(pending) < workers and time_left() > 100:
                i, d = queue.pop(0)
                pending[pool.submit(_one, i, d)] = d
            if not pending:
                break
            fut = next(iter(wait(list(pending), return_when=FIRST_COMPLETED)[0]))
            d = pending.pop(fut)
            try:
                evidence.extend(fut.result())
                done_docs += 1
            except Exception as e:
                report.notes.append(f"extract error on {d.final_url}: {type(e).__name__}: {e}")
        if queue:
            report.notes.append(f"extraction stopped after {done_docs} of {len(on_subject)} documents: wall clock budget")
    # 4b. ProPublica foundations, matched by name against the identity card and the claims seen so far
    try:
        mentioned = [f.fact for f in card.anchor_facts] + [e.claim for e in evidence]
        pp_docs, pp_ev, pp_notes = await collect_propublica(fetcher, subject=subject, spouse=card.spouse, card_facts=[f.fact for f in card.anchor_facts], mentioned=mentioned, log=log)
        report.notes.extend(pp_notes)
    except Exception as e:
        pp_docs, pp_ev = [], []
        report.notes.append(f"ProPublica collection error: {type(e).__name__}: {e}")
    for d in pp_docs:
        preverified[d.cache_key] = "foundation_name_matches_subject"
    docs.extend(pp_docs)
    evidence.extend(pp_ev)
    report.counts["propublica_foundations"] = len(pp_docs)
    report.counts["claims_extracted"] = len(evidence)
    log(f"[extract] {len(evidence)} claims")

    # 5. verify: (a) quote in source, (b) specifics in quote — code only
    docs_by_key = {d.cache_key: d for d in docs}
    verify_evidence(evidence, docs_by_key, config, subject, card.name_variants)
    report.counts["passed_checks_ab"] = sum(1 for e in evidence if e.status == "verified")
    # (d) identity: exclude documents without a confirmed anchor
    excluded_docs = apply_identity(evidence, docs_by_key, id_anchors, config, preverified)
    report.counts["documents_possibly_different_person"] = len(excluded_docs)
    # (c) entailment: cheap model check, only "supports" passes
    check_entailment(llm, evidence)
    # (e) tiers, (f) corroboration and conflict, (g) staleness
    subject_orgs = [v for k, v in id_anchors.items() if k in ("employer", "company")] + [f.fact for f in card.anchor_facts if "foundation" in f.fact.lower()]
    for e in evidence:
        e.source_tier = source_tier(e.source_url, config, institution=institution, subject_orgs=subject_orgs)
    corroborate_and_conflict(evidence, subject, id_anchors)
    report.counts["claims_stale_rewritten"] = mark_stale(evidence, config)
    n_ver = sum(1 for e in evidence if e.status == "verified")
    report.counts["claims_verified"] = n_ver
    report.counts["claims_dropped"] = sum(1 for e in evidence if e.status == "dropped")
    report.counts["claims_flagged_identity"] = sum(1 for e in evidence if e.status == "flagged")
    report.counts["claims_corroborated"] = sum(1 for e in evidence if e.corroborated_by and e.status == "verified")
    conflict_pairs = sorted({tuple(sorted((e.id, o))) for e in evidence if e.status == "verified" for o in e.conflicts_with})
    report.counts["conflicts"] = len(conflict_pairs)
    log(f"[verify] {n_ver} verified, {report.counts['claims_dropped']} dropped, {report.counts['claims_flagged_identity']} flagged as possibly a different person, {len(conflict_pairs)} conflicts")

    # 6. write
    if n_ver:
        draft, wstats = write_brief(llm, config, subject, institution, evidence)
        report.counts["sentences_kept"] = wstats["sentences_kept"]
        report.counts["sentences_deleted_no_citation"] = wstats["sentences_deleted"]
        report.counts["writer_attempts"] = wstats["attempts"]
        log(f"[write] {wstats['sentences_kept']} sentences kept, {wstats['sentences_deleted']} deleted for missing citations, {wstats['attempts']} attempt(s)")
    else:
        from .models import BriefDraft

        draft = BriefDraft(sections=[BriefSection(key=s["key"], sentences=[]) for s in config.get("brief_sections", default=[])], talking_points=[])
        report.notes.append("no verified evidence; brief is empty")

    gaps = []
    reasons: dict[str, int] = {}
    for e in evidence:
        if e.status == "dropped" and e.drop_reason:
            reasons[e.drop_reason.split(":")[0]] = reasons.get(e.drop_reason.split(":")[0], 0) + 1
    for r, n in sorted(reasons.items(), key=lambda t: -t[1]):
        gaps.append(f"{n} claim(s) dropped: {r.replace('_', ' ')}.")
    if report.counts.get("sentences_deleted_no_citation"):
        gaps.append(f"{report.counts['sentences_deleted_no_citation']} drafted sentence(s) were removed because they cited no verified evidence.")
    for note in report.notes:
        gaps.append(note)

    # 7. report + render
    await fetcher.aclose()
    report.usage = llm.usage()
    report.cost_usd = round(sum(u.cost_usd for u in report.usage), 4)
    report.finished_at = datetime.now(timezone.utc)
    report.wall_seconds = round(time.monotonic() - t0, 1)
    report.counts["sources_cited"] = len({e.source_url for e in evidence if e.status == "verified"})
    brief = Brief(
        subject=subject, institution=institution, anchors=anchors, generated_at=report.finished_at, sections=draft.sections,
        talking_points=draft.talking_points, evidence=evidence, gaps=gaps, report=report, identity=card,
        possibly_different_person=[{"title": d.title or d.final_url, "url": d.final_url, "identity_score": next((e.identity_score for e in evidence if e.cache_key == d.cache_key), 0.0)} for d in excluded_docs],
        conflicts=[list(p) for p in conflict_pairs],
    )
    n_flag = report.counts.get("claims_flagged_identity", 0)
    summary_line = (
        f"{len(evidence)} claims extracted, {n_ver} verified, {report.counts['claims_dropped']} dropped"
        + (f", {n_flag} set aside as possibly a different person" if n_flag else "") + ", "
        f"{report.counts['sources_cited']} sources, {report.wall_seconds:.0f}s run time, estimated cost ${report.cost_usd:.2f}."
    )
    out_dir = config.briefs_dir / run_id
    html_path = write_outputs(brief, config, out_dir, summary_line)
    log(f"[done] {summary_line}")
    return brief, html_path
