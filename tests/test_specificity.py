"""Title rules, the stricter critic path, cross-radar dedup and the rewrite job (2026-11-25)."""

from __future__ import annotations

import pytest

from radar.config import get_config
from radar.db import Database
from radar.llm import LLMClient
from radar.pipeline import prompts, specificity
from radar.pipeline.rewrite import StatementRewriter
from radar.pipeline.synthesis import Candidate, SynthesisStats, Synthesiser


@pytest.fixture(scope="module")
def cfg():
    return get_config()


@pytest.fixture(scope="module")
def tech(cfg):
    return specificity.technology_phrases(cfg)


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    database.init_schema()
    return database


@pytest.fixture()
def synth(cfg, db):
    return Synthesiser(cfg, db, LLMClient(provider="mock"), portfolio_overlap=None)


GOOD = dict(statement="Chemicals plants preventing exposure incidents with wearables that predict worker fatigue",
            buyer="chemicals plants", why_now="Seveso sites under pressure after exposure incidents",
            who_signs="Plant HSE director", use_case_phrase="preventing exposure incidents",
            technology_phrase="wearables")


def make(**overrides) -> Candidate:
    base = dict(vertical="manufacturing", use_case="worker_safety_monitoring", technology="wearables",
                why_hot=[{"claim": "Deployments were published.", "signals": []}], **GOOD)
    base.update(overrides)
    return Candidate(**base)


# -- the rules ---------------------------------------------------------------

def test_every_worked_example_passes_the_rules(tech):
    for e in prompts.POSITIVE_EXAMPLES:
        assert specificity.lint(e["statement"], buyer=e["buyer"], why_now=e["why_now"],
                                who_signs=e["who_signs"], tech_phrases=tech,
                                settings={"require_triple": True, "max_title_words": 22},
                                use_case_phrase=e["use_case_phrase"],
                                technology_phrase=e["technology_phrase"]).ok, e["statement"]


@pytest.mark.parametrize("statement, rule", [
    ("Confidential computing for hospitals keeping clinical data private", "opens with the technology"),
    ("Hospitals can keep clinical data private with confidential computing", "hedge word"),
    ("Hospitals running resilient, governed and sovereign clinical data platforms", "filler words"),
    ("Hospitals cutting incident response time " + "with more words " * 8, "words; keep it"),
])
def test_a_generic_title_breaks_a_named_rule(tech, statement, rule):
    issues = specificity.lint(statement, buyer="hospitals", why_now="x", who_signs="CIO", tech_phrases=tech).issues
    assert any(rule in i for i in issues), issues


def test_buyer_why_now_and_who_signs_are_required(tech):
    issues = specificity.lint("Port authorities cutting berth idle time with predictive scheduling",
                              tech_phrases=tech).issues
    assert any("no named buyer" in i for i in issues)
    assert any("why now" in i for i in issues)
    assert any("who signs" in i for i in issues)


def test_the_buyer_must_appear_in_the_title(tech):
    issues = specificity.lint("Port authorities cutting berth idle time with predictive scheduling",
                              buyer="regional hospital groups", why_now="x", who_signs="y",
                              tech_phrases=tech).issues
    assert any("does not appear in the title" in i for i in issues)


# -- the synthesis path ------------------------------------------------------

def test_a_title_the_model_cannot_fix_is_dropped(synth):
    stats = SynthesisStats()
    bad = make(statement="Wearables can provide resilient and governed safety monitoring for workers")
    assert synth._sharpen(bad, {"signals": []}, stats) is False
    assert stats.failed_specificity == 1 and stats.sharpened == 1
    assert "title rules" in stats.rejections[-1]["reason"]


def test_a_title_that_passes_is_not_sent_back(synth):
    stats = SynthesisStats()
    assert synth._sharpen(make(), {"signals": []}, stats) is True
    assert stats.sharpened == 0


def test_a_score_of_three_now_fails_the_critic(synth):
    # The mock critic answers 3 — the score that used to ship as "borderline".
    stats = SynthesisStats()
    assert synth._criticise(make(), {"signals": []}, [], stats) is False
    assert stats.failed_critic == 1


def _insert(db, topic_id, statement, triple=("manufacturing", "worker_safety_monitoring", "wearables")):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO opportunity_spaces (id, vertical, use_case, technology, statement, state, why_hot, "
            "first_seen, last_refresh, pipeline_version) VALUES (?,?,?,?,?,'active','[]','2026-11-01',"
            "'2026-11-01','t')", (topic_id, *triple, statement))


def test_a_reworded_radar_space_attaches_instead_of_creating(synth, db):
    _insert(db, "OS001", GOOD["statement"])
    # Same opportunity, different cell: it must not create a second space.
    twin = make(technology="low_power_sensors")
    stats = SynthesisStats()
    kept = synth._against_radar([twin], stats)
    assert twin.merge_into == "OS001" and stats.merged_into_existing == 1
    synth._persist(kept, "R-1", stats)
    assert stats.created_ids == [] and stats.updated_ids == ["OS001"]
    count = db.query_one("SELECT COUNT(*) n FROM opportunity_spaces")["n"]
    assert count == 1


