"""The Orange Business portfolio as a browsable hierarchy: SVP > value proposition > offer > customer reference.

Three of those four links come from Expresso, and one does not:

* an offer carries the SVPs it serves (``strategicValuePropositions`` and the
  secondary ``strategicValuePropositionsSec``), one tag ID per language;
* a customer reference carries the offers it used (``relatedOffers``);
* but nothing in Expresso says which offers make up a *value proposition*.
  Only one of its value-proposition records points at an offer at all.

That missing middle link is curated in ``config/portfolio.yaml`` (taken from
docs/SVP_queries_v2.docx) as a list of offer NAMES per value proposition, and
matched to Expresso offers here. An offer an SVP is tagged with that no curated
value proposition claims is kept under a per-SVP "Other offers" bucket, so the
browser never hides part of the catalogue because the curation is incomplete.

A Salesforce CRM extract (``radar.crm`` -> ``data/crm_portfolio.json``), when
present, fills in part of that middle link from the other side: the CRM files
products under families named like the value propositions, so a value
proposition's ``crm_families`` place the matching Expresso offers under it
(listed in ``crm_offers``, so the browser can mark them). The same extract adds
account figures to customer references matched by company name, and opportunity
totals to the SVPs whose ``crm_svp`` names a CRM SVP.

Offers exist once per language in Expresso. They are merged by normalised name,
so the French and English records of one offer are one node, and the customer
references of both hang under it.

The result is built from the crawl in ``data/espresso`` the first time it is
asked for and kept in memory. ``python -m radar.portfolio`` writes the same
payload to ``data/portfolio.json`` for a deployment that does not ship the crawl.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import threading
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import PROJECT_ROOT

CURATED_PATH = PROJECT_ROOT / "config" / "portfolio.yaml"
CRAWL_API = PROJECT_ROOT / "data" / "espresso" / "site" / "expresso.sso.infra.ftgroup" / "api" / "v1"
SNAPSHOT_PATH = PROJECT_ROOT / "data" / "portfolio.json"
EXPRESSO = "https://expresso.sso.infra.ftgroup/public"


class PortfolioUnavailable(RuntimeError):
    """Neither the crawl nor a snapshot is present."""


def norm(text: str | None) -> str:
    """Comparison key for names: accents, case and punctuation ignored."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _plain(value: Any, limit: int = 280) -> str:
    if not value or not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tags(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)]


def _category_label(code: str) -> str:
    """'client_cat_activity-sector.public-sector' -> 'Public sector'."""
    tail = code.rsplit(".", 1)[-1].replace("-", " ").replace("_", " ").strip()
    return tail[:1].upper() + tail[1:] if tail else code


def _records(api_dir: Path) -> Iterable[dict[str, Any]]:
    """Every content record in the crawl: list pages and detail pages alike."""
    files = list((api_dir / "contents").glob("*")) if (api_dir / "contents").is_dir() else []
    files += list(api_dir.glob("contents__*"))
    for path in files:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rec in data if isinstance(data, list) else [data]:
            if isinstance(rec, dict) and isinstance(rec.get("contentElement"), dict) and rec.get("_id"):
                yield rec


