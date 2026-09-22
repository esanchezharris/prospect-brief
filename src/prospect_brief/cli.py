"""prospect-brief CLI: `run` builds a brief, `signals` runs the SEC signal watch."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import click

from .config import Config


def _parse_anchors(values: tuple[str, ...]) -> dict[str, str]:
    anchors: dict[str, str] = {}
    for v in values:
        if "=" not in v:
            raise click.BadParameter(f"anchor must look like key=value, got {v!r}")
        k, val = v.split("=", 1)
        anchors[k.strip().lower()] = val.strip()
    return anchors


@click.group()
def main() -> None:
    """Source-cited donor briefing generator. Public sources only."""


@main.command()
@click.argument("name")
@click.option("--anchor", "anchors", multiple=True, help="Identity anchor, e.g. employer=..., city=..., school=... (at least one)")
@click.option("--institution", required=True, help='Institution of interest, e.g. "University of Southern California"')
@click.option("--yes", is_flag=True, help="Skip the identity confirmation prompt")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--search", "search_name", type=click.Choice(["tavily", "mock"]), default=None, help="Search provider (default from config)")
@click.option("--llm", "llm_name", type=click.Choice(["anthropic", "fake"]), default="anthropic")
def run(name: str, anchors: tuple[str, ...], institution: str, yes: bool, config_path: Path | None, search_name: str | None, llm_name: str) -> None:
    """Build a brief for NAME."""
    anchor_map = _parse_anchors(anchors)
    if not anchor_map:
        raise click.UsageError("at least one --anchor is required (employer=, city=, school=, company=, spouse=)")
    config = Config.load(config_path)
    from .pipeline import make_run_id, run_brief

    run_dir = config.runs_dir / make_run_id(name)
    run_dir.mkdir(parents=True, exist_ok=True)

    transport = None
    if llm_name == "fake" or search_name == "mock":
        from .testing import build_fake_llm, fixture_transport, mock_search

        transport = fixture_transport()
        config.root = config.root / ".fixture"  # fixture runs never touch the real cache or outputs
        run_dir = config.runs_dir / run_dir.name
        run_dir.mkdir(parents=True, exist_ok=True)
    if llm_name == "fake":
        llm = build_fake_llm(run_dir)
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise click.ClickException("ANTHROPIC_API_KEY is empty. Add it to .env (see .env.example).")
        from .llm import AnthropicProvider

        llm = AnthropicProvider(config, run_dir)
    provider = search_name or config.get("search", "provider", default="tavily")
    if provider == "mock":
        search = mock_search()
    else:
        from .search import TavilyProvider

        search = TavilyProvider(exclude_domains=config.blocklist, user_agent=config.user_agent)

    def confirm(card) -> bool:
        return click.confirm("Is this the right person? Proceed with research?", default=True)

    async def go():
        try:
            return await run_brief(subject=name, anchors=anchor_map, institution=institution, config=config, llm=llm, search=search, run_dir=run_dir, transport=transport, log=click.echo, confirm=confirm, yes=yes)
        finally:
            await search.aclose()

    from .pipeline import IdentityUnresolved

    try:
        brief, html_path = asyncio.run(go())
    except IdentityUnresolved as e:
        raise click.ClickException(str(e))
    click.echo(f"\nBrief: {html_path}\nRun log: {run_dir}")


@main.command()
@click.option("--institution", required=True)
@click.option("--days", default=90, show_default=True)
@click.option("--forms", default="S-1,8-K,DEF14A", show_default=True)
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--llm", "llm_name", type=click.Choice(["anthropic", "fake"]), default="anthropic")
@click.option("--max-filings", default=200, show_default=True, help="Safety cap on filings fetched")
def signals(institution: str, days: int, forms: str, config_path: Path | None, llm_name: str, max_filings: int) -> None:
    """List people affiliated with INSTITUTION named in recent SEC filings."""
    config = Config.load(config_path)
    from .signals.pipeline import run_signals

    form_list = [f.strip() for f in forms.split(",") if f.strip()]
    transport = None
    if llm_name == "fake":
        from .testing import build_fake_llm, fixture_transport

        llm = build_fake_llm(None)
        transport = fixture_transport()
        config.root = config.root / ".fixture"  # fixture runs never touch the real cache or outputs
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise click.ClickException("ANTHROPIC_API_KEY is empty. Add it to .env (see .env.example).")
        from .llm import AnthropicProvider

        run_dir = config.runs_dir / f"signals-{institution.lower().replace(' ', '-')[:30]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        llm = AnthropicProvider(config, run_dir)
    try:
        html_path, csv_path = asyncio.run(run_signals(institution=institution, days=days, forms=form_list, config=config, llm=llm, max_filings=max_filings, transport=transport, log=click.echo))
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(f"\nHTML: {html_path}\nCSV:  {csv_path}")


@main.command()
@click.argument("slug")
@click.option("--run-id", default=None, help="Brief run to evaluate (default: the latest brief for the subject)")
@click.option("--all-runs", is_flag=True, help="Score every brief for the subject and print a run-to-run table")
@click.option("--judge", is_flag=True, help="Also ask the author model whether each sampled claim is supported by its quote (precision estimate, ~$0.05)")
@click.option("--judge-all", is_flag=True, help="Judge every verified claim, not just the 25-claim sample (~$0.50 per brief)")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
def eval(slug: str, run_id: str | None, all_runs: bool, judge: bool, judge_all: bool, config_path: Path | None) -> None:
    """Score briefs against eval/SLUG.yaml (or eval/real/SLUG.yaml): recall of known facts, plus a precision audit CSV."""
    import json

    from .evaluate import evaluate, format_table, load_eval, write_outputs
    from .models import Brief

    config = Config.load(config_path)
    spec_path = next((p for p in (config.root / "eval" / f"{slug}.yaml", config.root / "eval" / "real" / f"{slug}.yaml") if p.exists()), None)
    if spec_path is None:
        raise click.ClickException(f"no eval file eval/{slug}.yaml or eval/real/{slug}.yaml")
    spec = load_eval(spec_path)
    runs = sorted(config.briefs_dir.glob("*/brief.json")) + sorted((config.root / ".fixture" / "briefs").glob("*/brief.json"))
    runs.sort(key=lambda r: r.parent.name)
    if run_id:
        runs = [r for r in runs if r.parent.name == run_id]
    else:
        runs = [r for r in runs if json.loads(r.read_text())["subject"].lower() == spec["subject"].lower()]
    if not runs:
        raise click.ClickException(f"no brief found for {spec['subject']!r}; run `prospect-brief run` first")
    if all_runs:
        click.echo(f"{'run':<42} {'recall':>8} {'verified':>8} {'sources':>7} {'time':>6} {'cost':>6}  writer")
        for r in runs:
            b = Brief.model_validate_json(r.read_text())
            res = evaluate(b, spec["facts"])
            writer = next((u.model for u in b.report.usage if "opus" in u.model), next((u.model for u in b.report.usage if "sonnet" in u.model), "?"))
            click.echo(f"{b.report.run_id:<42} {res['recalled']:>3}/{res['facts']:<4} {res['verified_claims']:>8} {b.report.counts.get('sources_cited', 0):>7} {b.report.wall_seconds:>5.0f}s {b.report.cost_usd:>6.2f}  {writer}")
        return
    brief = Brief.model_validate_json(runs[-1].read_text())
    result = evaluate(brief, spec["facts"], sample_size=10_000 if judge_all else 25)
    if judge or judge_all:
        from .evaluate import judge_precision
        from .llm import AnthropicProvider

        result["judge"] = judge_precision(AnthropicProvider(config, config.runs_dir / f"eval-{slug}"), result["precision_sample"])
    audit, summary = write_outputs(result, config.root / "eval" / "out", slug)
    click.echo(format_table(result))
    click.echo(f"\nPrecision audit sample: {audit}\nSummary: {summary}")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True)
def serve(host: str, port: int) -> None:
    """Start the demo web page (signal watch, brief builder with live progress, trust check)."""
    import uvicorn

    Config.load()  # loads .env so the workers see the keys
    click.echo(f"prospect-brief demo page: http://{host}:{port}")
    uvicorn.run("prospect_brief.web.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
