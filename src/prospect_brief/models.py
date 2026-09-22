"""Pydantic models shared by the brief pipeline and the signal watch."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["verified", "dropped", "flagged"]


class SearchResult(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    published_date: str | None = None
    query: str = ""


class Document(BaseModel):
    """One cached fetched document. The cache record is the audit trail."""

    url: str
    final_url: str
    cache_key: str
    fetched_at: datetime
    status: int
    content_type: str = ""
    title: str = ""
    text: str = ""
    publisher: str = ""
    published_date: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300 and bool(self.text.strip())


class Checks(BaseModel):
    quote: bool | None = None
    specifics: bool | None = None
    entailment: Literal["supports", "partially", "no"] | None = None


class RawClaim(BaseModel):
    """What the extractor model returns for one fact. Model output, unverified."""

    claim: str = Field(description="One atomic factual sentence about the subject.")
    section: str = Field(description="Brief section key this fact belongs to.")
    claim_type: str = Field(description="e.g. gift, role, board_seat, education, transaction, award, statement, news")
    supporting_quote: str = Field(description="Verbatim passage copied from the document, max 300 characters.")
    published_date: str | None = Field(default=None, description="Date the source was published, ISO if known, else null.")


class ExtractionResult(BaseModel):
    claims: list[RawClaim]


class Evidence(BaseModel):
    """Evidence schema from SPEC.md. Dropped items are kept with their reason."""

    id: str
    claim: str
    section: str
    claim_type: str
    supporting_quote: str
    source_url: str
    source_title: str = ""
    publisher: str = ""
    published_date: str | None = None
    accessed_at: datetime
    cache_key: str
    source_tier: int = 3
    identity_score: float | None = None
    identity_reasons: list[str] = Field(default_factory=list)
    checks: Checks = Field(default_factory=Checks)
    corroborated_by: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)
    status: Status = "flagged"
    drop_reason: str | None = None


class PlannedQuery(BaseModel):
    section: str
    query: str
    topic: Literal["general", "news"] = "general"


class ResearchPlan(BaseModel):
    queries: list[PlannedQuery]


class Sentence(BaseModel):
    text: str = Field(description="One sentence, no citation markers inside the text.")
    evidence_ids: list[str] = Field(description="Ids of the evidence items that support this sentence.")


class BriefSection(BaseModel):
    key: str
    sentences: list[Sentence]


class TalkingPoint(BaseModel):
    text: str
    evidence_ids: list[str]


class BriefDraft(BaseModel):
    sections: list[BriefSection]
    talking_points: list[TalkingPoint]


class LLMUsage(BaseModel):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0


class RunReport(BaseModel):
    run_id: str
    subject: str
    institution: str
    anchors: dict[str, str]
    started_at: datetime
    finished_at: datetime | None = None
    wall_seconds: float = 0.0
    counts: dict[str, int] = Field(default_factory=dict)
    usage: list[LLMUsage] = Field(default_factory=list)
    cost_usd: float = 0.0
    searches_run: int = 0
    notes: list[str] = Field(default_factory=list)


class Brief(BaseModel):
    subject: str
    institution: str
    anchors: dict[str, str]
    generated_at: datetime
    sections: list[BriefSection]
    talking_points: list[TalkingPoint]
    evidence: list[Evidence]
    gaps: list[str] = Field(default_factory=list)
    possibly_different_person: list[dict] = Field(default_factory=list)  # {title, url, identity_score}
    conflicts: list[list[str]] = Field(default_factory=list)              # pairs of evidence ids
    identity: "IdentityCard | None" = None
    report: RunReport


# --- signal watch ---------------------------------------------------------------------


class FilingHit(BaseModel):
    adsh: str
    filename: str
    form: str
    root_form: str
    file_date: str
    display_name: str
    company: str
    ticker: str | None = None
    cik: str
    items: list[str] = Field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/{self.adsh.replace('-', '')}/{self.filename}"

    @property
    def index_url(self) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/{self.adsh.replace('-', '')}/"


class PassageExtraction(BaseModel):
    passage_index: int = Field(description="Index of the passage this was extracted from.")
    person_name: str = Field(description="Full name of the person the passage is about, or empty if none.")
    role: str = Field(description="Their role at the filing company as stated, e.g. 'Chief Financial Officer', 'director'.")
    company: str = Field(description="The company the role is at, as stated.")
    is_bio: bool = Field(description="True only when the passage is a biography of a person who studied or worked at the institution.")
    affiliation_as_stated: str = Field(description="The exact affiliation with the institution as the filing states it, e.g. 'B.A. in Economics from the University of Southern California'.")
    quote: str = Field(description="Verbatim passage, max 300 chars, containing the affiliation statement.")
    confidence: float = Field(ge=0, le=1)


class PassageExtractions(BaseModel):
    extractions: list[PassageExtraction]


class Signal(BaseModel):
    person_name: str
    company: str
    ticker: str | None = None
    role: str
    event: str
    filing_date: str
    form: str
    affiliation_as_stated: str
    quote: str
    confidence: float
    filing_urls: list[str]
    status: Status = "verified"
    drop_reason: str | None = None
    command: str = ""


# --- identity resolution (M2) -----------------------------------------------------------


class AnchorFact(BaseModel):
    fact: str = Field(description="One short identifying fact, e.g. 'Founder of Halcyon Reef Capital'.")
    source_url: str = Field(description="URL of the search result or page that states it.")


class Namesake(BaseModel):
    description: str = Field(description="Who this other person with the same name is, and how they differ from the subject.")
    source_url: str = ""


class IdentityCard(BaseModel):
    full_name: str
    name_variants: list[str] = Field(default_factory=list, description="Other spellings or forms of the name seen in sources.")
    current_role: str = Field(default="", description="Current role and employer if stated in sources, else empty.")
    employer: str = ""
    city: str = ""
    spouse: str = Field(default="", description="Spouse's name ONLY if the sources show them jointly in public philanthropy with the subject (joint gift, family foundation officer); otherwise empty.")
    education: list[str] = Field(default_factory=list)
    anchor_facts: list[AnchorFact] = Field(default_factory=list, description="3 to 5 identifying facts, each with its source.")
    namesakes: list[Namesake] = Field(default_factory=list)
    can_separate: bool = Field(description="True if the subject can be told apart from every namesake using the given anchors.")
    reasoning: str = Field(description="One or two sentences on how the anchors pin down the subject.")


class EntailmentVerdict(BaseModel):
    id: str
    verdict: Literal["supports", "partially", "no"]


class EntailmentVerdicts(BaseModel):
    verdicts: list[EntailmentVerdict]


Brief.model_rebuild()
