"""Deterministic specificity lint for opportunity-space titles (2026-11-25).

The critic is a model and can be talked round; these rules cannot. They catch
the patterns that made the first 75 spaces read alike:

  * technology-first titles ("Confidential computing for …") — the buyer and the
    outcome are what a customer recognises, not the technology;
  * hedges ("… can provide …") — a title states the opportunity, it does not
    speculate about it;
  * stacked filler ("resilient, governed, sovereign … under … control");
  * no named buyer, no "why now", no "who signs";
  * (2026-11-25, second pass) a title that does not answer VERTICAL x USE CASE
    x TECHNOLOGY on its own: the model names the concrete use case and the
    concrete technology as phrases, and both must appear in the title; a bare
    "AI" or "cloud" does not count as naming the technology.

A title that fails is sent back to the model once with the list of problems
(`Synthesiser._sharpen`); if it still fails it is dropped. Every threshold is in
`curation.specificity` in settings.yaml (NFR-11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Config

#: Hedging verbs. "Can", "could" etc. turn a title into a speculation.
HEDGES = re.compile(r"\b(can|could|may|might|aims? to|helps? to|has the potential to)\b", re.I)

#: Words that sound specific and say nothing. One is tolerated, two is a pattern.
FILLER = ("resilient", "governed", "trusted", "trust-controlled", "sovereign", "scalable",
          "integrated", "holistic", "seamless", "end-to-end", "next-generation", "innovative",
          "robust", "secure", "readiness", "under jurisdictional control", "under control",
          "operational", "across")

_WORD = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9'’\-]*")
#: Too common to show that a buyer was named.
_STOP = {"and", "the", "for", "with", "that", "their", "teams", "team", "operators", "operator",
         "organisations", "organizations", "companies", "company", "sector", "public", "private",
         "large", "small", "european", "enterprises", "enterprise", "providers", "provider", "of", "in"}


#: Technology words too broad to tell a reader WHICH technology is meant.
GENERIC_TECH = {"ai", "ml", "cloud", "iot", "data", "analytics", "digital", "technology", "technologies",
                "platform", "platforms", "software", "automation", "connectivity", "network", "networks",
                "solution", "solutions", "digital tools", "it"}


@dataclass
class LintResult:
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def technology_phrases(cfg: Config) -> list[str]:
    """Every technology label and synonym, lower-cased, longest first."""
    phrases: set[str] = set()
    for item in cfg.technologies:
        phrases.add(item.label.lower())
        phrases.add(item.id.replace("_", " ").lower())
        phrases.update(s.lower() for s in item.synonyms)
    # Bare acronyms and short generic tokens that open tech-first titles.
    phrases.update({"ai", "ml", "machine learning", "machine-learning", "generative ai", "agentic ai",
                    "edge computing", "digital twin", "digital twins", "private 5g", "5g", "leo",
                    "sd-wan", "iot", "blockchain", "rag", "llm", "llms", "cloud", "satellite"})
    return sorted((p for p in phrases if p), key=len, reverse=True)


def lint(statement: str, *, buyer: str = "", why_now: str = "", who_signs: str = "",
         tech_phrases: list[str] | None = None, settings: dict[str, Any] | None = None,
         use_case_phrase: str | None = None, technology_phrase: str | None = None,
         own_tech_phrases: list[str] | None = None) -> LintResult:
    """Return every rule the title breaks. An empty list means it passes."""
    s = settings or {}
    max_words = int(s.get("max_title_words", 18))
    max_filler = int(s.get("max_filler_words", 1))
    max_why_now = int(s.get("max_why_now_chars", 150))
    max_who = int(s.get("max_who_signs_chars", 70))
    require_triple = bool(s.get("require_triple", False))

    out = LintResult()
    text = (statement or "").strip()
    low = text.lower()
    words = _words(text)

    if len(words) > max_words:
        out.issues.append(f"title is {len(words)} words; keep it to {max_words} or fewer")
    if m := HEDGES.search(text):
        out.issues.append(f"hedge word '{m.group(0)}' — state the opportunity, do not speculate")
    lead = " ".join(w.lower() for w in words[:4])
    for phrase in tech_phrases or ():
        if lead.startswith(phrase + " ") or lead == phrase:
            out.issues.append(f"opens with the technology ('{phrase}') — open with the buyer or the outcome")
            break
    fillers = [f for f in FILLER if re.search(rf"\b{re.escape(f)}\b", low)]
    if len(fillers) > max_filler:
        out.issues.append("filler words " + ", ".join(f"'{f}'" for f in fillers)
                          + " — replace with something concrete")

    buyer_words = [w.lower() for w in _words(buyer) if len(w) > 3 and w.lower() not in _STOP]
    if not buyer.strip():
        out.issues.append("no named buyer")
    elif not any(w[:5] in low for w in buyer_words):
        out.issues.append(f"the buyer ('{buyer.strip()}') does not appear in the title")
    if not why_now.strip():
        out.issues.append("no 'why now' (the trigger that makes this urgent)")
    elif len(why_now) > max_why_now:
        out.issues.append(f"'why now' is {len(why_now)} characters; keep it under {max_why_now}")
    if not who_signs.strip():
        out.issues.append("no 'who signs' (the role that owns the budget)")
    elif len(who_signs) > max_who:
        out.issues.append(f"'who signs' is {len(who_signs)} characters; keep it under {max_who}")
    if require_triple:
        _check_triple(out, low, use_case_phrase or "", technology_phrase or "", own_tech_phrases or [])
    return out


def _contains(low_title: str, phrase: str) -> bool:
    phrase = " ".join(phrase.lower().split())
    return bool(phrase) and re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", low_title) is not None


def _check_triple(out: LintResult, low: str, use_case: str, technology: str, own: list[str]) -> None:
    """The title alone must say WHAT the buyer does and WITH WHICH technology."""
    use_case, technology = use_case.strip(), technology.strip()
    if not use_case:
        out.issues.append("no use case named: state the concrete task or workflow the buyer runs "
                          "(e.g. 'settling simple motor claims'), and return it as use_case_phrase")
    elif not _contains(low, use_case):
        out.issues.append(f"the use case ('{use_case}') does not appear word for word in the title")
    elif len(_words(use_case)) < 2:
        out.issues.append(f"the use case ('{use_case}') is one word; name the task or workflow")
    if technology:
        if not _contains(low, technology):
            out.issues.append(f"the technology ('{technology}') does not appear word for word in the title")
        elif technology.lower() in GENERIC_TECH:
            out.issues.append(f"'{technology}' does not say which technology; name it "
                              "(e.g. 'agentic AI claims assistant', 'private 5G', 'ML-KEM key exchange')")
    elif not any(_contains(low, p) for p in own):
        out.issues.append("no technology named in the title: say which technology delivers it, "
                          "and return it as technology_phrase")
