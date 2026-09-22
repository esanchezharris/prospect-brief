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


def test_amounts_never_conflict_with_dates_or_other_periods():
    est = _ev("f1", "The Vexley-Marsh Family Foundation was established in 2016 by Dorian.", "https://a.example/1", ctype="foundation")
    a24 = _ev("f2", "Vexley-Marsh Family Foundation reported total assets of $48,300,000 for tax year 2024.", "https://b.example/2", ctype="foundation")
    a23 = _ev("f3", "Vexley-Marsh Family Foundation reported total assets of $45,100,000 for tax year 2023.", "https://c.example/3", ctype="foundation")
    a24b = _ev("f4", "Vexley-Marsh Family Foundation had total assets of $50,000,000 at year end 2024.", "https://d.example/4", ctype="foundation")
    corroborate_and_conflict([est, a24, a23, a24b], "Dorian Vexley-Marsh")
    assert not est.conflicts_with and not a23.conflicts_with  # different kinds / different periods
    assert a24b.id in a24.conflicts_with  # same period, different amount


def test_date_only_claims_about_different_events_do_not_conflict():
    novel = _ev("n1", "MacKenzie Scott published her first novel, The Testing of Luther Albright, in 2005.", "https://a.example/1", section="background", ctype="publication")
    award = _ev("n2", "MacKenzie Scott won an American Book Award in 2006 for The Testing of Luther Albright.", "https://b.example/2", section="background", ctype="publication")
    p1 = _ev("p1", "MacKenzie Scott signed the Giving Pledge in May 2019.", "https://a.example/3", ctype="pledge")
    p2 = _ev("p2", "MacKenzie Scott signed the Giving Pledge in 2019.", "https://b.example/4", ctype="pledge")
    corroborate_and_conflict([novel, award, p1, p2], "MacKenzie Scott")
    assert not novel.conflicts_with and not p1.conflicts_with
    assert p2.id in p1.corroborated_by


def test_subjects_own_organizations_do_not_create_conflicts():
    a = _ev("g1", "MacKenzie Scott donated $20 million to Climate Lead through Yield Giving in 2025.", "https://a.example/1")
    b = _ev("g2", "MacKenzie Scott's Yield Giving gave $70 million to UNCF in 2025.", "https://b.example/2")
    corroborate_and_conflict([a, b], "MacKenzie Scott", {"employer": "Yield Giving"})
    assert not a.conflicts_with


def test_marriage_and_divorce_years_do_not_conflict():
    m = _ev("m1", "MacKenzie Scott married Jeff Bezos in 1993.", "https://a.example/1", section="background", ctype="personal")
    d = _ev("m2", "MacKenzie Scott divorced Amazon founder Jeff Bezos in 2019.", "https://b.example/2", section="background", ctype="personal")
    corroborate_and_conflict([m, d], "MacKenzie Scott")
    assert not m.conflicts_with


def test_year_total_and_lifetime_total_do_not_conflict():
    a = _ev("t1", "MacKenzie Scott's 2020 charitable giving totaled $5.8 billion.", "https://a.example/1")
    b = _ev("t2", "MacKenzie Scott's total lifetime giving is about $19.25 billion, according to her website.", "https://b.example/2")
    m = _ev("t3", "MacKenzie Scott married Jeff Bezos in 1993.", "https://a.example/3", section="background", ctype="personal")
    y = _ev("t4", "MacKenzie Scott was married to Jeff Bezos for 25 years.", "https://b.example/4", section="background", ctype="personal")
    corroborate_and_conflict([a, b, m, y], "MacKenzie Scott")
    assert not a.conflicts_with and not m.conflicts_with


def test_amount_with_breakdown_is_not_a_conflict():
    a = _ev("h1", "MacKenzie Scott gave $80 million to Howard University.", "https://a.example/1")
    b = _ev("h2", "MacKenzie Scott donated $80 million to Howard University, including $63 million for the endowment.", "https://b.example/2")
    corroborate_and_conflict([a, b], "MacKenzie Scott")
    assert not a.conflicts_with
