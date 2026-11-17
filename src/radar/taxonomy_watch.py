"""Taxonomy watchlist — a closed vocabulary that can still learn (§4.4.2).

Ported from the Orange_use_case team (best-of selection, see
docs/best_of_selection.md). Their pipeline noticed something the closed
vocabulary otherwise hides: when the model keeps reaching for a use case or a
technology the taxonomy does not have, that is a signal about the TAXONOMY, not
noise to throw away. They kept a `watchlist_terms` table, promoted frequent
terms to proposals and had a curator approve or reject them from the CLI.

This module brings that loop into the radar, with three changes:

* **The hook is the existing validation failure.** Synthesis already rejects a
  candidate whose vertical, use case or technology is outside the vocabulary
  (`SynthesisEngine._validate`). That rejection now also records the term here,
  so nothing new has to be asked of the model.
* **Review is in the UI**, not an interactive CLI prompt, and records who
  decided and when (LK-06's rule for machine proposals, applied to vocabulary).
* **Approval writes `config/taxonomy/extensions.yaml`**, which the config
  loader merges into the live vocabularies (`Config.apply_taxonomy_extensions`).
  The hand-written taxonomy files are never rewritten, so a reviewer can see
  every machine-originated term in one short file and revert it with one edit.

Generic terms ("AI", "data", …) are never recorded — the Orange_use_case data
showed "AI" alone accounting for 77 of its watchlist hits, all of them useless
as a taxonomy entry. The blocklist is configuration (`taxonomy_watch.generic_terms`).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from .config import (CONFIG_DIR, EXTENSIBLE_VOCABULARIES, TAXONOMY_EXTENSIONS_FILE, Config)
from .db import Database, js, unjs

SCHEMA = """
CREATE TABLE IF NOT EXISTS taxonomy_watchlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dimension     TEXT NOT NULL,             -- use_cases | technologies | verticals
    term          TEXT NOT NULL,             -- as first seen
    term_key      TEXT NOT NULL,             -- normalised, for dedup
    frequency     INTEGER NOT NULL DEFAULT 1,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    examples      TEXT NOT NULL DEFAULT '[]',-- JSON [{statement, vertical}] (max 5)
    origin        TEXT NOT NULL,             -- synthesis | import:<repo>
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    decided_by    TEXT,
    decided_at    TEXT,
    approved_id   TEXT,
    note          TEXT,
    UNIQUE(dimension, term_key)
);
"""

SEED_FILE = CONFIG_DIR / "taxonomy" / "watchlist_seed.json"

DEFAULT_GENERIC = {
    "ai", "artificial intelligence", "data", "technology", "technologies", "digital",
    "digitalisation", "digitalization", "digital transformation", "innovation", "software",
    "automation", "platform", "solution", "solutions", "it", "ict", "other", "none", "n/a",
}

#: Synthesis names the dimension by the candidate field; the vocabulary by its
#: plural. One map so the two never drift.
FIELD_TO_DIMENSION = {"vertical": "verticals", "use_case": "use_cases", "technology": "technologies"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalise(term: str) -> str:
    text = unicodedata.normalize("NFKC", term or "").strip().lower()
    text = re.sub("[\u2010\u2011\u2012\u2013\u2014]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,:;'\"")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(CONFIG_DIR.parent))
    except ValueError:
        return str(path)


def slug(term: str) -> str:
    text = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:48] or "term"


class TaxonomyWatch:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        settings = (cfg.settings.get("taxonomy_watch") or {})
        self.threshold = int(settings.get("proposal_threshold", 2))
        self.generic = DEFAULT_GENERIC | {normalise(t) for t in settings.get("generic_terms") or []}
        with db.cursor() as cur:
            cur.executescript(SCHEMA)

    # -- recording -------------------------------------------------------

    def is_recordable(self, dimension: str, term: str) -> bool:
        key = normalise(term)
        if not key or len(key) < 3 or len(key) > 80 or key in self.generic:
            return False
        vocab = getattr(self.cfg, dimension, None)
        # A term the vocabulary can already resolve is a spelling, not a gap.
        return vocab is not None and vocab.resolve(term) is None and key not in vocab

    def record(self, field_or_dimension: str, term: str, *, statement: str = "", vertical: str = "",
               origin: str = "synthesis", count: int = 1, seen_at: str | None = None) -> bool:
        """Count one sighting of an out-of-vocabulary term. Never raises."""
        dimension = FIELD_TO_DIMENSION.get(field_or_dimension, field_or_dimension)
        try:
            if not self.is_recordable(dimension, term):
                return False
            key, now = normalise(term), seen_at or _now()
            row = self.db.query_one(
                "SELECT id, examples FROM taxonomy_watchlist WHERE dimension = ? AND term_key = ?",
                (dimension, key),
            )
            example = {"statement": statement[:240], "vertical": vertical} if statement or vertical else None
            with self.db.cursor() as cur:
                if row is None:
                    cur.execute(
                        "INSERT INTO taxonomy_watchlist (dimension, term, term_key, frequency, first_seen, "
                        "last_seen, examples, origin) VALUES (?,?,?,?,?,?,?,?)",
                        (dimension, term.strip(), key, count, now, now, js([example] if example else []), origin),
                    )
                else:
                    examples = unjs(row["examples"]) or []
                    if example and len(examples) < 5:
                        examples.append(example)
                    cur.execute(
                        "UPDATE taxonomy_watchlist SET frequency = frequency + ?, last_seen = MAX(last_seen, ?), "
                        "examples = ? WHERE id = ?",
                        (count, now, js(examples), row["id"]),
                    )
            return True
        except Exception:  # noqa: BLE001 — bookkeeping must never fail a synthesis run
            return False

    def seed_from_file(self, path: Path = SEED_FILE) -> int:
        """Import another pipeline's watchlist once, into an empty table."""
        if not path.exists():
            return 0
        if self.db.query_one("SELECT COUNT(*) n FROM taxonomy_watchlist")["n"]:
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        added = 0
        for entry in data.get("terms", []):
            if self.record(entry["dimension"], entry["term"], vertical=", ".join(entry.get("verticals") or [])[:120],
                           statement="", origin=data.get("origin", "import"),
                           count=int(entry.get("frequency") or 1), seen_at=entry.get("last_seen")):
                added += 1
        return added

    # -- reading ---------------------------------------------------------

    def list(self, status: str | None = None) -> dict[str, Any]:
        rows = [dict(r) for r in self.db.query(
            "SELECT * FROM taxonomy_watchlist ORDER BY (status = 'pending') DESC, frequency DESC, last_seen DESC"
        )]
        items = []
        for r in rows:
            r["examples"] = unjs(r["examples"]) or []
            r["proposed"] = r["status"] == "pending" and r["frequency"] >= self.threshold
            r["can_approve"] = r["dimension"] in EXTENSIBLE_VOCABULARIES
            if status and not (r["status"] == status or (status == "proposed" and r["proposed"])):
                continue
            items.append(r)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        counts["proposed"] = sum(1 for r in rows if r["status"] == "pending" and r["frequency"] >= self.threshold)
        return {"threshold": self.threshold, "counts": counts, "items": items,
                "extensions_file": _display_path(TAXONOMY_EXTENSIONS_FILE)}

    # -- deciding --------------------------------------------------------

    def decide(self, item_id: int, decision: str, user: str, *, label: str | None = None,
               definition: str | None = None, domains: list[str] | None = None,
               synonyms: list[str] | None = None, note: str | None = None) -> dict[str, Any]:
        if decision not in ("approve", "reject", "reopen"):
            raise ValueError("decision must be approve, reject or reopen")
        row = self.db.query_one("SELECT * FROM taxonomy_watchlist WHERE id = ?", (item_id,))
        if row is None:
            raise KeyError(item_id)
        if row["status"] == "approved" and decision != "reopen":
            raise ValueError("already approved — reopen is not possible once a term is in the vocabulary")
        if decision == "reopen":
            if row["status"] == "approved":
                raise ValueError("an approved term is part of the vocabulary; remove it from "
                                 "config/taxonomy/extensions.yaml by hand to retire it")
            with self.db.cursor() as cur:
                cur.execute("UPDATE taxonomy_watchlist SET status='pending', decided_by=NULL, decided_at=NULL, "
                            "note=NULL WHERE id = ?", (item_id,))
            return self._row(item_id)

        if decision == "reject":
            with self.db.cursor() as cur:
                cur.execute("UPDATE taxonomy_watchlist SET status='rejected', decided_by=?, decided_at=?, note=? "
                            "WHERE id = ?", (user, _now(), note, item_id))
            return self._row(item_id)

        dimension = row["dimension"]
        if dimension not in EXTENSIBLE_VOCABULARIES:
            raise ValueError(f"{dimension} cannot be extended from the watchlist (only {EXTENSIBLE_VOCABULARIES})")
        vocab = getattr(self.cfg, dimension)
        label = (label or row["term"]).strip()
        new_id = slug(label)
        base, n = new_id, 2
        while new_id in vocab:
            new_id, n = f"{base}_{n}", n + 1
        domains = [d for d in (domains or []) if d in self.cfg.domains]
        if dimension == "use_cases" and not domains:
            raise ValueError("a use case needs at least one business domain")
        entry: dict[str, Any] = {
            "id": new_id, "label": label,
            "definition": (definition or f"Curator-approved term first proposed as {row['term']!r}.").strip(),
            "synonyms": sorted({s.strip() for s in (synonyms or []) + [row["term"]] if s.strip()
                                and s.strip().lower() != label.lower()}),
            "domains": domains,
            "approved_by": user, "approved_at": _now(), "origin": row["origin"],
            "watchlist_frequency": row["frequency"],
        }
        self._append_extension(dimension, entry)
        self.cfg.apply_taxonomy_extensions()
        with self.db.cursor() as cur:
            cur.execute("UPDATE taxonomy_watchlist SET status='approved', decided_by=?, decided_at=?, "
                        "approved_id=?, note=? WHERE id = ?", (user, _now(), new_id, note, item_id))
        return self._row(item_id)

    def _row(self, item_id: int) -> dict[str, Any]:
        row = dict(self.db.query_one("SELECT * FROM taxonomy_watchlist WHERE id = ?", (item_id,)))
        row["examples"] = unjs(row["examples"]) or []
        return row

    @staticmethod
    def _append_extension(dimension: str, entry: dict[str, Any]) -> None:
        path = TAXONOMY_EXTENSIONS_FILE
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
        raw = raw or {}
        raw.setdefault("version", "ext-1")
        for name in EXTENSIBLE_VOCABULARIES:
            raw.setdefault(name, [])
        raw[dimension].append(entry)
        header = ("# Curator-approved vocabulary additions, written by radar.taxonomy_watch.\n"
                  "# Merged into the closed vocabularies at load (Config.apply_taxonomy_extensions).\n"
                  "# Delete an entry here to retire it.\n")
        path.write_text(header + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
