"""Landscape map — every opportunity space placed by what its evidence says.

Ported idea (best-of selection, see docs/best_of_selection.md): the
innovation_radar_orange team's topic data-map (BERTopic + UMAP + datamapplot)
was the most immediately legible picture in any of the five projects, but it
lived in a notebook and shipped as a static PNG.

Here it is computed live from what the radar already stores: each signal's
multilingual sentence embedding (`signals.embedding`, float32). A space's
position is the centroid of the embeddings of the evidence attached to it,
projected to two dimensions with PCA (an SVD of the centred centroid matrix).

PCA rather than UMAP on purpose: it is deterministic, needs no extra
dependency, is cheap enough to recompute on request for a few hundred spaces,
and its axes keep a meaning (share of variance explained is reported) — two
spaces close on the map share vocabulary in their evidence, and the map says
how much of the spread the two axes actually capture rather than presenting a
non-linear projection as geography.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from .db import Database

LIVE_STATES = ("active", "watchlist", "fading", "candidate")

_cache: dict[str, Any] = {}
_lock = threading.Lock()


def _fingerprint(db: Database) -> str:
    row = db.query_one(
        "SELECT COUNT(*) n, MAX(attached_at) last FROM opportunity_signals"
    )
    spaces = db.query_one(
        "SELECT COUNT(*) n, MAX(last_refresh) last FROM opportunity_spaces WHERE merged_into IS NULL"
    )
    return f"{row['n']}|{row['last']}|{spaces['n']}|{spaces['last']}"


def compute(db: Database) -> dict[str, Any]:
    """Centroid-per-space PCA projection. Cached until the evidence changes."""
    key = _fingerprint(db)
    with _lock:
        if _cache.get("key") == key:
            return _cache["value"]

    placeholders = ",".join("?" * len(LIVE_STATES))
    rows = db.query(
        "SELECT os.opportunity_id topic_id, s.embedding emb FROM opportunity_signals os "
        "JOIN signals s ON s.id = os.signal_id "
        "JOIN opportunity_spaces t ON t.id = os.opportunity_id "
        f"WHERE t.merged_into IS NULL AND t.state IN ({placeholders}) AND s.embedding IS NOT NULL",
        LIVE_STATES,
    )
    sums: dict[str, np.ndarray] = {}
    counts: dict[str, int] = {}
    dim = None
    for r in rows:
        vec = np.frombuffer(r["emb"], dtype=np.float32)
        if dim is None:
            dim = vec.shape[0]
        if vec.shape[0] != dim:
            continue  # an embedding from a different model; never mix spaces
        norm = float(np.linalg.norm(vec)) or 1.0
        sums[r["topic_id"]] = sums.get(r["topic_id"], 0) + vec / norm
        counts[r["topic_id"]] = counts.get(r["topic_id"], 0) + 1

    ids = sorted(sums)
    result: dict[str, Any] = {"points": [], "explained_variance": [0.0, 0.0],
                              "embedded_spaces": len(ids), "method": "pca-of-evidence-centroids"}
    if len(ids) >= 3:
        matrix = np.vstack([sums[i] / counts[i] for i in ids]).astype(np.float64)
        centred = matrix - matrix.mean(axis=0)
        u, s, _vt = np.linalg.svd(centred, full_matrices=False)
        coords = u[:, :2] * s[:2]
        # Fix the sign so the map does not mirror itself between refreshes:
        # each axis is oriented so its largest-magnitude coordinate is positive.
        for axis in range(2):
            if coords[np.argmax(np.abs(coords[:, axis])), axis] < 0:
                coords[:, axis] *= -1
        span = np.abs(coords).max(axis=0)
        span[span == 0] = 1.0
        coords = coords / span
        total = float((s ** 2).sum()) or 1.0
        result["explained_variance"] = [round(float(s[0] ** 2 / total), 4),
                                        round(float(s[1] ** 2 / total), 4)]
        result["points"] = [
            {"id": tid, "x": round(float(coords[k, 0]), 4), "y": round(float(coords[k, 1]), 4),
             "n_embedded": counts[tid]}
            for k, tid in enumerate(ids)
        ]

    with _lock:
        _cache["key"], _cache["value"] = key, result
    return result
