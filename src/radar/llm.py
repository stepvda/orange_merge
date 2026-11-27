"""Provider-agnostic LLM access.

§4.4.6: the pipeline is model-agnostic behind a thin abstraction, because the
economics change every few months. Practically — a small, cheap model for the
high-volume gating and classification stages; a strong model for synthesis and
critique, where quality dominates cost because the volume is low.

NFR-05 / NFR-10: Orange sells trusted AI on sovereign infrastructure, so the
design must not depend on any single provider. Every provider below speaks the
OpenAI-compatible wire format, which is what Ollama and most sovereign
inference stacks expose — switching is an .env change, not a re-architecture.

Two hard rules from §4.4.4 are enforced here rather than left to prompts:
  * every call records the prompt version that produced it (DR-10);
  * `NO_NUMBERS_RULE` is appended to every generative system prompt, because
    "no model-generated numbers" is the rule most worth enforcing rigidly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# §4.4.4 defence 3. Market sizes, growth rates and percentages are never
# generated — they are looked up, attributed and dated, or they are absent.
NO_NUMBERS_RULE = (
    "\n\nHARD RULE — NO GENERATED NUMBERS: never state a market size, growth rate, "
    "percentage, monetary value, headcount or date that does not appear verbatim in "
    "the evidence provided to you. If you want to express magnitude, use qualitative "
    "language instead. Inventing a figure invalidates the entire output."
)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Any = field(default=None, repr=False)

    def json(self) -> Any:
        """Parse the response as JSON, tolerating what models wrap around it.

        With JSON mode on, the provider guarantees a bare object. With it off
        (RADAR_LLM_JSON_MODE=0, for endpoints whose JSON mode is broken), the
        prompt still asks for JSON but the model may add a markdown fence, a
        `<think>` block (Qwen3 and other reasoning models) or a sentence of
        preamble. Those are stripped, and the first complete object is parsed.

        "First complete object", not "first `{` to last `}`": deepseek-chat
        sometimes finishes the object and then keeps writing — a second object,
        a closing fence, a note — and the span to the last brace then contains
        two documents ("Extra data", seen on description generation
        2026-11-27). raw_decode stops where the first object ends.
        """
        text = _THINK_BLOCK.sub("", self.text).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            start = text.find("{")
            if start >= 0:
                try:
                    value, _ = json.JSONDecoder().raw_decode(text, start)
                    return value
                except json.JSONDecodeError:
                    pass
            raise LLMError(f"Model did not return valid JSON: {exc}\n---\n{self.text[:800]}") from exc


#: How many 429s one call will wait out before it is reported as failed.
_MAX_RATE_LIMIT_WAITS = 8

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


class _RateLimiter:
    """Requests-per-minute ceiling shared by every client in the process.

    Synthesis runs clusters and passes concurrently, and every LLMClient the
    API or CLI builds talks to the same endpoint, so the limit has to be
    process-wide: a per-client limiter would let four clients each spend the
    whole budget. A sliding one-minute window, because that is how providers
    count ("60 requests/min") — a token bucket refilled per second would still
    allow a burst the provider rejects.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sent: deque[float] = deque()

    def acquire(self, rpm: int) -> None:
        if rpm <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= 60.0:
                    self._sent.popleft()
                if len(self._sent) < rpm:
                    self._sent.append(now)
                    return
                wait = 60.0 - (now - self._sent[0]) + 0.05
            time.sleep(wait)


_RATE_LIMITER = _RateLimiter()


