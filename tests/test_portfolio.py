"""Portfolio browser (/portfolio): SVP > value proposition > offer > customer reference."""

from __future__ import annotations

import json

import pytest

from radar import portfolio
from radar.portfolio import PortfolioStore, PortfolioUnavailable, build, norm

DI_EN, DI_FR, OX_EN = "tag-di-en", "tag-di-fr", "tag-ox-en"

CURATED = {
    "version": "test",
    "svps": [
        {"key": "DI", "name": "Digital Infrastructure", "role": "Foundation", "summary": "s",
         "expresso_tags": [DI_EN, DI_FR],
         "value_propositions": [
             {"name": "Connectivity", "description": "d", "ideas": ["DI-1"],
              "offers": ["Flexible SD-WAN", "Cloud Avenue", "Not In Expresso"]},
         ]},
        {"key": "OX", "name": "Operational Experience", "role": "Ops", "summary": "s",
         "expresso_tags": [OX_EN], "value_propositions": []},
    ],
}


def offer(_id, name, lang="en", svp=None, sec=None):
    return {"_id": _id, "type": "offer", "lang": lang, "updatedAt": "2026-01-01",
            "contentElement": {"name": name, "headline": f"<p>{name} headline</p>",
                               "strategicValuePropositions": svp, "strategicValuePropositionsSec": sec}}


def client(_id, name, offers, agreement=False):
    return {"_id": _id, "type": "client", "lang": "fr",
            "contentElement": {"name": name, "projectName": f"Projet {name}", "relatedOffers": offers,
                               "activitySector": ["client_cat_activity-sector.public-sector"],
                               "signatureDate": "2025-03-04T00:00:00Z", "commAgreement": agreement}}


@pytest.fixture()
def crawl(tmp_path):
    api = tmp_path / "v1"
    (api / "contents").mkdir(parents=True)
    records = [
        offer("o-sdwan-en", "Flexible SD-WAN", svp=DI_EN),
        offer("o-sdwan-fr", "Flexible SD-WAN", lang="fr", svp=DI_FR),   # same offer, French record
        offer("o-cloud", "Cloud Avenue & SecNumCloud", svp=OX_EN),       # matched by whole words
        offer("o-other", "Business VPN", svp=DI_EN),                     # tagged DI, not curated
        offer("o-ox", "Mobile Private Networks", sec=OX_EN),
        offer("o-untagged", "Orphan offer"),
        client("c-1", "Ville de Paris", ["o-sdwan-fr"]),
        client("c-2", "Acme", ["o-sdwan-en", "o-other"], agreement=True),
        client("c-3", "Nobody", ["unknown-offer"]),
    ]
    (api / "contents" / "page1").write_text(json.dumps(records), encoding="utf-8")
    # A list page with less content for the same ID must not replace the detail record.
    (api / "contents__thin").write_text(json.dumps(
        {"_id": "o-other", "type": "offer", "contentElement": {"name": "Business VPN"}}), encoding="utf-8")
    return api


def test_norm_ignores_accents_case_and_punctuation():
    assert norm("Numérique  Responsable!") == norm("numerique responsable")


def test_hierarchy_links_svp_vp_offer_and_reference(crawl):
    data = build(crawl, CURATED)
    vp = next(v for v in data["value_propositions"] if v["id"] == "DI-VP1")
    names = sorted(data["offers"][o]["name"] for o in vp["offers"])
    assert names == ["Cloud Avenue & SecNumCloud", "Flexible SD-WAN"]
    assert vp["unmatched_offers"] == ["Not In Expresso"]

    # The English and French records are one offer, with the references of both.
    sdwan = next(o for o in data["offers"].values() if o["name"] == "Flexible SD-WAN")
    assert sdwan["id"] == "o-sdwan-en" and sdwan["languages"] == ["en", "fr"]
    assert sorted(data["references"][r]["customer"] for r in sdwan["references"]) == ["Acme", "Ville de Paris"]

    paris = data["references"]["c-1"]
    assert paris["communication_agreement"] is False
    assert paris["sectors"] == ["Public sector"] and paris["signed"] == "2025-03-04"
    assert "c-3" not in data["references"]  # linked to nothing we show


def test_unclaimed_tagged_offers_go_to_an_other_bucket(crawl):
    data = build(crawl, CURATED)
    other = {v["id"]: v for v in data["value_propositions"] if not v["curated"]}
    assert [data["offers"][o]["name"] for o in other["DI-OTHER"]["offers"]] == ["Business VPN"]
    # Claiming is per SVP: Cloud Avenue is curated under DI but tagged OX, so OX
    # still lists it. Secondary tags count too; an untagged offer is not shown.
    assert [data["offers"][o]["name"] for o in other["OX-OTHER"]["offers"]] == [
        "Cloud Avenue & SecNumCloud", "Mobile Private Networks"]
    assert all(o["name"] != "Orphan offer" for o in data["offers"].values())
    # The richer detail record wins over the thin list-page copy.
    assert data["offers"]["o-other"]["headline"] == "Business VPN headline"


