# Data sources across the five Orange use-case projects

This document lists every data source used by the five projects merged into this
best-of-breed application. That covers external APIs and feeds, scraped sites,
committed datasets, reference data and LLM providers. For each one it says
whether the merged application uses it, and why.

**Selection key**

| Mark | Meaning |
|---|---|
| ✅ **Selected** | Wired and enabled in the merged app (`config/sources.yaml`, or the path named in the row) |
| ➕ **Selected (new)** | Adopted from a peer project during this merge |
| 🔁 **Covered** | The peer project used it and the merged app already gets the same data through its own connector |
| 📦 **Data reused** | A committed dataset from a peer project that was imported |
| ⏸ **Catalogued** | Kept in `config/sources.yaml` but disabled, with the reason given there |
| ❌ **Not selected** | Not adopted; the reason is given in the row |

**The five projects**

| Short name | Repository | Stack |
|---|---|---|
| **Base** | [stepvda/orange](https://github.com/stepvda/orange), local copy at `../orange` | FastAPI · SQLite · React |
| **HuMaSoo** | [husseinabuammar24-cloud/orange_opportunity_spaces_HuMaSoo](https://github.com/husseinabuammar24-cloud/orange_opportunity_spaces_HuMaSoo) | Python pipeline · SQLite · Streamlit |
| **Use case** | [amellalajiba-sys/Orange_use_case](https://github.com/amellalajiba-sys/Orange_use_case) | Python pipeline · SQLite · Streamlit · Power BI |
| **Radar Orange** | [VictorCourtois135/innovation_radar_orange](https://github.com/VictorCourtois135/innovation_radar_orange) | Azure AI Foundry · Azure SQL · Streamlit |
| **Radar V2** | [IkarusV/Orange-business-innovation-radar](https://github.com/IkarusV/Orange-business-innovation-radar) | Reflex · SQLite |

Signal counts come from the merged database (`data/radar.db`) at the time of the
merge.

---

## 1. News and market signals

| Source | Used by | What it provides | Access | Selection |
|---|---|---|---|---|
| **Google News RSS**, seven editions (EN-GB, FR, DE, ES, IT, NL, plus MEA and APAC English) | Base, Use case (EN only), Radar V2 (paused) | News headlines and extracts. Base queries are derived from the taxonomy grid. The Use case project also ran competitor-watch queries and `site:eur-lex` queries through it. | Free, keyless | ✅ Base connector `rss_search`. 3,332 signals across the editions. The Use case project's `site:eur-lex` trick is **not** adopted: it pulled in 251 junk rows from a mirror site. EUR-Lex is queried directly (§3). |
| **Bing News RSS** | Base | A second news engine, so the publisher mix is more diverse | Free | ✅ 180 signals |
| **GDELT DOC 2.0** | Base, Use case | Global, geo-tagged news with the widest publisher diversity | Free; aggressive per-IP rate limit | ✅ 296 signals. Base paces requests and has a circuit breaker. The Use case project's 75 s spacing starved the source. |
| **Independent trade press RSS** (Light Reading, The Register) | Base | Tier-2 independent reporting | Free | ✅ 156 signals |
| **Hacker News (Algolia API)** | Base, Use case | Practitioner attention (tier 3) | Free | ✅ 66 signals |
| **SEC EDGAR full-text search** | Base | Named enterprises describing their own deployments in regulated filings | Free; User-Agent with contact required | ✅ 142 signals |
| **NewsAPI.ai / Event Registry** | HuMaSoo (3,075 articles, the main source), Use case | Full-text news and press releases. HuMaSoo filtered it to 21 European countries. | **Paid key** (`NEWSAPI_AI_KEY`) | ⏸ `newsapi_ai`. Its top publishers were financial newswires (finanznachrichten.de, marketscreener.com, wallstreet-online.de). The free engines above already give more diverse, multilingual coverage. |
| **Azure AI Foundry agent with Bing grounding** | Radar Orange (908 signals) | An LLM agent searches about 52 priority domains and returns JSON signals. Some dates are LLM estimates. | Azure subscription | ❌ Signals discovered by an LLM have no reliable date and no fixed provenance, which breaks DR-04 (every signal is dated). The agent's priority domain list was reused as **publisher tiers** instead (§8). |
| **Web search: Tavily / SearXNG** | Radar V2 (Discovery and Reports pages) | Ad-hoc web search for focused business reports | Tavily key, or self-hosted SearXNG | ⏸ `web_search`. Results are undated and untiered. On-demand research for a single space is already handled by `radar.presales.research`. |
| **Generic RSS feeds** (Retail Dive, Finextra, Breaking Defense…) | Radar V2 (collector deleted on 22 Aug) | Industry news | Free | 🔁 Covered by Google News and the trade-press feeds |
| **Google Trends (pytrends)** | Radar Orange | 12-month search interest per technology, blended into market-signal strength | Unofficial scraper; heavy rate limits | ⏸ `google_trends`. Consumer search interest is near zero for most B2B terms (the peer team needed a noise floor of 15), and the client is unofficial. |
| **Competitor watch** (operators' names combined with vertical seeds) | Use case | Competitor moves | Through Google News | 🔁 Covered and extended by Base competitor intelligence: 65 named competitors, 1,745 crawled pages, per-topic analyses (`config/business_graph/competitors.yaml`) |

## 2. Procurement (buying signals)

| Source | Used by | What it provides | Access | Selection |
|---|---|---|---|---|
| **TED, EU Tenders Electronic Daily, API v3** | Base, HuMaSoo, Use case, Radar V2 | Above-threshold EU notices: CPV codes, buyer, country, value, deadline | Free, keyless for search | ✅ 4,267 signals. Base slices the date window into 14-day chunks, which fixes the "40 notices, all from one day" sampling bug. HuMaSoo's idea of asking an LLM which domain a tender belongs to is covered by Base's CPV→vertical and CPV→use-case crosswalks (`config/crosswalks/`). |
| **BOAMP** | Base | French below-threshold tenders | Free | ✅ 289 signals |
| **UK Find a Tender (OCDS)** | Base, Radar V2 | UK above-threshold notices | Free | ✅ 173 signals |
| **UK Contracts Finder** | Base, Radar V2 | UK below-threshold notices | Free | ✅ 63 signals |
| **TenderNed** | Base | Dutch notices | Free | ✅ 77 signals |
| **Ukraine ProZorro** | Radar V2 | Ukrainian public tenders (OpenProcurement API 2.5) | Free; needs one extra request per tender | ⏸ `prozorro`. Outside Orange Business's priority market clusters. |

## 3. Regulation and standards

| Source | Used by | What it provides | Selection |
|---|---|---|---|
| **EUR-Lex (CELLAR SPARQL)** | Base (directly); Use case (through Google News `site:` queries) | Dated legal instruments, with their stage inferred | ✅ 86 signals, queried directly |
| **EC "Have your say"** | Base | Consultations with their feedback deadlines | ✅ 171 signals |
| **CERT-EU, NCSC-UK, CISA, CERT-Bund/BSI, ANSSI/CERT-FR** | Base | Security advisories and national guidance | ✅ 370 signals in total |
| **EBA / ESMA (DORA)**, **ACER** | Base | Financial and energy regulators | ✅ 19 signals |
| **NIST** | Base | Standards timelines (for example post-quantum cryptography) | ✅ 40 signals |
| **IETF Datatracker** | Base | Drafts and RFCs | ✅ 51 signals |
| **Regulators in the Radar Orange source registry** (BIPT, FCC, Ofcom, BEREC, European 5G Observatory) | Radar Orange | Found only through the agent's web search | ➕ Added as **tier-1 publisher overrides** (`config/source_tiers.yaml`), so any item from them is tiered correctly |
| ENISA, 3GPP/ETSI/O-RAN/GSMA | Base (catalogue) | — | ⏸ ENISA retired its RSS feeds. The standards bodies publish HTML, not feeds. |

## 4. Research and technology maturity

| Source | Used by | What it provides | Access | Selection |
|---|---|---|---|---|
| **OpenAlex** | Base | Scholarly works, with a flag for Orange-affiliated authors | Free | ✅ 1,002 signals |
| **Crossref** | Base | Peer-reviewed output by concept | Free | ✅ 603 signals |
| **arXiv** | Base, Use case | Preprints | Free | ✅ 288 signals |
| **CORDIS** | Base, Radar V2 (315 of its 548 articles) | EU-funded research projects | Free | ✅ 77 signals. Base fixed the date-template bug that made the connector return nothing. |
| **Semantic Scholar Graph API** | Use case | Papers with venue, citation count and an exact publication-date filter | Free; `SEMANTIC_SCHOLAR_API_KEY` recommended | ➕ **Selected (new)**. Connector `semantic_scholar` (`src/radar/connectors/research.py`). The first live run was rate-limited (HTTP 429) on the keyless shared pool, so set a free API key before relying on it. |
| **Vendor engineering blogs** (AWS News, Microsoft Azure, NVIDIA, Cisco) | Use case | Product launches and general-availability announcements, used as technology-maturity markers | Free RSS | ➕ **Selected (new)** as `vendor_blogs`, tier 4 (interested party), with `signal_type_hint: technology_maturity`. 57 signals on the first run. |
| Patents (EPO OPS, PatentsView), GitHub, Adzuna jobs | Base (catalogue) | — | — | ⏸ Reasons are recorded in `config/sources.yaml` |

## 5. Reference data (market sizing)

| Source | Used by | What it provides | Selection |
|---|---|---|---|
| **Eurostat**: structural business statistics (`sbs_sc_ovw`), enterprise cloud use, AI use, IoT use, ICT security | Base | Enterprise counts, turnover and adoption rates by NACE sector and country, used for bottom-up TAM/SAM/SOM | ✅ 56,385 observations, fetched through `radar.reference` (stored as reference data, not as signals) |
| OECD, ITU, IEA, World Bank | Base (catalogue) | — | ⏸ Not yet wired |

## 6. Orange Business internal and portfolio data

| Source | Used by | What it provides | Selection |
|---|---|---|---|
| **Business graph**: offers (14), named references (18), partners and assets (31 entries), competitors (65) | Base | Right-to-win inputs and competitor register | ✅ `config/business_graph/*.yaml` |
| **Use case reference lists**: `ORANGE_BUSINESS_ASSETS` (17 APIs), 18 customer references, analyst recognitions, capability statistics, partner tiers | Use case (`pipeline/config.py`) | Grounding for its right-to-win classification | 🔁 Covered by the Base business graph, which holds the same kind of data with typed, sourced links |
| **Orange capability baseline in a prompt**: about 14 capabilities, each marked DEPLOYED, PARTIAL GAP, UNCLEAR or DISCONTINUED, with its geographic scope | Radar Orange (`prompts/prompt_opportunity_space.txt`) | A capability check before creating a space | 🔁 Covered by the Base business graph and portfolio distance (L0–L4). Its geographic-scope caveats (for example Bleu is France-only) are worth adding to `assets.yaml` during calibration. |
| **Orange Business partner directory** (scraped from orange-business.com) | HuMaSoo (60 partners) | Partner names by domain | ⏸ `orange_partner_directory`. Names only, scraped with a spoofed User-Agent, and HuMaSoo's own pipeline never used them. Base keeps a curated partner register with tiers and technologies instead. |
| **Company documents and priorities** (uploaded PDF, PPTX, DOCX) | Radar V2 (Company workspace) | Internal context for classification | ❌ Not adopted. Base's internal signal intake (`internal_signals`, tier 3, 11 rows) is the equivalent path for internal knowledge. |
| **Internal signals** (moderated conversations, RFP themes) | Base | What Orange's own people are hearing | ✅ 11 rows, entered by hand |

## 7. Committed datasets in the peer repositories

| Dataset | Repository | Contents | Selection |
|---|---|---|---|
| `data/radar.db` (126 MB), plus a `data/db-archive/` split zip | Base | 11,816 signals from 35 enabled sources (including the two added in this merge), 457 opportunity spaces, scores with their inputs, 56k Eurostat observations, competitor pages, briefs, plans | ✅ **The initial database of the merged app**, copied from the local `../orange` repository |
| `data/opportunity_spaces.db` | HuMaSoo | 4,725 articles (NewsAPI.ai and TED), 107 opportunity spaces (59 scored), 60 partners | ❌ Not imported. The signals are mostly financial newswires and English-only. The spaces use free-text personas and geographies that do not map onto the closed taxonomy. |
| `radar.db` | Use case | 3,926 signals, 125 spaces, 248 recurring themes, **223 watchlist terms** | 📦 **The watchlist terms were reused.** They were aggregated into 174 distinct terms in `config/taxonomy/watchlist_seed.json` and seeded once into the new taxonomy watchlist. Generic terms such as "AI" and terms the vocabulary already resolves are dropped at import, leaving 164 terms (37 proposed). The signals were not imported: 8 days of coverage, keyword-overlap linking. |
| `data/signals.csv`, `opportunity_spaces.csv`, `opportunity_space_signals.csv` | Radar Orange | 908 agent-collected signals with 1536-d embeddings, 30 spaces, 205 links | ❌ Not imported. The publication dates are partly LLM estimates and the verticals are free text (135 distinct strings). |
| `source/source_registry.csv` | Radar Orange | 65 publishers on a 1–5 credibility scale | ➕ **Reused as configuration.** 40 publishers that were missing were mapped onto the radar's 1–4 tier scale and added to `config/source_tiers.yaml`. |
| `source/trends_cache.json` | Radar Orange | Google Trends values for 101 keywords | ❌ Not reused (see Google Trends in §1) |
| `imports/pipeline_opportunity_export/*` | Radar V2 | 309 spaces and 548 articles (CORDIS, Google News, TED, OCDS) | ❌ Not imported. Most spaces rest on one or two articles, and the CORDIS and TED content is already collected by Base connectors. |
| `fixtures/signal_type_golden_set.json` | Radar V2 | 60 signal-type examples it labelled itself | ❌ Not reused. The examples have not been reviewed by a human. |
| CodeCarbon CSVs | HuMaSoo | 28 emissions-log runs | ❌ The data is not reused, but the idea is: see the AI footprint ledger in `docs/best_of_selection.md` |

## 8. LLM and embedding providers

| Provider and model | Used by | Selection |
|---|---|---|
| **DeepSeek** (`deepseek-chat` / `deepseek-reasoner`) through an OpenAI-compatible client; **OpenAI**; **Ollama** for local or sovereign deployment | Base | ✅ Configured with `RADAR_LLM_PROVIDER`. Every call is now also recorded in the **AI footprint ledger** (`llm_usage` table). |
| Google Gemini (`gemini-3.6-flash` with fallbacks, key rotation) | HuMaSoo | ❌ The provider-agnostic client in Base already covers this |
| Groq `gpt-oss-120b` → Cerebras → SambaNova DeepSeek-V3.1 → Ollama `llama3.2:3b` | Use case | ❌ A fallback chain is not needed at this scale. Ollama remains the sovereign option. |
| Azure OpenAI GPT-5-mini, `text-embedding-3-large` | Radar Orange | ❌ Ties the app to Azure |
| "Navy" OpenAI-compatible proxy (`gpt-5.6-luna`, `glm-5.1`) | Radar V2 | ❌ Non-standard proxy |
| **Sentence-transformers `paraphrase-multilingual-MiniLM-L12-v2`** (local) | Base (Radar V2 used the larger mpnet model) | ✅ Signal embeddings. They now also feed the **Landscape map**. |

---

## Summary

| | Count |
|---|---|
| Distinct external sources across the five projects | about 50 |
| Enabled in the merged app | **35**: 33 from Base plus 2 new (Semantic Scholar, vendor blogs) |
| Catalogued but disabled, with a reason | 14, including 5 added during this merge |
| Peer datasets reused | 2: Use case watchlist terms and the Radar Orange source registry |

The full runtime catalogue is in `config/sources.yaml`. Live yields and the
sampling lessons learned are in [DATA_SOURCES_AND_STATUS.md](DATA_SOURCES_AND_STATUS.md).