def _richest(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One record per ID. A list page and a detail page carry the same ID with
    different amounts of content; the one with more filled fields wins, and the
    later update breaks a tie."""
    best: dict[str, dict[str, Any]] = {}
    for rec in records:
        filled = sum(1 for v in rec["contentElement"].values() if v)
        key = (filled, str(rec.get("updatedAt") or ""))
        cur = best.get(rec["_id"])
        if cur is None or key > cur[0]:
            best[rec["_id"]] = (key, rec)  # type: ignore[assignment]
    return {i: pair[1] for i, pair in best.items()}  # type: ignore[index]


def load_curated(path: Path = CURATED_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _crm_account_index(crm: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Company key -> CRM account. Where several accounts share a name, the one with
    the most won business stands for it."""
    index: dict[str, dict[str, Any]] = {}
    for acc in (crm or {}).get("accounts", []):
        for key in acc.get("keys", []):
            cur = index.get(key)
            if cur is None or (acc["won"], acc["won_tcv_eur"]) > (cur["won"], cur["won_tcv_eur"]):
                index[key] = acc
    return index


CRM_ACCOUNT_FIELDS = ("name", "type", "industry", "region", "country", "segment",
                      "won", "won_tcv_eur", "lost", "open", "open_tcv_eur", "last_won")


def build(api_dir: Path = CRAWL_API, curated: dict[str, Any] | None = None,
          crm: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the hierarchy from the crawl. Pure: reads files, returns a dict.

    ``crm`` is the optional Salesforce extract (``radar.crm``): it places offers in
    value propositions by CRM product family, adds account figures to matched
    customer references, and opportunity figures to the SVPs."""
    from .crm import company_key
    curated = curated or load_curated()
    if not api_dir.is_dir():
        raise PortfolioUnavailable(f"No Expresso crawl at {api_dir}")
    by_id = _richest(_records(api_dir))

    tag_to_svp: dict[str, str] = {}
    for svp in curated["svps"]:
        for tag in svp.get("expresso_tags") or []:
            tag_to_svp[tag] = svp["key"]

    # ---------------------------------------------------------------- offers
    # Merged across languages by normalised name; English supplies the display
    # record when there is one.
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in by_id.values():
        if rec.get("type") == "offer" and norm(rec["contentElement"].get("name")):
            groups[norm(rec["contentElement"]["name"])].append(rec)

    offers: dict[str, dict[str, Any]] = {}
    offer_of_record: dict[str, str] = {}
    for key, recs in groups.items():
        recs.sort(key=lambda r: (r.get("lang") != "en", str(r.get("updatedAt") or "")))
        lead = recs[0]
        ce = lead["contentElement"]
        primary, secondary = set(), set()
        for r in recs:
            primary |= {tag_to_svp[t] for t in _tags(r["contentElement"].get("strategicValuePropositions")) if t in tag_to_svp}
            secondary |= {tag_to_svp[t] for t in _tags(r["contentElement"].get("strategicValuePropositionsSec")) if t in tag_to_svp}
        offers[lead["_id"]] = {
            "id": lead["_id"],
            "name": _plain(ce.get("name"), 160),
            "headline": _plain(ce.get("headline")) or _plain(ce.get("introduction")),
            "languages": sorted({r.get("lang") or "?" for r in recs}),
            "svps": sorted(primary),
            "svps_secondary": sorted(secondary - primary),
            "url": f"{EXPRESSO}/solutions/{lead['_id']}",
            "references": [],
            "_key": key,
        }
        for r in recs:
            offer_of_record[r["_id"]] = lead["_id"]

    # ---------------------------------------------------- customer references
    crm_accounts = _crm_account_index(crm)
    references: dict[str, dict[str, Any]] = {}
    for rec in by_id.values():
        if rec.get("type") != "client":
            continue
        ce = rec["contentElement"]
        linked = sorted({offer_of_record[o] for o in _tags(ce.get("relatedOffers")) if o in offer_of_record})
        if not linked:
            continue
        signed = str(ce.get("signatureDate") or "")[:10] or None
        references[rec["_id"]] = {
            "id": rec["_id"],
            "customer": _plain(ce.get("name"), 120) or "(unnamed customer)",
            "project": _plain(ce.get("projectName"), 200),
            "headline": _plain(ce.get("headline")),
            "sectors": [_category_label(c) for c in _tags(ce.get("activitySector"))],
            "regions": [_category_label(c) for c in _tags(ce.get("region"))],
            "signed": signed,
            "language": rec.get("lang"),
            # False means Expresso records no agreement to name the customer outside Orange.
            "communication_agreement": bool(ce.get("commAgreement")),
            "svps": sorted({tag_to_svp[t] for t in _tags(ce.get("strategicValuePropositions")) if t in tag_to_svp}),
            "offers": linked,
            "url": f"{EXPRESSO}/clients/{rec['_id']}",
        }
        acc = crm_accounts.get(company_key(ce.get("name")))
        if acc:
            references[rec["_id"]]["crm"] = {k: acc.get(k) for k in CRM_ACCOUNT_FIELDS}
        for oid in linked:
            offers[oid]["references"].append(rec["_id"])
    for o in offers.values():
        o["references"].sort(key=lambda rid: (references[rid]["customer"].lower(), references[rid]["project"].lower()))

    # ------------------------------------------- value propositions (curated)
    by_key: dict[str, list[str]] = defaultdict(list)
    for oid, o in offers.items():
        by_key[o["_key"]].append(oid)

    def match(name: str) -> list[str]:
        key = norm(name)
        if not key:
            return []
        if key in by_key:
            return list(by_key[key])
        # No exact name: accept offers whose name contains the curated one as
        # whole words ("Cloud Avenue" -> "Cloud Avenue SecNum"), never a fragment.
        pattern = re.compile(rf"(^| ){re.escape(key)}( |$)")
        return [oid for k, ids in by_key.items() if pattern.search(k) for oid in ids]

    def crm_match(product: str) -> list[str]:
        """A CRM product's Expresso offer: same name, or else the longest offer name
        (5+ characters) the product name contains as whole words ("Cloud Expert
        Services - PS for Azure" -> "Cloud Expert Services")."""
        key = norm(product)
        if key in by_key:
            return list(by_key[key])
        hits = [k for k in by_key if len(k) >= 5 and re.search(rf"(^| ){re.escape(k)}( |$)", key)]
        return list(by_key[max(hits, key=len)]) if hits else []

    crm_products = (crm or {}).get("products", [])
    svps_out, vps_out = [], []
    for svp in curated["svps"]:
        claimed: set[str] = set()
        vp_ids = []
        for i, vp in enumerate(svp.get("value_propositions") or [], 1):
            ids: list[str] = []
            unmatched: list[str] = []
            for name in vp.get("offers") or []:
                found = match(name)
                if found:
                    ids.extend(x for x in found if x not in ids)
                else:
                    unmatched.append(name)
            # CRM product families named for this value proposition place their
            # products' Expresso offers here too.
            families = {norm(f) for f in vp.get("crm_families") or []}
            crm_ids: list[str] = []
            for prod in crm_products:
                if norm(prod["family"]) not in families:
                    continue
                for oid in crm_match(prod["name"]):
                    names = offers[oid].setdefault("crm_products", [])
                    if prod["name"] not in names:
                        names.append(prod["name"])
                    if oid not in ids and oid not in crm_ids:
                        crm_ids.append(oid)
            ids += crm_ids
            claimed |= set(ids)
            vid = f"{svp['key']}-VP{i}"
            vp_ids.append(vid)
            vps_out.append({"id": vid, "svp": svp["key"], "name": vp["name"],
                            "description": vp.get("description", ""), "ideas": vp.get("ideas") or [],
                            "offers": sorted(ids, key=lambda x: offers[x]["name"].lower()),
                            "crm_offers": sorted(crm_ids, key=lambda x: offers[x]["name"].lower()),
                            "unmatched_offers": unmatched, "curated": True})
        rest = [oid for oid, o in offers.items()
                if svp["key"] in o["svps"] + o["svps_secondary"] and oid not in claimed]
        if rest:
            vid = f"{svp['key']}-OTHER"
            vp_ids.append(vid)
            vps_out.append({"id": vid, "svp": svp["key"], "name": f"Other {svp['name']} offers",
                            "description": "Offers Expresso tags with this SVP that no curated value proposition names.",
                            "ideas": [], "offers": sorted(rest, key=lambda x: offers[x]["name"].lower()),
                            "crm_offers": [], "unmatched_offers": [], "curated": False})
        svp_out = {"key": svp["key"], "name": svp["name"], "role": svp.get("role", ""),
                   "summary": svp.get("summary", ""), "value_propositions": vp_ids}
        figures = (crm or {}).get("svps", {}).get(svp.get("crm_svp") or "")
        if figures:
            svp_out["crm"] = {"name": svp["crm_svp"], **figures}
        svps_out.append(svp_out)

    used = {oid for vp in vps_out for oid in vp["offers"]}
    for o in offers.values():
        o.pop("_key", None)
    offers = {k: v for k, v in offers.items() if k in used}
    references = {k: v for k, v in references.items() if any(o in used for o in v["offers"])}
    for r in references.values():
        r["offers"] = [o for o in r["offers"] if o in used]

    crm_info = None
    if crm:
        crm_info = {
            "source": crm.get("source"), "extracted": crm.get("extracted"),
            "offers_placed": len({o for vp in vps_out for o in vp["crm_offers"]}),
            "references_matched": sum(1 for r in references.values() if "crm" in r),
            "svps_with_figures": sum(1 for s in svps_out if "crm" in s),
        }
    return {
        "version": curated.get("version"),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "Expresso crawl (data/espresso) + config/portfolio.yaml"
                  + (f" + CRM extract ({crm.get('source')})" if crm else ""),
        "svps": svps_out,
        "value_propositions": vps_out,
        "offers": offers,
        "references": references,
        "crm": crm_info,
        "counts": {"svps": len(svps_out), "value_propositions": len(vps_out),
                   "offers": len(offers), "references": len(references)},
    }


class PortfolioStore:
    """Builds once, on first use, and serves the same payload afterwards."""

    def __init__(self, api_dir: Path = CRAWL_API, snapshot: Path = SNAPSHOT_PATH,
                 curated: Path = CURATED_PATH, crm: Path | None = None):
        from .crm import CRM_PATH
        self.api_dir, self.snapshot, self.curated = api_dir, snapshot, curated
        self.crm = crm or CRM_PATH
        self._data: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._data is None:
                if self.api_dir.is_dir():
                    from .crm import load as load_crm
                    self._data = build(self.api_dir, load_curated(self.curated), load_crm(self.crm))
                elif self.snapshot.is_file():
                    self._data = json.loads(self.snapshot.read_text(encoding="utf-8"))
                else:
                    raise PortfolioUnavailable(
                        "The portfolio browser needs the Expresso crawl in data/espresso, "
                        "or a snapshot made with `python -m radar.portfolio`.")
            return self._data


def main(argv: list[str] | None = None) -> int:
    from .crm import load as load_crm
    out = Path((argv or sys.argv[1:] or [str(SNAPSHOT_PATH)])[0])
    data = build(crm=load_crm())
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out}: {data['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
