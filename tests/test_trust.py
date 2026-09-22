from datetime import date, datetime, timezone

from prospect_brief.config import Config
from prospect_brief.models import Checks, Document, Evidence
from prospect_brief.trust import corroborate_and_conflict, identity_score, mark_stale, source_tier


def _ev(id, claim, url, section="philanthropy", ctype="gift", date_=None, status="verified"):
    return Evidence(id=id, claim=claim, section=section, claim_type=ctype, supporting_quote=claim, source_url=url, accessed_at=datetime.now(timezone.utc), cache_key=id, status=status, published_date=date_, checks=Checks(quote=True, specifics=True))


def test_identity_score_needs_an_anchor():
    doc = Document(url="u", final_url="u", cache_key="k", fetched_at=datetime.now(timezone.utc), status=200, text="Dr. Dorian Vexley-Marsh practices dentistry in Columbus, Ohio.")
    score, reasons = identity_score(doc, {"employer": "Halcyon Reef Capital", "city": "Long Beach"})
    assert score == 0 and reasons == []
    doc.text = "Dorian Vexley-Marsh, founder of Halcyon Reef Capital in Long Beach"
    score, reasons = identity_score(doc, {"employer": "Halcyon Reef Capital", "city": "Long Beach"})
    assert score == 1.0 and set(reasons) == {"employer:Halcyon Reef Capital", "city:Long Beach"}


def test_source_tiers():
    cfg = Config.load()
    assert source_tier("https://www.sec.gov/Archives/x", cfg) == 1
    assert source_tier("https://about.usc.edu/x", cfg) == 1
    assert source_tier("https://www.nytimes.com/x", cfg) == 2
    assert source_tier("https://en.wikipedia.org/wiki/X", cfg) == 3
    assert source_tier("https://www.halcyonreef.com/team", cfg, subject_orgs=["Halcyon Reef Capital"]) == 1


def test_corroboration_across_domains_and_conflict():
    a = _ev("a", "Dorian gave $12 million to the University of Southern California in 2025.", "https://news.usc.edu/a")
    b = _ev("b", "Dorian gave $12 million to the University of Southern California in 2025.", "https://www.latimes.com/b")
    c = _ev("c", "Dorian gave $10 million to the University of Southern California in 2025.", "https://magazine.example/c")
    d = _ev("d", "Dorian gave $12 million to the University of Southern California in 2025.", "https://news.usc.edu/dup")  # same domain as a
    corroborate_and_conflict([a, b, c, d])
    assert "b" in a.corroborated_by and "a" in b.corroborated_by
    assert "d" not in a.corroborated_by  # same domain is not independent
    assert "c" in a.conflicts_with and "a" in c.conflicts_with and "b" in c.conflicts_with


def test_stale_current_roles_get_as_of():
    cfg = Config.load()
    old = _ev("r1", "Dorian Vexley-Marsh serves on the board of the Tidewater Trust.", "https://x.example/1", section="career", ctype="board_seat", date_="2023-01-15")
    fresh = _ev("r2", "Dorian Vexley-Marsh serves on the board of the Tidewater Trust.", "https://x.example/2", section="career", ctype="board_seat", date_="2026-06-01")
    past = _ev("r3", "Dorian Vexley-Marsh served as COO of Pelagic Systems from 1998 to 2003.", "https://x.example/3", section="career", ctype="role", date_="2020-01-01")
    n = mark_stale([old, fresh, past], cfg, today=date(2026, 9, 21))
    assert n == 1 and old.claim.startswith("As of 2023-01-15, Dorian") and fresh.claim.startswith("Dorian") and past.claim.startswith("Dorian")
