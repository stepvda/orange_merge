"""Best-of-breed additions (docs/best_of_selection.md).

Covers the four ported capabilities that carry logic of their own:
the AI footprint ledger, the taxonomy watchlist, the landscape projection and
the Semantic Scholar connector, plus the vendor-blog signal-type hint.
"""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pytest

from radar import footprint, landscape
from radar.config import get_config
from radar.db import Database, js

REF = dt.date(2026, 11, 17)


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "radar.db")
    database.init_schema()
    return database


# ---------------------------------------------------------------------------
# AI footprint (from orange_opportunity_spaces_HuMaSoo)
# ---------------------------------------------------------------------------

def test_footprint_estimate_decomposes_into_stated_factors():
    f = {"wh_per_1k_prompt_tokens": 0.1, "wh_per_1k_completion_tokens": 2.0,
         "pue": 1.5, "grid_gco2e_per_kwh": 400}
    out = footprint.estimate(10_000, 1_000, f)
    # (10 × 0.1 + 1 × 2.0) × 1.5 = 4.5 Wh ; 4.5 / 1000 × 400 = 1.8 g
    assert out == {"energy_wh": 4.5, "co2e_g": 1.8}


def test_footprint_ledger_records_calls_and_names_the_caller(db):
    ledger = footprint.Ledger(db)
    ledger.record(provider="deepseek", model="deepseek-chat", prompt_tokens=1000,
                  completion_tokens=100, latency_ms=50)
    row = db.query_one("SELECT * FROM llm_usage")
    assert row["prompt_tokens"] == 1000 and row["completion_tokens"] == 100
    # Called from a test module, not from radar.*, so no radar caller is found.
    assert row["purpose"] == "unspecified"


def test_footprint_report_counts_pre_ledger_runs_once(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO refreshes (id, started_at, reference_date, pipeline_version, weight_set, stats) "
            "VALUES (?,?,?,?,?,?)",
            ("R-1", "2026-08-01T00:00:00+00:00", "2026-08-01", "0.1.0", "w",
             js({"llm_usage": {"calls": 3, "prompt_tokens": 3000, "completion_tokens": 300,
                               "cheap_model": "m"}})),
        )
    footprint.Ledger(db).record(provider="p", model="m", prompt_tokens=1000, completion_tokens=100)
    # A run that started after the ledger began must not be double-counted.
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO refreshes (id, started_at, reference_date, pipeline_version, weight_set, stats) "
            "VALUES (?,?,?,?,?,?)",
            ("R-2", "2999-01-01T00:00:00+00:00", "2999-01-01", "0.1.0", "w",
             js({"llm_usage": {"calls": 9, "prompt_tokens": 9000, "completion_tokens": 900}})),
        )
    report = footprint.report(db, {})
    assert report["historic_runs"] == 1
    assert report["totals"]["calls"] == 4
    assert report["totals"]["prompt_tokens"] == 4000
    assert report["totals"]["completion_tokens"] == 400
    assert report["totals"]["energy_wh"] > 0


