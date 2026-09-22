"""Writer: sees only verified evidence, returns sentences with evidence ids. A code post-check
removes any factual sentence without a valid id; if more than 10% are removed, regenerate once."""

from __future__ import annotations

import re

from .config import Config
from .llm import LLMProvider
from .models import BriefDraft, BriefSection, Evidence, Sentence, TalkingPoint

SYSTEM = """You write a 1-2 page donor briefing for a university development office from a list of
verified evidence items. You have no other knowledge: use ONLY the evidence rows provided. Every
sentence must be supported by one or more evidence ids from the list, and you must list those ids
in evidence_ids. Never combine facts into totals, never estimate wealth or giving capacity, never
infer anything not stated in a row. Write in plain, neutral prose, third person, past tense for
events. Do not include citation markers, brackets or ids inside the sentence text itself.
Prefer tier 1 and tier 2 evidence; use a tier 3 row only when no better row covers the fact.
When the same fact appears in several rows, cite all of their ids on one sentence rather than
repeating the fact.

Sections (use exactly these keys; leave a section's sentences empty if there is no evidence):
summary (3-5 sentences drawn from the strongest evidence), background, career, wealth,
philanthropy, institution, interests, news.

talking_points: 3-5 short suggestions a gift officer could raise in conversation, each tied to
evidence_ids. These are labeled as AI suggestions in the document."""

_ID_RE = re.compile(r"\b[A-Za-z]\d+-\d+\b")


def _evidence_table(evidence: list[Evidence]) -> str:
    rows = []
    for e in evidence:
        rows.append(f"{e.id} | section={e.section} | date={e.published_date or 'unknown'} | tier={e.source_tier} | {e.claim}")
    return "\n".join(rows)


def _post_check(draft: BriefDraft, valid_ids: set[str], factual_keys: set[str]) -> tuple[BriefDraft, int, int]:
    """Delete factual sentences lacking a valid evidence id. Returns (draft, kept, deleted)."""
    kept = deleted = 0
    sections: list[BriefSection] = []
    for sec in draft.sections:
        sents: list[Sentence] = []
        for s in sec.sentences:
            ids = [i for i in s.evidence_ids if i in valid_ids]
            text = _ID_RE.sub("", s.text).strip()
            if sec.key in factual_keys and not ids:
                deleted += 1
                continue
            kept += 1
            sents.append(Sentence(text=text, evidence_ids=ids))
        sections.append(BriefSection(key=sec.key, sentences=sents))
    tps = [TalkingPoint(text=t.text, evidence_ids=[i for i in t.evidence_ids if i in valid_ids]) for t in draft.talking_points]
    tps = [t for t in tps if t.evidence_ids]
    return BriefDraft(sections=sections, talking_points=tps), kept, deleted


def write_brief(llm: LLMProvider, config: Config, subject: str, institution: str, evidence: list[Evidence]) -> tuple[BriefDraft, dict]:
    verified = [e for e in evidence if e.status == "verified"]
    valid_ids = {e.id for e in verified}
    factual_keys = {s["key"] for s in config.get("brief_sections", default=[])}
    max_frac = float(config.get("verify", "max_sentence_drop_fraction", default=0.10))
    user = (
        f"Subject: {subject}\nInstitution: {institution}\n\nVerified evidence (id | section | date | tier | claim):\n"
        + _evidence_table(verified)
        + "\n\nWrite the briefing."
    )
    stats: dict = {"attempts": 0, "sentences_deleted": 0, "sentences_kept": 0}
    draft = BriefDraft(sections=[], talking_points=[])
    for attempt in range(2):
        stats["attempts"] += 1
        raw = llm.structured(purpose=f"write-{attempt + 1}", role="writer", system=SYSTEM, user=user, schema=BriefDraft, max_tokens=8000)
        draft, kept, deleted = _post_check(raw, valid_ids, factual_keys)
        stats["sentences_kept"], stats["sentences_deleted"] = kept, deleted
        total = kept + deleted
        if total == 0 or deleted / total <= max_frac:
            break
    return draft, stats
