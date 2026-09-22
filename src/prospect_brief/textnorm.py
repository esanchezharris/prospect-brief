"""Text normalization shared by verification and the signal watch.

Normalization is deliberately simple and deterministic: casefold, unify curly quotes and
dashes, drop punctuation, collapse whitespace. Both the quote and the source pass through
the same function, so a match means the same words in the same order.
"""

from __future__ import annotations

import re
import unicodedata

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-", " ": " ", "…": "...",
}
_PUNCT_RE = re.compile(r"[^\w\s$%]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def unify(text: str) -> str:
    """Unicode NFKC plus quote/dash unification. Keeps case and punctuation."""
    text = unicodedata.normalize("NFKC", text)
    for k, v in _QUOTES.items():
        text = text.replace(k, v)
    return text


def normalize(text: str) -> str:
    """Casefold, unify, strip punctuation (keeps $ and %), collapse whitespace."""
    text = unify(text).casefold()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def squash_ws(text: str) -> str:
    return _WS_RE.sub(" ", unify(text)).strip()
