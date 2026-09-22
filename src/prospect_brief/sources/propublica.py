"""ProPublica Nonprofit Explorer API v2: foundations bearing the subject's or family's name.
GET only, no key. License: noncommercial with attribution (cited in the sources list and README).

Shapes confirmed live on Sep 21, 2026:
- /search.json?q=.. -> {total_results, organizations[] {ein, strein, name, city, state, ntee_code, have_filings}}
- /organizations/<ein>.json -> {organization{name, strein, city, state, ntee_code}, filings_with_data[] {tax_prd_yr, formtype (0=990, 1=990-EZ, 2=990-PF),
  totrevenue, totfuncexpns, totassetsend, pdf_url, 990-PF: grscontrgifts (contributions received), contrpdpbks (contributions/grants paid),
  fairmrktvaleoy; 990: totcntrbgfts}}
"""

from __future__ import annotations

import re
from urllib.parse import quote

from ..fetch import Fetcher
from ..models import Document, Evidence
from ..textnorm import normalize, unify
from .structured import code_claim, get_json, record_document

BASE = "https://projects.propublica.org/nonprofits/api/v2"
ORG_PAGE = "https://projects.propublica.org/nonprofits/organizations/{ein}"
FORMS = {0: "Form 990", 1: "Form 990-EZ", 2: "Form 990-PF"}


def _money(v) -> str | None:
    try:
        return f"${int(round(float(v))):,}"
    except (TypeError, ValueError):
        return None


def candidate_names(subject: str, card_facts: list[str], spouse: str = "") -> list[str]:
    """Search phrases: '<surname> foundation', '<full name> foundation', and any foundation named in the identity card."""
    parts = subject.split()
    out = [f"{parts[-1]} foundation", f"{subject} foundation"]
    if spouse:
        out.append(f"{spouse.split()[-1]} foundation")
    for f in card_facts:
        low = f.lower()
        if "foundation" in low or "trust" in low or "fund" in low:
            out.append(f)
    return list(dict.fromkeys(out))


