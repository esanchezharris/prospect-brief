"""Passage extraction for the signal watch, and the code checks that gate it."""

from __future__ import annotations

import re

from ..llm import LLMProvider, UNTRUSTED_INPUT_NOTICE
from ..models import FilingHit, PassageExtraction, PassageExtractions
from ..textnorm import normalize
from ..verify import CheckResult, quote_in_source

SYSTEM = f"""You read passages from SEC filings and identify people whose biography states that they
studied or worked at a named institution. {UNTRUSTED_INPUT_NOTICE}

For each numbered passage, return one extraction. Set is_bio=true ONLY when the passage is a
biography of a specific named person and states that person's own affiliation with the
institution (degree, attended, faculty, employed, trustee). Set is_bio=false for anything else:
licensing or research agreements, clinical trial sites, addresses, sponsorships, donations by the
company, lists of institutions, or mentions of the institution unrelated to a person's biography.
The quote must be copied verbatim from the passage, at most 300 characters, and must contain the
affiliation statement. If a passage mentions several people, pick the one whose affiliation is
with the institution. confidence reflects how clearly the passage is that person's biography."""


def find_passages(text: str, phrase: str, *, window: int = 600, max_passages: int = 25) -> list[str]:
    """Every passage within `window` characters of the exact phrase, merged when overlapping."""
    low = text.lower()
    p = phrase.lower()
    spans: list[list[int]] = []
    i = low.find(p)
    while i >= 0 and len(spans) < max_passages * 4:
        a, b = max(0, i - window), min(len(text), i + len(p) + window)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = b
        else:
            spans.append([a, b])
        i = low.find(p, i + len(p))
    return [text[a:b] for a, b in spans[:max_passages]]


def extract_passages(llm: LLMProvider, hit: FilingHit, institution: str, passages: list[str]) -> list[PassageExtraction]:
    numbered = "\n\n".join(f"<document index=\"{i}\">\n{p}\n</document>" for i, p in enumerate(passages))
    user = (
        f"Institution: {institution}\nFiling: {hit.form} by {hit.company}, filed {hit.file_date}\n\n{numbered}\n\n"
        f"Return one extraction per passage index (0 to {len(passages) - 1})."
    )
    out = llm.structured(purpose=f"signals-{hit.adsh}", role="writer", system=SYSTEM, user=user, schema=PassageExtractions, max_tokens=6000)
    return out.extractions


def name_near_quote(person_name: str, quote: str, doc_text: str, span: tuple[int, int] | None, *, window: int = 300) -> CheckResult:
    """The person's name must appear in the quote or within `window` normalized characters of it."""
    name = normalize(person_name)
    parts = [p for p in name.split() if len(p) > 1]
    if not parts:
        return CheckResult(False, "no_person_name")
    surname = parts[-1]
    q = normalize(quote)
    if name in q or (surname in q and len(surname) > 2):
        return CheckResult(True)
    if span:
        s = normalize(doc_text)
        ctx = s[max(0, span[0] - window): span[1] + window]
        if name in ctx or surname in ctx:
            return CheckResult(True)
    return CheckResult(False, "name_not_near_quote")


_ORG_WORDS = {"inc", "inc.", "corp", "corp.", "corporation", "llc", "ltd", "ltd.", "plc", "co", "co.", "company", "holdings", "group", "partners",
              "capital", "research", "technologies", "technology", "systems", "sciences", "therapeutics", "pharmaceuticals", "biosciences", "labs",
              "laboratories", "university", "college", "institute", "foundation", "fund", "trust", "bank", "ventures", "acquisition", "energy", "networks"}


def looks_like_organization(person_name: str, company: str = "") -> bool:
    """'Lam Research' is not a person. Reject names that carry a corporate word or sit inside the filer's name."""
    toks = [t.lower().strip(",") for t in person_name.split()]
    if not toks or any(t in _ORG_WORDS for t in toks):
        return True
    n = normalize(person_name)
    c = normalize(company)
    return bool(n) and bool(c) and (n in c or c in n)


