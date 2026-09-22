"""Verification checks. These run in code and never trust the model.

(a) quote_in_source: the supporting quote must be found in the cached source text.
(b) specifics_in_quote: every number, dollar amount, date and proper noun in the claim
    must appear in the quote.
Failing either check drops the evidence item with a recorded reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .textnorm import normalize, unify

DEFAULT_PARTIAL_RATIO_MIN = 92


@dataclass
class CheckResult:
    passed: bool
    reason: str | None = None
    detail: dict = field(default_factory=dict)


def quote_in_source(quote: str, source_text: str, *, partial_ratio_min: int = DEFAULT_PARTIAL_RATIO_MIN) -> CheckResult:
    """Check (a). Exact normalized substring, or rapidfuzz partial_ratio >= threshold."""
    q = normalize(quote)
    s = normalize(source_text)
    if len(q) < 12:
        return CheckResult(False, "quote_too_short", {"len": len(q)})
    i = s.find(q)
    if i >= 0:
        return CheckResult(True, None, {"method": "exact", "start": i, "end": i + len(q)})
    al = fuzz.partial_ratio_alignment(q, s)
    score = al.score if al else 0
    if al and score >= partial_ratio_min:
        return CheckResult(True, None, {"method": "fuzzy", "score": score, "start": al.dest_start, "end": al.dest_end})
    return CheckResult(False, "quote_not_in_source", {"score": score})


# --- check (b): specifics --------------------------------------------------------------

_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|thousand|mm|bn|m|b|k))?", re.I)
_NUMBER_RE = re.compile(r"(?<![\w$])\d[\d,]*(?:\.\d+)?%?")
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_RE = re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{4}}\b|\b(?:{_MONTHS})\.?\s+\d{{1,2}}\b(?:,\s*\d{{4}})?|\b\d{{4}}-\d{{2}}-\d{{2}}\b", re.I)

# Sentence-initial capitalized words are not proper nouns by default; these common words are
# also skipped anywhere so "The", "In", "According" etc. never count.
_STOP = {
    "the", "a", "an", "in", "on", "at", "of", "for", "to", "by", "with", "and", "or", "but", "as",
    "he", "she", "they", "it", "his", "her", "their", "its", "this", "that", "these", "those",
    "according", "after", "before", "during", "since", "from", "into", "over", "under", "per",
    "mr", "ms", "mrs", "dr", "board", "chair", "chairman", "president", "ceo", "founder",
    "director", "trustee", "university", "foundation", "company", "inc", "llc", "corp", "co",
    "school", "college", "institute", "center", "centre", "fund", "gift", "million", "billion",
    "i", "ii", "iii", "iv", "us", "u", "s", "usd",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'’.\-]*")


def _proper_noun_runs(text: str) -> list[str]:
    """Return runs of capitalized tokens, ignoring sentence-initial single words and stopwords."""
    text = unify(text)
    runs: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        tokens = list(_TOKEN_RE.finditer(sentence))
        i = 0
        while i < len(tokens):
            tok = tokens[i].group(0)
            if tok[0].isupper() and tok.lower().strip(".") not in _STOP:
                run = [tok]
                j = i + 1
                while j < len(tokens):
                    nxt = tokens[j].group(0)
                    # allow connectors inside a name run: "University of Southern California"
                    if nxt[0].isupper() and nxt.lower().strip(".") not in _STOP:
                        run.append(nxt)
                    elif nxt.lower() in {"of", "for", "and", "de", "the"} and j + 1 < len(tokens) and tokens[j + 1].group(0)[0].isupper():
                        run.append(nxt)
                    else:
                        break
                    j += 1
                is_sentence_initial = i == 0 and len(run) == 1
                if not is_sentence_initial:
                    runs.append(" ".join(run))
                i = j
            else:
                i += 1
    return runs


def extract_specifics(claim: str) -> dict[str, list[str]]:
    c = unify(claim)
    money = [m.group(0) for m in _MONEY_RE.finditer(c)]
    dates = [m.group(0) for m in _DATE_RE.finditer(c)]
    covered = " ".join(money + dates)
    numbers = [n for n in (m.group(0) for m in _NUMBER_RE.finditer(c)) if n.strip(",.%") and n not in covered]
    nouns = _proper_noun_runs(c)
    return {"money": money, "dates": dates, "numbers": numbers, "proper_nouns": nouns}


def _present(token: str, quote_norm: str) -> bool:
    t = normalize(token)
    if not t:
        return True
    if t in quote_norm:
        return True
    # a multi-word proper noun run passes if each significant word is present
    words = [w for w in t.split() if w not in _STOP and len(w) > 1]
    if len(words) > 1 and all(w in quote_norm for w in words):
        return True
    # numbers: tolerate thousands separators either way
    digits = t.replace(",", "").replace("$", "").strip()
    if digits and digits.replace(".", "").isdigit():
        qn = quote_norm.replace(",", "")
        return re.search(rf"(?<![\d.]){re.escape(digits)}(?![\d])", qn) is not None
    return False


_INJECTION_PATTERNS = [
    "ignore previous instructions", "ignore all previous", "ignore the above", "disregard previous",
    "you are an ai", "as an ai", "system prompt", "new instructions", "assistant:", "instructions:",
    "output the following", "do not follow", "override", "jailbreak",
]


def looks_like_instruction(quote: str, source_text: str = "", span: tuple[int, int] | None = None, window: int = 300) -> CheckResult:
    """Hard rule 6 guard: a quote that reads like an instruction to the model, or that sits inside a
    passage that does, is never evidence. `span` is the normalized-source match from quote_in_source."""
    q = normalize(quote)
    for pat in _INJECTION_PATTERNS:
        if pat in q:
            return CheckResult(False, f"quote_contains_instruction:{pat}")
    if source_text and span:
        s = normalize(source_text)
        ctx = s[max(0, span[0] - window): span[1] + window]
        for pat in _INJECTION_PATTERNS:
            if pat in ctx:
                return CheckResult(False, f"quote_near_instruction:{pat}")
    return CheckResult(True)


_ESTIMATE_RE = re.compile(
    r"\b(net worth|networth|fortune (?:is |was )?(?:estimated|valued)|estimated (?:wealth|fortune|net worth)|wealth (?:is |was )?estimated"
    r"|(?:is|was|are|were|be|being|now) worth (?:more than |over |about |around |roughly |an estimated |nearly |at least |approximately |\$)"
    r"|billionaires? (?:index|list|ranking)|richest (?:people|women|men|person|americans)|giving capacity|capacity rating|wealth rating)", re.I)


_OUT_OF_SCOPE = {
    "health": re.compile(r"\b(?:(?:his|her|their) (?:health|illness|diagnosis|surgery|cancer|disease|disability|pregnancy|therapy|addiction|medication|mental health|dental work|dental care|medical (?:condition|treatment|bills?))|(?:was|were|is|are|been) (?:diagnosed|hospitali[sz]ed|treated for|recovering from|battling)|dental work|in remission|suffers? from)\b", re.I),
    "contact": re.compile(r"(?:\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b|[\w.+-]+@[\w-]+\.[\w.]+|\b\d{1,5} [A-Z][\w]+ (?:Street|St\.|Avenue|Ave\.|Lane|Ln\.|Road|Rd\.|Drive|Dr\.|Boulevard|Blvd\.|Court|Ct\.|Way|Place|Pl\.)\b|\b(?:lives|resides|residing) at \d)", re.I),
    "religion": re.compile(r"\b(?:(?:is|was|are|were) (?:a |an )?(?:devout|practicing|observant) |converted to (?:christianity|judaism|islam|catholicism|buddhism|hinduism)|attends (?:church|synagogue|mosque|temple) )", re.I),
    "ethnicity": re.compile(r"\b(?:(?:his|her|their) (?:race|ethnicity|ethnic background|racial background)|(?:is|was) (?:of )?(?:african[- ]american|asian[- ]american|hispanic|latino|latina|white|black|caucasian|jewish|native american) (?:descent|heritage|origin|woman|man))\b", re.I),
    "immigration": re.compile(r"\b(?:immigration status|undocumented|green card|(?:work|student|H-1B) visa|naturali[sz]ed citizen|asylum)\b", re.I),
    "criminal": re.compile(r"\b(?:arrested|indicted|convicted|pleaded (?:guilty|no contest)|charged with|criminal (?:record|charges?)|sentenced to|felony|misdemeanor|DUI)\b", re.I),
    "sexual_orientation": re.compile(r"\b(?:sexual orientation|came out as|(?:is|was) (?:gay|lesbian|bisexual|transgender|queer))\b", re.I),
}


def is_out_of_scope(claim: str, quote: str = "") -> CheckResult:
    """Hard rule 5: categories the brief never carries, checked in code with deliberately narrow
    patterns (a gift to a cancer center is fine; the subject's own diagnosis is not)."""
    text = f"{claim} {quote}"
    for cat, rx in _OUT_OF_SCOPE.items():
        if rx.search(text):
            return CheckResult(False, f"out_of_scope:{cat}")
    return CheckResult(True)


def is_wealth_estimate(claim: str, quote: str = "") -> CheckResult:
    """Hard rule 3: net worth and capacity figures are estimates even when a publisher prints them.
    The brief carries transactions and holdings, never a wealth number."""
    if _ESTIMATE_RE.search(claim) or _ESTIMATE_RE.search(quote):
        return CheckResult(False, "wealth_estimate_not_allowed")
    return CheckResult(True)


_PRONOUN_RE = re.compile(r"\b(he|she|they|him|her|his|hers|their|theirs|himself|herself|themselves|mr|mrs|ms|dr)\b\.?", re.I)


def quote_references_subject(quote: str, subject: str, extra_names: list[str] | None = None) -> CheckResult:
    """A quote that never refers to the subject (by name, a name variant, a pronoun or an honorific)
    cannot support a claim about them on its own: 'gave $20 million to Morehouse College' could be
    anyone. Such fragments are dropped; the extractor is told to widen the quote instead."""
    q = normalize(quote)
    names = [subject] + list(extra_names or [])
    for n in names:
        for part in normalize(n).split():
            if len(part) > 2 and re.search(rf"\b{re.escape(part)}\b", q):
                return CheckResult(True)
    if _PRONOUN_RE.search(unify(quote)):
        return CheckResult(True)
    return CheckResult(False, "quote_names_no_subject")


def specifics_in_quote(claim: str, quote: str, exempt_names: list[str] | None = None) -> CheckResult:
    """Check (b). Every specific in the claim must be present in the quote.

    exempt_names: the subject's own name (and its parts). Claims are written to name the subject
    while quotes often say "he" or "she"; attributing the passage to the subject is the job of the
    document-level identity check, not of this check. Every other proper noun must be in the quote.
    """
    quote_norm = normalize(quote)
    exempt = {normalize(n) for n in (exempt_names or []) if n.strip()}
    exempt_words = {w for n in exempt for w in n.split()}
    specifics = extract_specifics(claim)
    for kind, items in specifics.items():
        for item in items:
            if kind == "proper_nouns":
                words = [w for w in normalize(item).split() if w not in _STOP]
                if normalize(item) in exempt or (words and all(w in exempt_words for w in words)):
                    continue
            if not _present(item, quote_norm):
                return CheckResult(False, f"specific_missing:{kind}:{item}", specifics)
    return CheckResult(True, None, specifics)
