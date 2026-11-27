"""Open-opportunity list exported from CL (Salesforce), e.g. ``data/CL data 27.11.csv``.

One row per open opportunity: name, CL link, account, create / close date,
stage, cluster, SVP, domain, partner, a products summary and TCV in MEUR. The
products summary has one line per product::

    Flexible SD-WAN (Connectivity, DI): 15.5MEUR

``radar.portfolio`` merges this into the portfolio: products are matched to
Expresso offers by name, accounts to customer references by company name, and
the totals are summed per SVP. The file is small, so it is read on every build
(no import step). The newest ``CL data *.csv`` in ``data/`` is used.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"
CL_GLOB = "CL data *.csv"

_PRODUCT = re.compile(r"^(?P<name>.*?)\s*\((?P<family>[^,()]*),\s*(?P<svp>[^()]*)\):\s*(?P<tcv>[\d.]+)\s*MEUR\s*$")
_OPP_ID = re.compile(r"/Opportunity/([A-Za-z0-9]+)/")


def latest(data_dir: Path = DATA_DIR) -> Path | None:
    files = sorted(data_dir.glob(CL_GLOB), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _num(value: str | None) -> float:
    try:
        return float((value or "").replace(",", "."))
    except ValueError:
        return 0.0


def _date(value: str | None) -> str | None:
    return (value or "").strip()[:10] or None


def parse(path: Path) -> dict[str, Any]:
    opportunities: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh), 1):
            link = (row.get("CL link") or "").strip()
            m = _OPP_ID.search(link)
            oid = m.group(1) if m else f"row-{i}"
            products = []
            for line in (row.get("Products Summary") or "").splitlines():
                pm = _PRODUCT.match(line.strip())
                if pm:
                    products.append({"name": pm["name"].strip(), "family": pm["family"].strip(),
                                     "svp": pm["svp"].strip(), "tcv_meur": round(_num(pm["tcv"]), 3)})
            opportunities[oid] = {
                "id": oid,
                "name": (row.get("Opportunity name") or "").strip(),
                "account": (row.get("Account name") or "").strip(),
                "stage": (row.get("Stage") or "").strip(),
                "created": _date(row.get("Create date")),
                "close": _date(row.get("Close date")),
                "cluster": (row.get("Cluster") or "").strip(),
                "svp": (row.get("SVP") or "").strip(),
                "domain": (row.get("Opportunity Domain") or "").strip(),
                "partner": (row.get("Partner name") or "").strip(),
                "tcv_meur": round(_num(row.get("TCV, MEUR")), 3),
                "products": products,
                "url": link,
            }
    return {"source": path.name, "opportunities": opportunities}


def load(path: Path | None = None) -> dict[str, Any] | None:
    """The newest CL export, or None when there is none (the portfolio works without it)."""
    path = path or latest()
    if not path or not path.is_file():
        return None
    return parse(path)
