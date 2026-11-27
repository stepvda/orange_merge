"""Running the pipeline on small or rate-limited models (2026-11-26).

Each test pins one fix made to get synthesis working on qwen-fast without
lowering the quality bar: tolerant JSON parsing with JSON mode off, the
rate-limit handling, the drafting rule, and a critic that fails closed.
"""

from __future__ import annotations

import copy
import types

import pytest

import radar.llm as llm
from radar.config import get_config
from radar.db import Database
from radar.llm import LLMClient, LLMError, LLMResponse
from radar.pipeline import prompts
from radar.pipeline.synthesis import Candidate, SynthesisStats, Synthesiser


# ---------------------------------------------------------------------------
# Tolerant JSON parsing — what a model wraps around JSON when JSON mode is off
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"candidates": [1]}',
    '```json\n{"candidates": [1]}\n```',
    '<think>The evidence shows a tender…</think>\n{"candidates": [1]}',
    'Here is the JSON you asked for:\n{"candidates": [1]}\nHope this helps.',
    # The model finishes the object and keeps going (deepseek-chat, 2026-11-27).
    '{"candidates": [1]}\n{"candidates": [2]}',
    '{"candidates": [1]}\n\nNote: field {x} was left out.',
])
def test_json_is_recovered_from_common_wrappers(text):
    assert LLMResponse(text=text, model="m").json() == {"candidates": [1]}


def test_text_without_json_still_fails_loudly():
    with pytest.raises(LLMError):
        LLMResponse(text="I could not find anything.", model="m").json()


# ---------------------------------------------------------------------------
# JSON mode switch and rate-limit handling
# ---------------------------------------------------------------------------

