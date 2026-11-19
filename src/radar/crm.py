"""Salesforce CRM extract -> the compact file the Portfolio browser reads.

The extract (an Excel workbook such as data/CL_data_19.11.xlsx) has three sheets:
accounts (Query1), opportunities (Query2) and the product catalogue (Query3).
It holds no opportunity-to-product link, so three things are taken from it:

* **product families**: the CRM files products under families named like the
  curated value propositions ("Next Gen Secured Connectivity", ...). That is
  the offer -> value-proposition link Expresso lacks; ``radar.portfolio`` places
  a product's Expresso offer (matched by name) under the value propositions
  whose ``crm_families`` list that family.
* **accounts**: industry, region, segment and won / open opportunity figures per
  account, matched to Expresso customer references by company name.
* **SVP figures**: opportunity, win and pipeline totals per CRM SVP, shown on
  the SVPs whose ``crm_svp`` names it.

Reading the workbook takes minutes, so it is done once, here:

    python -m radar.crm data/CL_data_19.11.xlsx      # writes data/crm_portfolio.json

Personal data is left out on purpose: no owner, manager or contact names, no
phone numbers, no free-text descriptions. Amounts are in euro (the extract's
"TCV converted in Euro").
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

CRM_PATH = PROJECT_ROOT / "data" / "crm_portfolio.json"

ACCOUNT_SHEET, OPPORTUNITY_SHEET, PRODUCT_SHEET = "Query1", "Query2", "Query3"
NO_FAMILY = {"", "to be defined"}
# Stages that are not real outcomes.
IGNORED_STAGES = {"Closed Duplicate"}

_LEGAL = re.compile(
    r"\b(sa|sas|sasu|sarl|ag|gmbh|ltd|limited|inc|llc|plc|bv|nv|spa|srl|co|corp|corporation|company"
    r"|group|groupe|holding|holdings|international|the|pte|pvt|private|ab|oy|kk)\b")


def company_key(name: str | None) -> str:
    """Comparison key for company names: accents, case, punctuation and legal forms ignored."""
    from .portfolio import norm  # local import: portfolio imports this module too
    return re.sub(r"\s+", " ", _LEGAL.sub(" ", norm(name))).strip()


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _s(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "n/a") else text


def _rows(ws, columns: list[str]):
    """Yield dicts of just `columns` from a read-only worksheet."""
    it = ws.iter_rows(values_only=True)
    header = [str(h) if h is not None else "" for h in next(it)]
    idx = {c: header.index(c) for c in columns if c in header}
    for row in it:
        yield {c: (row[i] if i < len(row) else None) for c, i in idx.items()}


def extract(xlsx: Path) -> dict[str, Any]:
    import openpyxl  # only needed for the import, not to serve the app

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "Workbook contains no default style"
        wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)

    # ------------------------------------------------------------ products
    products = []
    for r in _rows(wb[PRODUCT_SHEET], ["Product_Name", "Product_Family", "Active", "Product_Domain",
                                       "Sub_Domain", "Catalogue"]):
        name, family = _s(r["Product_Name"]), _s(r["Product_Family"])
        if not name or family.lower() in NO_FAMILY:
            continue
        products.append({"name": name, "family": family, "active": _s(r["Active"]).lower() == "true",
                         "domain": _s(r["Product_Domain"]), "sub_domain": _s(r["Sub_Domain"]),
                         "catalogue": _s(r["Catalogue"])})

    # ------------------------------------------------------- opportunities
    per_account: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "won": 0, "won_tcv_eur": 0.0, "lost": 0, "open": 0, "open_tcv_eur": 0.0, "last_won": ""})
    per_svp: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "opportunities": 0, "won": 0, "lost": 0, "won_tcv_eur": 0.0, "open": 0, "open_tcv_eur": 0.0})
    extracted = ""
    for r in _rows(wb[OPPORTUNITY_SHEET], ["Account_ID", "Stage", "Closed", "svp", "TCV converted in Euro",
                                            "Actual_Close_Date", "Close_Date", "refresh timestamp"]):
        stage = _s(r["Stage"])
        if not stage or stage in IGNORED_STAGES:
            continue
        extracted = max(extracted, _s(r["refresh timestamp"])[:10])
        tcv = _num(r["TCV converted in Euro"])
        acc, svp = per_account[_s(r["Account_ID"])], per_svp[_s(r["svp"]) or "(none)"]
        svp["opportunities"] += 1
        if stage == "Closed Won":
            acc["won"] += 1; acc["won_tcv_eur"] += tcv
            svp["won"] += 1; svp["won_tcv_eur"] += tcv
            when = _s(r["Actual_Close_Date"])[:10] or _s(r["Close_Date"])[:10]
            acc["last_won"] = max(acc["last_won"], when)
        elif stage in ("Closed Lost", "Closed Cancelled"):
            acc["lost"] += 1; svp["lost"] += 1
        elif _s(r["Closed"]).lower() != "true":
            acc["open"] += 1; acc["open_tcv_eur"] += tcv
            svp["open"] += 1; svp["open_tcv_eur"] += tcv

    # ------------------------------------------------------------ accounts
    accounts = []
    for r in _rows(wb[ACCOUNT_SHEET], ["Account_ID", "Account_Name", "Legal_Company_Name", "CORE_Account_Name",
                                       "Ultimate_Parent_Name", "Account_Record_Type", "Industry_Sector",
                                       "Industry_Description", "Account_Region", "Account_Country",
                                       "Account_Cluster", "Sales_Segment", "Account_Status", "refresh timestamp"]):
        aid = _s(r["Account_ID"])
        if not aid:
            continue
        names = sorted({n for n in (_s(r[c]) for c in ("Account_Name", "Legal_Company_Name", "CORE_Account_Name",
                                                       "Ultimate_Parent_Name")) if n})
        figures = per_account.get(aid, {})
        accounts.append({
            "id": aid,
            "name": _s(r["Account_Name"]) or (names[0] if names else ""),
            "keys": sorted({k for k in map(company_key, names) if len(k) >= 3}),
            "type": _s(r["Account_Record_Type"]),            # Customer / Prospect
            "status": _s(r["Account_Status"]),
            "industry": _s(r["Industry_Sector"]) or _s(r["Industry_Description"]),
            "region": _s(r["Account_Region"]),
            "country": _s(r["Account_Country"]),
            "cluster": _s(r["Account_Cluster"]),
            "segment": _s(r["Sales_Segment"]),
            "won": figures.get("won", 0),
            "won_tcv_eur": round(figures.get("won_tcv_eur", 0.0)),
            "lost": figures.get("lost", 0),
            "open": figures.get("open", 0),
            "open_tcv_eur": round(figures.get("open_tcv_eur", 0.0)),
            "last_won": figures.get("last_won") or None,
        })
        extracted = max(extracted, _s(r["refresh timestamp"])[:10])

    for s in per_svp.values():
        s["won_tcv_eur"] = round(s["won_tcv_eur"]); s["open_tcv_eur"] = round(s["open_tcv_eur"])
        decided = s["won"] + s["lost"]
        s["win_rate"] = round(s["won"] / decided, 3) if decided else None

    return {
        "source": xlsx.name,
        "extracted": extracted or None,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "products": products,
        "accounts": accounts,
        "svps": dict(per_svp),
        "counts": {"products_with_family": len(products), "accounts": len(accounts),
                   "opportunities": sum(s["opportunities"] for s in per_svp.values())},
    }


def load(path: Path = CRM_PATH) -> dict[str, Any] | None:
    """The CRM file if one has been imported, else None (the browser works without it)."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m radar.crm <extract.xlsx> [out.json]")
        return 2
    xlsx = Path(args[0])
    out = Path(args[1]) if len(args) > 1 else CRM_PATH
    data = extract(xlsx)
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out}: {data['counts']} (extract of {data['extracted']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
