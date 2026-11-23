"""Collaboration workflow, conviction and divergence (FR-25, §4.10, SC-12, SC-14)."""

from __future__ import annotations

import datetime as dt

import pytest

from radar.config import get_config
from radar.db import Database
from radar.readmodel import ReadModel
from radar.workflow import AXES, STAGES, WorkflowService


@pytest.fixture(scope="module")
def cfg():
    return get_config()


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "wf.db")
    database.init_schema()
    with database.cursor() as cur:
        cur.execute(
            """INSERT INTO opportunity_spaces
               (id, vertical, use_case, technology, statement, state, first_seen,
                last_refresh, pipeline_version)
               VALUES ('OS001','manufacturing','predictive_maintenance','machine_learning',
                       'Predictive maintenance for rotating equipment.','active','2026-01-01',
                       '2026-08-18','0.1.0')"""
        )
    return database


@pytest.fixture()
def wf(cfg, db):
    return WorkflowService(cfg, db)


# ---------------------------------------------------------------------------
# Model A — the stage gate
# ---------------------------------------------------------------------------

def test_topics_start_shortlisted(wf):
    state = wf.state_for("OS001")
    assert state["stage"] == "shortlisted"
    assert "owner_role" not in state, "roles were removed: a stage has no owning role"
    assert state["next_stage"] == "demand_tested"


def test_stage_advances(wf):
    wf.transition("OS001", "demand_tested", actor="a@x")
    assert wf.state_for("OS001")["stage"] == "demand_tested"


def test_every_transition_is_recorded(wf, db):
    wf.transition("OS001", "demand_tested", actor="a@x", reason="looks real")
    wf.transition("OS001", "packaged", actor="b@x")
    rows = db.query("SELECT * FROM workflow_transitions WHERE opportunity_id='OS001' ORDER BY id")
    assert [r["to_stage"] for r in rows] == ["demand_tested", "packaged"]
    assert rows[0]["reason"] == "looks real"
    assert [r["actor"] for r in rows] == ["a@x", "b@x"], "a move records the user who made it"


def test_unknown_stage_is_rejected(wf):
    with pytest.raises(ValueError):
        wf.transition("OS001", "shipped", actor="a@x")


def test_a_topic_can_be_parked_from_any_stage(wf):
    wf.transition("OS001", "parked", actor="a@x", reason="no budget")
    assert wf.state_for("OS001")["stage"] == "parked"


# ---------------------------------------------------------------------------
# Model C — distributed assessment
# ---------------------------------------------------------------------------

def test_anyone_rates_any_axis(wf):
    """Roles were removed: one user may rate all three axes."""
    for axis, rating in zip(AXES, (2, 4, 5)):
        wf.record_assessment("OS001", axis, rating=rating, author="s@x")
    conviction = wf.conviction_for("OS001")
    assert set(conviction["axes"]) == set(AXES)
    assert conviction["axes"]["customer_demand"]["score"] == 80.0
    assert conviction["axes"]["customer_demand"]["n"] == 1


def test_ratings_are_confidence_weighted(wf):
    """Someone who says "4, but I'm guessing" should move the aggregate less."""
    wf.record_assessment("OS001", "customer_demand", rating=5, author="certain@x", confidence=5)
    wf.record_assessment("OS001", "customer_demand", rating=1, author="guessing@x", confidence=1)
    score = wf.conviction_for("OS001")["axes"]["customer_demand"]["score"]
    # Unweighted mean would be 3.0 -> 60. Confidence weighting pulls it up.
    assert score > 60.0


def test_disagreement_is_flagged_not_averaged_away(wf):
    """§4.7.6: persistently low agreement means the CRITERION is ill-defined."""
    wf.record_assessment("OS001", "customer_demand", rating=5, author="a@x")
    wf.record_assessment("OS001", "customer_demand", rating=1, author="b@x")
    axis = wf.conviction_for("OS001")["axes"]["customer_demand"]
    assert axis["rater_spread"] == 4
    assert axis["contested"] is True


def test_a_changed_mind_supersedes_rather_than_duplicates(wf, db):
    wf.record_assessment("OS001", "customer_demand", rating=1, author="a@x")
    wf.record_assessment("OS001", "customer_demand", rating=5, author="a@x")
    axis = wf.conviction_for("OS001")["axes"]["customer_demand"]
    assert axis["n"] == 1, "the same author must not count twice"
    assert axis["score"] == 100.0
    # The earlier opinion is kept — a changed mind is itself a label (§4.7.7).
    assert db.query_one("SELECT COUNT(*) n FROM assessments WHERE superseded=1")["n"] == 1


