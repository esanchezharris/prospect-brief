"""M2 trust checks: identity match (d), entailment (c), source tiers (e), corroboration and
conflict (f), staleness (g)."""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urlparse

from rapidfuzz import fuzz

from .config import Config
from .llm import LLMProvider, UNTRUSTED_INPUT_NOTICE
from .models import Document, EntailmentVerdicts, Evidence
from .textnorm import normalize
from .verify import extract_specifics

# --- (d) identity ---------------------------------------------------------------------------

_ANCHOR_WEIGHTS = {"employer": 1.0, "company": 1.0, "school": 0.8, "school1": 0.6, "spouse": 1.0, "city": 0.5, "age": 0.5}


def _anchor_present(value: str, text_norm: str) -> bool:
    v = normalize(value)
    if not v:
        return False
    if v in text_norm:
        return True
    # tolerate "USC" vs "University of Southern California" style variants only via fuzzy partial match
    return len(v) >= 8 and fuzz.partial_ratio(v, text_norm) >= 90


def identity_score(doc: Document, anchors: dict[str, str]) -> tuple[float, list[str]]:
    """Fraction of anchor weight found in the document, plus the reasons."""
    text_norm = normalize(doc.text)
    total = 0.0
    hit = 0.0
    reasons: list[str] = []
    for k, v in anchors.items():
        w = _ANCHOR_WEIGHTS.get(k, 0.5)
        total += w
        if _anchor_present(v, text_norm):
            hit += w
            reasons.append(f"{k}:{v}")
    return (hit / total if total else 0.0), reasons


def apply_identity(evidence: list[Evidence], docs_by_key: dict[str, Document], anchors: dict[str, str], config: Config) -> list[Document]:
    """Exclude documents without a confirmed anchor. Returns the excluded documents."""
    min_score = float(config.get("verify", "identity_score_min", default=0.25))
    excluded: list[Document] = []
    scores: dict[str, tuple[float, list[str]]] = {}
    for e in evidence:
        if e.cache_key not in scores:
            doc = docs_by_key.get(e.cache_key)
            scores[e.cache_key] = identity_score(doc, anchors) if doc else (0.0, [])
            if not scores[e.cache_key][1] or scores[e.cache_key][0] < min_score:
                if doc:
                    excluded.append(doc)
        score, reasons = scores[e.cache_key]
        e.identity_score = round(score, 2)
        e.identity_reasons = reasons
        if e.status == "verified" and (not reasons or score < min_score):
            e.status, e.drop_reason = "flagged", "possibly_different_person"
    return excluded


# --- (e) tiers ------------------------------------------------------------------------------


def _domain(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _slug_tokens(name: str) -> list[str]:
    toks = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) > 3 and t not in {"capital", "group", "holdings", "company", "foundation", "family", "university", "school", "the", "and", "inc", "corp", "llc", "partners"}]
    return toks


def source_tier(url: str, config: Config, *, institution: str = "", subject_orgs: list[str] | None = None) -> int:
    d = _domain(url)
    if any(d == s or d.endswith("." + s) for s in config.get("tiers", "tier1_suffixes", default=[])):
        return 1
    # the subject's own company or foundation site counts as primary
    for org in subject_orgs or []:
        if any(t in d.replace("-", "") for t in _slug_tokens(org)):
            return 1
    if any(d == s or d.endswith("." + s) for s in config.get("tiers", "tier2_domains", default=[])):
        return 2
    return 3


# --- (c) entailment -------------------------------------------------------------------------

ENTAIL_SYSTEM = f"""You judge whether a quoted passage supports a claim. {UNTRUSTED_INPUT_NOTICE}
For each item answer exactly one of: supports (the quote states the claim, including every
number, date and name in it), partially (the quote is related but does not state the whole
claim, or hedges it), no (the quote does not support the claim or contradicts it). Be strict:
a claim that adds a detail the quote does not contain is 'partially', not 'supports'."""