def _loose(s: str) -> str:
    """Casefolded text that keeps hyphens, so 'Marsh Family Foundation' is not found inside
    'Vexley-Marsh Family Foundation'."""
    s = unify(s).casefold()
    s = re.sub(r"[^\w\s\-']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _phrase_in(phrase: str, text: str) -> bool:
    return re.search(r"(?<![\w\-])" + re.escape(_loose(phrase)) + r"(?![\w\-])", _loose(text)) is not None


def _pos(word: str, text: str) -> int:
    m = re.search(r"(?<![\w\-])" + re.escape(_loose(word)) + r"(?![\w\-])", _loose(text))
    return m.start() if m else -1


def foundation_is_subjects(org_name: str, subject: str, spouse: str, mentioned: list[str]) -> bool:
    """Keep a foundation only if its name carries the subject's surname (whole word, hyphens intact)
    AND either the subject's given name BEFORE the surname ("MacKenzie Scott Foundation", not
    "Scott R Mackenzie Foundation"), the spouse's given name before the surname, or the foundation
    is named in already-collected evidence or the identity card."""
    surname = subject.split()[-1]
    given = subject.split()[0]
    s_pos = _pos(surname, org_name)
    if s_pos < 0:
        return False
    g_pos = _pos(given, org_name)
    if 0 <= g_pos < s_pos:
        return True
    if spouse:
        sp_pos = _pos(spouse.split()[0], org_name)
        if 0 <= sp_pos < s_pos:
            return True
    return any(_phrase_in(org_name, m) for m in mentioned)


def render_org(org: dict, filings: list[dict]) -> str:
    lines = [f"{org['name']} (EIN {org.get('strein') or org.get('ein')}), {org.get('city', '').title()}, {org.get('state', '')}. Data: IRS filings via ProPublica Nonprofit Explorer."]
    for f in sorted(filings, key=lambda x: -int(x.get("tax_prd_yr") or 0)):
        form = FORMS.get(f.get("formtype"), "Form 990")
        yr = f.get("tax_prd_yr")
        bits = []
        if _money(f.get("totassetsend")):
            bits.append(f"total assets at year end {_money(f['totassetsend'])}")
        recv = f.get("grscontrgifts") if f.get("formtype") == 2 else f.get("totcntrbgfts")
        if _money(recv):
            bits.append(f"contributions received {_money(recv)}")
        paid = f.get("contrpdpbks") if f.get("formtype") == 2 else None
        if _money(paid):
            bits.append(f"contributions and grants paid {_money(paid)}")
        if _money(f.get("totfuncexpns")):
            bits.append(f"total expenses {_money(f['totfuncexpns'])}")
        line = f"{org['name']} {form} for tax year {yr}: " + "; ".join(bits) + "."
        if f.get("pdf_url"):
            line += f" Filing PDF: {f['pdf_url']}"
        lines.append(line)
    return "\n".join(lines)


async def collect_propublica(fetcher: Fetcher, *, subject: str, spouse: str, card_facts: list[str], mentioned: list[str], max_orgs: int = 3, years: int = 5, log=print) -> tuple[list[Document], list[Evidence], list[str]]:
    docs: list[Document] = []
    ev: list[Evidence] = []
    notes: list[str] = []
    seen: set[int] = set()
    kept = 0
    unconfirmed: list[str] = []
    for phrase in candidate_names(subject, card_facts, spouse)[:4]:
        data = await get_json(fetcher, f"{BASE}/search.json?q={quote(phrase)}")
        for org in (data or {}).get("organizations", [])[:10]:
            ein = int(org["ein"])
            if ein in seen:
                continue
            seen.add(ein)
            if not foundation_is_subjects(org["name"], subject, spouse, mentioned):
                if normalize(subject).split()[-1] in normalize(org["name"]):  # surname-ish match, worth telling the researcher
                    unconfirmed.append(f"{org['name']} ({org.get('city', '').title()}, {org.get('state', '')})")
                continue
            detail = await get_json(fetcher, f"{BASE}/organizations/{ein}.json")
            if not detail:
                continue
            filings = [f for f in detail.get("filings_with_data", []) if f.get("tax_prd_yr")]
            filings = sorted(filings, key=lambda x: -int(x["tax_prd_yr"]))[:years]
            o = detail["organization"]
            text = render_org(o, filings)
            url = ORG_PAGE.format(ein=ein)
            doc = record_document(fetcher, url=url, title=f"{o['name']}: IRS filings via ProPublica Nonprofit Explorer", text=text, publisher="projects.propublica.org", published_date=(f"{filings[0]['tax_prd_yr']}" if filings else None))
            docs.append(doc)
            kept += 1
            i = 1
            for f in filings:
                form = FORMS.get(f.get("formtype"), "Form 990")
                line = next((ln for ln in text.splitlines() if ln.startswith(f"{o['name']} {form} for tax year {f['tax_prd_yr']}:")), None)
                if not line:
                    continue
                q = line.split(" Filing PDF:")[0]
                for label, key in (("total assets at year end", "totassetsend"), ("contributions received", "grscontrgifts" if f.get("formtype") == 2 else "totcntrbgfts"), ("contributions and grants paid", "contrpdpbks")):
                    m = _money(f.get(key))
                    if m and f"{label} {m}" in q:
                        ev.append(code_claim(doc, id=f"pp{kept}-{i}", claim=f"{o['name']} reported {label} of {m} for tax year {f['tax_prd_yr']} on its {form}.", section="philanthropy", claim_type="foundation", quote=q))
                        i += 1
            if kept >= max_orgs:
                break
        if kept >= max_orgs:
            break
    if unconfirmed:
        notes.append("ProPublica: foundations with a matching surname that could not be tied to the subject (not used): " + "; ".join(unconfirmed[:6]) + ".")
    log(f"[propublica] {kept} foundations used, {len(ev)} claims")
    return docs, ev, notes
