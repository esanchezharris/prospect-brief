import asyncio
from pathlib import Path

import yaml

from prospect_brief.evaluate import evaluate, fact_matches, format_table, write_outputs
from prospect_brief.pipeline import run_brief
from prospect_brief.testing import build_fake_llm, fixture_transport, mock_search
from tests.conftest import make_config


def test_fact_matching_requires_figures():
    ok, _ = fact_matches("Gave $12 million to USC in 2025.", "Dorian Vexley-Marsh announced a $12 million gift to the University of Southern California on March 3, 2025.")
    assert ok
    bad, _ = fact_matches("Gave $15 million to USC in 2025.", "Dorian Vexley-Marsh announced a $12 million gift to the University of Southern California on March 3, 2025.")
    assert not bad


def test_eval_on_fixture_brief(tmp_path):
    config = make_config(tmp_path)
    run_dir = tmp_path / "runs" / "eval-run"
    run_dir.mkdir(parents=True)
    spec = yaml.safe_load(Path("eval/dorian-vexley-marsh.yaml").read_text())
    brief, _ = asyncio.run(run_brief(subject=spec["subject"], anchors=spec["anchors"], institution=spec["institution"], config=config, llm=build_fake_llm(run_dir), search=mock_search(), run_dir=run_dir, transport=fixture_transport(), log=lambda *_: None))
    result = evaluate(brief, spec["facts"])
    assert result["facts"] == 11
    assert result["recalled"] >= 9, [r for r in result["rows"] if not r["recalled"]]
    nobel = next(r for r in result["rows"] if "Nobel" in r["fact"])
    assert not nobel["recalled"]
    audit, summary = write_outputs(result, tmp_path / "out", "dorian")
    assert audit.exists() and summary.exists()
    lines = audit.read_text().splitlines()
    assert len(lines) - 1 == min(25, result["verified_claims"]) and lines[0].startswith("id,claim,supporting_quote")
    table = format_table(result)
    assert "Recall:" in table and "cost:" in table
    rr = result["run_report"]
    assert rr["counts"]["claims_extracted"] > rr["counts"]["claims_verified"] and "wall_seconds" in rr and rr["usage"]