def test_llm_usage_sink_failure_never_fails_the_call(monkeypatch):
    from radar.llm import LLMClient

    class Usage:
        prompt_tokens, completion_tokens = 5, 2

    class Resp:
        usage = Usage()
        choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

    client = LLMClient(provider="mock")
    client.provider = "deepseek"
    client._client = type("X", (), {"chat": type("Y", (), {"completions": type("Z", (), {
        "create": staticmethod(lambda **kw: Resp())})()})()})()

    def broken_sink(**kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(LLMClient, "usage_sink", broken_sink)
    assert client.complete("s", "u").text == "ok"


# ---------------------------------------------------------------------------
# Taxonomy watchlist (from Orange_use_case)
# ---------------------------------------------------------------------------

@pytest.fixture()
def watch(db, tmp_path, monkeypatch):
    import radar.config as config_mod
    import radar.taxonomy_watch as watch_mod

    ext = tmp_path / "extensions.yaml"
    monkeypatch.setattr(config_mod, "TAXONOMY_EXTENSIONS_FILE", ext)
    monkeypatch.setattr(watch_mod, "TAXONOMY_EXTENSIONS_FILE", ext)
    cfg = get_config()
    return watch_mod.TaxonomyWatch(cfg, db), cfg, ext


def test_watchlist_ignores_generic_and_known_terms(watch):
    w, cfg, _ = watch
    assert not w.record("technology", "AI")
    assert not w.record("technology", "  data ")
    known = next(iter(cfg.technologies))
    assert not w.record("technology", known.label)  # a spelling, not a gap
    assert w.record("technology", "Quantum sensing", statement="Quantum sensing for grids")
    assert w.record("technology", "quantum  SENSING")  # normalised onto the same row
    items = w.list()["items"]
    assert len(items) == 1 and items[0]["frequency"] == 2 and items[0]["proposed"]


def test_approving_a_term_extends_the_live_vocabulary(watch):
    w, cfg, ext = watch
    w.record("use_case", "Wildfire early warning", statement="s", vertical="energy_utilities")
    item = w.list()["items"][0]
    domain = next(iter(cfg.domains)).id
    with pytest.raises(ValueError):
        w.decide(item["id"], "approve", "curator")  # a use case needs a domain
    row = w.decide(item["id"], "approve", "curator", domains=[domain], definition="Detecting fires early.")
    assert row["status"] == "approved" and row["approved_id"] == "wildfire_early_warning"
    assert "wildfire_early_warning" in cfg.use_cases
    assert cfg.use_cases.resolve("Wildfire early warning") == "wildfire_early_warning"
    assert "wildfire_early_warning" in ext.read_text()
    # Approved terms can no longer be reopened from the UI.
    with pytest.raises(ValueError):
        w.decide(item["id"], "reopen", "curator")


def test_reject_and_reopen(watch):
    w, _, _ = watch
    w.record("technology", "Neuromorphic chips")
    item = w.list()["items"][0]
    assert w.decide(item["id"], "reject", "curator", note="too early")["status"] == "rejected"
    assert w.decide(item["id"], "reopen", "curator")["status"] == "pending"


def test_seed_imports_once_into_an_empty_table(watch, tmp_path):
    w, _, _ = watch
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"origin": "import:test", "terms": [
        {"dimension": "use_cases", "term": "Health technology assessment", "frequency": 5, "verticals": ["Healthcare"]},
        {"dimension": "technologies", "term": "AI", "frequency": 77},
    ]}))
    assert w.seed_from_file(seed) == 1
    assert w.seed_from_file(seed) == 0
    assert w.list()["items"][0]["frequency"] == 5


def test_synthesis_vocabulary_rejection_feeds_the_watchlist(db, monkeypatch):
    from radar.pipeline.synthesis import Candidate, SynthesisStats, Synthesiser

    cfg = get_config()
    synth = Synthesiser.__new__(Synthesiser)
    synth.cfg, synth.db = cfg, db
    cand = Candidate.__new__(Candidate)
    cand.vertical = next(iter(cfg.verticals)).id
    cand.use_case = "Orbital debris tracking"
    cand.technology = next(iter(cfg.technologies)).id
    cand.statement = "Track orbital debris for satellite operators."
    stats = SynthesisStats()
    assert synth._validate(cand, set(), stats) is False
    assert stats.failed_vocabulary == 1
    row = db.query_one("SELECT dimension, term FROM taxonomy_watchlist")
    assert row["dimension"] == "use_cases" and row["term"] == "Orbital debris tracking"


# ---------------------------------------------------------------------------
# Landscape map (from innovation_radar_orange)
# ---------------------------------------------------------------------------

def _space(cur, sid, i):
    cur.execute(
        "INSERT INTO opportunity_spaces (id, vertical, use_case, technology, statement, state, first_seen, "
        "last_refresh, pipeline_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, f"v{i}", "u", "t", "s", "active", "2026-08-01", "2026-08-01", "0"),
    )


