"""One-off sharpening of the spaces already on the radar (2026-11-25).

New spaces go through the title rules, the stricter critic and the cross-radar
dedup as they are generated (synthesis.py). The spaces created before that did
not, so this module puts them through the same gates:

  1. PROPOSE — for every live space, the model rewrites the title and its parts
     (buyer, why now, outcome, Orange's angle, who signs) from the space's own
     evidence; the title rules get one fix-up; the critic judges the result
     against the closest OTHER spaces. Then every pair of spaces that is the
     same opportunity is listed as a merge suggestion. Nothing is written to the
     radar. The proposals go to an Excel file for review, where each row has an
     "Apply" column.

  2. APPLY — reads the reviewed Excel back and applies the rows marked "yes":
     new titles (the space's version moves on, so its description is
     regenerated), and merges (evidence moves to the space that is kept, the
     other is marked `merged_into`). Then every live space without a current
     description gets one.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database, js, unjs
from ..embeddings import Embedder
from ..llm import LLMClient
from . import prompts
from .synthesis import Candidate, SynthesisStats, Synthesiser, _merge_claims

log = logging.getLogger(__name__)

LIVE_STATES = ("active", "watchlist", "fading", "candidate")
FIELDS = ("statement", "buyer", "why_now", "outcome", "orange_angle", "who_signs")
#: Returned by the model so the title rules can check the triple; not stored.
PHRASES = ("use_case_phrase", "technology_phrase")


class StatementRewriter:
    def __init__(self, cfg: Config, db: Database, llm: LLMClient, embedder: Embedder | None = None):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.synth = Synthesiser(cfg, db, llm, embedder, portfolio_overlap=None)

    # -- propose ---------------------------------------------------------------

    def propose(self, topic_ids: list[str] | None = None, max_workers: int = 4) -> dict[str, Any]:
        self.db.init_schema()
        placeholders = ",".join("?" * len(LIVE_STATES))
        rows = [dict(r) for r in self.db.query(
            f"SELECT * FROM opportunity_spaces WHERE merged_into IS NULL AND state IN ({placeholders}) "
            f"ORDER BY id", LIVE_STATES)]
        if topic_ids:
            rows = [r for r in rows if r["id"] in set(topic_ids)]

        def work(row: dict[str, Any]) -> dict[str, Any]:
            try:
                return self._rewrite_one(row)
            except Exception as exc:  # noqa: BLE001 — one space must not stop the job
                log.warning("Rewrite failed for %s: %s", row["id"], exc)
                return {"id": row["id"], "status": "failed", "error": str(exc), "old": row["statement"],
                        "apply": False}

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rewrite") as pool:
            rewrites = list(pool.map(work, rows))
        merges = self._merge_suggestions(rows, rewrites)
        return {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "prompt_version": prompts.PROMPT_VERSION_REWRITE,
                "rewrites": rewrites, "merges": merges}

    def _evidence(self, topic_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT s.id, s.title, s.extract, s.publisher, s.published_at, s.signal_type, s.tier, "
            "s.geographies, s.url FROM opportunity_signals o JOIN signals s ON s.id = o.signal_id "
            "WHERE o.opportunity_id = ? ORDER BY s.tier ASC, s.published_at DESC LIMIT 16",
            (topic_id,))]

    def _rewrite_one(self, row: dict[str, Any]) -> dict[str, Any]:
        evidence = self._evidence(row["id"])
        payload = {"signals": evidence}
        current = {k: row.get(k) for k in ("vertical", "use_case", "technology", "statement")}
        current["why_hot"] = unjs(row["why_hot"], []) or []
        user = "\n".join([
            "SPACE TO SHARPEN",
            json.dumps(current | {"labels": {
                "vertical": self.cfg.verticals.label(row["vertical"]),
                "use_case": self.cfg.use_cases.label(row["use_case"]),
                "technology": self.cfg.technologies.label(row["technology"])}},
                ensure_ascii=False, indent=2),
            "",
            "EVIDENCE — the only facts you may use:",
            *[f"- [{s['id']}] ({s['published_at']}, {s['publisher']}) {s['title']}\n  {str(s['extract'] or '')[:400]}"
              for s in evidence],
        ])
        def attempt(prompt: str) -> tuple[Candidate, dict[str, Any], bool, int | None, str]:
            out = self.llm.complete_json(prompts.rewrite_system_prompt(self.cfg), prompt, strong=True,
                                         temperature=0.3, max_tokens=900) or {}
            cand = Candidate(vertical=row["vertical"], use_case=row["use_case"],
                             technology=row["technology"], statement=row["statement"],
                             why_hot=current["why_hot"], existing_id=row["id"])
            for name in FIELDS + PHRASES:
                value = str(out.get(name) or "").strip()
                if value:
                    setattr(cand, name, value)
            stats = SynthesisStats()
            ok = self.synth._sharpen(cand, payload, stats)
            if ok:
                self.synth._criticise(cand, payload, [], stats)
                return cand, out, ok, cand.critic_score, cand.critic_notes
            return cand, out, ok, None, (stats.rejections[-1]["reason"] if stats.rejections else "title rules")

        def rank(result) -> tuple[int, int]:
            _, _, ok, sc, _ = result
            return (1 if ok else 0, sc if sc is not None else 9)

        best = attempt(user)
        # A second attempt with the critic's (or the rules') objections, the
        # way a reviewer's comments go back to the writer. Kept only if better.
        _, _, ok1, score1, notes1 = best
        if not ok1 or (score1 is not None and score1 < self.synth.critic_min):
            first = best[0]
            retry = attempt(user + "\n\nYOUR PREVIOUS ATTEMPT\n" + json.dumps(
                {name: getattr(first, name) for name in FIELDS + PHRASES}, ensure_ascii=False, indent=2)
                + "\n\nA HOSTILE REVIEWER REJECTED IT FOR THESE REASONS — fix every one, using only "
                  "the evidence:\n" + notes1)
            if rank(retry) > rank(best):
                best = retry
        candidate, out, rules_ok, score, notes = best
        passed = rules_ok and (score is None or score >= self.synth.critic_min)
        changed = candidate.statement.strip() != (row["statement"] or "").strip()
        return {
            "id": row["id"],
            "old": row["statement"],
            **{name: getattr(candidate, name) for name in FIELDS},
            "critic_score": score,
            "old_critic_score": row.get("critic_score"),
            "critic_notes": notes,
            "too_generic": bool(out.get("too_generic")),
            "note": str(out.get("note") or ""),
            "status": "sharpened" if passed and changed else "still generic",
            # Applied by default when the title passes the rules: even a title
            # the critic scores 3 is more specific than the one it replaces.
            "apply": bool(rules_ok and changed),
        }

    def _merge_suggestions(self, rows: list[dict[str, Any]],
                           rewrites: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pairs of spaces that are the same opportunity, on the proposed titles."""
        if len(rows) < 2:
            return []
        title = {r["id"]: r["statement"] for r in rows}
        for r in rewrites:
            if r.get("apply") and r.get("statement"):
                title[r["id"]] = r["statement"]
        ids = [r["id"] for r in rows]
        signals = {r["opportunity_id"]: r["n"] for r in self.db.query(
            "SELECT opportunity_id, COUNT(*) n FROM opportunity_signals GROUP BY opportunity_id")}
        vectors = self.synth.embedder.encode([title[i] for i in ids])
        similarity = vectors @ vectors.T
        out: list[dict[str, Any]] = []
        taken: set[str] = set()
        pairs = [(float(similarity[i, j]), i, j) for i in range(len(ids)) for j in range(i + 1, len(ids))
                 if similarity[i, j] >= self.synth.judge_floor]
        for score, i, j in sorted(pairs, reverse=True):
            a, b = ids[i], ids[j]
            if a in taken or b in taken:
                continue
            if score >= self.synth.dup_threshold:
                how = f"similarity {score:.2f} ≥ {self.synth.dup_threshold:.2f}"
            elif self.synth._same_opportunity(title[a], title[b]):
                how = f"similarity {score:.2f}; the model judged them the same opportunity"
            else:
                continue
            # Keep the better-evidenced space; the other's evidence moves to it.
            keep, away = (a, b) if signals.get(a, 0) >= signals.get(b, 0) else (b, a)
            taken.update((a, b))
            out.append({"keep": keep, "merge_away": away, "similarity": round(score, 3), "how": how,
                        "keep_title": title[keep], "away_title": title[away], "apply": True})
        return out

    # -- review file -------------------------------------------------------------

    @staticmethod
    def write_review(result: dict[str, Any], path: Path) -> Path:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        wb = Workbook()
        notes = wb.active
        notes.title = "Notes"
        for line in [
            ["Opportunity-space rewrite review"],
            [f"Generated {result['generated_at']} with prompt {result['prompt_version']}."],
            [],
            ["Rewrites: one row per space. 'Apply' is 'yes' where the new title passes the title rules."],
            ["Change 'Apply' to 'no' to keep the current title. You may also edit the new title, why now"],
            ["and who signs in place; the edited text is what gets applied."],
            ["Critic score: 4 and 5 pass. 'still generic' means the evidence did not support a sharper title;"],
            ["those spaces are candidates to retire."],
            [],
            ["Merges: pairs that are the same opportunity. 'Keep' stays on the radar and takes the other's"],
            ["evidence; 'Merge away' disappears from the radar (marked merged, not deleted)."],
            [],
            ["Apply with:  radar rewrite apply --file <this file>"],
        ]:
            notes.append(line)
        notes.column_dimensions["A"].width = 110
        notes["A1"].font = Font(bold=True, size=13)

        head = Font(bold=True, color="FFFFFF")
        fill = PatternFill("solid", fgColor="FF7900")
        wrap = Alignment(wrap_text=True, vertical="top")

        ws = wb.create_sheet("Rewrites")
        cols = [("ID", 9), ("Apply", 8), ("Status", 14), ("Critic score", 8), ("Old critic score", 8),
                ("Old title", 50), ("New title", 50), ("Why now", 38), ("Who signs", 22), ("Buyer", 24),
                ("Outcome", 30), ("Orange angle", 30), ("Critic notes", 50), ("Model note", 40)]
        ws.append([c for c, _ in cols])
        for idx, (_, width) in enumerate(cols, start=1):
            ws.column_dimensions[ws.cell(1, idx).column_letter].width = width
            ws.cell(1, idx).font, ws.cell(1, idx).fill = head, fill
        for r in result["rewrites"]:
            ws.append([r["id"], "yes" if r.get("apply") else "no", r.get("status"), r.get("critic_score"),
                       r.get("old_critic_score"), r.get("old"), r.get("statement"), r.get("why_now"),
                       r.get("who_signs"), r.get("buyer"), r.get("outcome"), r.get("orange_angle"),
                       r.get("critic_notes") or r.get("error"), r.get("note")])
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = wrap
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = ws.dimensions

        ms = wb.create_sheet("Merges")
        mcols = [("Apply", 8), ("Keep", 9), ("Merge away", 11), ("Similarity", 10), ("How decided", 40),
                 ("Kept title", 60), ("Merged-away title", 60)]
        ms.append([c for c, _ in mcols])
        for idx, (_, width) in enumerate(mcols, start=1):
            ms.column_dimensions[ms.cell(1, idx).column_letter].width = width
            ms.cell(1, idx).font, ms.cell(1, idx).fill = head, fill
        for m in result["merges"]:
            ms.append(["yes" if m.get("apply") else "no", m["keep"], m["merge_away"], m["similarity"],
                       m["how"], m["keep_title"], m["away_title"]])
        for row in ms.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = wrap
        ms.freeze_panes = "B2"

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return path

    # -- apply -------------------------------------------------------------------

    def apply(self, path: Path, describe: bool = True) -> dict[str, Any]:
        from openpyxl import load_workbook

        self.db.init_schema()
        wb = load_workbook(path, data_only=True)
        rewrites = self._rows(wb["Rewrites"])
        merges = self._rows(wb["Merges"]) if "Merges" in wb.sheetnames else []
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        today = now[:10]
        stats = {"titles_applied": 0, "merges_applied": 0, "skipped": 0}

        with self.db.cursor() as cur:
            for r in rewrites:
                if str(r.get("Apply") or "").strip().lower() not in ("yes", "y", "true", "1"):
                    stats["skipped"] += 1
                    continue
                title = str(r.get("New title") or "").strip()
                if not title:
                    stats["skipped"] += 1
                    continue
                cur.execute(
                    """UPDATE opportunity_spaces
                       SET statement = ?, why_now = ?, who_signs = ?, buyer = ?, outcome = ?,
                           orange_angle = ?, critic_score = COALESCE(?, critic_score),
                           critic_notes = ?, prompt_version = ?, version = version + 1
                       WHERE id = ? AND merged_into IS NULL""",
                    (title, _clean(r.get("Why now")), _clean(r.get("Who signs")), _clean(r.get("Buyer")),
                     _clean(r.get("Outcome")), _clean(r.get("Orange angle")),
                     _int(r.get("Critic score")), _clean(r.get("Critic notes")),
                     prompts.PROMPT_VERSION_REWRITE, r.get("ID")),
                )
                stats["titles_applied"] += cur.rowcount
                # A new title does not change the evidence a description is
                # built from, so a description that was current stays current
                # rather than costing another model call. (A merge below does
                # add evidence, and its description is regenerated.)
                cur.execute(
                    "UPDATE topic_descriptions SET topic_version = topic_version + 1 "
                    "WHERE opportunity_id = ? AND topic_version = "
                    "(SELECT version - 1 FROM opportunity_spaces WHERE id = ?)",
                    (r.get("ID"), r.get("ID")),
                )

            for m in merges:
                if str(m.get("Apply") or "").strip().lower() not in ("yes", "y", "true", "1"):
                    continue
                keep, away = m.get("Keep"), m.get("Merge away")
                rows = {x["id"]: x for x in cur.execute(
                    "SELECT id, why_hot FROM opportunity_spaces WHERE id IN (?, ?) AND merged_into IS NULL",
                    (keep, away)).fetchall()}
                if keep not in rows or away not in rows:
                    continue
                merged = _merge_claims(unjs(rows[keep]["why_hot"], []) or [],
                                       unjs(rows[away]["why_hot"], []) or [])
                cur.execute("UPDATE opportunity_spaces SET why_hot = ?, version = version + 1, "
                            "last_refresh = ? WHERE id = ?", (js(merged), today, keep))
                cur.execute(
                    "INSERT OR IGNORE INTO opportunity_signals (opportunity_id, signal_id, attached_at, refresh_id) "
                    "SELECT ?, signal_id, attached_at, refresh_id FROM opportunity_signals WHERE opportunity_id = ?",
                    (keep, away))
                cur.execute("UPDATE opportunity_spaces SET merged_into = ?, state_reason = ?, "
                            "state_changed_at = ? WHERE id = ?",
                            (keep, f"merged into {keep}: same opportunity (rewrite review)", today, away))
                stats["merges_applied"] += 1

        if describe:
            from .describe import DescriptionGenerator
            stats["describe"] = DescriptionGenerator(self.cfg, self.db, self.llm).run(
                states=LIVE_STATES,
                max_workers=int((self.cfg.settings.get("description") or {}).get("max_parallel", 4)))
        return stats

    @staticmethod
    def _rows(ws) -> list[dict[str, Any]]:
        values = list(ws.iter_rows(values_only=True))
        if not values:
            return []
        header = [str(h or "") for h in values[0]]
        return [dict(zip(header, v)) for v in values[1:] if any(x is not None for x in v)]


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["StatementRewriter", "LIVE_STATES"]