class _FakeCompletions:
    def __init__(self, failures: list[Exception] | None = None):
        self.calls: list[dict] = []
        self.failures = list(failures or [])

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        message = types.SimpleNamespace(content='{"ok": true}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)


def _client(monkeypatch, completions, **env) -> LLMClient:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    for key in ("RADAR_LLM_JSON_MODE", "RADAR_LLM_MAX_RPM"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    monkeypatch.setattr(LLMClient, "_build_client", lambda self: fake)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    return LLMClient(provider="openai", strong_model="qwen-fast", cheap_model="qwen-fast", max_retries=0)


def test_json_mode_is_sent_by_default(monkeypatch):
    completions = _FakeCompletions()
    _client(monkeypatch, completions).complete_json("sys json", "user")
    assert completions.calls[0]["response_format"] == {"type": "json_object"}


def test_json_mode_can_be_switched_off_per_endpoint(monkeypatch):
    completions = _FakeCompletions()
    result = _client(monkeypatch, completions, RADAR_LLM_JSON_MODE="0").complete_json("sys json", "user")
    assert "response_format" not in completions.calls[0]
    assert result == {"ok": True}


class _RateLimited(Exception):
    status_code = 429


def test_a_429_is_waited_out_without_spending_a_retry(monkeypatch):
    completions = _FakeCompletions([_RateLimited("Error code: 429"), _RateLimited("Error code: 429")])
    client = _client(monkeypatch, completions)          # max_retries=0: any real failure is final
    assert client.complete_json("sys json", "user") == {"ok": True}
    assert len(completions.calls) == 3


def test_a_real_failure_still_fails(monkeypatch):
    completions = _FakeCompletions([RuntimeError("boom")])
    with pytest.raises(LLMError):
        _client(monkeypatch, completions).complete_json("sys json", "user")


def test_rate_limiter_holds_the_request_rate(monkeypatch):
    clock = {"now": 0.0}
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock["now"])

    def sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds
    monkeypatch.setattr(llm.time, "sleep", sleep)

    limiter = llm._RateLimiter()
    for _ in range(3):
        limiter.acquire(3)
    assert slept == []                                  # within the budget: no wait
    limiter.acquire(3)
    assert slept and clock["now"] >= 60.0               # the 4th waits for the window


# ---------------------------------------------------------------------------
# Drafting rule 5
# ---------------------------------------------------------------------------

def _cfg_with_minimum(minimum: int):
    cfg = copy.copy(get_config())
    cfg.settings = copy.deepcopy(cfg.settings)
    cfg.settings["curation"]["min_candidates_per_pass"] = minimum
    return cfg


def test_minimum_zero_keeps_the_original_rule():
    text = prompts.synthesis_system_prompt(_cfg_with_minimum(0))
    assert "An\n   empty list is a valid and often correct answer." in text


def test_a_minimum_asks_for_drafts_and_never_for_invention():
    text = prompts.synthesis_system_prompt(_cfg_with_minimum(1))
    assert "Return between 1 and 3 candidates" in text
    assert "often correct answer" not in text
    assert "Never invent a fact" in text
    # Small models copied the GOOD examples; the rule against it is always on.
    assert "Never reuse a GOOD example" in text


# ---------------------------------------------------------------------------
# Example echo — copies of the prompt's GOOD examples are rejected
# ---------------------------------------------------------------------------

from radar.pipeline.synthesis import example_echo  # noqa: E402


@pytest.mark.parametrize("statement, why_now", [
    # Both seen from qwen-fast on 2026-11-26, each scored 5/5 by its critic.
    ("Retail banks detecting branch WAN attacks with SD-WAN telemetry fed into a SOC",
     "DORA ICT-risk rules apply from January 2025"),
    ("Ministries answering citizen queries from policy documents using sovereign RAG enclaves",
     "SecNumCloud now required for sensitive public data"),
])
def test_a_reworded_example_is_caught(statement, why_now):
    assert example_echo(statement, why_now)


@pytest.mark.parametrize("statement, why_now", [
    ("Coastal administrations monitoring maritime traffic with satellite SAR analytics",
     "Norwegian Coastal Administration launching satellite SAR procurement in 2026"),
    ("NHS ambulance trusts training major-incident triage teams with virtual reality mass-casualty simulations",
     "NEAS EPRR Training VR Project tender"),
])
def test_an_original_space_is_not_mistaken_for_an_echo(statement, why_now):
    assert example_echo(statement, why_now) is None


# ---------------------------------------------------------------------------
# Critic fails closed
# ---------------------------------------------------------------------------

class _BrokenCritic(LLMClient):
    def __init__(self):
        super().__init__(provider="mock")
        self.attempts = 0

    def complete_json(self, system, user, **kwargs):
        self.attempts += 1
        raise LLMError("Model did not return valid JSON: Unterminated string")


def test_a_candidate_the_critic_could_not_judge_is_rejected(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init_schema()
    critic = _BrokenCritic()
    synth = Synthesiser(get_config(), db, critic)
    stats = SynthesisStats()
    candidate = Candidate(
        vertical="healthcare", use_case="workforce_training_xr", technology="ar_vr_xr",
        statement="NHS ambulance trusts training triage teams with virtual reality simulations",
        why_hot=[{"claim": "A VR training tender was published.", "signals": ["SIG-1"]}],
    )
    assert synth._criticise(candidate, {"signals": []}, [], stats) is False
    assert critic.attempts == 2                          # one retry before giving up
    assert stats.failed_critic == 1
    assert stats.rejections[-1]["reason"].startswith("critic unavailable")


# ---------------------------------------------------------------------------
# Description sections written as bare strings (deepseek-chat, 2026-11-27)
# ---------------------------------------------------------------------------

def test_a_bare_string_section_is_kept_and_cited_ones_still_need_evidence():
    from radar.pipeline.describe import DescriptionGenerator
    generator = DescriptionGenerator.__new__(DescriptionGenerator)
    prose = "A card issuer has put an agent assist in front of its contact centre for disputes."
    payload = {"sections": {
        "summary": prose,                                        # uncited section: kept
        "what_is_changing": prose + " See SIG-ABCDEF123456.",    # cites a real id: kept
        "competitive_landscape": prose,                          # cited section, no id: dropped
    }}
    out, stripped = generator._validate_sections(payload, {"SIG-ABCDEF123456"}, [])
    assert out["summary"] == {"text": prose, "signals": []}
    assert out["what_is_changing"]["signals"] == ["SIG-ABCDEF123456"]
    assert "competitive_landscape" not in out
    assert {"section": "competitive_landscape",
            "reason": "no claim survived evidence binding"} in stripped
