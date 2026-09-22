import csv

import pytest

from prospect_brief.signals.efts import hit_from_json, parse_display_name
from prospect_brief.signals.extract import find_passages, frame_event
from prospect_brief.signals.pipeline import run_signals
from prospect_brief.testing import build_fake_llm, fixture_transport

INST = "University of Southern California"


def test_parse_display_name():
    assert parse_display_name("Eloxx Pharmaceuticals, Inc.  (ELOX)  (CIK 0001035354)") == ("Eloxx Pharmaceuticals, Inc.", "ELOX")
    assert parse_display_name("Legion Capital Acquisition Corp.  (CIK 0002151558)") == ("Legion Capital Acquisition Corp.", None)


def test_hit_from_json_builds_document_url():
    h = hit_from_json({"_id": "0001213900-26-097368:ea0304337-01.htm", "_source": {"ciks": ["0002151558"], "display_names": ["Legion Capital Acquisition Corp.  (CIK 0002151558)"], "root_forms": ["S-1"], "form": "S-1", "file_date": "2026-09-04", "adsh": "0001213900-26-097368", "items": []}})
    assert h.url == "https://www.sec.gov/Archives/edgar/data/2151558/000121390026097368/ea0304337-01.htm"
    assert h.company == "Legion Capital Acquisition Corp." and h.root_form == "S-1"


def test_find_passages_windows_and_merges():
    text = "x" * 2000 + " University of Southern California " + "y" * 200 + " University of Southern California " + "z" * 2000
    ps = find_passages(text, INST, window=600)
    assert len(ps) == 1 and "y" * 200 in ps[0] and len(ps[0]) < 1600


def test_event_framing():
    from prospect_brief.models import FilingHit

    h = FilingHit(adsh="a", filename="f", form="8-K", root_form="8-K", file_date="2026-08-20", display_name="", company="Harborline Robotics Corp.", cik="1")
    assert frame_event(h, "Chief Financial Officer") == "appointed Chief Financial Officer at Harborline Robotics Corp., 2026-08-20"
    h.form = h.root_form = "S-1"
    assert frame_event(h, "") == "IPO registration filed 2026-08-20"
    h.form = h.root_form = "DEF 14A"
    assert frame_event(h, "director") == "listed as director in Harborline Robotics Corp.'s proxy statement, 2026-08-20"


async def test_bare_acronym_rejected(config):
    with pytest.raises(ValueError):
        await run_signals(institution="USC", days=90, forms=["S-1"], config=config, llm=build_fake_llm(None), transport=fixture_transport(), log=lambda *_: None)


async def test_signal_watch_fixture(config):
    llm = build_fake_llm(None)
    t = fixture_transport()
    html_path, csv_path = await run_signals(institution=INST, days=90, forms=["S-1", "8-K", "DEF14A"], config=config, llm=llm, transport=t, log=lambda *_: None)
    rows = list(csv.DictReader(open(csv_path)))
    assert [r["name"] for r in rows] == ["Priya Ellsworth-Nakamura"]
    row = rows[0]
    assert row["company"] == "Coralline Therapeutics, Inc." and row["ticker"] == "CRLN" and row["form"] == "S-1"
    assert row["event"] == "IPO registration filed 2026-09-10"
    assert row["brief_command"].startswith("prospect-brief run 'Priya Ellsworth-Nakamura' --anchor employer=") and "--institution" in row["brief_command"]
    assert row["filing_urls"] == "https://www.sec.gov/Archives/edgar/data/9000001/000090000026000001/coralline-s1.htm"
    html = html_path.read_text()
    assert "Priya Ellsworth-Nakamura" in html and "prospect-brief run" in html
    assert "not_bio" in html and "quote_not_in_source" in html  # dropped rows are shown for audit with reasons
    assert "Quenneville-Adair" not in html.split("<details>")[0]  # never in the kept table
    # polite client: declared UA with contact email, efts and archive fetched, never twice
    assert t.requests.count("https://www.sec.gov/Archives/edgar/data/9000001/000090000026000001/coralline-s1.htm") == 1
    # every signal prompt framed the passages as untrusted documents
    sig = [c for c in llm.calls if c["purpose"].startswith("signals-")]
    assert len(sig) == 3 and all("untrusted data" in c["system"] and "<document" in c["user"] for c in sig)