def check_entailment(llm: LLMProvider, evidence: list[Evidence], *, batch: int = 20) -> None:
    items = [e for e in evidence if e.status == "verified"]
    for i in range(0, len(items), batch):
        chunk = items[i: i + batch]
        user = "\n\n".join(f"id: {e.id}\nclaim: {e.claim}\n<document>quote: {e.supporting_quote}</document>" for e in chunk) + "\n\nReturn one verdict per id."
        try:
            out = llm.structured(purpose=f"entail-{i // batch + 1}", role="checker", system=ENTAIL_SYSTEM, user=user, schema=EntailmentVerdicts, max_tokens=2000)
        except Exception as ex:
            for e in chunk:
                e.status, e.drop_reason = "dropped", f"entailment_error:{type(ex).__name__}"
            continue
        verdicts = {v.id: v.verdict for v in out.verdicts}
        for e in chunk:
            v = verdicts.get(e.id, "no")
            e.checks.entailment = v
            if v != "supports":
                e.status, e.drop_reason = "dropped", f"entailment:{v}"


# --- (f) corroboration and conflict -----------------------------------------------------------


def _topic(claim: str) -> str:
    """Claim text with numbers, money and dates removed, for comparing 'the same fact'."""
    s = extract_specifics(claim)
    t = claim
    for kind in ("money", "dates", "numbers"):
        for item in s[kind]:
            t = t.replace(item, " ")
    return normalize(t)


def _figures(claim: str) -> set[str]:
    s = extract_specifics(claim)
    return {normalize(x).replace(",", "") for x in s["money"] + s["dates"] + s["numbers"]}


def _entities(claim: str, subject_names: set[str]) -> set[str]:
    """Non-subject proper-noun runs in a claim, normalized."""
    out = set()
    for run in extract_specifics(claim)["proper_nouns"]:
        n = normalize(run)
        if n and n not in subject_names and not all(w in subject_names for w in n.split()):
            out.add(n)
    return out


def corroborate_and_conflict(evidence: list[Evidence], subject: str = "", *, same_fact: int = 85, related: int = 50) -> None:
    """Corroborated: the same fact (same figures, near-identical wording) from two independent domains.
    Conflict: same section and claim type from two independent domains, about the same named entity,
    with different figures (amounts or dates). Conflicts are surfaced, never resolved."""
    names = {normalize(subject)} | {w for w in normalize(subject).split()}
    live = [e for e in evidence if e.status == "verified"]
    for i, a in enumerate(live):
        ta, fa, ea = _topic(a.claim), _figures(a.claim), _entities(a.claim, names)
        for b in live[i + 1:]:
            if a.section != b.section or _domain(a.source_url) == _domain(b.source_url):
                continue
            sim = fuzz.token_set_ratio(ta, _topic(b.claim))
            fb = _figures(b.claim)
            if fa == fb and sim >= same_fact:
                a.corroborated_by.append(b.id)
                b.corroborated_by.append(a.id)
            elif fa and fb and fa != fb and a.claim_type == b.claim_type and sim >= related and (ea & _entities(b.claim, names)):
                a.conflicts_with.append(b.id)
                b.conflicts_with.append(a.id)


# --- (g) staleness ----------------------------------------------------------------------------

_CURRENT_RE = re.compile(r"\b(serves|is|are|currently|current|sits on|remains|holds|chairs|leads)\b", re.I)
_ROLE_TYPES = {"role", "board_seat", "position", "title", "employment"}


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y", "%B %d, %Y", "%B %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s.strip()[:len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    m = re.search(r"\b(19|20)\d{2}\b", s)
    return date(int(m.group(0)), 1, 1) if m else None


def mark_stale(evidence: list[Evidence], config: Config, today: date | None = None) -> int:
    """Rewrite 'current' role claims from old sources as 'As of <date>, ...'. Returns count."""
    months = int(config.get("verify", "stale_role_months", default=18))
    today = today or date.today()
    n = 0
    for e in evidence:
        if e.status != "verified" or e.claim_type not in _ROLE_TYPES or not _CURRENT_RE.search(e.claim):
            continue
        d = _parse_date(e.published_date)
        if d is None:
            continue
        age_months = (today.year - d.year) * 12 + (today.month - d.month)
        if age_months > months and not e.claim.lower().startswith("as of"):
            e.claim = f"As of {d.isoformat()}, {e.claim}"
            n += 1
    return n
