"""Research plan: the model writes search queries per brief section, bounded by the budget."""

from __future__ import annotations

from .config import Config
from .llm import LLMProvider
from .models import PlannedQuery, ResearchPlan

SYSTEM = """You plan public-source research for a university development office. You write web search
queries that will find documented facts about one specific person: their background, career,
board seats, business transactions reported in the press, philanthropy (gifts, family foundation,
nonprofit boards), connections to the named institution, stated interests, and recent news.

Rules: never write queries aimed at home address, contact details, family members outside public
philanthropy, health, religion, ethnicity, immigration status or criminal history. Never write
queries for net-worth estimates. Prefer queries that will surface primary sources (the person's
company or foundation, the institution, SEC filings, established news outlets). Include the
person's full name in every query; add an anchor (employer, city, school) to most of them so
namesakes are excluded."""


def make_plan(llm: LLMProvider, config: Config, subject: str, anchors: dict[str, str], institution: str) -> ResearchPlan:
    sections = [s for s in config.get("brief_sections", default=[]) if s["key"] != "summary"]
    max_q = int(config.get("budgets", "max_searches", default=30))
    per = max(2, min(4, max_q // max(1, len(sections))))
    user = (
        f"Subject: {subject}\nAnchors: " + "; ".join(f"{k}={v}" for k, v in anchors.items()) + f"\nInstitution: {institution}\n\n"
        f"Write {per} queries for each of these sections (section key in parentheses):\n"
        + "\n".join(f"- {s['title']} ({s['key']})" for s in sections)
        + "\n\nUse topic 'news' only for the recent news section. Total queries must not exceed "
        f"{max_q}."
    )
    plan = llm.structured(purpose="plan", role="writer", system=SYSTEM, user=user, schema=ResearchPlan, max_tokens=4000)
    # enforce budget and name presence in code
    seen: set[str] = set()
    out: list[PlannedQuery] = []
    surname = subject.split()[-1].lower()
    for q in plan.queries:
        text = q.query.strip()
        key = text.lower()
        if key in seen or surname not in key:
            continue
        seen.add(key)
        out.append(PlannedQuery(section=q.section, query=text, topic=q.topic))
        if len(out) >= max_q:
            break
    return ResearchPlan(queries=out)
