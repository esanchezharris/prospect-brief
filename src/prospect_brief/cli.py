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

    if not yes:
        click.echo(f"Subject: {name}\nAnchors: {anchor_map}\nInstitution: {institution}")
        click.echo("Identity resolution arrives in M2; for now confirm these anchors describe one specific person.")
        if not click.confirm("Proceed?", default=True):
            raise SystemExit(1)

    transport = None
    if llm_name == "fake" or search_name == "mock":
        from .testing import build_fake_llm, fixture_transport, mock_search

        transport = fixture_transport()
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

    async def go():
        try:
            return await run_brief(subject=name, anchors=anchor_map, institution=institution, config=config, llm=llm, search=search, run_dir=run_dir, transport=transport, log=click.echo)
        finally:
            await search.aclose()

    brief, html_path = asyncio.run(go())
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
    if llm_name == "fake":
        from .testing import build_fake_llm

        llm = build_fake_llm(None)
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise click.ClickException("ANTHROPIC_API_KEY is empty. Add it to .env (see .env.example).")
        from .llm import AnthropicProvider

        run_dir = config.runs_dir / f"signals-{institution.lower().replace(' ', '-')[:30]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        llm = AnthropicProvider(config, run_dir)
    html_path, csv_path = asyncio.run(run_signals(institution=institution, days=days, forms=form_list, config=config, llm=llm, max_filings=max_filings, log=click.echo))
    click.echo(f"\nHTML: {html_path}\nCSV:  {csv_path}")


if __name__ == "__main__":
    sys.exit(main())
