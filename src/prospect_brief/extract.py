"""Evidence extraction: the model turns one document into atomic claims with verbatim quotes.
Output is unverified until verify.py has run."""

from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .llm import LLMProvider, UNTRUSTED_INPUT_NOTICE
from .models import Document, Evidence, ExtractionResult

SYSTEM = f"""You extract facts about one named person from a single public web document for a
university development office. {UNTRUSTED_INPUT_NOTICE}

Output rules:
- One fact per claim, written as a complete short sentence naming the person.
- supporting_quote must be copied VERBATIM from the document, contiguous, at most 300
  characters, and must itself contain every number, amount, date and proper noun that the claim uses
  (the subject's own name is the one exception: the quote may say "he" or "she"). Any other name in
  the claim, such as a company, school, foundation or city, must appear in the quote. Do not
  paraphrase inside the quote. If no such passage exists, do not make the claim.
- Only facts about the subject person. If the document is about a different person with the same
  name, or you cannot tell, return an empty list.
- Never estimate net worth, wealth, or giving capacity. Report only stated facts such as
  "sold company X for $Y" or "gave $Z to W".
- Never extract: home address, phone, email, whereabouts, photos, minors, health, religion, race
  or ethnicity, sexual orientation, immigration status, criminal history. Family members only when
  they appear jointly in public philanthropy with the subject.
- section must be one of: background, career, wealth, philanthropy, institution, interests, news.
- published_date: the document's publication date in ISO format if it is stated, else null."""


def _slice_text(text: str, subject: str, max_chars: int) -> str:
    """Keep text near mentions of the subject when the document is longer than the budget."""
    if len(text) <= max_chars:
        return text
    surname = subject.split()[-1].lower()
    low = text.lower()
    windows: list[tuple[int, int]] = []
    start = 0
    while len(windows) < 40:
        i = low.find(surname, start)
        if i < 0:
            break
        windows.append((max(0, i - 1200), min(len(text), i + 1800)))
        start = i + len(surname)
    if not windows:
        return text[:max_chars]
    merged: list[list[int]] = []
    for a, b in sorted(windows):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = " ... ".join(text[a:b] for a, b in merged)
    return out[:max_chars]


def extract_evidence(llm: LLMProvider, config: Config, doc: Document, subject: str, anchors: dict[str, str], institution: str, id_prefix: str) -> list[Evidence]:
    max_chars = int(config.get("budgets", "max_doc_chars", default=15000))
    text = _slice_text(doc.text, subject, max_chars)
    user = (
        f"Subject person: {subject}\nKnown anchors: " + "; ".join(f"{k}={v}" for k, v in anchors.items())
        + f"\nInstitution of interest: {institution}\n"
        f"Document URL: {doc.final_url}\nDocument title: {doc.title}\n\n"
        f"<document>\n{text}\n</document>\n\n"
        "Extract every distinct fact about the subject person that the document supports, following the rules."
    )
    result = llm.structured(purpose=f"extract-{doc.cache_key[:8]}", role="writer", system=SYSTEM, user=user, schema=ExtractionResult, max_tokens=6000)
    now = datetime.now(timezone.utc)
    out: list[Evidence] = []
    for i, c in enumerate(result.claims, 1):
        out.append(
            Evidence(
                id=f"{id_prefix}{i}",
                claim=c.claim.strip(),
                section=c.section.strip().lower(),
                claim_type=c.claim_type.strip().lower(),
                supporting_quote=c.supporting_quote.strip()[:300],
                source_url=doc.final_url,
                source_title=doc.title,
                publisher=doc.publisher,
                published_date=c.published_date or doc.published_date,
                accessed_at=doc.fetched_at or now,
                cache_key=doc.cache_key,
                status="flagged",
            )
        )
    return out