def test_ratings_outside_the_scale_are_rejected(wf):
    for bad in (-1, 6):
        with pytest.raises(ValueError):
            wf.record_assessment("OS001", "customer_demand", rating=bad, author="a@x")
    with pytest.raises(ValueError):
        wf.record_assessment("OS001", "nobody", rating=3, author="a@x")   # unknown axis


def test_unrated_axes_are_reported(wf):
    wf.record_assessment("OS001", "customer_demand", rating=3, author="a@x")
    conviction = wf.conviction_for("OS001")
    assert set(conviction["axes_missing"]) == {"strategic_fit", "deliverability"}


# ---------------------------------------------------------------------------
# Divergence — the review trigger
# ---------------------------------------------------------------------------

def test_agreement_produces_no_review_trigger(wf):
    wf.record_assessment("OS001", "customer_demand", rating=4, author="a@x")   # -> 80
    assert wf.divergence_for("OS001", attractiveness=78.0, right_to_win=None) is None


def test_team_below_the_evidence_is_flagged(wf):
    wf.record_assessment("OS001", "customer_demand", rating=1, author="a@x")   # -> 20
    flags = wf.divergence_for("OS001", attractiveness=85.0, right_to_win=None)["flags"]
    assert flags[0]["direction"] == "internal_lower"
    assert flags[0]["delta"] == pytest.approx(-65.0)
    assert "market is moving before our conversations" in flags[0]["reading"]


def test_team_above_the_evidence_is_flagged(wf):
    wf.record_assessment("OS001", "deliverability", rating=5, author="a@x")  # -> 100
    flags = wf.divergence_for("OS001", attractiveness=None, right_to_win=20.0)["flags"]
    assert flags[0]["axis"] == "deliverability"
    assert flags[0]["direction"] == "internal_higher"


def test_divergence_needs_an_assessment_to_compare_against(wf):
    assert wf.divergence_for("OS001", attractiveness=90.0, right_to_win=10.0) is None


# ---------------------------------------------------------------------------
# SC-12 / SC-14 — conviction must not contaminate the published scores
# ---------------------------------------------------------------------------

def test_conviction_never_alters_attractiveness_or_right_to_win(cfg, db, wf):
    """SC-14: internal data "adjusts but does not replace external discovery",
    and SC-12 forbids collapsing the two published scores into one."""
    read = ReadModel(cfg, db)
    with db.cursor() as cur:
        for kind, score in (("attractiveness", 60.0), ("right_to_win", 40.0)):
            cur.execute(
                "INSERT INTO scores (opportunity_id, computed_at, refresh_id, kind, score, "
                "components, inputs, weight_set, pipeline_version) VALUES (?,?,?,?,?,?,?,?,?)",
                ("OS001", "2026-08-18T00:00:00", "R1", kind, score, "{}", "{}", cfg.weight_set, "0.1.0"),
            )
    before = read.topic("OS001")
    wf.record_assessment("OS001", "customer_demand", rating=5, author="a@x")
    wf.record_assessment("OS001", "deliverability", rating=0, author="b@x")
    after = read.topic("OS001")

    assert after["attractiveness"]["score"] == before["attractiveness"]["score"] == 60.0
    assert after["right_to_win"]["score"] == before["right_to_win"]["score"] == 40.0
    # ...but conviction is now present as a separate, third quantity.
    assert after["conviction"]["assessed"] == 2
    assert after["conviction"]["score"] is not None


def test_the_board_orders_each_column_by_attractiveness(cfg, db, wf):
    """No role-specific ordering any more: every column is by attractiveness."""
    from radar.readmodel import sort_topics
    topics = [{"id": "LOW", "attractiveness": {"score": 40.0}},
              {"id": "HIGH", "attractiveness": {"score": 90.0}},
              {"id": "NONE", "attractiveness": None}]
    assert [t["id"] for t in sort_topics(topics, "attractiveness")] == ["HIGH", "LOW", "NONE"]


def test_stage_gate_order_is_the_documented_one():
    assert STAGES == ["shortlisted", "demand_tested", "packaged", "live"]
