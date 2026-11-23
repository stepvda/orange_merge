"""Keep generation from proposing what Orange Business already sells.

An opportunity space is meant to point at a gap. A candidate that restates an
offer already in the catalogue (``data/portfolio.json``) is not a gap, however
well evidenced, so synthesis rejects it before the critic spends a model call.

"Equal" is judged fuzzily: the candidate statement against each offer's name and
headline, in the same multilingual embedding space clustering uses. Most offers
are written in French and every candidate in English, so string distance would
miss nearly all of them; the multilingual encoder does not.

Word-for-word name matching was tried and dropped. Many offer names are plain
technology terms ("Computer Vision", "Asset Management", "Managed Secure
Access"), so it rejected any statement that merely used the term — 23 false
rejections out of 464 on the calibration set. A statement that genuinely names
an offer scores high on similarity anyway.

The threshold lives in ``config/settings.yaml`` (NFR-11), calibrated against the
464 spaces the radar held on 2026-11-23: at 0.70 the matches were mostly genuine
restatements (drone detection vs Orange Drone 360, sovereign AI cloud vs Cloud
Avenue, satellite backhaul vs Internet Satellite); around 0.66 unrelated pairs
started to appear (post-quantum messaging vs a security-awareness catalogue).
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
class OfferMatch:
    offer_id: str
    offer_name: str
    score: float

    def reason(self) -> str:
        return f"restates existing offer {self.offer_name!r} (similarity {self.score:.2f})"


class PortfolioOverlap:
    """Matches candidate statements against the offer catalogue."""

    #: Offer embeddings, keyed by (file, mtime, model). Synthesis builds a new
    #: Synthesiser per run; re-encoding the catalogue each time is wasted work.
    _cache: dict[tuple[str, float, str], tuple[list[dict[str, Any]], np.ndarray]] = {}
    _cache_lock = threading.Lock()

    def __init__(self, offers: list[dict[str, Any]], vectors: np.ndarray, embedder, threshold: float):
        self.offers = offers
        self.vectors = vectors
        self.embedder = embedder
        self.threshold = threshold

    @classmethod
    def from_settings(cls, settings: dict[str, Any], embedder,
                      path: Path = SNAPSHOT_PATH) -> "PortfolioOverlap | None":
        """Build from the `curation.portfolio_overlap` block, or None when off.

        A missing catalogue disables the check with a warning rather than
        failing the run: the snapshot is gitignored, so a fresh checkout or CI
        has none, and generation without the filter is still generation.
        """
        conf = (settings.get("curation") or {}).get("portfolio_overlap") or {}
        if not conf.get("enabled", True):
            return None
        if not path.is_file():
            log.warning("Portfolio overlap check disabled: %s not found.", path)
            return None
        offers, vectors = cls._load(path, embedder)
        if not offers:
            return None
        return cls(offers, vectors, embedder, threshold=float(conf.get("similarity_threshold", 0.70)))

    @classmethod
    def _load(cls, path: Path, embedder) -> tuple[list[dict[str, Any]], np.ndarray]:
        key = (str(path.resolve()), path.stat().st_mtime, getattr(embedder, "model_name", ""))
        with cls._cache_lock:
            if key in cls._cache:
                return cls._cache[key]
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("offers") or {}
            offers = [o for o in (raw.values() if isinstance(raw, dict) else raw) if o.get("name")]
            texts = [f"{o['name']}. {o.get('headline') or ''}".strip() for o in offers]
            vectors = cls._normalise(embedder.encode(texts)) if texts else np.zeros((0, 1), np.float32)
            log.info("Portfolio overlap: %d offer(s) loaded from %s", len(offers), path)
            cls._cache[key] = (offers, vectors)
            return offers, vectors

    @staticmethod
    def _normalise(vectors) -> np.ndarray:
        vectors = np.asarray(vectors, dtype=np.float32)
        return vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-12)

    def match(self, statement: str) -> OfferMatch | None:
        """The offer this statement restates, or None."""
        if not statement or not self.offers:
            return None
        sims = self.vectors @ self._normalise(self.embedder.encode([statement])[0])
        best = int(np.argmax(sims))
        if float(sims[best]) < self.threshold:
            return None
        offer = self.offers[best]
        return OfferMatch(str(offer.get("id", "")), offer["name"], float(sims[best]))
