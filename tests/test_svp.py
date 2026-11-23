"""Radar sectors and the SVP filter come from the portfolio's SVPs (radar.svp)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from radar.readmodel import _facets, _matches
from radar.svp import SvpClassifier

AXES = (("network", "connectivity", "sd-wan"), ("factory", "industrial", "machine"), ("carbon", "energy"))


class StubEmbedder:
    model_name = "stub"

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        arr = np.asarray([[float(any(k in t.lower() for k in keys)) for keys in AXES] + [0.2]
                          for t in texts], dtype=np.float32)
        return arr / np.linalg.norm(arr, axis=1, keepdims=True)


@pytest.fixture()
def clf(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({
        "svps": [{"key": "DI", "name": "Digital Infrastructure"},
                 {"key": "OX", "name": "Operational Experience"},
                 {"key": "SU", "name": "Sustainability"}],
        "value_propositions": [
            {"id": "DI-VP1", "svp": "DI", "name": "Secured Connectivity", "description": "SD-WAN and network"},
            {"id": "OX-VP1", "svp": "OX", "name": "Smart Manufacturing", "description": "Industrial factory ops"},
            {"id": "SU-VP1", "svp": "SU", "name": "IT for Green", "description": "Carbon and energy reporting"},
            # No description of its own: never a target.
            {"id": "SU-OTHER", "svp": "SU", "name": "Other offers", "description": "network factory carbon"},
        ],
    }), encoding="utf-8")
    return SvpClassifier.from_portfolio(StubEmbedder(), path=path)


def test_every_portfolio_svp_is_a_sector(clf):
    assert [s["id"] for s in clf.svps] == ["DI", "OX", "SU"]
    assert all(not v["id"].endswith("-OTHER") for v in clf.vps)


def test_a_space_goes_to_the_svp_of_its_closest_value_proposition(clf):
    hit = clf.assign("Industrial vision for factory machine monitoring")
    assert hit.svp == "OX" and hit.label == "Operational Experience"
    assert clf.assign("Carbon accounting for energy-intensive sites").svp == "SU"
    assert clf.assign("SD-WAN connectivity for retail branches").value_proposition == "Secured Connectivity"


def test_statements_are_embedded_once(clf):
    calls = clf.embedder.calls
    clf.warm(["Carbon reporting", "SD-WAN for branches", "Carbon reporting"])
    clf.assign("Carbon reporting")
    assert clf.embedder.calls == calls + 1


def test_missing_portfolio_means_no_svps(tmp_path):
    assert SvpClassifier.from_portfolio(StubEmbedder(), path=tmp_path / "nope.json") is None


def _topic(svp):
    return {"triple": {"vertical": "manufacturing"}, "domains": [], "personas": [], "geographies": [],
            "market_clusters": [], "svp": svp}


def test_svp_filter_and_facets():
    assert _matches(_topic("OX"), {"svp": ["OX", "DI"]})
    assert not _matches(_topic("SU"), {"svp": ["OX"]})
    assert not _matches(_topic(None), {"svp": ["OX"]})
    assert _facets([_topic("OX"), _topic("OX"), _topic("DI")])["svp"] == {"OX": 2, "DI": 1}


# ---------------------------------------------------------------------------
# The Generate screen's SVP constraint is enforced on the candidate's statement
# ---------------------------------------------------------------------------

def _synth(clf, tmp_path, monkeypatch, svps, accept_rank):
    from radar.config import get_config
    from radar.db import Database
    from radar.llm import LLMClient
    from radar.pipeline.synthesis import GenerationConstraints, Synthesiser

    db = Database(tmp_path / "t.db")
    db.init_schema()
    synth = Synthesiser(get_config(), db, LLMClient(provider="mock"), embedder=clf.embedder,
                        constraints=GenerationConstraints.from_dict({"svps": svps}),
                        portfolio_overlap=None)
    monkeypatch.setattr(synth, "_svp_classifier", lambda: clf)
    monkeypatch.setattr(type(synth), "_svp_settings",
                        property(lambda self: {"accept_rank": accept_rank, "min_cluster_signals": 2}))
    return synth


def candidate(statement):
    from radar.pipeline.synthesis import Candidate
    return Candidate(vertical="manufacturing", use_case="predictive_maintenance",
                     technology="machine_learning", statement=statement,
                     domains=["ox_smart_industries"],
                     why_hot=[{"claim": "Three deployments this quarter.", "signals": ["SIG-1"]}])


def test_generation_svp_constraint_is_enforced(clf, tmp_path, monkeypatch):
    from radar.pipeline.synthesis import SynthesisStats
    synth = _synth(clf, tmp_path, monkeypatch, ["SU"], accept_rank=1)
    stats = SynthesisStats()
    assert synth._validate(candidate("Carbon and energy reporting for chemical process plants."),
                           {"SIG-1"}, stats) is True
    assert synth._validate(candidate("SD-WAN connectivity for chemical plant branch network sites."),
                           {"SIG-1"}, stats) is False
    assert stats.failed_constraints == 1
    assert "SVP 'DI' outside" in stats.rejections[0]["reason"]
    # And the prompt names the SVP and its value propositions.
    assert any("Sustainability (value propositions: IT for Green)" in line
               for line in synth._constraint_block() or [])


def test_svp_vocabulary_needs_no_embedder(tmp_path):
    """/api/meta gates the UI; it must not wait for the embedding model."""
    from radar.svp import svp_vocabulary
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({
        "svps": [{"key": "DI", "name": "Digital Infrastructure"}],
        "value_propositions": [{"id": "DI-VP1", "svp": "DI", "name": "Secured Connectivity"},
                               {"id": "DI-OTHER", "svp": "DI", "name": "Other offers"}],
    }), encoding="utf-8")
    assert svp_vocabulary(path) == [{"id": "DI", "label": "Digital Infrastructure",
                                     "definition": "Secured Connectivity"}]
    assert svp_vocabulary(tmp_path / "missing.json") == []


def test_a_requested_svp_that_is_a_close_second_is_accepted(clf, tmp_path, monkeypatch):
    """SVPs overlap; at accept_rank 2 a candidate whose second-closest SVP is
    the requested one passes, at rank 1 it does not."""
    from radar.pipeline.synthesis import SynthesisStats
    statement = "SD-WAN network for industrial factory sites"   # DI and OX both close
    ranking = clf.assign(statement).ranking
    requested = ranking[1]
    assert _synth(clf, tmp_path, monkeypatch, [requested], accept_rank=2)._validate(
        candidate(statement), {"SIG-1"}, SynthesisStats()) is True
    stats = SynthesisStats()
    assert _synth(clf, tmp_path, monkeypatch, [requested], accept_rank=1)._validate(
        candidate(statement), {"SIG-1"}, stats) is False
    assert "ranks 2" in stats.rejections[0]["reason"]


def test_constrained_runs_read_the_clusters_with_evidence_for_the_svp(clf, tmp_path, monkeypatch):
    """Clusters are ranked by how many signals sit closest to the requested SVP;
    a cluster with fewer than min_cluster_signals is not read at all."""
    synth = _synth(clf, tmp_path, monkeypatch, ["SU"], accept_rank=2)
    vec = {
        "SU": clf.embedder.encode(["carbon energy"])[0],
        "DI": clf.embedder.encode(["sd-wan network"])[0],
    }
    layout = {1: ["DI"] * 9 + ["SU"],          # biggest cluster, 1 SU signal: skipped
              2: ["SU"] * 2 + ["DI"] * 2,      # 2 SU signals
              3: ["SU"] * 4}                    # 4 SU signals: read first
    with synth.db.cursor() as cur:
        for cid, svps in layout.items():
            cur.execute("INSERT INTO clusters (id, label, keyphrases, size, created_at, refresh_id) "
                        "VALUES (?,?,?,?,?,?)", (cid, f"c{cid}", "[]", len(svps), "2026-11-23", "R"))
            for k, svp in enumerate(svps):
                cur.execute(
                    "INSERT INTO signals (id, source_id, publisher, title, published_at, ingested_at, tier, "
                    "extract, relevance, cluster_id, embedding, pipeline_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"S{cid}-{k}", "x", "p", "t", "2026-11-01", "2026-11-01", 1, "e", 1.0, cid,
                     np.asarray(vec[svp], dtype=np.float32).tobytes(), "0"))
    assert synth._cluster_ids(None, set()) == [3, 2]
