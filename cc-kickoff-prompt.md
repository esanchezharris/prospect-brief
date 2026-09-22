# Paste this into Claude Code, in a fresh repo that contains SPEC.md

Read SPEC.md in this repo from top to bottom before doing anything else. It is the full spec for `prospect-brief`, a source-cited donor briefing generator plus a signal-watch module. The "Context update" and "Signal watch module" sections at the bottom were added after the rest and take precedence wherever they conflict.

Priorities, in this order:

1. M1, the thin slice. By tomorrow morning I need one real HTML brief for a prominent public figure with documented philanthropy, with a numbered footnote on every factual sentence that links to its source and shows the supporting quote on hover. If M1 can't be finished end to end tonight, cut scope inside M1 (fewer sections, plainer layout, one search provider) rather than skipping verification checks (a) and (b). Those checks are the product. Also run it on the fictional fixture person so the tests exist from the start.
2. M1.5, the signal watch. The SEC filings list for "University of Southern California" over the last 90 days, as HTML and CSV, with each row showing the command to build that person's brief. Confirm the SEC full-text search response shape with one real request before writing the parser.
3. M2, then M3, then M4. Skip M5 unless I ask for it.

How to work:

- Start now. Print the plan and the repo tree in a few lines, then begin M1. Don't wait for me to confirm the plan.
- Create CLAUDE.md first, containing the hard rules from SPEC.md and the project conventions, so they survive context resets.
- Before coding against any external API (Anthropic, Tavily, SEC EDGAR, ProPublica), read its current docs and confirm request and response shapes. If an API's real response doesn't match SPEC.md, trust the API and update the spec.
- Commit after each milestone with tests green. At the end of each milestone print: what works, what you cut, the exact command to run, and the time and cost of the last real run.
- Log every model prompt and response under `runs/<run_id>/`.
- Don't add features outside the spec. Put ideas in IDEAS.md and keep going.
- Only stop to ask me if something actually blocks you. If a real public figure produces poor results, try a second one and tell me.

Environment: Python 3.12 with `uv`. Keys are in `.env`: `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, `CONTACT_EMAIL`. `.env`, `runs/`, and `cache/` are gitignored and never committed. No real person's data is committed anywhere.

When M1 works, before moving on, write `README.md` with a "Safeguards" section whose headings are exactly: Grounding, Citations, Confidence thresholds, Hallucination testing, Human-in-the-loop. Each heading gets two or three plain-English sentences a non-technical reader can follow, describing what the code actually does, not what it aspires to.
