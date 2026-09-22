"""Render the brief to one self-contained HTML file plus brief.json, evidence.csv, run_report.json."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config
from .models import Brief, Evidence

TEMPLATES = Path(__file__).parent / "templates"


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)


def number_sources(brief: Brief) -> tuple[dict[str, int], list[dict]]:
    """Footnote numbers are per evidence item, assigned in order of first appearance."""
    by_id = {e.id: e for e in brief.evidence}
    numbers: dict[str, int] = {}
    sources: list[dict] = []

    def visit(ids: list[str]) -> None:
        for eid in ids:
            if eid in numbers or eid not in by_id:
                continue
            e = by_id[eid]
            numbers[eid] = len(sources) + 1
            sources.append({"title": e.source_title, "publisher": e.publisher, "published_date": e.published_date, "accessed_at": e.accessed_at, "tier": e.source_tier, "url": e.source_url})

    for sec in brief.sections:
        for s in sec.sentences:
            visit(s.evidence_ids)
    for t in brief.talking_points:
        visit(t.evidence_ids)
    return numbers, sources


def render_html(brief: Brief, config: Config, summary_line: str) -> str:
    numbers, sources = number_sources(brief)
    section_meta = config.get("brief_sections", default=[])
    return _env().get_template("brief.html.j2").render(
        brief=brief,
        section_meta=section_meta,
        sections_by_key={s.key: s for s in brief.sections},
        evidence_by_id={e.id: e for e in brief.evidence},
        fn_numbers=numbers,
        sources=sources,
        dropped=[e for e in brief.evidence if e.status == "dropped"],
        summary_line=summary_line,
    )


EVIDENCE_COLUMNS = [
    "id", "status", "drop_reason", "section", "claim_type", "claim", "supporting_quote", "source_url", "source_title",
    "publisher", "published_date", "accessed_at", "cache_key", "source_tier", "identity_score", "identity_reasons",
    "check_quote", "check_specifics", "check_entailment", "corroborated_by", "conflicts_with",
]


def write_evidence_csv(evidence: list[Evidence], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(EVIDENCE_COLUMNS)
        for e in evidence:
            w.writerow([
                e.id, e.status, e.drop_reason or "", e.section, e.claim_type, e.claim, e.supporting_quote, e.source_url, e.source_title,
                e.publisher, e.published_date or "", e.accessed_at.isoformat(), e.cache_key, e.source_tier,
                "" if e.identity_score is None else e.identity_score, "; ".join(e.identity_reasons),
                e.checks.quote, e.checks.specifics, e.checks.entailment or "", "; ".join(e.corroborated_by), "; ".join(e.conflicts_with),
            ])


def write_outputs(brief: Brief, config: Config, out_dir: Path, summary_line: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "brief.html"
    html_path.write_text(render_html(brief, config, summary_line))
    (out_dir / "brief.json").write_text(brief.model_dump_json(indent=1))
    write_evidence_csv(brief.evidence, out_dir / "evidence.csv")
    (out_dir / "run_report.json").write_text(json.dumps(brief.report.model_dump(mode="json"), indent=1))
    return html_path
