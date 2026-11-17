"""AI footprint ledger — energy and carbon of the model calls the radar makes.

Ported idea (best-of selection, see docs/best_of_selection.md): the HuMaSoo
team wrapped their ingestion and LLM runs in CodeCarbon and EcoLogits and kept
the emissions log beside the data. For an Orange Business product — where
sustainability is a sold domain, not a footnote — "what did this radar cost the
planet to build" is a fair question for a buyer to ask, and NFR-10 already makes
inference cost a reported quantity.

This module does it without either library:

* CodeCarbon measures the *local* machine. The radar's heavy work happens on a
  hosted model, so a local power meter would measure the wrong computer.
* EcoLogits instruments provider SDKs and needs to know the model's parameter
  count. DeepSeek, OpenAI and Ollama are all served through one OpenAI-compatible
  client here, so the instrumented SDK would see one provider it may not know.

What it does instead is the honest version of the same estimate: every call's
token counts are recorded (exact — they come back from the provider), and the
energy is ESTIMATED from per-token factors kept in `config/settings.yaml`
(`footprint:`), then converted to CO2e with a configurable grid intensity. The
factors are stated, versioned and shown in the UI, so the number decomposes the
way every other number in the radar does — and anyone with a better factor can
change one line rather than trust an opaque meter.

Two populations are reported:

* **ledger** — one row per call from the moment this module existed, with the
  calling module recorded as the purpose (synthesis, description, brief…).
* **historic** — the per-run `llm_usage` totals that every refresh and
  generation run already stamped into `refreshes.stats` before the ledger
  existed. Only runs that started before the first ledger row are counted, so
  nothing is counted twice.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from typing import Any

from .db import Database, unjs

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    at                TEXT NOT NULL,
    purpose           TEXT NOT NULL,           -- calling module, e.g. pipeline.synthesis
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER,
    ok                INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_at ON llm_usage(at);
"""

#: Used when settings.yaml carries no `footprint:` block. Order-of-magnitude
#: figures for a large hosted model; see the settings file for their basis.
DEFAULT_FACTORS: dict[str, Any] = {
    "version": "fp-2026-11-a",
    "wh_per_1k_prompt_tokens": 0.1,
    "wh_per_1k_completion_tokens": 2.0,
    "pue": 1.2,
    "grid_gco2e_per_kwh": 400.0,
    "basis": "Default order-of-magnitude factors; calibrate in config/settings.yaml.",
}


def init_schema(db: Database) -> None:
    with db.cursor() as cur:
        cur.executescript(SCHEMA)


def factors(settings: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_FACTORS)
    out.update((settings or {}).get("footprint") or {})
    return out


def estimate(prompt_tokens: int, completion_tokens: int, f: dict[str, Any]) -> dict[str, float]:
    """Energy (Wh) and emissions (g CO2e) for a token count, under factors `f`.

    energy_wh = (prompt/1000 × wh_prompt + completion/1000 × wh_completion) × PUE
    co2e_g    = energy_wh / 1000 × grid_gco2e_per_kwh
    """
    it_wh = (prompt_tokens / 1000.0) * float(f["wh_per_1k_prompt_tokens"]) \
        + (completion_tokens / 1000.0) * float(f["wh_per_1k_completion_tokens"])
    energy_wh = it_wh * float(f["pue"])
    co2e_g = energy_wh / 1000.0 * float(f["grid_gco2e_per_kwh"])
    return {"energy_wh": round(energy_wh, 3), "co2e_g": round(co2e_g, 3)}


def calling_module(skip: tuple[str, ...] = ("radar.llm", "radar.footprint")) -> str:
    """The first `radar.*` module on the stack that is not the LLM plumbing.

    Recorded as the purpose so the ledger can say "synthesis spent this, the
    brief writer spent that" without threading a label through every call site.
    """
    frame = sys._getframe(1)
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if name.startswith("radar.") and name not in skip:
            return name.removeprefix("radar.")
        frame = frame.f_back
    return "unspecified"