def check_extraction(x: PassageExtraction, doc_text: str, *, partial_ratio_min: int = 92, company: str = "") -> CheckResult:
    if not x.is_bio:
        return CheckResult(False, "not_bio")
    if not x.person_name.strip():
        return CheckResult(False, "no_person_name")
    if looks_like_organization(x.person_name, company):
        return CheckResult(False, "name_looks_like_organization")
    a = quote_in_source(x.quote, doc_text, partial_ratio_min=partial_ratio_min)
    if not a.passed:
        return a
    span = (a.detail["start"], a.detail["end"]) if "start" in a.detail else None
    return name_near_quote(x.person_name, x.quote, doc_text, span)


_ROLE_CLEAN = re.compile(r"\s+")


def frame_event(hit: FilingHit, role: str) -> str:
    role = _ROLE_CLEAN.sub(" ", role).strip() or "an officer or director"
    if hit.root_form.startswith("S-1"):
        return f"IPO registration {'amendment ' if hit.form.endswith('/A') else ''}filed {hit.file_date}"
    if hit.root_form.startswith("8-K"):
        return f"appointed {role} at {hit.company}, {hit.file_date}"
    if "14A" in hit.root_form:
        return f"listed as {role} in {hit.company}'s proxy statement, {hit.file_date}"
    return f"named as {role} in {hit.form} filed {hit.file_date}"


_HONORIFIC_RE = re.compile(r"^(?:mr|ms|mrs|dr|prof|professor|sir|dame|hon)\.?\s+(?P<surname>[A-Za-z][\w'\-]+)$", re.I)
_NAME_TOKEN = r"(?:[A-Z][\w'\-]+|[A-Z]\.|[a-z]{1,3}[A-Z][\w'\-]+|[\"“][A-Z][\w'\-]+[\"”])"  # word, initial, deGoma-style, or a "Kate" nickname
_SKIP_TOKENS = {"mr", "ms", "mrs", "dr", "prof", "professor", "sir", "dame", "hon", "the", "and", "our", "by", "with", "of", "to", "in", "for", "on", "at", "as", "a", "an",
                # headline verbs that precede a name in press-release exhibits
                "appoints", "appointed", "names", "named", "announces", "welcomes", "elects", "elected", "promotes", "hires", "adds", "taps", "introduces"}


def _name_candidates(token: str, doc_text: str) -> dict[str, int]:
    """Capitalized runs of 2-4 tokens in the text that contain `token` (as any word), counted."""
    pat = re.compile(rf"\b((?:{_NAME_TOKEN}\s+){{0,3}}{re.escape(token)}(?:\s+{_NAME_TOKEN}){{0,3}})\b")
    counts: dict[str, int] = {}
    for cand in pat.findall(doc_text):
        toks = [t for t in cand.split() if t[0] not in "\"“"]  # drop quoted nicknames: Kathleen "Kate" Rubins -> Kathleen Rubins
        while toks and toks[0].lower().strip(".") in _SKIP_TOKENS:
            toks = toks[1:]
        # trim trailing words that are clearly not part of a name
        while toks and toks[-1].lower().strip(".") in _SKIP_TOKENS | {"has", "is", "was", "who", "served", "joined", "holds", "received", "earned"}:
            toks = toks[:-1]
        if 2 <= len(toks) <= 4 and all(t[0].isupper() or any(c.isupper() for c in t) or t == token for t in toks):
            name = " ".join(toks)
            counts[name] = counts.get(name, 0) + 1
    return counts


def resolve_full_name(person_name: str, doc_text: str) -> tuple[str, bool]:
    """Filing bios open with the full name and then say 'Mr. Ament' or just 'Ament'. When the
    extractor only saw a short form, look the full name up in the filing text. Returns (name, resolved)."""
    name = person_name.strip()
    m = _HONORIFIC_RE.match(name)
    token = m["surname"] if m else (name if len(name.split()) == 1 and name else None)
    if not token:
        return name, False
    counts = _name_candidates(token, doc_text)
    if not counts:
        return name, False
    best = max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
    return best, True
