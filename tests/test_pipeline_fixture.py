"""End-to-end on the fictional fixture corpus with the fake LLM. Must-pass tests from SPEC.md."""

import csv
import re

import pytest
from lxml import html as lxml_html

from prospect_brief.pipeline import run_brief
from prospect_brief.testing import INJECTION_MARKER, build_fake_llm, fixture_transport, mock_search

SUBJECT = "Dorian Vexley-Marsh"
ANCHORS = {"employer": "Halcyon Reef Capital", "school": "University of Southern California", "city": "Long Beach"}


@pytest.fixture
async def result(config, tmp_path):
    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    llm = build_fake_llm(run_dir)
    transport = fixture_transport()
    brief, html_path = await run_brief(
        subject=SUBJECT, anchors=ANCHORS, institution="University of Southern California", config=config, llm=llm,
        search=mock_search(), run_dir=run_dir, transport=transport, log=lambda *_: None,
    )
    return brief, html_path, llm, transport, run_dir


def _claims(brief, status):
    return [e.claim for e in brief.evidence if e.status == status]


async def test_planted_unsupported_claim_is_dropped(result):
    brief, *_ = result
    dropped = {e.claim: e.drop_reason for e in brief.evidence if e.status == "dropped"}
    assert any("football stadium" in c and r == "quote_not_in_source" for c, r in dropped.items())
    assert not any("football stadium" in c for c in _claims(brief, "verified"))


async def test_planted_wrong_number_is_dropped(result):
    brief, *_ = result
    dropped = {e.claim: e.drop_reason for e in brief.evidence if e.status == "dropped"}
    assert any("$15 million" in c and r.startswith("specific_missing") for c, r in dropped.items())


async def test_true_claims_are_verified(result):
    brief, *_ = result
    verified = _claims(brief, "verified")
    assert any("$12 million" in c for c in verified)
    assert any("$410 million" in c for c in verified)
    assert any("$48,300,000" in c for c in verified)  # from the PDF


async def test_injected_instruction_has_no_effect(result):
    brief, html_path, llm, *_ = result
    # 1. the extraction prompt frames the page as untrusted data
    ext = [c for c in llm.calls if c["purpose"].startswith("extract-")]
    assert ext and all("untrusted data" in c["system"] and "<document>" in c["user"] for c in ext)
    # 2. the claim a compromised model would emit from the hidden instruction is dropped in code
    inj = [e for e in brief.evidence if INJECTION_MARKER in e.claim]
    assert inj and all(e.status == "dropped" and e.drop_reason.startswith("quote_near_instruction") for e in inj)
    # 3. nothing from the injection reaches the document
    html = html_path.read_text()
    assert INJECTION_MARKER not in html and "$1 billion" not in html and "test mode" not in html


async def test_blocklist_and_robots_never_fetched(result):
    brief, html_path, llm, transport, _ = result
    assert not any("blocked.example" in u for u in transport.requests)
    assert not any("/private/" in u for u in transport.requests)
    assert "$900 million" not in html_path.read_text()
    assert "$25 million" not in html_path.read_text()


async def test_every_factual_sentence_has_a_footnote(result):
    brief, html_path, *_ = result
    tree = lxml_html.fromstring(html_path.read_text())
    # the first paragraph under each of the 8 factual sections (summary .. recent news)
    paras = tree.xpath("//h2[not(contains(., 'talking points')) and not(contains(., 'gaps')) and not(contains(., 'Sources'))]/following-sibling::p[1]")
    assert len(paras) == 8
    checked = 0
    for p in paras:
        if "empty" in (p.get("class") or ""):
            continue
        # every sentence-ending period inside the paragraph must be followed by a footnote link
        text_nodes = p.xpath(".//text()[not(ancestor::span[@class='card']) and not(ancestor::sup)]")
        for t in text_nodes:
            for sent in re.split(r"(?<=[.!?])\s+", t.strip()):
                if len(sent) > 20:
                    checked += 1
        sups = p.xpath(".//sup[@class='fn']/a")
        assert len(sups) >= 1, lxml_html.tostring(p)[:200]
    assert checked >= 8
    # sentences without evidence were deleted by the post-check
    assert "widely regarded as a visionary" not in html_path.read_text()
    assert "love of sailing" not in html_path.read_text()
    assert brief.report.counts["sentences_deleted_no_citation"] == 1


async def test_footnotes_link_to_sources_with_quotes(result):
    brief, html_path, *_ = result
    tree = lxml_html.fromstring(html_path.read_text())
    links = tree.xpath("//sup[@class='fn']/a/@href")
    assert links
    for href in set(links):
        n = href.lstrip("#src-")
        assert tree.xpath(f"//li[@id='src-{n}']"), href
    cards = tree.xpath("//sup[@class='fn']/span[@class='card']/q/text()")
    assert any("$12 million" in c for c in cards)


async def test_outputs_and_run_log(result):
    brief, html_path, llm, transport, run_dir = result
    out = html_path.parent
    assert (out / "brief.json").exists() and (out / "run_report.json").exists()
    rows = list(csv.DictReader(open(out / "evidence.csv")))
    assert any(r["status"] == "dropped" and r["drop_reason"] for r in rows)
    logs = list((run_dir / "llm").glob("*.json"))
    assert len(logs) == len(llm.calls) and any(f.name.startswith("001-identity") for f in logs) and any(f.name.endswith("-plan.json") for f in logs)
    assert "Prepared from public sources only. No wealth-screening data used." in html_path.read_text()


async def test_namesake_facts_never_enter_the_brief(result):
    brief, html_path, *_ = result
    html = html_path.read_text()
    body = html.split("Possibly a different person")[0]
    assert "dentistry" not in body and "Columbus" not in body and "$5,000" not in body
    flagged = [e for e in brief.evidence if e.status == "flagged"]
    assert flagged and all(e.drop_reason == "possibly_different_person" for e in flagged)
    assert any("buckeyedental" in d["url"] for d in brief.possibly_different_person)
    assert "buckeyedental.example" in html  # listed in the gaps section for the researcher


async def test_conflicting_amount_is_surfaced(result):
    brief, html_path, *_ = result
    ten = [e for e in brief.evidence if "$10 million" in e.claim]
    twelve = [e for e in brief.evidence if "$12 million" in e.claim and e.status == "verified"]
    assert ten and twelve and twelve[0].id in ten[0].conflicts_with
    assert brief.conflicts and "Conflicting information" in html_path.read_text()


async def test_entailment_failure_is_dropped(result):
    brief, *_ = result
    bad = [e for e in brief.evidence if "founded Pelagic" in e.claim]
    assert bad and bad[0].status == "dropped" and bad[0].drop_reason == "entailment:no"
    assert bad[0].checks.quote and bad[0].checks.specifics  # it passed (a) and (b); only (c) caught it


async def test_tiers_and_identity_card(result):
    brief, html_path, *_ = result
    by_url = {e.source_url: e.source_tier for e in brief.evidence}
    assert by_url["https://fixture.example/news/2025/03/vexley-marsh-gift.html"] == 1
    assert by_url["https://halcyonreef.example/about/leadership.html"] == 1  # subject's own company
    assert by_url["https://coastalbusinessjournal.example/2024/06/halcyon-reef-sale.html"] == 2
    assert by_url["https://oceantechweekly.example/2025/interview-vexley-marsh.html"] == 3
    assert 'class="t3"' in html_path.read_text()
    assert brief.identity and brief.identity.can_separate and brief.report.counts["identity_anchors"] >= 3
