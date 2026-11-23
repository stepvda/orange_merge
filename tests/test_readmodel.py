"""Filtering, sorting and serving guarantees (FR-21, AC-04/05).

Role modes were removed on 2026-11-23: every view shows every live space.
"""

from __future__ import annotations

import pytest

from radar.config import get_config
from radar.readmodel import DEFAULT_SORT, SORTS, ReadModel, _for_list, _matches, _sorted, next_action


@pytest.fixture(scope="module")
def cfg():
    return get_config()


def topic(topic_id: str, **overrides):
    base = {
        "id": topic_id,
        "triple": {"vertical": "manufacturing", "use_case": "predictive_maintenance",
                   "technology": "machine_learning"},
        "labels": {"vertical": "Manufacturing", "use_case": "Predictive maintenance",
                   "technology": "Machine learning"},
        "statement": "Predictive maintenance for rotating equipment in chemical plants.",
        "domains": ["ox_smart_industries"],
        "personas": ["coo_production"],
        "geographies": ["FR"],
        "state": "active",
        "horizon": "next",
        "why_hot": [],
        "next_action": None,
        "attractiveness": {"score": 70.0, "components": {"novelty_momentum": 60.0}, "weight_set": "w"},
        "right_to_win": {"score": 50.0, "components": {}, "weight_set": "w"},
        "portfolio_distance": 0,
        "link_types": ["L0"],
        "links": [{"node_type": "reference", "link_type": "SUP"}],
        "evidence_gap_warning": False,
        "signal_count": 5,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# No roles: everything is shown, attractiveness first
# ---------------------------------------------------------------------------

def test_the_default_order_is_attractiveness():
    assert DEFAULT_SORT == "attractiveness"
    assert "rank" not in SORTS, "the role ranking function is gone"
    ordered = _sorted([
        topic("LOW", attractiveness={"score": 30.0}),
        topic("HIGH", attractiveness={"score": 90.0}),
        topic("UNSCORED", attractiveness=None),
    ], DEFAULT_SORT)
    assert [t["id"] for t in ordered] == ["HIGH", "LOW", "UNSCORED"]


def test_the_view_shows_every_space_with_no_cap_or_role_filter(cfg, monkeypatch):
    """White space (L4), no-reference and evidence-gap spaces used to be hidden
    from some roles, and every view was capped. Now all of them are shown."""
    read = ReadModel(cfg, db=None)
    spaces = [topic(f"OS{i:03}", attractiveness={"score": float(i)}) for i in range(40)]
    spaces += [topic("OS-WS", link_types=["L4"], portfolio_distance=4, links=[]),
               topic("OS-GAP", evidence_gap_warning=True)]
    monkeypatch.setattr(read, "topics", lambda: spaces)

    class _NoRefresh:
        @staticmethod
        def query_one(*args, **kwargs):
            return None
    read.db = _NoRefresh()
    view = read.view()
    assert view["total_matching"] == len(view["topics"]) == 42
    assert view["sort"] == "attractiveness"
    # The two formerly-hidden spaces score 70, the highest, so they lead.
    assert {t["id"] for t in view["topics"][:2]} == {"OS-WS", "OS-GAP"}
    assert "role" not in view and "exploration" not in view and "cap" not in view


def test_next_action_prefers_the_single_action_and_reads_old_per_role_records():
    assert next_action({"next": "Do this."}) == "Do this."
    # Records written before roles were removed hold one action per role.
    assert next_action({"sales": "Sell it.", "strategist": "Study it."}) == "Study it."
    assert next_action({"sales": "Sell it."}) == "Sell it."
    assert next_action({}) is None and next_action(None) is None


# ---------------------------------------------------------------------------
# AC-04 / FR-12 — multi-select filtering
# ---------------------------------------------------------------------------

def test_all_four_filter_dimensions_apply():
    t = topic("OS-1")
    assert _matches(t, {"vertical": ["manufacturing"]})
    assert not _matches(t, {"vertical": ["retail"]})
    assert _matches(t, {"domain": ["ox_smart_industries"]})
    assert not _matches(t, {"domain": ["cloud"]})
    assert _matches(t, {"persona": ["coo_production"]})
    assert not _matches(t, {"persona": ["ciso"]})
    assert _matches(t, {"geography": ["FR"]})
    assert not _matches(t, {"geography": ["JP"]})


def test_multi_select_is_a_union_within_a_dimension():
    t = topic("OS-1")
    assert _matches(t, {"vertical": ["retail", "manufacturing"]})


def test_dimensions_combine_as_an_intersection():
    t = topic("OS-1")
    assert _matches(t, {"vertical": ["manufacturing"], "domain": ["ox_smart_industries"]})
    assert not _matches(t, {"vertical": ["manufacturing"], "domain": ["cloud"]})


def test_a_topic_without_geography_is_global_not_excluded():
    t = topic("OS-1", geographies=[])
    assert _matches(t, {"geography": ["JP"]})


def test_free_text_search_covers_statement_and_claims():
    t = topic("OS-1", why_hot=[{"claim": "A regulator mandated continuous monitoring.", "signals": ["SIG-1"]}])
    assert _matches(t, {"q": "rotating equipment"})
    assert _matches(t, {"q": "regulator mandated"})
    assert not _matches(t, {"q": "quantum"})


# ---------------------------------------------------------------------------
# Sorting and the new filter dimensions (§4.3.3, §4.3.4)
# ---------------------------------------------------------------------------

def sized(topic_id: str, sam: float | None, level: str | None = "medium", **overrides):
    return topic(
        topic_id,
        market_size_summary=({"method": "bottom_up_adoption", "sam_base": sam,
                              "tam_base": (sam or 0) * 2, "confidence": "observed"}
                             if sam is not None else None),
        competition=({"level": level, "level_label": level.title()} if level else None),
        **overrides,
    )


def test_unsized_topics_sort_last_not_first():
    """§4.3.4 leaves a topic unsized on purpose when nothing attributable exists.

    An unsized topic is not a topic worth nothing, so it must never lead a
    "largest market" list by being read as zero — or as infinity.
    """
    ordered = _sorted([sized("OS001", None), sized("OS002", 5e9), sized("OS003", 1e6)], "market_size")
    assert [t["id"] for t in ordered] == ["OS002", "OS003", "OS001"]


def test_competition_sort_puts_the_open_field_first():
    """"Where would we not be fighting four incumbents" is the question this answers."""
    ordered = _sorted(
        [sized("OS001", 1e6, "high"), sized("OS002", 1e6, "none"), sized("OS003", 1e6, "medium")],
        "competition",
    )
    assert [t["id"] for t in ordered] == ["OS002", "OS003", "OS001"]


@pytest.mark.parametrize("sort", sorted(SORTS))
def test_every_advertised_sort_is_total_and_stable(sort):
    """A sort the API advertises must order every topic, including empty ones."""
    topics = [sized("OS001", None, None, attractiveness=None, right_to_win=None,
                    signal_count=0, last_refresh="2026-01-01"),
              sized("OS002", 2e6, "high", last_refresh="2026-08-01"),
              sized("OS003", 1e6, "low", last_refresh="2026-05-01")]
    ordered = _sorted(topics, sort)
    assert sorted(t["id"] for t in ordered) == ["OS001", "OS002", "OS003"]


def test_competition_and_brief_filters():
    """§4.3.3 / FR-18 made two new facts filterable."""
    crowded = sized("OS001", 1e6, "high", has_brief=True)
    open_field = sized("OS002", 1e6, "low", has_brief=False)
    assert _matches(open_field, {"competition": ["low", "none"]})
    assert not _matches(crowded, {"competition": ["low", "none"]})
    assert _matches(crowded, {"has_brief": True})
    assert not _matches(open_field, {"has_brief": True})


# ---------------------------------------------------------------------------
# The list projection (payload discipline)
# ---------------------------------------------------------------------------

def test_list_rows_drop_detail_only_fields_but_keep_what_they_render():
    """§4.9 puts the decomposition on the topic page; the list ships what it shows."""
    row = _for_list(topic(
        "OS001",
        why_hot=[{"claim": "c", "signals": ["SIG-1"]}],
        next_action="do the thing",
        provenance={"pipeline_version": "0.1.0"},
        competition={"level": "high", "level_label": "High", "meaning": "crowded",
                     "competitors": [{"id": "x"}], "inputs": {"bands": {}}},
        conviction={"assessed": 1, "score": 60.0,
                    "axes": {"customer_demand": {"score": 60.0, "voices": [{"author": "a"}]}}},
    ))
    for dropped in ("links", "why_hot", "provenance"):
        assert dropped not in row
    # The CLI list prints the next action, so it stays on the row.
    assert row["next_action"] == "do the thing"
    # ... and everything a row or a marker actually draws survives.
    for kept in ("id", "statement", "labels", "triple", "domains", "horizon", "state",
                 "attractiveness", "right_to_win", "portfolio_distance", "signal_count",
                 "evidence_gap_warning", "competition"):
        assert kept in row
    # The level survives; the roster does not.
    assert row["competition"]["level"] == "high"
    assert "competitors" not in row["competition"]
    assert "voices" not in row["conviction"]["axes"]["customer_demand"]
