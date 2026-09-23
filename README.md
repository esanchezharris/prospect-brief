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
cp .env.example .env   # then fill in TAVILY_API_KEY, CONTACT_EMAIL, and the key for your LLM provider
```

The LLM provider is one setting: `llm.provider` in `config/default.yaml` (`anthropic` or `openai`;
the env var `PROSPECT_LLM` overrides it, and every command accepts `--llm`). Each provider is one
class in `src/prospect_brief/llm.py` with the same interface, logging and cost accounting, and the
model ids and prices per provider live in the config. Azure OpenAI would be a third class there.

## Demo page

```bash
uv run prospect-brief serve
```

Opens http://127.0.0.1:8765: the signal watch, a brief builder with live progress, a trust check on
the fictional test subject, and the list of previous runs. The page is a client of the same
pipeline functions the CLI calls; nothing runs there that the CLI can't run.

### 5-minute demo script

Before the meeting: start the server, open the page, and start the real brief for your chosen
subject so it is already streaming when you begin (about 5 minutes and about $1). Have the
previous run's brief open in another tab as a fallback.

1. **Signal watch (1 min).** Click *Load latest* (or *Run signal watch* if you have 90 seconds).
   Point at one row: name, company, event, the verbatim affiliation quote, and the filing link.
   Click *Build brief* to show the prefilled form. Say: production would match these names against
   Salesforce; this demo stops at the list on purpose.
2. **Trust check (1 min).** Click *Run on the fictional test subject*. In a few seconds the traps
   appear with counts and reasons: the fabricated quote, the wrong dollar figure, the claim its quote
   does not support, the hidden "ignore previous instructions", and the namesake dentist set aside.
   Say: these checks run in code, not by asking the model whether it was honest.
3. **The real brief (2 min).** Back in panel 2, the stages have landed. Open the brief. Hover two
   footnotes to show the quotes; click one through to the live page. Scroll to the conflicts shown
   side by side, then the numbered sources with tiers, then the verification table at the bottom.
   Print preview: a clean 2-3 pages.
4. **Safeguards (1 min).** The five headings below, in plain English, plus the time and cost line.

If asked: identity is resolved and shown before any research; net-worth figures are dropped even
when a publisher prints them; tier-3 sources are marked °; every dropped claim and its reason is in
`evidence.csv`; nothing is committed about real people; no CRM access exists in this repo.

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

Run the pipeline on the fictional test person, with no API keys and no network (fixture runs write
under `.fixture/` so they never touch the real cache or outputs):

```bash
uv run prospect-brief run "Dorian Vexley-Marsh" --anchor employer="Halcyon Reef Capital" --institution "University of Southern California" --search mock --llm fake --yes
```

Signal watch (people affiliated with an institution named in SEC filings over the last N days):

```bash
uv run prospect-brief signals --institution "University of Southern California" --days 90
```

Eval (recall against known facts, plus a 25-claim sample for hand precision audit). Copy
`eval/TEMPLATE.yaml` to `eval/<slug>.yaml` with 10 known public facts, run the brief, then:

```bash
uv run prospect-brief eval <slug>
```

It prints a recall table and writes `eval/out/<slug>-precision-audit.csv` (mark each sampled claim
correct or not) and `eval/out/<slug>-eval.json`. The fictional subject's file is
`eval/dorian-vexley-marsh.yaml`; files for real people go in `eval/real/`, which is gitignored.

- `--all-runs` prints one line per brief for the subject (recall, verified claims, sources, time,
  cost, writer model) to see run-to-run variance.
- `--judge` asks the author model whether each of the 25 sampled claims is supported by its quote
  on its own, as a precision estimate (about 5 cents); `--judge-all` does it for every verified
  claim (about 50 cents). The hand audit in the CSV remains the ground truth.

Measured on two public-figure subjects in September 2026: recall 7 to 9 of 11 and 8 of 10 known
facts across eight runs. Model-judged precision was 89% and 92% of verified claims; judging every
claim showed the misses were quote fragments that never said who did the thing, so a code rule now
requires the quote to reference the subject. On the next runs precision was 99% (217 of 219) and 95%
(249 of 261) with recall unchanged. The entailment checker was then compared on the same 261
claims: Haiku 4.5 re-dropped 31 (4 of them real misses) and Sonnet 5 dropped 16 (5 real), so the
checker now runs on Sonnet 5 for about ten cents more per brief.

Tests:

```bash
uv run pytest -q
```

## Safeguards

### Grounding

The writer never sees a web page or a filing. It only sees a table of short claims that have already passed
the code checks below, each with an id, and it must attach those ids to every sentence it writes.
The tool also refuses to compute anything: it reports "sold X for $Y" as stated by a source and
never estimates net worth, totals, or giving capacity.

### Citations

Every claim is extracted together with a verbatim quote, and code then checks that the quote
really appears in the cached copy of the source (allowing only for whitespace and punctuation
differences, or a near-exact fuzzy match of 92 out of 100). Code also checks that every number,
dollar amount, date, and name in the claim appears inside that quote, so a claim cannot say more
than its quote. The footnote number in the brief links to the source list, and hovering shows the
quote, publisher, date, and source tier (1 = primary or official such as SEC, IRS data, or the
institution; 2 = established news; 3 = everything else, marked with a ° in the text). The same
fact from two independent sites is marked corroborated; a "current" role from a source older than
18 months is written "as of" that date.

### Confidence thresholds

A quote must match the source at 92 or better on a 0 to 100 partial-match scale, or the claim is
dropped; there is no partial credit. A document is only used if it contains at least one of the
confirmed identity anchors (employer, city, school, spouse named in joint philanthropy), otherwise
every claim from it is set aside and the document is listed under "Possibly a different person".
A small, cheap model then answers one question per surviving claim, "does this quote support this
claim: supports, partially, or no", and only "supports" passes. After the brief is written, code
deletes any factual sentence that does not cite a valid evidence id, and if more than 10 percent
of the sentences had to be deleted, the writer is run once more. Dropped claims stay in
`evidence.csv` with the reason.

### Hallucination testing

The test suite runs the whole pipeline on a fictional person with a local corpus of pages that
includes planted traps: a claim whose quote is not in the source, a claim with a wrong dollar
figure, a claim whose quote is real but does not actually say what the claim says, a page about a
different person with the same name, two pages that disagree on a gift amount, a page on a blocked
domain, a page that robots.txt forbids, and a page containing a hidden "ignore previous
instructions" message. The tests assert that each trap is caught in code: the
bad claims are dropped, the namesake's facts never enter the brief, the disagreement is shown side
by side rather than silently resolved, the forbidden pages are never fetched, and nothing from
the injected instruction reaches the brief. A property test mutates a real quote 200 times and requires that
every mutated version is rejected.

### Human-in-the-loop

Before any research, the tool looks the person up (a few searches plus Wikipedia and Wikidata),
prints an identity card with the anchor facts, their sources, and every namesake it found, and
asks you to confirm it is the right person, in the terminal or on the demo page (`--yes` and the
page's checkbox skip the pause for scripted use). If the
anchors cannot separate the person from a namesake, it stops and asks for another anchor instead
of guessing. A prospect researcher is expected to click footnotes and check them, and the
signal watch stops at a list of names with a ready-to-copy command for each person: it does not
match names against Salesforce or any CRM, and this repository has no CRM access of any kind.

## Sources

- Web and news via the search provider (Tavily by default; the provider is one file, `search.py`).
- The institution's own domain (configured in `config/default.yaml` under `institution_domains`).
- SEC EDGAR: when the subject has an EDGAR reporting-owner record whose filings are for a company
  named in the confirmed anchors, Forms 3 and 4 (holdings and transactions) are rendered into plain
  sentences and cited line by line, and the issuer's latest DEF 14A proxy statement goes through the
  normal extraction and verification path.
- ProPublica Nonprofit Explorer (IRS Form 990 and 990-PF data) for foundations that carry the
  subject's name. A foundation is used only if its name contains the subject's surname as a whole
  word plus the given name, the spouse's name (when the spouse appears in joint public
  philanthropy), or the foundation is named in other collected evidence; look-alike names are
  listed in the run notes and never used. **Attribution:** nonprofit financial data is from IRS
  filings via [ProPublica Nonprofit Explorer](https://projects.propublica.org/nonprofits/), used
  under its noncommercial terms with attribution; the brief says so in its sources section.

## Data handling

`.env`, `runs/`, `cache/`, `briefs/` and `signals/` are gitignored. No real person's data is
committed; automated tests use only the fictional corpus under `tests/fixtures/`.

## License

MIT. See [LICENSE](LICENSE). Nonprofit financial data comes from IRS filings via ProPublica Nonprofit Explorer under its own noncommercial-with-attribution terms.