def test_store_prefers_crawl_then_snapshot(tmp_path, crawl, monkeypatch):
    curated = tmp_path / "portfolio.yaml"
    curated.write_text(json.dumps(CURATED), encoding="utf-8")  # JSON is valid YAML
    store = PortfolioStore(crawl, tmp_path / "none.json", curated, crm=tmp_path / "no-crm.json")
    assert store.get()["counts"]["svps"] == 2 and store.get()["crm"] is None

    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"counts": {"svps": 9}}), encoding="utf-8")
    assert PortfolioStore(tmp_path / "missing", snap, curated).get()["counts"]["svps"] == 9

    with pytest.raises(PortfolioUnavailable):
        PortfolioStore(tmp_path / "missing", tmp_path / "none.json", curated).get()


CRM = {
    "source": "test.xlsx", "extracted": "2026-11-19",
    "products": [
        # Family of the curated VP: "Orphan offer" is untagged, so the CRM is what puts it on show.
        {"name": "Orphan offer", "family": "Connectivity Family", "active": True},
        # Whole-word containment: the longest offer name inside the product name wins.
        {"name": "Business VPN Galerie - Premium", "family": "connectivity family", "active": True},
        {"name": "Flexible SD-WAN", "family": "Connectivity Family", "active": True},  # already curated
        {"name": "Mobile Private Networks", "family": "Some other family", "active": True},  # not mapped
        {"name": "Nothing like it", "family": "Connectivity Family", "active": True},
    ],
    "accounts": [
        {"id": "a1", "name": "ACME Ltd", "keys": ["acme"], "type": "Customer", "industry": "Retail",
         "region": "Europe", "country": "UK", "segment": "Enterprise", "won": 3, "won_tcv_eur": 1200,
         "lost": 1, "open": 1, "open_tcv_eur": 500, "last_won": "2026-02-01"},
        {"id": "a2", "name": "Acme Group", "keys": ["acme"], "type": "Prospect", "industry": "",
         "region": "", "country": "", "segment": "", "won": 0, "won_tcv_eur": 0,
         "lost": 0, "open": 0, "open_tcv_eur": 0, "last_won": None},
    ],
    "svps": {"Digital Infrastructure": {"opportunities": 10, "won": 4, "lost": 2, "won_tcv_eur": 900,
                                        "open": 3, "open_tcv_eur": 300, "win_rate": 0.667}},
}


def curated_with_crm():
    c = json.loads(json.dumps(CURATED))
    c["svps"][0]["crm_svp"] = "Digital Infrastructure"
    c["svps"][0]["value_propositions"][0]["crm_families"] = ["Connectivity Family"]
    return c


def test_crm_families_place_offers_in_value_propositions(crawl):
    data = build(crawl, curated_with_crm(), CRM)
    vp = next(v for v in data["value_propositions"] if v["id"] == "DI-VP1")
    names = lambda ids: sorted(data["offers"][o]["name"] for o in ids)  # noqa: E731
    assert names(vp["crm_offers"]) == ["Business VPN", "Orphan offer"]
    assert names(vp["offers"]) == ["Business VPN", "Cloud Avenue & SecNumCloud", "Flexible SD-WAN", "Orphan offer"]
    # Claimed now, so it leaves the "Other" bucket; the untagged offer appears.
    assert not any(v["id"] == "DI-OTHER" for v in data["value_propositions"])
    assert data["offers"]["o-other"]["crm_products"] == ["Business VPN Galerie - Premium"]
    assert data["crm"]["offers_placed"] == 2


def test_crm_accounts_and_svp_figures(crawl):
    data = build(crawl, curated_with_crm(), CRM)
    acme = data["references"]["c-2"]["crm"]
    # Two accounts share the name; the one with won business stands for it.
    assert acme["name"] == "ACME Ltd" and acme["won"] == 3 and acme["won_tcv_eur"] == 1200
    assert "crm" not in data["references"]["c-1"]
    di = next(s for s in data["svps"] if s["key"] == "DI")
    assert di["crm"]["won"] == 4 and di["crm"]["name"] == "Digital Infrastructure"
    assert "crm" not in next(s for s in data["svps"] if s["key"] == "OX")
    assert data["crm"]["references_matched"] == 1


def test_without_crm_nothing_changes(crawl):
    assert build(crawl, curated_with_crm())["crm"] is None
    vp = next(v for v in build(crawl, curated_with_crm())["value_propositions"] if v["id"] == "DI-VP1")
    assert vp["crm_offers"] == []


def test_company_key_ignores_legal_forms():
    from radar.crm import company_key
    assert company_key("ACME Ltd.") == company_key("Acme Group") == "acme"


def test_repo_curation_names_every_svp():
    keys = [s["key"] for s in portfolio.load_curated()["svps"]]
    assert keys == ["DI", "OX", "EX", "CX", "DT", "SU", "DA"]
