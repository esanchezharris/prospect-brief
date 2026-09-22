# prospect-brief

A source-cited donor briefing generator for a university development office, plus a signal
watch that finds people connected to the institution in recent SEC filings. Every factual
sentence in a brief carries a numbered footnote that links to a cached public source and shows
the exact supporting passage on hover. Public sources only; no wealth-screening data.

Built as a demo. The full specification is in [SPEC.md](SPEC.md); the non-negotiable rules are in
[CLAUDE.md](CLAUDE.md).

## Install

```bash
uv sync
cp .env.example .env   # then fill in ANTHROPIC_API_KEY, TAVILY_API_KEY, CONTACT_EMAIL
```

## Commands

Build a brief (at least one `--anchor` is required; anchors are the facts that pin down which
person you mean):

```bash
uv run prospect-brief run "Full Name" --anchor employer="Acme Corp" --anchor school="Some University" --institution "University of Southern California" --yes
```

Outputs land in `briefs/<run_id>/`: `brief.html` (self-contained, print to PDF for a clean 2-3
pages), `brief.json`, `evidence.csv` (every extracted claim, including the ones the tool threw out
and why), and `run_report.json` (counts, time, tokens, estimated cost). Every model prompt and
response is logged under `runs/<run_id>/llm/`.

Run the pipeline on the fictional test person, with no API keys and no network:

```bash
uv run prospect-brief run "Dorian Vexley-Marsh" --anchor employer="Halcyon Reef Capital" --institution "University of Southern California" --search mock --llm fake --yes
```

Signal watch (people affiliated with an institution named in SEC filings over the last N days):

```bash
uv run prospect-brief signals --institution "University of Southern California" --days 90
```

Tests:

```bash
uv run pytest -q
```

## Safeguards

### Grounding

The writer never sees a web page. It only sees a table of short claims that have already passed
the code checks below, each with an id, and it must attach those ids to every sentence it writes.
The tool also refuses to compute anything: it reports "sold X for $Y" as stated by a source and
never estimates net worth, totals, or giving capacity.

### Citations

Every claim is extracted together with a verbatim quote, and code then checks that the quote
really appears in the cached copy of the source (allowing only for whitespace and punctuation
differences, or a near-exact fuzzy match of 92 out of 100). Code also checks that every number,
dollar amount, date, and name in the claim appears inside that quote, so a claim cannot say more
than its quote. The footnote number in the brief links to the source list, and hovering shows the
quote, publisher, date, and source tier.

### Confidence thresholds

A quote must match the source at 92 or better on a 0 to 100 partial-match scale, or the claim is
dropped; there is no partial credit. After the brief is written, code deletes any factual sentence
that does not cite a valid evidence id, and if more than 10 percent of the sentences had to be
deleted, the writer is run once more from the same evidence. Dropped claims are kept in
`evidence.csv` with the reason, so a reviewer can see what was excluded.

### Hallucination testing

The test suite runs the whole pipeline on a fictional person with a local corpus of pages that
includes planted traps: a claim whose quote is not in the source, a claim with a wrong dollar
figure, a page on a blocked domain, a page that robots.txt forbids, and a page containing a hidden
"ignore previous instructions" message. The tests assert that each trap is caught in code: the
bad claims are dropped, the forbidden pages are never fetched, and nothing from the injected
instruction reaches the brief. A property test mutates a real quote 200 times and requires that
every mutated version is rejected.

### Human-in-the-loop

The tool prints the subject, anchors, and institution and asks for confirmation before it runs
(`--yes` skips this for scripted use); full identity resolution with a printed identity card is
the next milestone. A prospect researcher is expected to click footnotes and check them, and the
signal watch stops at a list of names with a ready-to-copy command for each person: it does not
match names against Salesforce or any CRM, and this repository has no CRM access of any kind.

## Data handling

`.env`, `runs/`, `cache/`, `briefs/` and `signals/` are gitignored. No real person's data is
committed; automated tests use only the fictional corpus under `tests/fixtures/`.
