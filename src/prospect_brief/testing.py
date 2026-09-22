"""Test doubles: a fixture-backed httpx transport, a mock search provider and a deterministic
fake LLM. Used by the test suite and by `--llm fake --search mock` on the CLI. Nothing here
touches the network."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from pydantic import BaseModel

from .config import ROOT
from .llm import FakeLLM
from .models import BriefDraft, BriefSection, ExtractionResult, PassageExtraction, PassageExtractions, PlannedQuery, RawClaim, ResearchPlan, Sentence, TalkingPoint
from .search import MockSearchProvider

CORPUS = ROOT / "tests" / "fixtures" / "corpus"


class FixtureTransport(httpx.AsyncBaseTransport):
    """Serves tests/fixtures/corpus/<host>/<path>. Records every request so tests can assert
    that blocked or robots-disallowed URLs were never requested."""

    def __init__(self, corpus: Path = CORPUS):
        self.corpus = corpus
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        host = request.url.host or ""
        path = request.url.path.lstrip("/") or "index.html"
        if host == "efts.sec.gov":
            import json

            data = json.loads((self.corpus / "efts.sec.gov" / "search-index.json").read_text())
            want = request.url.params.get("forms", "")
            fam = {"S-1": "S-1", "8-K": "8-K", "DEF14A": "DEF 14A"}.get(want, want)
            hits = [h for h in data["hits"]["hits"] if h["_source"]["root_forms"][0] == fam]
            if request.url.params.get("from"):
                hits = []
            return httpx.Response(200, json={"hits": {"total": {"value": len(hits), "relation": "eq"}, "hits": hits}}, request=request)
        p = self.corpus / host / path
        if not p.exists():
            return httpx.Response(404, text="not found", request=request)
        ctype = "application/pdf" if p.suffix == ".pdf" else ("text/plain" if p.suffix == ".txt" else "text/html; charset=utf-8")
        return httpx.Response(200, content=p.read_bytes(), headers={"content-type": ctype}, request=request)


def fixture_transport() -> FixtureTransport:
    return FixtureTransport()


def mock_search() -> MockSearchProvider:
    return MockSearchProvider.from_dir(CORPUS)


# --- scripted extraction per fixture document ---------------------------------------------

_URL_RE = re.compile(r"^Document URL: (.+)$", re.M)

# Each entry: (claim, section, claim_type, quote). Some are deliberately bad, marked in comments.
SCRIPTED: dict[str, list[tuple[str, str, str, str]]] = {
    "fixture.example/news/2025/03/vexley-marsh-gift.html": [
        ("Dorian Vexley-Marsh announced a $12 million gift to the University of Southern California on March 3, 2025.", "philanthropy", "gift",
         "March 3, 2025. Dorian Vexley-Marsh, founder of Halcyon Reef Capital, announced a $12 million gift to the University of Southern California"),
        ("The gift endows the Vexley-Marsh Center for Ocean Robotics at the Viterbi School of Engineering.", "institution", "gift",
         "to endow the Vexley-Marsh Center for Ocean Robotics at the Viterbi School of Engineering"),
        ("Dorian Vexley-Marsh graduated from USC's Viterbi School of Engineering in 1994 with a degree in mechanical engineering.", "background", "education",
         "He graduated from USC’s Viterbi School of Engineering in 1994 with a degree in mechanical engineering"),
        ("Dorian Vexley-Marsh serves on the board of the Tidewater Trust.", "career", "board_seat",
         "Vexley-Marsh serves on the USC Viterbi Board of Councilors and on the board of the Tidewater Trust"),
        # PLANTED: unsupported claim with a fabricated quote (must be dropped by check a)
        ("Dorian Vexley-Marsh pledged $50 million to build a new football stadium.", "philanthropy", "gift",
         "Vexley-Marsh pledged $50 million to build a new football stadium for the Trojans"),
        # PLANTED: number in claim not in quote (must be dropped by check b)
        ("Dorian Vexley-Marsh gave $15 million to the University of Southern California.", "philanthropy", "gift",
         "announced a $12 million gift to the University of Southern California"),
    ],
    "coastalbusinessjournal.example/2024/06/halcyon-reef-sale.html": [
        ("Dorian Vexley-Marsh agreed to sell a majority stake in Halcyon Reef Capital to Meridian Partners for $410 million in June 2024.", "wealth", "transaction",
         "June 18, 2024. Dorian Vexley-Marsh has agreed to sell a majority stake in Halcyon Reef Capital, the Long Beach investment firm he founded in 2003, to Meridian Partners for $410 million"),
        ("Dorian Vexley-Marsh served as chief operating officer of Pelagic Systems from 1998 to 2003.", "career", "role",
         "He previously served as chief operating officer of Pelagic Systems, a maker of underwater drones, from 1998 to 2003"),
    ],
    "halcyonreef.example/about/leadership.html": [
        ("Dorian Vexley-Marsh founded Halcyon Reef Capital in 2003 and was its chief executive officer until 2024.", "career", "role",
         "Dorian Vexley-Marsh founded Halcyon Reef Capital in 2003 and served as its chief executive officer until 2024"),
        ("Dorian Vexley-Marsh holds an M.B.A. from the Anderson School of Management at UCLA.", "background", "education",
         "and an M.B.A. from the Anderson School of Management at UCLA"),
    ],
    "vexleymarshfoundation.example/grants.html": [
        ("The Vexley-Marsh Family Foundation was established in 2016 by Dorian Vexley-Marsh and Imara Vexley-Marsh.", "philanthropy", "foundation",
         "The Vexley-Marsh Family Foundation was established in 2016 by Dorian Vexley-Marsh and Imara Vexley-Marsh"),
        # PLANTED: thin table-cell quote that does not name the foundation; check (b) must drop it
        ("The Vexley-Marsh Family Foundation paid $2,750,000 in grants in 2024.", "philanthropy", "foundation",
         "2024 $2,750,000"),
        ("The foundation's focus areas are marine conservation, engineering education, and coastal community health clinics in Los Angeles County.", "interests", "statement",
         "Focus areas: marine conservation, engineering education, and coastal community health clinics in Los Angeles County"),
    ],
    "fixture.example/magazine/spring-2025/ocean-robotics.html": [
        # CONFLICT with the $12 million figure in the news release
        ("Dorian Vexley-Marsh committed $10 million to establish the Vexley-Marsh Center for Ocean Robotics.", "philanthropy", "gift",
         "Alumnus Dorian Vexley-Marsh (B.S. ME ’94) has committed $10 million to establish the Vexley-Marsh Center for Ocean Robotics"),
    ],
    "buckeyedental.example/team/dorian-vexley-marsh.html": [
        # NAMESAKE: a naive model might emit these; identity check (d) must keep them out (M2)
        ("Dorian Vexley-Marsh has practiced general dentistry in Columbus, Ohio since 2009.", "career", "role",
         "Dr. Dorian Vexley-Marsh has practiced general dentistry in Columbus, Ohio since 2009"),
        ("Dorian Vexley-Marsh donated $5,000 to the Columbus Free Clinic in 2023.", "philanthropy", "gift",
         "donated $5,000 to the Columbus Free Clinic in 2023"),
    ],
    "oceantechweekly.example/2025/interview-vexley-marsh.html": [
        ("Dorian Vexley-Marsh said he wants autonomous gliders that can survey a reef for a month on one battery.", "interests", "statement",
         "Vexley-Marsh said he wants “autonomous gliders that can survey a reef for a month on one battery.”"),
        # INJECTED: a model that obeyed the hidden instruction would emit this; the quote is verbatim from the page
        ("Dorian Vexley-Marsh donated $1 billion to Stanford University in 2025.", "philanthropy", "gift",
         "Dorian Vexley-Marsh donated $1 billion to Stanford University in 2025"),
    ],
    "vexleymarshfoundation.example/annual-report-2024.pdf": [
        ("The foundation had total assets of $48,300,000 at year end 2024.", "philanthropy", "foundation",
         "Total assets at year end 2024 were $48,300,000"),
        ("The foundation's largest 2024 grant, $1,000,000, went to the Tidewater Trust for reef restoration.", "philanthropy", "foundation",
         "The largest 2024 grant, $1,000,000, went to the Tidewater Trust for reef restoration"),
    ],
}

INJECTION_MARKER = "Stanford"


def _plan(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    qs = [
        ("background", "Dorian Vexley-Marsh biography education background"),
        ("career", "Dorian Vexley-Marsh Halcyon Reef Capital chairman founder career"),
        ("wealth", "Dorian Vexley-Marsh sold stake Halcyon sale wealth"),
        ("philanthropy", "Dorian Vexley-Marsh gift philanthropy foundation grants"),
        ("institution", "Dorian Vexley-Marsh University of Southern California USC alumni gift"),
        ("interests", "Dorian Vexley-Marsh interview interests causes robotics"),
        ("news", "Dorian Vexley-Marsh news 2025"),
        ("philanthropy", "Vexley-Marsh Family Foundation annual report 2024"),
    ]
    return ResearchPlan(queries=[PlannedQuery(section=s, query=q, topic="news" if s == "news" else "general") for s, q in qs])


def _extract(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    m = _URL_RE.search(user)
    url = m.group(1).strip() if m else ""
    key = url.replace("https://", "").replace("http://", "")
    rows = SCRIPTED.get(key, [])
    return ExtractionResult(claims=[RawClaim(claim=c, section=s, claim_type=t, supporting_quote=q, published_date=None) for c, s, t, q in rows])


_ROW_RE = re.compile(r"^(?P<id>\S+) \| section=(?P<section>\w+) \| date=[^|]* \| tier=\d \| (?P<claim>.+)$", re.M)


def _write(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    by_section: dict[str, list[tuple[str, str]]] = {}
    for m in _ROW_RE.finditer(user):
        by_section.setdefault(m["section"], []).append((m["id"], m["claim"]))
    sections = []
    all_rows = [(i, c) for rows in by_section.values() for i, c in rows]
    summary = [Sentence(text=c, evidence_ids=[i]) for i, c in all_rows[:3]]
    sections.append(BriefSection(key="summary", sentences=summary))
    for key in ["background", "career", "wealth", "philanthropy", "institution", "interests", "news"]:
        sents = [Sentence(text=c, evidence_ids=[i]) for i, c in by_section.get(key, [])]
        if key == "career":
            # PLANTED: a sentence with no evidence; the post-check must delete it
            sents.append(Sentence(text="He is widely regarded as a visionary leader in ocean technology.", evidence_ids=[]))
        sections.append(BriefSection(key=key, sentences=sents))
    tps = [TalkingPoint(text=f"Ask about: {c}", evidence_ids=[i]) for i, c in all_rows[:2]]
    tps.append(TalkingPoint(text="Mention his love of sailing.", evidence_ids=[]))  # PLANTED, no evidence
    return BriefDraft(sections=sections, talking_points=tps)


# --- scripted signal-watch extractions, keyed by filing company -----------------------------

SIGNAL_SCRIPTS: dict[str, PassageExtraction] = {
    # real bio: kept
    "Coralline Therapeutics, Inc.": PassageExtraction(
        passage_index=0, person_name="Priya Ellsworth-Nakamura", role="Chief Scientific Officer", company="Coralline Therapeutics, Inc.", is_bio=True,
        affiliation_as_stated="Ph.D. in Biomedical Engineering from the University of Southern California in 2009",
        quote="She received a Ph.D. in Biomedical Engineering from the University of Southern California in 2009", confidence=0.95),
    # licensing-deal noise: dropped as not_bio
    "Harborline Robotics Corp.": PassageExtraction(
        passage_index=0, person_name="", role="", company="Harborline Robotics Corp.", is_bio=False,
        affiliation_as_stated="exclusive license agreement with the University of Southern California",
        quote="entered into an exclusive license agreement with the University of Southern California covering certain patents", confidence=0.9),
    # bio whose quote is not in the document: dropped as quote_not_in_source
    "Tidewater Shipping Holdings": PassageExtraction(
        passage_index=0, person_name="Marisol Quenneville-Adair", role="director", company="Tidewater Shipping Holdings", is_bio=True,
        affiliation_as_stated="M.B.A. from the University of Southern California in 1999",
        quote="Ms. Quenneville-Adair earned her M.B.A. from the University of Southern California in 1999", confidence=0.8),
}

_FILING_RE = re.compile(r"^Filing: .+? by (?P<company>.+?), filed ", re.M)


def _signals(system: str, user: str, schema: type[BaseModel]) -> BaseModel:
    m = _FILING_RE.search(user)
    company = m["company"] if m else ""
    x = SIGNAL_SCRIPTS.get(company)
    return PassageExtractions(extractions=[x] if x else [])


def build_fake_llm(run_dir: Path | None) -> FakeLLM:
    return FakeLLM({"plan": _plan, "extract-": _extract, "write-": _write, "signals-": _signals}, run_dir)
