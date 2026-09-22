"""Eval harness (M4): recall of a brief's verified claims against a list of known facts, plus a
random sample of verified claims exported for hand precision audit."""

from __future__ import annotations

import csv
import json
import random
import re
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from .models import Brief
from .textnorm import normalize
from .verify import extract_specifics


def load_eval(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    for key in ("subject", "facts"):
        if key not in data:
            raise ValueError(f"{path} is missing '{key}'")
    return data


def fact_matches(fact: str, claim: str, *, min_ratio: int = 70) -> tuple[bool, int]:
    """A known fact is recalled when a verified claim has similar wording AND contains every
    number, amount and date in the fact."""
    ratio = int(fuzz.token_set_ratio(normalize(fact), normalize(claim)))
    if ratio < min_ratio:
        return False, ratio
    spec = extract_specifics(fact)
    cn = normalize(claim).replace(",", "")
    for item in spec["money"] + spec["numbers"]:
        if normalize(item).replace(",", "") not in cn:
            return False, ratio
    for d in spec["dates"]:
        # dates are compared by year: "March 2025" in the fact accepts "March 3, 2025" or "2025-03-03" in the claim
        years = re.findall(r"\b(?:19|20)\d{2}\b", d)
        if any(y not in cn for y in years):
            return False, ratio
    return True, ratio


def evaluate(brief: Brief, facts: list[str], *, sample_size: int = 25, seed: int = 0, min_ratio: int = 70) -> dict:
    verified = [e for e in brief.evidence if e.status == "verified"]
    rows = []
    for fact in facts:
        best = (False, 0, None)
        for e in verified:
            ok, ratio = fact_matches(fact, e.claim, min_ratio=min_ratio)
            if (ok, ratio) > (best[0], best[1]):
                best = (ok, ratio, e)
        rows.append({"fact": fact, "recalled": best[0], "score": best[1], "claim_id": best[2].id if best[2] else "", "claim": best[2].claim if best[2] else ""})
    recalled = sum(1 for r in rows if r["recalled"])
    rng = random.Random(seed)
    sample = rng.sample(verified, min(sample_size, len(verified)))
    return {
        "subject": brief.subject, "run_id": brief.report.run_id, "facts": len(facts), "recalled": recalled,
        "recall": round(recalled / len(facts), 3) if facts else None, "verified_claims": len(verified),
        "rows": rows, "precision_sample": [{"id": e.id, "claim": e.claim, "supporting_quote": e.supporting_quote, "source_url": e.source_url, "publisher": e.publisher, "tier": e.source_tier} for e in sample],
        "run_report": brief.report.model_dump(mode="json"),
    }


def write_outputs(result: dict, out_dir: Path, slug: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    audit = out_dir / f"{slug}-precision-audit.csv"
    with open(audit, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "claim", "supporting_quote", "source_url", "publisher", "tier", "correct? (Y/N)", "notes"])
        for r in result["precision_sample"]:
            w.writerow([r["id"], r["claim"], r["supporting_quote"], r["source_url"], r["publisher"], r["tier"], "", ""])
    summary = out_dir / f"{slug}-eval.json"
    summary.write_text(json.dumps({k: v for k, v in result.items() if k != "precision_sample"}, indent=1))
    return audit, summary


def format_table(result: dict) -> str:
    lines = [f"Eval: {result['subject']}  (run {result['run_id']})", f"Recall: {result['recalled']}/{result['facts']} known facts" + (f" = {result['recall']:.0%}" if result["recall"] is not None else ""), ""]
    lines.append(f"{'#':>2}  {'hit':3}  {'score':>5}  fact  ->  matched claim")
    for i, r in enumerate(result["rows"], 1):
        lines.append(f"{i:>2}  {'yes' if r['recalled'] else 'no ':3}  {r['score']:>5}  {r['fact'][:60]}  ->  {r['claim'][:60] if r['claim'] else '-'}")
    rr = result["run_report"]
    c = rr.get("counts", {})
    lines += ["", f"Verified claims: {result['verified_claims']}   extracted: {c.get('claims_extracted', '?')}   dropped: {c.get('claims_dropped', '?')}   set aside (identity): {c.get('claims_flagged_identity', 0)}",
              f"Run time: {rr.get('wall_seconds', '?')}s   cost: ${rr.get('cost_usd', 0):.2f}   searches: {rr.get('searches_run', '?')}"]
    return "\n".join(lines)
