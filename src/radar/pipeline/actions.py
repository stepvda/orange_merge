"""Next-action generation (FR-17, AC-03).

AC-03 Actionability: "Every topic has a clear next step: idea, deep-dive, or
customer talking point". Every topic gets one non-empty next action (one per
role until roles were removed on 2026-11-23).

§4.9 sets the bar: "when I open a topic I don't just want to read about it, I
want to act on it."

Table 23 assigns this to the model as "short, templated generation" — but the NAMED ASSETS handed to the prompt come
from the graph, never from the model. That is the difference between "Orange has
relevant assets" (unverifiable) and "lead with Live Objects and the Saint-Gobain
Glass reference" (inspectable, and wrong in a way someone can correct).
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from ..db import Database, js, unjs
from ..llm import LLMClient
from . import prompts

log = logging.getLogger(__name__)


class NextActionGenerator:
    def __init__(self, cfg: Config, db: Database, llm: LLMClient):
        self.cfg = cfg
        self.db = db
        self.llm = llm

    def run(self, states: tuple[str, ...] = ("active", "watchlist", "fading"),
            topic_ids: list[str] | None = None) -> dict[str, Any]:
        """Generate the next action for every live topic or just `topic_ids`.

        The subset form is what a targeted generation run uses: this stage is one
        model call per topic, so re-running it across the whole radar to serve
        five new spaces is the single most expensive thing such a run could do.
        The subset ignores `states` deliberately — a freshly synthesised space is
        `candidate` until scoring promotes it, and AC-03 asks for an action on
        every topic anyone can open, not only the promoted ones.
        """
        if topic_ids:
            placeholders = ",".join("?" * len(topic_ids))
            topics = self.db.query(
                f"SELECT * FROM opportunity_spaces WHERE merged_into IS NULL AND id IN ({placeholders})",
                tuple(topic_ids),
            )
        else:
            placeholders = ",".join("?" * len(states))
            topics = self.db.query(
                f"SELECT * FROM opportunity_spaces WHERE merged_into IS NULL AND state IN ({placeholders})",
                states,
            )
        generated = 0
        fallbacks = 0
        unverified_names = 0
        for topic in topics:
            assets = self._named_assets(topic["id"])
            try:
                payload = self.llm.complete_json(
                    prompts.next_action_prompt(self.cfg),
                    self._describe(dict(topic), assets),
                    temperature=0.4, max_tokens=600,
                )
                action = str(payload.get("next_action", "")).strip()
                if not action:
                    raise ValueError("model returned an empty next action")
                actions = {"next": action}

                # Validate the model's own declaration of what it named against
                # the assets actually supplied. §4.4.4's evidence-binding rule
                # applied to generated actions: an action that names an account
                # Orange has no relationship with is the failure mode most
                # likely to reach a customer conversation as fact.
                unverified = self._unverified_names(payload.get("assets_named"), assets)
                if unverified:
                    unverified_names += 1
                    log.warning(
                        "Next action for %s named unsupplied entities %s — using template instead",
                        topic["id"], unverified,
                    )
                    raise ValueError(f"named unsupplied entities: {unverified}")
            except Exception as exc:  # noqa: BLE001
                # AC-03 requires a non-empty action for EVERY topic, so a model
                # failure falls back to a deterministic template rather than
                # leaving the field blank.
                log.warning("Next-action generation failed for %s (%s) — using template", topic["id"], exc)
                actions = self._fallback(dict(topic), assets)
                fallbacks += 1
            with self.db.cursor() as cur:
                cur.execute(
                    "UPDATE opportunity_spaces SET next_actions = ?, prompt_version = ? WHERE id = ?",
                    (js(actions), prompts.PROMPT_VERSION_NEXT_ACTION, topic["id"]),
                )
            generated += 1
        return {"topics": generated, "template_fallbacks": fallbacks,
                "rejected_for_unverified_names": unverified_names}

    def _unverified_names(self, declared: Any, assets: dict[str, list[str]]) -> list[str]:
        """Names the model declared that were never supplied to it.

        Matching is deliberately lenient — the supplied strings carry a trailing
        "(L0)" link-type marker, and the model may cite a shorter form of a long
        label. Anything that is a substring of a supplied asset, or vice versa,
        counts as supplied. Only genuinely unrecognised names are returned.
        """
        if not isinstance(declared, list):
            return []

        def norm(text: str) -> str:
            """Compare on letters and digits only.

            The real capability pool is labelled "AI / Data / Cloud experts";
            the model writes "AI/Data/Cloud experts". Those are the same asset,
            but a literal substring test says otherwise, so a correct action was
            being thrown away and replaced by a template. Spacing and
            punctuation are not evidence of invention.
            """
            return "".join(ch for ch in text.split(" (")[0].lower() if ch.isalnum())

        supplied = [a.split(" (")[0].strip().lower() for values in assets.values() for a in values]
        supplied += [o["label"].lower() for o in self.cfg.offers.get("offers", [])]
        supplied += [r["label"].lower() for r in self.cfg.references.get("named", [])]
        supplied += [p["label"].lower() for p in self.cfg.assets.get("partners", [])]
        supplied += [c["label"].lower() for c in self.cfg.assets.get("certifications", [])]
        supplied += [p["label"].lower() for p in self.cfg.assets.get("capability_pools", [])]
        supplied += ["orange", "orange business", "orange cyberdefense"]

        normalised = [norm(item) for item in supplied if item]
        unverified = []
        for name in declared:
            needle = norm(str(name))
            if len(needle) < 3:
                continue
            if any(needle in item or item in needle for item in normalised if item):
                continue
            unverified.append(str(name))
        return unverified

    def _named_assets(self, topic_id: str) -> dict[str, list[str]]:
        rows = self.db.query(
            """SELECT l.node_id, l.link_type, n.label FROM opportunity_links l
               JOIN graph_nodes n ON n.id = l.node_id
               WHERE l.opportunity_id = ? AND l.rejected = 0""",
            (topic_id,),
        )
        assets: dict[str, list[str]] = {}
        for row in rows:
            kind = row["node_id"].split(":", 1)[0]
            assets.setdefault(kind, []).append(f"{row['label']} ({row['link_type']})")
        return assets

    def _describe(self, topic: dict[str, Any], assets: dict[str, list[str]]) -> str:
        claims = "\n".join(f"  - {c.get('claim')}" for c in unjs(topic["why_hot"], []) or [])
        asset_lines = "\n".join(
            f"  {kind}: {', '.join(values[:5])}" for kind, values in sorted(assets.items())
        ) or "  (none linked — say what to find out, do not claim an asset)"
        return f"""OPPORTUNITY SPACE {topic['id']}
