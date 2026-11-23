"""Which portfolio SVP an opportunity space belongs to.

The radar's sectors and the SVP filter read this. The taxonomy's six domains
map onto only four of the portfolio's seven SVPs (domains.yaml has no home for
Digital Trust, Sustainability or Data & AI), so the SVP is assigned from the
portfolio itself: each space goes to the SVP of the value proposition its
statement is closest to, in the same multilingual embedding space clustering
and the portfolio-overlap check use.

Derived at read time rather than stored, so it follows data/portfolio.json when
the catalogue is rebuilt. Statements are embedded once and cached; a statement
only changes when a space is re-synthesised.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .portfolio import SNAPSHOT_PATH

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SvpAssignment:
    svp: str            # portfolio SVP key, e.g. "DI"
    label: str          # "Digital Infrastructure"
    value_proposition: str  # name of the closest value proposition
    score: float
    #: Every SVP, closest first (by its best value proposition). The SVPs
    #: overlap — "collaboration for hospital staff" is Employee Experience but
    #: sits next to Digital Trust's Health proposition — so a constrained run
    #: accepts a requested SVP that is a close second, not only the first.
    ranking: tuple[str, ...] = ()

    def rank_of(self, svp: str) -> int | None:
        """1-based position of `svp` in the ranking, or None."""
        return self.ranking.index(svp) + 1 if svp in self.ranking else None


class SvpClassifier:
    def __init__(self, svps: list[dict[str, str]], vps: list[dict[str, Any]], vectors: np.ndarray, embedder):
        self.svps = svps
        self._labels = {s["id"]: s["label"] for s in svps}
        self.vps = vps
        self.vectors = vectors
        self.embedder = embedder
        self._cache: dict[str, SvpAssignment] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_portfolio(cls, embedder, path: Path = SNAPSHOT_PATH) -> "SvpClassifier | None":
        if not path.is_file():
            log.warning("SVP assignment disabled: %s not found.", path)
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        svps = [{"id": s["key"], "label": s["name"]} for s in data.get("svps") or []]
        names = {s["id"]: s["label"] for s in svps}
        # "<SVP>-OTHER" buckets hold offers no value proposition claims; they
        # carry no description of their own, so they are not a target.
        vps = [v for v in data.get("value_propositions") or []
               if v.get("svp") in names and not str(v.get("id", "")).endswith("-OTHER")]
        if not vps:
            return None
        texts = [f"{names[v['svp']]}: {v['name']}. {v.get('description') or ''} "
                 f"{' '.join(v.get('ideas') or [])}".strip() for v in vps]
        return cls(svps, vps, _normalise(embedder.encode(texts)), embedder)

    def label(self, svp: str) -> str:
        return self._labels.get(svp, svp)

    def warm(self, statements: list[str]) -> None:
        """Embed every uncached statement in one batch."""
        with self._lock:
            missing = list(dict.fromkeys(s for s in statements if s and s not in self._cache))
        if not missing:
            return
        sims = _normalise(self.embedder.encode(missing)) @ self.vectors.T
        with self._lock:
            for statement, row in zip(missing, sims):
                best = int(np.argmax(row))
                vp = self.vps[best]
                self._cache[statement] = SvpAssignment(vp["svp"], self.label(vp["svp"]), vp["name"],
                                                       float(row[best]), self._ranking(row))

    def _ranking(self, row: np.ndarray) -> tuple[str, ...]:
        best: dict[str, float] = {}
        for i, vp in enumerate(self.vps):
            best[vp["svp"]] = max(best.get(vp["svp"], -1.0), float(row[i]))
        return tuple(sorted(best, key=best.get, reverse=True))

    def nearest_svps(self, vectors: np.ndarray) -> list[str]:
        """Closest SVP for each of a batch of already-embedded vectors."""
        if len(vectors) == 0:
            return []
        idx = (_normalise(vectors) @ self.vectors.T).argmax(axis=1)
        return [self.vps[int(i)]["svp"] for i in idx]

    def assign(self, statement: str) -> SvpAssignment | None:
        if not statement:
            return None
        if statement not in self._cache:
            self.warm([statement])
        return self._cache.get(statement)


def _normalise(vectors) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    return vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-12)


def svp_vocabulary(path: Path = SNAPSHOT_PATH) -> list[dict[str, str]]:
    """The SVPs as filter/sector vocabulary, read from the catalogue alone.

    No embedder involved, so it is instant even on a cold start — the meta
    endpoint that gates the whole UI must not wait for the model to load.
    """
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    names = [(s["key"], s["name"]) for s in data.get("svps") or []]
    vps = [v for v in data.get("value_propositions") or []
           if not str(v.get("id", "")).endswith("-OTHER")]
    return [{"id": key, "label": name,
             "definition": "; ".join(v["name"] for v in vps if v.get("svp") == key)}
            for key, name in names]


_instance: SvpClassifier | None = None
_instance_built = False
_instance_lock = threading.Lock()


def classifier() -> SvpClassifier | None:
    """The process-wide classifier, built on first use (loads the embedder)."""
    global _instance, _instance_built
    with _instance_lock:
        if not _instance_built:
            from .embeddings import Embedder
            try:
                _instance = SvpClassifier.from_portfolio(Embedder())
            except Exception as exc:  # noqa: BLE001 — the radar must render without it
                log.warning("SVP assignment unavailable: %s", exc)
                _instance = None
            _instance_built = True
        return _instance


def set_classifier(value: SvpClassifier | None) -> None:
    """Install a classifier (tests), or None to run without SVPs."""
    global _instance, _instance_built
    with _instance_lock:
        _instance, _instance_built = value, True