def _is_rate_limited(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 429 or "429" in str(exc)[:40]


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_reasoning_model(model: str) -> bool:
    """OpenAI reasoning models (gpt-5*, o1/o3/o4*) take a different parameter set."""
    name = model.lower().rsplit("/", 1)[-1]  # tolerate gateway prefixes like "openai/gpt-5.4"
    return name.startswith("gpt-5") or (len(name) > 1 and name[0] == "o" and name[1].isdigit())


class LLMClient:
    """Thin wrapper over an OpenAI-compatible chat completions endpoint."""

    #: Optional per-call usage sink (radar.footprint.Ledger.record). Class-level
    #: so every client the API or CLI builds reports into the same ledger
    #: without each call site having to pass one in. Never set in unit tests
    #: that do not ask for it, and never allowed to fail a call.
    usage_sink: Any = None

    def __init__(
        self,
        provider: str | None = None,
        strong_model: str | None = None,
        cheap_model: str | None = None,
        max_retries: int = 2,
    ):
        self.provider = (provider or os.getenv("RADAR_LLM_PROVIDER", "deepseek")).lower()
        self.max_retries = max_retries
        self.strong_model = strong_model or os.getenv("RADAR_LLM_MODEL_STRONG", "deepseek-chat")
        self.cheap_model = cheap_model or os.getenv("RADAR_LLM_MODEL_CHEAP", "deepseek-chat")
        # Endpoint quirks, set beside the endpoint in .env rather than in
        # settings.yaml, because they belong to the provider, not the radar.
        # JSON mode: some OpenAI-compatible servers implement response_format
        # with constrained decoding that steers small models to the shortest
        # valid object — `{"candidates": []}` in 7 tokens, every time (seen
        # with qwen-fast, 2026-11-26). Off means the prompt alone asks for JSON
        # and LLMResponse.json() tolerates fences and <think> blocks.
        self.json_mode = _env_flag("RADAR_LLM_JSON_MODE", True)
        # Provider rate limit in requests per minute; 0 = unlimited.
        try:
            self.max_rpm = int(os.getenv("RADAR_LLM_MAX_RPM", "0") or 0)
        except ValueError:
            self.max_rpm = 0
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._client = None if self.provider == "mock" else self._build_client()

    def _build_client(self):
        from openai import OpenAI

        if self.provider == "deepseek":
            key = os.getenv("DEEPSEEK_API_KEY")
            if not key:
                raise LLMError("DEEPSEEK_API_KEY is not set. Copy .env.example to .env and fill it in.")
            return OpenAI(api_key=key, base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
        if self.provider == "openai":
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                raise LLMError("OPENAI_API_KEY is not set.")
            return OpenAI(api_key=key, base_url=os.getenv("OPENAI_BASE_URL") or None)
        if self.provider == "ollama":
            # Sovereign / local deployment path (NFR-05). Ollama needs no key.
            return OpenAI(api_key="ollama", base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
        raise LLMError(f"Unknown LLM provider {self.provider!r}. Supported: deepseek, openai, ollama, mock.")

    # -- core call ---------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        *,
        strong: bool = False,
        temperature: float = 0.2,
        json_mode: bool = False,
        max_tokens: int = 4000,
        apply_no_numbers_rule: bool = True,
    ) -> LLMResponse:
        model = self.strong_model if strong else self.cheap_model
        if apply_no_numbers_rule:
            system = system + NO_NUMBERS_RULE
        if json_mode:
            # DeepSeek's JSON mode requires the word "json" in the prompt.
            if "json" not in (system + user).lower():
                system += "\n\nRespond with a single valid JSON object."

        if self.provider == "mock":
            return self._mock(system, user, model, json_mode)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if _is_reasoning_model(model):
            # GPT-5 / o-series reject `max_tokens` and any non-default temperature.
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["temperature"] = temperature
            kwargs["max_tokens"] = max_tokens
        if json_mode and self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        # A rate-limit refusal is not a failure of the request, so it does not
        # spend one of the request's retries: it waits and goes again, up to a
        # ceiling, so a saturated endpoint slows a run down instead of turning
        # into "LLM call failed" and silently dropping a synthesis pass.
        rate_limited = 0
        attempt = 0
        while attempt <= self.max_retries:
            _RATE_LIMITER.acquire(self.max_rpm)
            try:
                started = time.monotonic()
                resp = self._client.chat.completions.create(**kwargs)
                usage = getattr(resp, "usage", None)
                self.calls += 1
                if usage:
                    self.prompt_tokens += usage.prompt_tokens or 0
                    self.completion_tokens += usage.completion_tokens or 0
                if LLMClient.usage_sink is not None:
                    try:
                        LLMClient.usage_sink(
                            provider=self.provider, model=model,
                            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                            latency_ms=int((time.monotonic() - started) * 1000),
                        )
                    except Exception as sink_exc:  # noqa: BLE001 — accounting never fails a call
                        log.warning("Usage sink failed: %s", sink_exc)
                return LLMResponse(
                    text=resp.choices[0].message.content or "",
                    model=model,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    raw=resp,
                )
            except Exception as exc:  # noqa: BLE001 — provider SDKs raise varied types
                last_error = exc
                if _is_rate_limited(exc) and rate_limited < _MAX_RATE_LIMIT_WAITS:
                    rate_limited += 1
                    wait = _retry_after(exc) or min(60.0, 5.0 * rate_limited)
                    log.info("LLM endpoint rate-limited (429); waiting %.0fs (%d/%d)",
                             wait, rate_limited, _MAX_RATE_LIMIT_WAITS)
                    time.sleep(wait)
                    continue
                if attempt < self.max_retries:
                    backoff = 2 ** attempt
                    log.warning("LLM call failed (attempt %d/%d): %s — retrying in %ds",
                                attempt + 1, self.max_retries + 1, exc, backoff)
                    time.sleep(backoff)
                attempt += 1
        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}") from last_error

    def complete_json(self, system: str, user: str, **kwargs) -> Any:
        kwargs.setdefault("json_mode", True)
        return self.complete(system, user, **kwargs).json()

    # -- mock provider -----------------------------------------------------

    def _mock(self, system: str, user: str, model: str, json_mode: bool) -> LLMResponse:
        """Deterministic stub so the pipeline and tests run with no network.

        Keyed on a hash of the prompt so repeated runs are identical (SC-11).
        """
        self.calls += 1
        digest = hashlib.sha256((system + user).encode()).hexdigest()
        if not json_mode:
            return LLMResponse(text=f"[mock output {digest[:8]}]", model="mock")
        # Shape-appropriate stubs, chosen by a marker the caller puts in the system prompt.
        if "MOCK_KIND=signal_type" in system:
            payload = {"items": []}
        elif "MOCK_KIND=synthesis" in system:
            payload = {"candidates": []}
        elif "MOCK_KIND=sharpen" in system or "MOCK_KIND=rewrite" in system:
            # No rewrite: the deterministic title rules then decide on the
            # original wording, which is what the tests exercise.
            payload = {}
        elif "MOCK_KIND=same_opportunity" in system:
            payload = {"same": False, "reason": "mock"}
        elif "MOCK_KIND=critic" in system:
            payload = {"score": 3, "verdict": "revise", "notes": "mock", "issues": []}
        elif "MOCK_KIND=relevance" in system:
            payload = {"items": []}
        elif "MOCK_KIND=brief_support" in system:
            # Supports nothing. The tests that reach this branch are about what
            # happens when the vocabulary test came up short AND the model agrees
            # the evidence is only adjacent — the case that must block.
            payload = {"supporting": [], "note": "mock"}
        elif "MOCK_KIND=scoping" in system:
            # Shaped like a first turn: a question, nothing understood, nothing
            # ready. The scoping tests are about what the SERVER does with a
            # reply — resolving ids, re-retrieving each brief, refusing a ready
            # flag the corpus does not support — so the stub has to be a
            # well-formed reply rather than a marker.
            payload = {"reply": "[mock question]", "understood": {}, "missing": ["vertical"],
                       "asking_for": "vertical", "suggestions": [], "ready": False, "briefs": []}
        elif "MOCK_KIND=presales" in system:
            # One superset payload rather than a branch per collateral kind:
            # each writer in `presales.content` picks out the keys it needs and
            # ignores the rest, so a single stub exercises every validator on
            # the real path. Deliberately well-formed — the presales tests are
            # about what the RENDERERS do with valid content (does the deck
            # paginate, does the battlecard place its dots, does a missing
            # section produce a note instead of a hole), and a marker payload
            # would short-circuit all of it into the fallback branch.
            payload = {
                "buying_centre": [
                    {"role": "Operations director", "stance": "economic buyer",
                     "cares_about": "unplanned downtime on the line",
                     "trigger": "a regulatory audit due this year"},
                    {"role": "Plant IT manager", "stance": "technical evaluator",
                     "cares_about": "integrating without touching the control network",
                     "trigger": "an ageing gateway estate"},
                ],
                "qualification": [{"criterion": "Economic buyer",
                                   "question": "Who signs for downtime reduction?",
                                   "what_good_looks_like": "A named director with a budget line"}],
                "disqualifiers": ["No budget cycle before next year"],
                "components": [{"label": "Connectivity", "provider": "orange", "note": "private network"},
                               {"label": "Edge analytics", "provider": "third_party",
                                "note": "to be sourced"}],
                "interfaces": [{"between": "Connectivity and Edge analytics",
                                "carries": "telemetry from the plant floor"}],
                "open_questions": ["Which control system is in place?"],
                "field": "A crowded field of integrators with one telco incumbent.",
                "cards": [{"competitor": "Example Competitor", "their_pitch": "One vendor, one throat to choke",
                           "strong_where": "Existing estate on the customer site",
                           "thin_where": "No sovereign hosting option",
                           "trap_question": "Where does the data physically reside?",
                           "our_proof": "No named proof point yet — this is a gap.",
                           "reach": "high", "depth": "medium",
                           "dimensions": [{"dimension": "Sovereignty", "verdict": "Orange ahead"}]}],
                "drivers": [{"driver": "Unplanned downtime",
                             "mechanism": "Failures are caught before the line stops"}],
                "cost_of_inaction": "The estate keeps ageing and each outage is absorbed as cost.",
                "proof_plan": ["Replay last year's outage log against the model"],
                "slides": [{"title": "What has changed", "bullets": ["Regulation now bites"],
                            "notes": "Open on the change, not on Orange."}],
                "phases": [{"label": "Instrument", "weeks": 2, "deliverable": "Telemetry flowing"},
                           {"label": "Prove", "weeks": 3, "deliverable": "Detection report"}],
                "in_scope": ["One production line"],
                "out_scope": ["Rollout to other sites"],
                "success_criteria": [{"criterion": "Detection ahead of failure",
                                      "measured_by": "Alerts raised before the recorded stoppage"}],
                "customer_provides": ["Access to the historian"],
                "blocks": [{"section": "Our understanding of the requirement",
                            "answer": "We understand that [customer name] needs to reduce "
                                      "unplanned stoppages on its production lines without "
                                      "changing the control network. Our approach separates the "
                                      "telemetry path from the control path entirely."}],
                "emails": [{"stage": "first touch", "subject": "downtime on the [company] lines",
                            "body": "Hello [first name], we have been looking at how plants in "
                                    "your sector are catching failures earlier without touching "
                                    "the control network. Worth a short conversation?"}],
                "options": [{"model": "Subscription", "how_it_works": "A per-site monthly fee",
                             "orange_risk": "low", "customer_appeal": "medium",
                             "levers": ["term", "volume commitment"],
                             "use_when": "The customer wants predictable operating cost"}],
                "risks": [{"risk": "The control network owner blocks access", "likelihood": "medium",
                           "impact": "high", "mitigation": "Prove the telemetry path is read-only",
                           "owner_role": "Solution architect"}],
                "gaps": [{"capability": "Edge analytics", "why_needed": "Detection runs on site",
                          "candidate_type": "systems integrator with OT experience"}],
                "the_ask": "Confirm a partner for edge analytics before the customer workshop.",
                "what_orange_brings": ["The connectivity and the sovereign hosting"],
            }
        else:
            payload = {"mock": True, "digest": digest[:16]}
        return LLMResponse(text=json.dumps(payload), model="mock")

    # -- reporting ---------------------------------------------------------

    def usage_summary(self) -> dict[str, Any]:
        """NFR-10: per-refresh inference cost is measured and reported."""
        return {
            "provider": self.provider,
            "strong_model": self.strong_model,
            "cheap_model": self.cheap_model,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
