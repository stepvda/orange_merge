"""Best-of-breed additions to the read API.

Everything here was selected from the four peer projects and is documented in
docs/best_of_selection.md:

* `/api/explore/scores`     — component scores for the What-if simulator and
                              the Compare view (innovation_radar_orange).
* `/api/explore/landscape`  — the evidence landscape map (innovation_radar_orange).
* `/api/analytics/footprint`— the AI footprint ledger (orange_opportunity_spaces_HuMaSoo).
* `/api/taxonomy/watchlist` — the self-extending taxonomy review (Orange_use_case).

Kept in its own router so the provenance of each endpoint stays legible and the
base API module does not grow another few hundred lines. It is mounted by
`radar.api`, so it inherits the application-level session dependency.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import footprint, landscape
from .config import Config
from .db import Database
from .readmodel import ReadModel
from .taxonomy_watch import TaxonomyWatch

LIVE_STATES = ("active", "watchlist", "fading", "candidate")


class WatchDecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject|reopen)$")
    label: str | None = Field(None, max_length=120)
    definition: str | None = Field(None, max_length=600)
    domains: list[str] = Field(default_factory=list, max_length=6)
    synonyms: list[str] = Field(default_factory=list, max_length=12)
    note: str | None = Field(None, max_length=400)


def _slim(topic: dict[str, Any]) -> dict[str, Any]:
    attr = topic.get("attractiveness") or {}
    rtw = topic.get("right_to_win") or {}
    return {
        "id": topic["id"],
        "labels": topic["labels"],
        "statement": topic["statement"],
        "state": topic["state"],
        "horizon": topic.get("horizon"),
        "domains": topic.get("domains") or [],
        "domain_labels": topic.get("domain_labels") or [],
        "signal_count": topic.get("signal_count", 0),
        "portfolio_distance": topic.get("portfolio_distance"),
        "attractiveness": attr.get("score"),
        "attractiveness_components": attr.get("components") or {},
        "right_to_win": rtw.get("score"),
        "right_to_win_components": rtw.get("components") or {},
    }


def build_router(cfg: Config, db: Database, read: ReadModel) -> APIRouter:
    router = APIRouter()
    watch = TaxonomyWatch(cfg, db)
    try:
        watch.seed_from_file()
    except Exception:  # noqa: BLE001 — a seed is a convenience, never a startup failure
        pass
    footprint.init_schema(db)

    @router.get("/api/explore/scores")
    def explore_scores() -> dict[str, Any]:
        """Every live space with its stored score components.

        The What-if simulator re-weights these in the browser: the stored score
        is never changed, and the weight set it was computed with is returned so
        the UI can say exactly what it is comparing against.
        """
        topics = [_slim(t) for t in read.topics(states=LIVE_STATES)]
        return {
            "weight_set": cfg.weight_set,
            "attractiveness_weights": cfg.attractiveness_weights,
            "right_to_win_weights": cfg.right_to_win_weights,
            "topics": [t for t in topics if t["attractiveness"] is not None],
        }

    @router.get("/api/explore/landscape")
    def explore_landscape() -> dict[str, Any]:
        result = landscape.compute(db)
        by_id = {t["id"]: _slim(t) for t in read.topics(states=LIVE_STATES)}
        points = []
        for p in result["points"]:
            t = by_id.get(p["id"])
            if t is None:
                continue
            points.append({**p, "labels": t["labels"], "statement": t["statement"], "state": t["state"],
                           "domain": (t["domains"] or [None])[0],
                           "domain_label": (t["domain_labels"] or [None])[0],
                           "attractiveness": t["attractiveness"], "right_to_win": t["right_to_win"],
                           "horizon": t["horizon"]})
        return {**result, "points": points,
                "domains": [{"id": d.id, "label": d.label} for d in cfg.domains]}

    @router.get("/api/analytics/footprint")
    def analytics_footprint() -> dict[str, Any]:
        return footprint.report(db, cfg.settings)

    @router.get("/api/taxonomy/watchlist")
    def taxonomy_watchlist(status: str | None = Query(None, pattern="^(pending|proposed|approved|rejected)$")
                           ) -> dict[str, Any]:
        out = watch.list(status)
        out["domains"] = [{"id": d.id, "label": d.label} for d in cfg.domains]
        return out

    @router.post("/api/taxonomy/watchlist/{item_id}/decision")
    def taxonomy_decision(item_id: int, payload: WatchDecisionIn, request: Request) -> dict[str, Any]:
        user = getattr(request.state, "user", None) or {}
        who = user.get("username", "unknown") if isinstance(user, dict) else str(user)
        try:
            return watch.decide(item_id, payload.decision, who, label=payload.label,
                                definition=payload.definition, domains=payload.domains,
                                synonyms=payload.synonyms, note=payload.note)
        except KeyError:
            raise HTTPException(404, f"No watchlist item {item_id}")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    return router