class Ledger:
    """Writes one row per completed model call. Never raises into the caller."""

    def __init__(self, db: Database):
        self.db = db
        try:
            init_schema(db)
        except Exception as exc:  # noqa: BLE001 — a ledger must never break a call
            log.warning("Footprint ledger unavailable: %s", exc)

    def record(self, *, provider: str, model: str, prompt_tokens: int, completion_tokens: int,
               latency_ms: int | None = None, ok: bool = True, purpose: str | None = None) -> None:
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "INSERT INTO llm_usage (at, purpose, provider, model, prompt_tokens, "
                    "completion_tokens, latency_ms, ok) VALUES (?,?,?,?,?,?,?,?)",
                    (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     purpose or calling_module(), provider, model, int(prompt_tokens or 0),
                     int(completion_tokens or 0), latency_ms, 1 if ok else 0),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not record LLM usage: %s", exc)


def report(db: Database, settings: dict[str, Any] | None) -> dict[str, Any]:
    """Totals, splits and a monthly series for the Analytics tab."""
    init_schema(db)
    f = factors(settings)

    first = db.query_one("SELECT MIN(at) first FROM llm_usage")
    ledger_start = first["first"] if first else None

    # Historic per-run totals, only from before the ledger began.
    historic_rows = []
    for row in db.query("SELECT id, started_at, stats FROM refreshes ORDER BY started_at"):
        if ledger_start and row["started_at"] >= ledger_start:
            continue
        usage = (unjs(row["stats"]) or {}).get("llm_usage") or {}
        p, c = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        if not (p or c):
            continue
        historic_rows.append({
            "month": row["started_at"][:7], "purpose": "refresh/generation run (pre-ledger)",
            "model": usage.get("strong_model") or usage.get("cheap_model") or "unknown",
            "calls": int(usage.get("calls") or 0), "prompt_tokens": p, "completion_tokens": c,
        })

    ledger_rows = [dict(r) for r in db.query(
        "SELECT substr(at,1,7) month, purpose, model, COUNT(*) calls, "
        "SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens "
        "FROM llm_usage GROUP BY 1,2,3"
    )]

    rows = historic_rows + ledger_rows

    def fold(key: str) -> list[dict[str, Any]]:
        acc: dict[str, dict[str, Any]] = {}
        for r in rows:
            k = r[key]
            a = acc.setdefault(k, {key: k, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            a["calls"] += r["calls"]
            a["prompt_tokens"] += r["prompt_tokens"]
            a["completion_tokens"] += r["completion_tokens"]
        out = []
        for a in acc.values():
            a.update(estimate(a["prompt_tokens"], a["completion_tokens"], f))
            out.append(a)
        return out

    total_p = sum(r["prompt_tokens"] for r in rows)
    total_c = sum(r["completion_tokens"] for r in rows)
    totals = {
        "calls": sum(r["calls"] for r in rows),
        "prompt_tokens": total_p,
        "completion_tokens": total_c,
        **estimate(total_p, total_c, f),
    }
    spaces = db.query_one("SELECT COUNT(*) n FROM opportunity_spaces WHERE merged_into IS NULL")
    n_spaces = spaces["n"] if spaces else 0
    return {
        "factors": f,
        "ledger_start": ledger_start,
        "historic_runs": len(historic_rows),
        "totals": totals,
        "per_space": {
            "spaces": n_spaces,
            "energy_wh": round(totals["energy_wh"] / n_spaces, 2) if n_spaces else None,
            "co2e_g": round(totals["co2e_g"] / n_spaces, 2) if n_spaces else None,
        },
        "by_month": sorted(fold("month"), key=lambda r: r["month"]),
        "by_purpose": sorted(fold("purpose"), key=lambda r: -r["energy_wh"]),
        "by_model": sorted(fold("model"), key=lambda r: -r["energy_wh"]),
        # Everyday equivalents make a gram figure legible. Both are stated
        # conversions, not measurements: ~0.17 kg CO2e per km for an average
        # EU petrol car, ~8 Wh to fully charge a smartphone.
        "equivalents": {
            "car_km": round(totals["co2e_g"] / 170.0, 1),
            "phone_charges": round(totals["energy_wh"] / 8.0, 0),
        },
    }