def test_landscape_places_similar_evidence_together(db):
    rng = np.random.default_rng(0)
    centres = {"A": rng.normal(size=16), "B": rng.normal(size=16)}
    with db.cursor() as cur:
        for i, (sid, group) in enumerate([("OS1", "A"), ("OS2", "A"), ("OS3", "B"), ("OS4", "B")]):
            _space(cur, sid, i)
            for k in range(3):
                vec = (centres[group] + rng.normal(scale=0.05, size=16)).astype(np.float32)
                sig = f"SIG-{sid}-{k}"
                cur.execute(
                    "INSERT INTO signals (id, source_id, publisher, title, published_at, ingested_at, tier, "
                    "extract, embedding, pipeline_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sig, "x", "p", sig, "2026-08-01", "2026-08-01", 1, "e", vec.tobytes(), "0"),
                )
                cur.execute("INSERT INTO opportunity_signals VALUES (?,?,?,?)", (sid, sig, "2026-08-01", "R"))
    result = landscape.compute(db)
    pts = {p["id"]: np.array([p["x"], p["y"]]) for p in result["points"]}
    assert set(pts) == {"OS1", "OS2", "OS3", "OS4"}
    # The first principal axis is the one that carries the group difference;
    # the second is noise here and is stretched to [-1, 1] like any other.
    within = abs(pts["OS1"][0] - pts["OS2"][0]) + abs(pts["OS3"][0] - pts["OS4"][0])
    across = abs(pts["OS1"][0] - pts["OS3"][0])
    assert within < 0.1 * across
    assert result["explained_variance"][0] > 0.8
    assert 0 < sum(result["explained_variance"]) <= 1.0001
    assert all(-1.0001 <= v <= 1.0001 for p in pts.values() for v in p)


# ---------------------------------------------------------------------------
# Sources (Semantic Scholar from Orange_use_case; vendor blogs; tiers)
# ---------------------------------------------------------------------------

def test_semantic_scholar_items_are_dated_doi_linked_and_typed():
    from radar.connectors import HttpSession, REGISTRY

    conn = REGISTRY["semantic_scholar"]({"id": "semantic_scholar", "params": {}}, HttpSession("test"))
    papers = [
        {"paperId": "p1", "title": "Private 5G for factories", "abstract": "We deploy...",
         "publicationDate": "2026-10-01", "externalIds": {"DOI": "10.1/abc"}, "venue": "IEEE"},
        {"paperId": "p2", "title": "Too old", "publicationDate": "2020-01-01", "url": "https://x"},
        {"paperId": "p3", "title": "Undated", "publicationDate": None, "url": "https://y"},
    ]
    items = list(conn.items_from(papers, "q", REF, 90))
    assert [i.url for i in items] == ["https://doi.org/10.1/abc"]
    assert items[0].signal_type_hint == "technology_maturity"
    assert items[0].attributes["venue"] == "IEEE"


def test_new_sources_are_configured_with_their_origin():
    cfg = get_config()
    by_id = {s["id"]: s for s in cfg.sources["sources"]}
    assert by_id["semantic_scholar"]["enabled"] and by_id["vendor_blogs"]["enabled"]
    assert by_id["vendor_blogs"]["default_tier"] == 4
    assert by_id["vendor_blogs"]["params"]["signal_type_hint"] == "technology_maturity"
    for rejected in ("prozorro", "newsapi_ai", "google_trends", "orange_partner_directory", "web_search"):
        assert by_id[rejected]["enabled"] is False and by_id[rejected]["note"]
    overrides = cfg.source_tiers["publisher_overrides"]
    assert overrides["bipt.be"] == 1 and overrides["rcrwireless.com"] == 2 and overrides["vodafone.com"] == 4


def test_rss_feed_carries_a_declared_signal_type():
    from radar.connectors import HttpSession, REGISTRY

    feed = b"""<?xml version="1.0"?><rss><channel><item><title>New GPU instances</title>
      <link>https://aws.amazon.com/blogs/aws/new</link><pubDate>Mon, 10 Nov 2026 10:00:00 GMT</pubDate>
      <description>Generally available today.</description></item></channel></rss>"""
    conn = REGISTRY["rss_feed"]({"id": "vendor_blogs", "params": {"signal_type_hint": "technology_maturity"}},
                                HttpSession("test"))
    items = list(conn._parse_rss(feed, REF, 30, {"feed": "f"}))
    assert items and items[0].signal_type_hint == "technology_maturity"