Statement: {topic['statement']}
Vertical: {self.cfg.verticals.label(topic['vertical'])}
Use case: {self.cfg.use_cases.label(topic['use_case'])}
Technology: {self.cfg.technologies.label(topic['technology'])}
Time horizon: {topic.get('horizon')} (basis: {topic.get('horizon_basis')})

WHY IT IS HOT (each claim is evidence-bound):
{claims or '  (none)'}

NAMED ORANGE ASSETS LINKED TO THIS TOPIC — use only these, verbatim:
{asset_lines}

Write the single next action. Return JSON."""

    def _fallback(self, topic: dict[str, Any], assets: dict[str, list[str]]) -> dict[str, str]:
        use_case = self.cfg.use_cases.label(topic["use_case"])
        vertical = self.cfg.verticals.label(topic["vertical"])
        technology = self.cfg.technologies.label(topic["technology"])
        offers = assets.get("offer", [])
        references = assets.get("reference", [])
        partners = assets.get("partner", [])
        return {
            "next": (
                f"Validate demand for {use_case} in {vertical} using {technology}"
                + (f": lead with {offers[0]}" if offers else "")
                + (f" and the {references[0]} reference" if references else
                   ", and confirm an internal proof point first")
                + (f", with {partners[0]} as delivery partner." if partners else ".")
            ),
        }
