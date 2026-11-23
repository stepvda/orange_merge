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

def test_generation_svp_constraint_is_enforced(clf, tmp_path, monkeypatch):
    from radar.config import get_config
    from radar.db import Database
    from radar.llm import LLMClient
    from radar.pipeline.synthesis import Candidate, GenerationConstraints, SynthesisStats, Synthesiser

    db = Database(tmp_path / "t.db")
    db.init_schema()
    synth = Synthesiser(get_config(), db, LLMClient(provider="mock"), embedder=clf.embedder,
                        constraints=GenerationConstraints.from_dict({"svps": ["SU"]}),
                        portfolio_overlap=None)
    monkeypatch.setattr(synth, "_svp_classifier", lambda: clf)

    def candidate(statement):
        return Candidate(vertical="manufacturing", use_case="predictive_maintenance",
                         technology="machine_learning", statement=statement,
                         domains=["ox_smart_industries"],
                         why_hot=[{"claim": "Three deployments this quarter.", "signals": ["SIG-1"]}])

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
