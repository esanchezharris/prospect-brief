"""SEC EDGAR for the subject as an insider: Forms 3/4 (holdings, transactions) and the issuer's
latest DEF 14A (bio, compensation). Only runs when the subject has an EDGAR reporting-owner
record whose Form 4 issuers match a confirmed anchor.

Shapes confirmed live on Sep 21, 2026:
- https://efts.sec.gov/LATEST/search-index?keysTyped=<name> -> hits.hits[] {_id: <cik>, _source.entity: "COOK TIMOTHY D"}
- https://data.sec.gov/submissions/CIK##########.json -> {name, entityType, filings.recent{accessionNumber[], filingDate[], form[], primaryDocument[]}}
- Form 4 primaryDocument "xslF345X06/form4.xml"; raw XML at the same folder without the xsl prefix, with
  issuerName, issuerTradingSymbol, rptOwnerName, officerTitle, isDirector, transactionDate, transactionCode,
  transactionShares, transactionPricePerShare, sharesOwnedFollowingTransaction, securityTitle.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from rapidfuzz import fuzz

from ..fetch import Fetcher
from ..models import Document, Evidence
from ..textnorm import normalize
from .structured import code_claim, get_json, get_text, record_document

ENTITY_URL = "https://efts.sec.gov/LATEST/search-index?keysTyped={q}"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{adsh}/{doc}"
_TX_CODES = {"P": "open-market purchase", "S": "open-market sale", "A": "grant or award", "M": "option exercise", "F": "tax withholding", "G": "gift", "D": "disposition to issuer", "J": "other"}


def name_matches_entity(subject: str, entity: str) -> bool:
    """EDGAR entity names look like 'COOK TIMOTHY D' (surname first). Require the surname tokens first
    and the given name next; hyphenated surnames become two tokens after normalization."""
    words = subject.split()
    if len(words) < 2:
        return False
    surname = normalize(words[-1]).split()
    given = normalize(words[0])
    ent = normalize(entity).split()
    return len(ent) > len(surname) and ent[: len(surname)] == surname and ent[len(surname)] == given


async def find_person_cik(fetcher: Fetcher, subject: str) -> list[tuple[str, str]]:
    data = await get_json(fetcher, ENTITY_URL.format(q=normalize(subject).replace(" ", "%20")))
    out = []
    for h in (data or {}).get("hits", {}).get("hits", []):
        ent = h.get("_source", {}).get("entity", "")
        if name_matches_entity(subject, ent):
            out.append((h["_id"], ent))
    return out


def _txt(el, tag: str) -> str:
    node = el.find(f".//{tag}")
    if node is None:
        return ""
    v = node.find("value")
    return (v.text if v is not None and v.text else node.text or "").strip()


def parse_form4(xml: str) -> dict | None:
    try:
        root = ET.fromstring(xml.encode("utf-8", errors="replace"))
    except ET.ParseError:
        return None
    owner = root.find(".//reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    roles = []
    if rel is not None:
        if _txt(rel, "isDirector") in ("1", "true"):
            roles.append("director")
        if _txt(rel, "isOfficer") in ("1", "true"):
            roles.append(_txt(rel, "officerTitle") or "officer")
        if _txt(rel, "isTenPercentOwner") in ("1", "true"):
            roles.append("10% owner")
    txs = []
    for t in root.findall(".//nonDerivativeTransaction"):
        txs.append({
            "date": _txt(t, "transactionDate"), "code": _txt(t, "transactionCode"), "shares": _txt(t, "transactionShares"),
            "price": _txt(t, "transactionPricePerShare"), "after": _txt(t, "sharesOwnedFollowingTransaction"), "security": _txt(t, "securityTitle"),
        })
    holdings = []
    for h in root.findall(".//nonDerivativeHolding"):
        holdings.append({"shares": _txt(h, "sharesOwnedFollowingTransaction"), "security": _txt(h, "securityTitle")})
    return {
        "issuer": _txt(root, "issuerName"), "ticker": _txt(root, "issuerTradingSymbol"), "owner": _txt(root, "rptOwnerName"),
        "period": _txt(root, "periodOfReport"), "roles": roles, "transactions": txs, "holdings": holdings, "form": _txt(root, "documentType"),
    }


def _fmt_num(s: str) -> str:
    try:
        f = float(s)
        return f"{int(f):,}" if f.is_integer() else f"{f:,.2f}"
    except ValueError:
        return s


def render_form4(p: dict, filed: str, url: str) -> str:
    """Render the filing as plain sentences. Each line is self-contained (issuer, form, filing date) so a
    claim built from it can quote that one line and still pass check (b)."""
    form = p["form"] or "4"
    tag = f"(SEC Form {form} filed {filed})"
    lines = [f"SEC Form {form} filed {filed} by {p['owner']} for {p['issuer']}" + (f" ({p['ticker']})" if p["ticker"] else "") + "."]
    if p["roles"]:
        lines.append(f"{p['owner']} is reported as {', '.join(p['roles'])} of {p['issuer']} in SEC Form {form} filed {filed}.")
    for t in p["transactions"]:
        kind = _TX_CODES.get(t["code"], f"code {t['code']}")
        s = f"On {t['date']}, {kind} of {_fmt_num(t['shares'])} shares of {p['issuer']} {t['security']}"
        if t["price"] and float(t["price"] or 0) > 0:
            s += f" at ${_fmt_num(t['price'])} per share"
        if t["after"]:
            s += f"; {_fmt_num(t['after'])} shares owned following the transaction"
        lines.append(f"{s} {tag}.")
    for h in p["holdings"]:
        lines.append(f"Holds {_fmt_num(h['shares'])} shares of {p['issuer']} {h['security']} {tag}.")
    lines.append(f"Source: {url}")
    return "\n".join(lines)


def _issuer_matches(issuer: str, anchors: dict[str, str], card_orgs: list[str]) -> bool:
    cands = [v for k, v in anchors.items() if k in ("employer", "company")] + card_orgs
    return any(fuzz.token_set_ratio(normalize(issuer), normalize(c)) >= 80 for c in cands)


async def collect_edgar(fetcher: Fetcher, *, subject: str, anchors: dict[str, str], card_orgs: list[str], max_filings: int = 6, log=print) -> tuple[list[Document], list[Evidence], list[str], list[dict]]:
    """Returns (documents, evidence, notes, def14a_targets). def14a_targets are issuer CIK/name pairs for the proxy step."""
    docs: list[Document] = []
    ev: list[Evidence] = []
    notes: list[str] = []
    targets: list[dict] = []
    people = await find_person_cik(fetcher, subject)
    if not people:
        notes.append("SEC EDGAR: no reporting-owner record found for the subject; insider filings skipped.")
        return docs, ev, notes, targets
    n = 0
    for cik, entity in people[:3]:
        sub = await get_json(fetcher, SUBMISSIONS_URL.format(cik=int(cik)))
        if not sub:
            continue
        rec = sub.get("filings", {}).get("recent", {})
        rows = [dict(zip(rec.keys(), vals)) for vals in zip(*rec.values())] if rec else []
        rows = [r for r in rows if r.get("form") in ("3", "4", "4/A", "3/A")]
        if not rows:
            continue
        for r in rows[:max_filings]:
            adsh = r["accessionNumber"].replace("-", "")
            doc_name = re.sub(r"^xsl[^/]*/", "", r["primaryDocument"])
            url = ARCHIVE.format(cik=int(cik), adsh=adsh, doc=doc_name)
            xml = await get_text(fetcher, url)
            if not xml:
                continue
            parsed = parse_form4(xml)
            if not parsed or not parsed["issuer"]:
                continue
            if not _issuer_matches(parsed["issuer"], anchors, card_orgs):
                notes.append(f"SEC EDGAR: Form {r['form']} for {parsed['issuer']} by {entity} not tied to a confirmed anchor; skipped.")
                continue
            text = render_form4(parsed, r["filingDate"], url)
            doc = record_document(fetcher, url=url, title=f"SEC Form {r['form']}: {parsed['owner']} / {parsed['issuer']} ({r['filingDate']})", text=text, publisher="sec.gov", published_date=r["filingDate"])
            docs.append(doc)
            n += 1
            pfx = f"sec{n}-"
            i = 1
            if parsed["roles"]:
                q = text.splitlines()[1]
                ev.append(code_claim(doc, id=f"{pfx}{i}", claim=f"{subject} is reported as {', '.join(parsed['roles'])} of {parsed['issuer']} in SEC Form {parsed['form'] or r['form']} filed {r['filingDate']}.", section="career", claim_type="board_seat" if "director" in parsed["roles"] else "role", quote=q))
                i += 1
            for t in parsed["transactions"]:
                line = next((ln for ln in text.splitlines() if ln.startswith(f"On {t['date']}, ")), None)
                if not line:
                    continue
                kind = _TX_CODES.get(t["code"], f"code {t['code']}")
                claim = f"{subject} reported a {kind} of {_fmt_num(t['shares'])} shares of {parsed['issuer']} {t['security']} on {t['date']}"
                if t["price"] and float(t["price"] or 0) > 0:
                    claim += f" at ${_fmt_num(t['price'])} per share"
                ev.append(code_claim(doc, id=f"{pfx}{i}", claim=claim + ".", section="wealth", claim_type="transaction", quote=line))
                i += 1
                if t["after"]:
                    ev.append(code_claim(doc, id=f"{pfx}{i}", claim=f"{subject} owned {_fmt_num(t['after'])} shares of {parsed['issuer']} {t['security']} following the {t['date']} transaction.", section="wealth", claim_type="holding", quote=line))
                    i += 1
            for h in parsed["holdings"]:
                line = next((ln for ln in text.splitlines() if ln.startswith(f"Holds {_fmt_num(h['shares'])} shares")), None)
                if not line:
                    continue
                ev.append(code_claim(doc, id=f"{pfx}{i}", claim=f"{subject} holds {_fmt_num(h['shares'])} shares of {parsed['issuer']} {h['security']} per SEC Form {parsed['form'] or r['form']} filed {r['filingDate']}.", section="wealth", claim_type="holding", quote=line))
                i += 1
            targets.append({"issuer": parsed["issuer"], "ticker": parsed["ticker"], "adsh_prefix": r["accessionNumber"].split("-")[0]})
    log(f"[edgar] {n} insider filings used, {len(ev)} claims")
    return docs, ev, notes, targets


async def collect_def14a(fetcher: Fetcher, *, targets: list[dict], subject: str, log=print) -> tuple[list[Document], list[str]]:
    """Fetch the latest DEF 14A for each issuer named in the subject's Form 4s. Returns documents
    (full text, sliced later by the extractor) for the normal model extraction path."""
    docs: list[Document] = []
    notes: list[str] = []
    seen: set[str] = set()
    for t in targets:
        issuer = t["issuer"]
        if issuer in seen:
            continue
        seen.add(issuer)
        data = await get_json(fetcher, ENTITY_URL.format(q=normalize(issuer).replace(" ", "%20")))
        hits = (data or {}).get("hits", {}).get("hits", [])
        cik = next((h["_id"] for h in hits if fuzz.token_set_ratio(normalize(h["_source"].get("entity", "")), normalize(issuer)) >= 85), None)
        if not cik:
            notes.append(f"SEC EDGAR: could not resolve issuer CIK for {issuer}; proxy statement skipped.")
            continue
        sub = await get_json(fetcher, SUBMISSIONS_URL.format(cik=int(cik)))
        rec = (sub or {}).get("filings", {}).get("recent", {})
        rows = [dict(zip(rec.keys(), vals)) for vals in zip(*rec.values())] if rec else []
        proxy = next((r for r in rows if r.get("form") == "DEF 14A"), None)
        if not proxy:
            notes.append(f"SEC EDGAR: no DEF 14A on file for {issuer}.")
            continue
        url = ARCHIVE.format(cik=int(cik), adsh=proxy["accessionNumber"].replace("-", ""), doc=proxy["primaryDocument"])
        doc = await fetcher.fetch(url, full_text=True)
        if doc.ok:
            doc.published_date = doc.published_date or proxy["filingDate"]
            doc.title = doc.title or f"{issuer} DEF 14A ({proxy['filingDate']})"
            docs.append(doc)
    log(f"[edgar] {len(docs)} proxy statements fetched")
    return docs, notes