def test_one_run_creates_at_most_two_spaces_per_technology(synth):
    stats = SynthesisStats()
    batch = [make(vertical=v, statement=f"{name} preventing exposure incidents with fatigue-predicting wearables",
                  buyer=name, critic_score=score)
             for v, name, score in (("manufacturing", "Chemicals plants", 4), ("energy", "Refineries", 5),
                                    ("construction", "Tunnel builders", 4))]
    kept = synth._against_radar(batch, stats)
    assert len(kept) == 2 and stats.capped_by_technology == 1
    assert {c.buyer for c in kept} >= {"Refineries"}, "the best critic score survives the cap"


def test_persist_stores_the_subtitle(synth, db):
    stats = SynthesisStats()
    synth._persist([make()], "R-1", stats)
    row = db.query_one("SELECT why_now, who_signs, buyer FROM opportunity_spaces WHERE id = ?",
                       (stats.created_ids[0],))
    assert row["why_now"] == GOOD["why_now"] and row["who_signs"] == GOOD["who_signs"]


# -- the rewrite job ---------------------------------------------------------

def test_rewrite_proposes_reviews_and_applies(cfg, db, tmp_path):
    _insert(db, "OS001", "Wearables can provide resilient monitoring for workers in factories")
    _insert(db, "OS002", "Port authorities cutting berth idle time with predictive scheduling",
            ("transport_logistics", "predictive_maintenance", "machine_learning"))
    rewriter = StatementRewriter(cfg, db, LLMClient(provider="mock"))
    result = rewriter.propose()
    assert {r["id"] for r in result["rewrites"]} == {"OS001", "OS002"}
    # The mock model rewrites nothing, so neither title can pass the rules.
    assert all(r["status"] == "still generic" and not r["apply"] for r in result["rewrites"])

    path = rewriter.write_review(result, tmp_path / "review.xlsx")
    from openpyxl import load_workbook
    wb = load_workbook(path)
    ws = wb["Rewrites"]
    header = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[0].value == "OS002":
            row[header.index("Apply")].value = "yes"
            row[header.index("Why now")].value = "Congestion fees at EU ports from 2026"
            row[header.index("Who signs")].value = "Port operations director"
            row[header.index("Buyer")].value = "port authorities"
    wb.save(path)

    stats = rewriter.apply(path, describe=False)
    assert stats["titles_applied"] == 1
    row = db.query_one("SELECT why_now, who_signs, version, prompt_version FROM opportunity_spaces "
                       "WHERE id = 'OS002'")
    assert row["who_signs"] == "Port operations director" and row["version"] == 2
    assert row["prompt_version"] == prompts.PROMPT_VERSION_REWRITE


# -- title alone answers vertical x use case x technology (2026-11-25) --------

_TRIPLE = {"require_triple": True, "max_title_words": 22}
_GOOD = "Motor insurers settling simple windscreen claims end to end with an agentic AI claims assistant"


def _triple(statement, **kw):
    base = dict(buyer="motor insurers", why_now="Storm claim surges in 2026", who_signs="Chief claims officer",
                settings=_TRIPLE)
    return specificity.lint(statement, **(base | kw)).issues


def test_triple_passes_when_both_phrases_are_in_the_title():
    assert _triple(_GOOD, use_case_phrase="settling simple windscreen claims",
                   technology_phrase="agentic AI claims assistant") == []


def test_triple_requires_a_use_case_phrase():
    issues = _triple(_GOOD, technology_phrase="agentic AI claims assistant")
    assert any("use case" in i for i in issues)


def test_triple_phrase_must_appear_in_title():
    issues = _triple(_GOOD, use_case_phrase="handling fraud cases", technology_phrase="agentic AI claims assistant")
    assert any("does not appear" in i for i in issues)


def test_triple_rejects_a_bare_generic_technology():
    title = "Motor insurers settling simple windscreen claims end to end with AI"
    issues = _triple(title, use_case_phrase="settling simple windscreen claims", technology_phrase="AI")
    assert any("which technology" in i for i in issues)


def test_triple_falls_back_to_the_technology_synonyms():
    title = "Motor insurers settling simple windscreen claims with private 5G"
    assert _triple(title, use_case_phrase="settling simple windscreen claims", own_tech_phrases=["private 5g"]) == []
    assert any("no technology" in i for i in
               _triple(title, use_case_phrase="settling simple windscreen claims", own_tech_phrases=["leo"]))


def test_triple_off_by_default():
    assert specificity.lint(_GOOD, buyer="motor insurers", why_now="x", who_signs="CFO").ok
