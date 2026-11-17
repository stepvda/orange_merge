# Best-of-breed selection

This document explains which functionality was selected from the five Orange
Business innovation-radar projects to build the merged application, and why.
Every data source is listed separately in [data_sources.md](data_sources.md).

## 1. The five projects

The five projects are:

| Project | Repository | Built with | Size and maturity |
|---|---|---|---|
| **Base** | [stepvda/orange](https://github.com/stepvda/orange) | FastAPI · SQLite · React + TypeScript | About 47k lines, 524 tests. 457 opportunity spaces from 11.8k signals across 33 sources. Includes market sizing, competitor intelligence, a workflow, a planner, briefs and 12 pre-sales documents. |
| **HuMaSoo** | [orange_opportunity_spaces_HuMaSoo](https://github.com/husseinabuammar24-cloud/orange_opportunity_spaces_HuMaSoo) | Python · SQLite · Streamlit · Gemini | About 2k lines, no tests. 59 scored spaces in 7 domains. |
| **Use case** | [Orange_use_case](https://github.com/amellalajiba-sys/Orange_use_case) | Python · SQLite · Streamlit · Power BI · Groq/Cerebras | About 34 tests. 125 spaces, two score axes, and a taxonomy that extends itself. |
| **Radar Orange** | [innovation_radar_orange](https://github.com/VictorCourtois135/innovation_radar_orange) | Azure AI Foundry · Azure SQL · Streamlit | No tests. 30 spaces from 908 agent-collected signals. Strongest visual exploration. |
| **Radar V2** | [Orange-business-innovation-radar](https://github.com/IkarusV/Orange-business-innovation-radar) | Reflex · SQLite · Navy LLM proxy | 159 tests. 309 spaces exported. Strong explainability and role modes. |

## 2. Choosing the base

The brief asks for a **Python backend, a SQLite database and a React frontend**.
Only one project already had all three: Base (`stepvda/orange`). It was also the
most complete on every axis the requirements care about:

- Every claim cites retrieved evidence.
- Scores are stored with their inputs, weight set and versions, so any stored
  score can be recalculated.
- It ingests sources in several languages.
- It runs a stage-gate workflow.
- It produces bottom-up market sizing.
- It has authentication and 524 automated tests.

Rebuilding another project's pipeline into this stack would have discarded most
of that work for no gain. The approach was therefore:

1. **Adopt Base as the foundation.** Code, configuration and the populated
   database were copied from the local `../orange` repository.
2. **Compare each capability of the other four projects with Base.** A peer
   feature that Base already does as well or better is marked **covered**. A
   feature Base lacks, that fits its principles (evidence-first, explainable,
   reproducible), is **ported**. A feature that conflicts with those principles
   is **rejected**, with the reason written down.
3. **Reimplement ported features in the target stack** rather than copying
   them. The peer code is Streamlit, Reflex or Azure-specific. The *idea* is
   ported and the weaknesses found in the peer implementation are fixed on the
   way in. Each case is listed below.

## 3. What was ported

| # | Feature | From | Where it lives now | Why it was selected |
|---|---|---|---|---|
| 1 | **What-if scoring simulator** | Radar Orange (`radar/views/methodology.py`) | **Explore → What-if weights** (`frontend/src/components/Explore.tsx`, `/api/explore/scores`) | Base shows *how* a score is calculated, but a strategist could not ask *"what if we cared more about strategic relevance?"*. The simulator answers that without changing the stored scores, so the rule that a weight change needs a new weight set still holds. |
| 2 | **Side-by-side compare of up to 3 spaces** | Radar Orange (`radar/views/opportunities.py`, overlaid polar chart) | **Explore → Compare** | Choosing between two similar spaces (for example the two drone-detection spaces in Defense and in Aerospace) is a real portfolio decision. Base only showed one space at a time. |
| 3 | **Landscape (topic) map** | Radar Orange (`tmp/20260819_orange.ipynb`: BERTopic, UMAP and datamapplot, shipped as a static PNG) | **Explore → Landscape map** (`src/radar/landscape.py`, `/api/explore/landscape`) | It was the most legible picture in any of the projects. The map shows that spaces cluster by the language of their evidence, which reveals cross-vertical overlaps the vertical × use case × technology grid hides. |
| 4 | **Taxonomy watchlist with human review** | Use case (`watchlist_terms`, `proposals`, `extend_taxonomy.py`; review was CLI-only) | **Coverage → Taxonomy watchlist** (`src/radar/taxonomy_watch.py`, `/api/taxonomy/watchlist`) | Base's closed vocabulary silently discarded every candidate whose use case or technology was outside the list. Those rejections tell you where the taxonomy is falling behind the market. This turns that waste into a curated growth path. |
| 5 | **AI footprint ledger** | HuMaSoo (CodeCarbon + EcoLogits around the pipeline) | **Analytics → AI footprint** (`src/radar/footprint.py`, `/api/analytics/footprint`, `llm_usage` table) | Sustainability is one of the domains Orange Business sells. A buyer can fairly ask what the radar's AI costs in energy and carbon, and NFR-10 already treats inference cost as a reported quantity. |
| 6 | **Semantic Scholar connector** | Use case (`fetch_semantic_scholar`) | `src/radar/connectors/research.py`, source `semantic_scholar` | Adds venue-aware applied research with an exact date filter, which complements OpenAlex, Crossref and arXiv. |
| 7 | **Vendor engineering blogs** (AWS, Azure, NVIDIA, Cisco) | Use case (`VENDOR_FEEDS`) | Source `vendor_blogs` (tier 4), plus a new `signal_type_hint` parameter on the generic RSS connector | Shows when a capability is generally available from a hyperscaler or network vendor, which is a technology-maturity signal. These vendors are often Orange partners as well as competitors. |
| 8 | **Publisher credibility registry** | Radar Orange (`source/source_registry.csv`) | 40 new entries in `config/source_tiers.yaml` | Base tiered publishers by domain but did not list much of the telecoms trade press, the telecoms analysts or the operators' own newsrooms. The registry fills that gap. |
| 9 | **Observed out-of-taxonomy terms** (data) | Use case (`radar.db`, 223 rows) | `config/taxonomy/watchlist_seed.json` (174 aggregated terms), seeded once into the watchlist (164 kept after filtering, 37 proposed) | Gives the watchlist real content from day one, observed by an independent pipeline. Examples include "Health Technology Assessment", "AI sovereignty", "Electronic attestation" and "Quantum computing". |
| 10 | **CSV export of the ranked list** | Radar Orange (download button on the top-opportunities page) | **List → CSV** button (`downloadCsv` in `App.tsx`) | Strategists and sales take shortlists into spreadsheets. The file contains exactly the rows on screen, in the same order. |

### What changed on the way in

Each ported feature fixes a problem found in the peer implementation.

**What-if simulator**
- The peer simulator re-weighted five sub-scores that the LLM partly invented.
  Here it re-weights Base's five stored, deterministic components.
- With the stored weights it reproduces every stored score exactly: 0 spaces
  move and 10 of the top 10 stay. This is checked in the UI, and it proves the
  client-side calculation matches the pipeline's.

**Landscape map**
- The peer map was UMAP and HDBSCAN run in a notebook and saved as a PNG. The
  map here is computed **live** from the embeddings already stored on every
  signal: the centroid of each space's evidence, projected with **PCA**.
- PCA needs no new dependency, gives the same result every run, and reports how
  much variance its axes explain. The map states that figure (about 24%), so it
  is read as clusters rather than as distances.
- The result is cached until the evidence changes.

**Taxonomy watchlist**
- **Hook:** the peer project needed a separate LLM output field for "watchlist
  candidates". Here the watchlist hooks into the vocabulary-validation failure
  that synthesis already had, so no prompt changed.
- **Generic terms:** "AI" made up 77 of the peer's watchlist hits, so generic
  terms are blocked. The blocklist is configuration (`taxonomy_watch.generic_terms`).
- **Spellings:** terms the vocabulary can already resolve are ignored.
- **Review and storage:** review happens in the UI and records who decided and
  when. Approved terms are written to a separate `config/taxonomy/extensions.yaml`,
  so the hand-curated taxonomy files are never rewritten by the machine.
- **Validation:** a use case cannot be approved without a business domain,
  because a space's domains are derived from its use case.
- **Scope:** only use cases and technologies can be extended. A vertical carries
  NACE codes and sizing crosswalks, so adding one is a modelling change rather
  than a review.
- **Thresholds:** the peer code used three inconsistent thresholds (≥2, ≥3 and
  ≥5). Here there is one, `proposal_threshold`.

**AI footprint**
- CodeCarbon measures the *local* machine, but the radar's inference runs on a
  hosted model. EcoLogits needs to know each model's size.
- The ledger therefore records **exact token counts per call**, with the calling
  module as its purpose, and *estimates* energy and CO₂e from per-token factors
  stated in `config/settings.yaml` (`footprint:`).
- The factors are shown beside the figures, consistent with the radar's rule
  that every number must break down into its parts.
- Runs from before the ledger existed are counted once, from the per-run
  `llm_usage` totals that Base already stored: 12,374 calls, about 9.4 kWh and
  3.7 kg CO₂e at the default factors.

**Semantic Scholar**
- The peer code retried a 429 response after a fixed sleep. Here the connector
  runs on Base's shared HTTP session, with pacing and a circuit breaker.

**Publisher registry**
- The peer registry's 1–5 scale was mapped onto §4.3.7's 1–4 tiers.
- Two bugs were fixed on the way in: a fused CSV line, and auto-classified rows
  given tier 3 on a scale where the others used 1–5.

## 4. What Base already covered

These peer features were judged and found already present in Base, usually in
a more rigorous form. Nothing was ported.

| Peer feature | From | Base equivalent |
|---|---|---|
| Two independent axes (attractiveness vs right to win), never combined | Use case, Radar V2 | Attractiveness and Right to Win, plus Conviction and Competitive intensity (`docs/SCORING_FORMULAS.md`) |
| L0–L4 portfolio distance, grounded in an asset catalogue | Use case | Portfolio distance from the business graph's typed links (`src/radar/graph.py`) |
| Signal-type horizon (Now/Next/Later) with a "why this timing" rule | Radar V2, Use case (urgency) | `derive_horizon` with a stored `horizon_basis` (`src/radar/scoring.py`) |
| Source-trust anchors and evidence quality | Radar V2, Radar Orange | Tiering in `config/source_tiers.yaml`, a cap on tier-4 contribution, and a diversity discount |
| Role modes (Strategist / Sales / Presales) that change ranking and emphasis, not the numbers | Radar V2, Radar Orange, Use case | `config/role_modes.yaml` and role ranking |
| Role-specific next actions | Use case, Radar V2 | `next_actions` per role (FR-17) |
| Polar radar view (ring = horizon, sector = domain, size = attractiveness) | Use case, Radar Orange, HuMaSoo (planned) | Radar tab (`RadarChart.tsx`) |
| Anti-hallucination ID resolution and claim–evidence traceability | HuMaSoo | Evidence binding: unsupported claims are removed (`src/radar/pipeline/synthesis.py`) |
| Embedding clustering into themes | Radar Orange, Radar V2 | Multilingual embeddings with theme clustering (`src/radar/pipeline/themes.py`) |
| Deduplication of spaces and signals | Radar Orange, Use case | Canonical triple identity (unique index), merge on synthesis, URL and content-hash dedup |
| Recurring-theme promotion into spaces | Use case | Clusters → candidate → state lifecycle (candidate / watchlist / active / fading) |
| Status workflow (candidate, watchlist, validated…) | Radar Orange | Stage-gate workflow board with transitions and assessments |
| Focused business report from web research | Radar V2 | PDF brief and 12 pre-sales documents, built from stored evidence |
| KPI tiles, "what's hot" cards, signal mix and evidence timelines | Use case, HuMaSoo, Radar Orange | Analytics tab, Coverage tab, per-topic evidence timeline |
| Cheap re-scoring without the LLM | Use case | `radar refresh --stages score` recomputes deterministic scores only |
| Per-source cooldowns and rate-limit handling | Use case | Per-source pacing, graceful degradation and a circuit breaker in `HttpSession` |
| Multi-provider LLM client | Use case, HuMaSoo | Provider-agnostic client (DeepSeek, OpenAI, Ollama) |
| Detached pipeline runs with progress | Radar V2 | Background generation jobs with progress and cancel (`/api/generate/*`) |

## 5. What was rejected, and why

| Peer feature | From | Reason |
|---|---|---|
| Strategic relevance, evidence quality or weighted score **produced by the LLM**, and arithmetic trusted to the LLM | HuMaSoo, Use case, Radar Orange | HuMaSoo stored LLM-computed weighted scores that did not match their own criteria. The radar requires every score to be calculated by code and to break down into its parts. |
| Collecting signals with an LLM agent (Bing grounding) | Radar Orange | Dates were partly estimated by the LLM and provenance was not fixed. This breaks DR-04. |
| Google Trends blended into market strength | Radar Orange | Unofficial client, and consumer search interest says little about B2B demand. Catalogued as disabled. |
| NewsAPI.ai | HuMaSoo, Use case | Paid, and dominated by financial newswires. Catalogued as disabled. |
| ProZorro | Radar V2 | Outside the priority markets. Catalogued as disabled. |
| Scraping the partner directory | HuMaSoo | Names only, with a spoofed User-Agent, and never used by its own pipeline. Base has a curated partner register. |
| Ad-hoc web search (Tavily / SearXNG) as evidence | Radar V2 | Undated, untiered pages. Catalogued as disabled. |
| Company workspace (document upload into prompts) | Radar V2 | Useful, but it brings in a document-processing stack and a prompt-injection risk. Internal signal intake covers the core need. Noted as a candidate for later. |
| Persona-damped ranking `base × (0.5 + 0.5w)` | Radar V2 | Base already ranks by role through `role_modes.yaml`. A second persona multiplier would hide why a space ranks where it does. |
| ML noise gate (embeddings + logistic regression) | Radar V2 | It needs labelled data the merged corpus does not have. Base's relevance gate plus multilingual lexicon covers this. |
| Keyword vertical tagger | HuMaSoo, Use case | Too noisy: the keyword `"ev"` matched "every". |
| Power BI report | Use case | Not part of the target stack. CSV export and the read API serve the same need. |
| Streamlit, Reflex and Azure SQL front ends | HuMaSoo, Use case, Radar Orange, Radar V2 | Replaced by the React frontend required by the brief. |

## 6. The resulting application

**Backend:** Python FastAPI (`src/radar/api.py`), with the best-of routes in
`src/radar/explore_api.py`.

**Database:** SQLite (`data/radar.db`), with two new tables, `llm_usage` and
`taxonomy_watchlist`.

**Frontend:** React + TypeScript + Vite (`frontend/`). The tabs, with what this
merge added:

| Tab | Content |
|---|---|
| Radar | Unchanged |
| List | Adds **CSV** export |
| Brief | Unchanged |
| Analytics | Adds **AI footprint** |
| **Explore** (new) | What-if weights · Compare · Landscape map |
| White space | Unchanged |
| Coverage | Adds **Taxonomy watchlist** |
| Generate, Workflow, Planner | Unchanged |

**Tests:** `tests/test_bestof.py` adds 13 tests on top of Base's suite. They
cover:
- the footprint calculation and ledger, and that a failing ledger never breaks
  an LLM call;
- watchlist filtering, approval into the live vocabulary, reject and reopen, the
  seed import, and the synthesis hook;
- landscape geometry;
- Semantic Scholar parsing;
- source and tier configuration;
- the RSS signal-type hint.

### Suggested next steps

- Set `SEMANTIC_SCHOLAR_API_KEY` and run a full `radar refresh`. The two new
  sources then flow through classification, scoring and synthesis like every
  other source.
- Calibrate the footprint factors against the LLM provider's own disclosures.
- Review the 37 proposed watchlist terms with a taxonomy curator.
- Add Radar Orange's geographic-scope caveats for Orange capabilities to
  `config/business_graph/assets.yaml`, for example Bleu being France-only and
  no US network presence.
