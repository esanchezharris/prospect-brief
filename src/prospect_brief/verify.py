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
