"""Taxonomy-derived collection parameters (stage 1, NFR-11).

WHY THIS EXISTS. config/sources.yaml said GDELT's queries "are built from the
taxonomy grid at runtime". They were not — every news and research query was a
hand-written literal. The consequences were visible in the first corpus: the
query `digital product passport regulation` returned 108 items and
`NIS2 DORA compliance deadline` returned 1, while whole branches of a 59-use-case,
38-technology vocabulary had no query at all. Topics concentrated in
manufacturing (90) and public sector (82) while retail, wholesale and
construction sat at 12, 8 and 12 — partly because the queries did.

NFR-11 says the vocabularies are the control surface. This module makes that
true for collection as well as for validation: adding a use case extends what
the radar looks for, without touching a connector or a source file.

Expansion happens HERE rather than inside the connectors so that connectors stay
free of a Config dependency and remain testable on a params dict alone. The
Ingestor calls `expand_source_params` once per source, before building it.

TWO KINDS OF EXPANSION:

  queries_from_taxonomy   vocabulary terms -> query strings, per connector syntax
  cpv_from_taxonomy       the `cpv` hints already carried by the use-case
                          vocabulary -> TED/OCDS CPV groups

Both MERGE with whatever literal `queries` / `cpv_groups` the source declares:
a hand-written query that encodes a subtlety the vocabulary cannot express is
still worth keeping, and deleting them silently would be a coverage regression.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config

log = logging.getLogger(__name__)

#: Vocabulary name -> attribute on Config.
_VOCABULARIES = {
    "technologies": "technologies",
    "use_cases": "use_cases",
    "verticals": "verticals",
    "domains": "domains",
}

#: Terms shorter than this match too much to be worth a request of their own.
MIN_TERM_CHARS = 6


def _item_terms(cfg: Config, vocab_name: str, language: str,
                include_synonyms: bool) -> list[tuple[Any, list[str]]]:
    """(vocabulary item, its terms in this language), in vocabulary file order."""
    vocab = getattr(cfg, _VOCABULARIES[vocab_name])
    lexicon = cfg.lexicon.get("terms") or {}
    out: list[tuple[Any, list[str]]] = []
    for item in vocab:
        if language == "en":
            terms = [item.label] + (list(item.synonyms) if include_synonyms else [])
        else:
            terms = [str(t) for t in (lexicon.get(item.id) or {}).get(language) or ()]
        if terms:
            out.append((item, terms))
    return out


def _spread(n: int) -> list[int]:
    """0..n-1 in bit-reversed order: 0, n/2, n/4, 3n/4, …

    A vocabulary file is written in thematic blocks (connectivity, cloud, AI,
    IoT, security, workplace…). Taking the first k of it under a query cap
    covers the first blocks only; taking k in this order samples every block.
    """
    bits = max(1, (n - 1).bit_length())
    return sorted(range(n), key=lambda i: int(format(i, f"0{bits}b")[::-1], 2))


def _balanced(cfg: Config, vocab_name: str, language: str, include_synonyms: bool) -> list[str]:
    """One vocabulary's terms, ordered so that a cap keeps a fair sample.

    Use cases are taken round-robin across their business domain, so a cap of
    16 queries covers operations, connectivity, security, cloud, customer and
    employee experience rather than the sixteen industrial use cases the file
    happens to start with. Other vocabularies carry no domain and are spread
    across the file. Every item's first term comes before anyone's second, so
    synonyms never crowd out an item that has not been queried at all.
    """
    items = _item_terms(cfg, vocab_name, language, include_synonyms)
    if vocab_name == "use_cases":
        groups: dict[str, list[list[str]]] = {}
        for item, terms in items:
            key = (item.get("domains") or [item.id])[0]
            groups.setdefault(key, []).append(terms)
        ordered_items: list[list[str]] = []
        queues = list(groups.values())
        while any(queues):
            for queue in queues:
                if queue:
                    ordered_items.append(queue.pop(0))
    else:
        ordered_items = [items[i][1] for i in _spread(len(items))] if items else []

    out: list[str] = []
    depth = max((len(t) for t in ordered_items), default=0)
    for level in range(depth):
        out.extend(t[level] for t in ordered_items if len(t) > level)
    return out


def taxonomy_terms(cfg: Config, spec: dict[str, Any]) -> list[str]:
    """Ordered, deduplicated term list for a `*_from_taxonomy` spec.

    Order matters because sources cap the list (`max_queries`). Until
    2026-11-23 the terms came in file order and were cut there, so 18 of the 19
    taxonomy-driven sources never searched for a single employee- or
    customer-experience term. Each vocabulary is now balanced (`_balanced`) and
    the vocabularies are interleaved, so whatever the cap keeps is a fair
    sample of the whole grid.
    """
    language = str(spec.get("language", "en")).lower()
    include_synonyms = bool(spec.get("include_synonyms", False))
    names = spec.get("vocabularies") or ["technologies", "use_cases"]

    per_vocab: list[list[str]] = []
    for name in names:
        if name not in _VOCABULARIES:
            log.warning("queries_from_taxonomy: unknown vocabulary %r — skipped", name)
            continue
        per_vocab.append(_balanced(cfg, name, language, include_synonyms))

    terms: list[str] = []
    for i in range(max((len(v) for v in per_vocab), default=0)):
        terms.extend(v[i] for v in per_vocab if i < len(v))

    for extra in spec.get("extra_terms") or []:
        terms.append(str(extra))

    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        cleaned = " ".join(str(term).split())
        key = cleaned.lower()
        if len(cleaned) < MIN_TERM_CHARS or key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)

    exclude = {str(e).lower() for e in spec.get("exclude_terms") or ()}
    if exclude:
        ordered = [t for t in ordered if t.lower() not in exclude]
    return ordered


def build_queries(cfg: Config, spec: dict[str, Any]) -> list[str]:
    """Render taxonomy terms through a per-connector query template.

    `template` is a format string receiving `{term}`. Connectors differ in
    syntax — GDELT and Google News want a quoted phrase with a qualifier, arXiv
    wants `all:"…"` — and encoding that here keeps the connectors unchanged.
    """
    template = spec.get("template") or '"{term}"'
    terms = taxonomy_terms(cfg, spec)

    limit = spec.get("max_queries")
    if limit is not None:
        limit = int(limit)
        if len(terms) > limit:
            # §4.12: a silent cap reads as "we covered everything". Say what was
            # dropped, and drop deterministically so a replay is reproducible.
            log.info(
                "queries_from_taxonomy: %d terms available, capped at %d — %d not queried this refresh",
                len(terms), limit, len(terms) - limit,
            )
            terms = terms[:limit]

    return [template.format(term=term) for term in terms]


def build_cpv_groups(cfg: Config, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """CPV groups from the `cpv` hints the use-case vocabulary already carries.

    The hand-written groups were CPV *roots* — `72000000` (IT services) matched
    785,215 notices in one 90-day window, of which 415 were sampled. Sampling a
    fraction of a percent of a set that broad is not coverage, it is noise with
    a budget attached: TED became 52% of the corpus while contributing 8% of
    attached evidence.

    The vocabulary's hints are specific codes chosen per use case, so grouping
    by use case gives narrower result sets that a 20-notice sample can honestly
    represent — and the group label is then a use-case id, which is more useful
    downstream than "IT services".
    """
    groups: list[dict[str, Any]] = []
    min_digits = int(spec.get("min_code_digits", 0))
    for item in cfg.use_cases:
        codes = [_normalise_cpv(c) for c in (item.get("cpv") or [])]
        codes = [c for c in codes if c and len(c) >= min_digits]
        if not codes:
            continue
        # Order-preserving dedup: padding can collapse "71314" onto "71314000".
        codes = list(dict.fromkeys(codes))
        groups.append({"label": item.id, "cpv": codes, "use_case": item.id})

    limit = spec.get("max_groups")
    if limit is not None and len(groups) > int(limit):
        log.info("cpv_from_taxonomy: %d use-case groups available, capped at %d",
                 len(groups), int(limit))
        groups = groups[: int(limit)]
    return groups


def _normalise_cpv(code: Any) -> str:
    """Zero-pad a CPV hint to the eight digits the procurement APIs require.

    CPV is an eight-digit hierarchical scheme and a shorter code is a PREFIX of
    it, not a different code — "71314" means the 71314000 group. The crosswalks
    match on prefixes so they read the short form happily, but TED's query
    parser does not: one 5-digit hint in the use-case vocabulary returned HTTP
    400 and silently cost that whole CPV group its notices for the refresh.

    Padding rather than dropping keeps the group: the code is valid, it was
    merely written in its abbreviated form.
    """
    text = str(code or "").strip()
    if not text.isdigit():
        return ""
    return text.ljust(8, "0")[:8]


def expand_source_params(cfg: Config, source: dict[str, Any]) -> dict[str, Any]:
    """Return `source` with taxonomy-derived params resolved to literals.

    The input is never mutated: the Config object is shared across the refresh
    and a replay must be able to expand the same source again.
    """
    params = source.get("params") or {}
    if not any(k in params for k in ("queries_from_taxonomy", "cpv_from_taxonomy")):
        return source

    expanded = dict(params)

    spec = params.get("queries_from_taxonomy")
    if spec:
        generated = build_queries(cfg, spec if isinstance(spec, dict) else {})
        literal = list(params.get("queries") or [])
        seen = {q.lower() for q in literal}
        merged = literal + [q for q in generated if q.lower() not in seen]
        expanded["queries"] = merged
        log.info("%s: %d literal + %d taxonomy queries = %d",
                 source.get("id"), len(literal), len(merged) - len(literal), len(merged))

    spec = params.get("cpv_from_taxonomy")
    if spec:
        generated = build_cpv_groups(cfg, spec if isinstance(spec, dict) else {})
        literal = list(params.get("cpv_groups") or [])
        seen = {g.get("label") for g in literal}
        merged = literal + [g for g in generated if g.get("label") not in seen]
        expanded["cpv_groups"] = merged
        log.info("%s: %d literal + %d taxonomy CPV groups = %d",
                 source.get("id"), len(literal), len(merged) - len(literal), len(merged))

    return {**source, "params": expanded}
