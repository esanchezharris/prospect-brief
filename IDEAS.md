# Ideas (out of spec, not built)

Things noticed while building that are outside SPEC.md. Nothing here is implemented.

- **Azure OpenAI provider** (`llm.py`): next provider per the context update; needs a structured-output path equivalent to `messages.parse`.
- **Brave / Exa search provider** (`search.py`): one class each; the interface is `search(query, max_results, topic, include_domains)`.
- **`--no-model` dry run for `signals`**: search EDGAR and fetch filings without extraction, to warm the cache cheaply before a demo (done ad hoc tonight with a script, not a flag).
- **Batch extraction with the Message Batches API** (50% cheaper) for non-interactive eval runs.
- **Wikidata entity id on the identity card** as a stable handle for namesake separation.
- **Form 4 "G" (gift) transactions** could feed the philanthropy section explicitly; today they land under wealth as transactions.
- **10-K** in the signal watch (spec says only if time allows).
- **Recall per section** in the eval table, and a "conflicts resolved by the researcher" column in the precision audit.
