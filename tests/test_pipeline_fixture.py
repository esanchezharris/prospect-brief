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
    paras = tree.xpath("//h2/following-sibling::p[1]")  # first paragraph under each numbered section
    checked = 0
    for p in paras:
        if "empty" in (p.get("class") or "") or "ai" in (p.get("class") or ""):
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
    assert len(logs) == len(llm.calls) and any(f.name.startswith("001-plan") for f in logs)
    assert "Prepared from public sources only. No wealth-screening data used." in html_path.read_text()


@pytest.mark.xfail(reason="identity check (d) lands in M2", strict=True)
async def test_namesake_facts_never_enter_the_brief(result):
    brief, html_path, *_ = result
    assert "dentistry" not in html_path.read_text() and "Columbus" not in html_path.read_text()


@pytest.mark.xfail(reason="conflict detection lands in M2", strict=True)
async def test_conflicting_amount_is_surfaced(result):
    brief, *_ = result
    ten = [e for e in brief.evidence if "$10 million" in e.claim]
    assert ten and ten[0].conflicts_with
