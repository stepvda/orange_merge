"""Generation must not propose what the portfolio already sells.

A stub embedder keeps these tests independent of the sentence-transformers
download and of data/portfolio.json, which is gitignored and absent in CI.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from radar.config import get_config
from radar.db import Database
from radar.llm import LLMClient
from radar.pipeline.synthesis import Candidate, SynthesisStats, Synthesiser
from radar.portfolio_overlap import PortfolioOverlap

#: One axis per theme. A text's vector is the themes it mentions, so "drone
#: detection" and the French "détection de drones" offer land on the same axis,
#: which is what the multilingual encoder does for real.
AXES = (("drone",), ("cloud", "souverain", "sovereign"), ("maintenance", "vibration"))


class StubEmbedder:
    model_name = "stub"

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            low = text.lower()
            vec = [float(any(k in low for k in keys)) for keys in AXES] + [0.3]
            out.append(vec)
        arr = np.asarray(out, dtype=np.float32)
        return arr / np.linalg.norm(arr, axis=1, keepdims=True)


@pytest.fixture()
def catalogue(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"offers": {
        "o1": {"id": "o1", "name": "Orange Drone 360°",
               "headline": "Une solution SaaS de détection de drones"},
        "o2": {"id": "o2", "name": "Cloud Avenue", "headline": "Le cloud souverain d'Orange Business"},
    }}), encoding="utf-8")
    return path


def settings(**overlap):
    return {"curation": {"portfolio_overlap": {"enabled": True, "similarity_threshold": 0.9, **overlap}}}


def test_a_restated_offer_matches(catalogue):
    overlap = PortfolioOverlap.from_settings(settings(), StubEmbedder(), path=catalogue)
    hit = overlap.match("Drone detection for airports and critical infrastructure.")
    assert hit is not None and hit.offer_name == "Orange Drone 360°"
    assert "Orange Drone 360°" in hit.reason()


def test_an_unrelated_statement_does_not(catalogue):
    overlap = PortfolioOverlap.from_settings(settings(), StubEmbedder(), path=catalogue)
    assert overlap.match("Predictive maintenance for pumps using vibration analytics.") is None


def test_the_threshold_comes_from_settings(catalogue):
    """NFR-11: the cut-off is configuration. A partial overlap (drones, but for
    maintenance inspection — cosine ~0.72 under the stub) is a match at 0.70
    and not at 0.90."""
    partial = "Drone-based vibration maintenance inspections for wind turbines."
    strict = PortfolioOverlap.from_settings(settings(similarity_threshold=0.90), StubEmbedder(),
                                            path=catalogue)
    loose = PortfolioOverlap.from_settings(settings(similarity_threshold=0.70), StubEmbedder(),
                                           path=catalogue)
    assert strict.match(partial) is None
    assert loose.match(partial) is not None


def test_disabled_or_missing_catalogue_means_no_check(catalogue, tmp_path):
    assert PortfolioOverlap.from_settings(settings(enabled=False), StubEmbedder(), path=catalogue) is None
    assert PortfolioOverlap.from_settings(settings(), StubEmbedder(), path=tmp_path / "nope.json") is None


def test_offer_vectors_are_encoded_once_per_catalogue(catalogue):
    """A Synthesiser is built per run; the catalogue should not be re-encoded each time."""
    embedder = StubEmbedder()
    PortfolioOverlap.from_settings(settings(), embedder, path=catalogue)
    PortfolioOverlap.from_settings(settings(), embedder, path=catalogue)
    assert embedder.calls == 1


# ---------------------------------------------------------------------------
# Wired into synthesis: the validator drops the candidate and says why
# ---------------------------------------------------------------------------

def candidate(statement: str) -> Candidate:
    return Candidate(vertical="manufacturing", use_case="predictive_maintenance",
                     technology="machine_learning", statement=statement,
                     domains=["ox_smart_industries"],
                     why_hot=[{"claim": "Three deployments this quarter.", "signals": ["SIG-1"]}])


@pytest.fixture()
def synth(tmp_path, catalogue):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    embedder = StubEmbedder()
    overlap = PortfolioOverlap.from_settings(settings(), embedder, path=catalogue)
    return Synthesiser(get_config(), db, LLMClient(provider="mock"), embedder=embedder,
                       portfolio_overlap=overlap)


def test_validator_rejects_a_candidate_that_restates_an_offer(synth):
    stats = SynthesisStats()
    ok = synth._validate(candidate("Drone detection service protecting airport airspace from intrusions."),
                         {"SIG-1"}, stats)
    assert ok is False
    assert stats.failed_portfolio == 1
    assert "Orange Drone 360°" in stats.rejections[0]["reason"]


def test_validator_keeps_a_genuine_gap(synth):
    stats = SynthesisStats()
    ok = synth._validate(candidate("Predictive maintenance for rotating equipment using vibration ML."),
                         {"SIG-1"}, stats)
    assert ok is True
    assert stats.failed_portfolio == 0


def test_failed_portfolio_is_summed_across_passes():
    a, b = SynthesisStats(failed_portfolio=2), SynthesisStats(failed_portfolio=3)
    assert a.absorb(b).failed_portfolio == 5
