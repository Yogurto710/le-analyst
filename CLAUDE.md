# Le Analyst — Project Context for Claude Code

## What this is

A single-file Python CLI (`analyst.py`) that produces sourced markdown research on public equities. Three commands:

- **`analyst research TICKER "QUESTION" [--lang en|zh]`** — focused brief on a specific question. Output → `briefs_en/` or `briefs_ch/` depending on `--lang` (default `en`). Model writes natively in the chosen language on the first pass; no translation step.
- **`analyst initiate TICKER [--lang en|zh]`** — deep-dive initiation report (10-K parsing, peer comps, valuation, investment framework). Output → `reports/`. Same `--lang` semantics.

Public repo: https://github.com/Yogurto710/le-analyst

## How it works (architecture in one screen)

The whole tool runs an **agentic tool-use loop** against Kimi K2.6 via the OpenAI-compatible Moonshot endpoint (`api.moonshot.cn/v1`). The loop, the prompt templates, and the tool implementations all live in `analyst.py` — single file by design until something hurts.

**Tools available to the model:**
- `web_search(query, max_results)` — Tavily, last-90-days-only filter
- `fetch_url(url)` — `httpx` + `trafilatura`; per-URL fetch char limit (10K default, 50K for transcripts via URL-pattern detection)
- `edgar_search(ticker, form_type)` — SEC EDGAR submissions API (CIK lookup → filings list)
- `edgar_fetch(url, item?)` — fetch filing + regex-based Item section extraction; 40K cap
- `python_repl(code)` — stateless `subprocess.run([sys.executable, "-c", code])`, 10s timeout, 4KB output cap. **Only available to `initiate`.**

**`research` command** is a simple loop: gather → write brief. Tool budget 30.

**`initiate` command** enforces three sequential phases via the system prompt:
1. **Gather** (Phase 1): SEC filings, transcripts, Yahoo key-stats, per-peer financials, TAM, historical multiples, catalysts, Wall Street consensus (revenue + EPS, FY+1 and FY+2, subject AND every peer). Aim under 65 calls.
2. **Compute** (Phase 2): `python_repl` for every derived metric and peer multiple. Hard cap of 3 calls. **Once Phase 2 begins, no more searches allowed.**
3. **Synthesize** (Phase 3): write the report. No tool calls.
4. **Review + revise** (Phase 4, optional): code-side checks C1-C7 parse the draft. If any fire, the model gets the finding list and produces a targeted revision; we save the revised version. Zero added cost when no findings; ~$0.20 + 3-5 min when a revision runs.

Phase 4 checks (in `_review_draft`):
- **C1** LFY column year in Financial Summary ≥ `today.year - 1` (the canonical persistent bug — LFY off-by-one was caught 4 of 5 China-ADR runs)
- **C2** Trading Snapshot reconciliation: `price ÷ EPS ≈ P/E` and NM-consistency
- **C3** Peer Comp Table median: <3 valid non-NM peer values → median cell must be `—`
- **C4** Forward valuation lens matches LTM profitability (loss-making → forward EV/Revenue; profitable → forward P/E)
- **C5** All 11 required H2 sections present
- **C6** Forward Estimates: gross margin ≥ EBITDA margin
- **C7** Citation completeness: every `[N]` in body has a matching `[N]` source entry, no truncation (highest-indexed source must contain a URL), body must cite sources if substantive (>500 words). Catches the DeepSeek failure mode where the model emits a long uncited body, and the KM-07 off-by-one regression.
- **C8** EPS footing (new shape only): for each Financial Summary column, recompute implied diluted share count = Net Income / |Diluted EPS|; flag if max/min spread across columns >1.5x. Catches the 10x-EPS-error class observed on MRVL 2026-06-15 v0 (FY24 NI $0.39B with EPS $0.05 implies 7,800M shares vs the real ~865M).
- **C9** Peer Comp Table LTM outlier (new shape only): flag any peer where `EV/Rev (LTM)` is more than 2x the peer median. The common cause is a single-quarter revenue value mislabeled as TTM (NVDA at 60.5x EV/Rev because $81.6B was Q1 FY27 single-quarter, not real TTM ~$253B).
- **C10** EBITDA basis (new shape only): for each Financial Summary column, assert EBITDA > Net Income (NI > 95% of EBITDA is structurally implausible). Catches basis mismatches where NI includes a one-time gain that adjusted EBITDA excludes.

The legacy 11-section `initiate_legacy` command runs only C1-C7 (those were tuned to the legacy layout). The new 8-section `initiate` runs C5+C7 PLUS C8/C9/C10, which parse the new Financial Summary / Peer Comp Table layout inside Appendix at H3.

The revision is a single model call with `_revise_draft`. If it comes back too short (<70% of draft) or drops section headings, we fall back to the draft and log the failure to stderr.

Total `initiate` tool budget: 95 (raised from 90 to fund per-peer consensus searches for the forward peer comp table).

## Language (`--lang en|zh`)

Language is chosen up-front via `--lang` and the model writes natively on the first pass. **There is no translation step.** This replaced a prior post-hoc translate flow after the user observed empirically that Kimi's first-pass Chinese reads better than a translation of its own English.

- **`--lang en`** (default): system prompt is unchanged from the long-standing template. Preserves Kimi's ~96% prompt-cache hit rate.
- **`--lang zh`**: the same template plus `_LANG_INSTRUCTION_ZH` appended. That block carries the preserve-verbatim rules (numbers, currency `$`, source URLs byte-identical, tickers in Latin, English source titles untranslated), the Chinese financial-term glossary (EBITDA/FCF/etc. as-is; established Chinese names like 网易/美光), and the Investment Framework discipline re-stated in Chinese (no 买入/卖出/持有, no 价格目标 — these phrasings are very natural in Chinese, so the rule must be explicit there).
- **Sources stay English regardless.** The model reads English 10-Ks, transcripts, FT, etc., and cites them with English titles and original URLs. The Chinese-mode prompt's preserve-verbatim rules enforce this.
- **YAML frontmatter records the language** via a `lang:` line so the saved file documents what produced it (alongside `model:` and `date:`).
- **File routing** is keyed off `--lang`: `briefs_en/` for English, `briefs_ch/` for Chinese. One `.md` per run — no `.zh.md` sibling.
- **The mini-app** (`miniapp_mvp/`) exposes a 中文/English toggle on the submit page; it auto-detects from the question's language as the default and the user can override. The chosen language is sent as a `lang` field on `POST /jobs` and forwarded to `analyst.py --lang ...`.
- Legacy `.zh.md` files in `briefs_en/` from the previous translate flow are historical artifacts; webapp.py and analyst.py no longer produce or consume them.

## Critical conventions that aren't obvious from the code

These are the rules and design decisions accumulated over several rounds of iteration. Don't undo them without understanding why they're there.

### Model and pricing

- **Default model is `kimi-k2.6`**, opt-in alternative is `deepseek-v4-pro` via `--model deepseek` on any command (or `LE_ANALYST_MODEL=deepseek` in env). Registered in `MODELS` dict at the top of `analyst.py` — each entry carries id + base_url + api_key_env + extra_body so adding a third model is a few lines. Do NOT switch to Claude/GPT without discussing — the cost story for Kimi depends on its prompt caching (cached input ~17% of uncached, ~96% hit rate on a typical run). DeepSeek's caching is automatic and shaped differently; per-run cost is roughly comparable but the savings come from different places. Whichever model produces the brief, its id is written to the YAML frontmatter `model:` line so the file records what generated it.
- **`thinking: {"type": "disabled"}`** is set in `extra_body`. Enabling thinking breaks the OpenAI-compatible tool-calling round-trip because `reasoning_content` is required in subsequent messages but the streaming SDK doesn't surface it cleanly.

### Source quality (hard rules — these encode prior failures)

- **Entity binding rule**: a competitor metric only belongs to that competitor if the source sentence explicitly names that competitor as the subject. Never extract competitor numbers from articles *about the subject company* — they're consistently garbled. Each row of the competitive landscape table contains data about ONE entity only.
- **Subject-company cross-reference**: third-party numbers about the subject must be reconciled against the company's own 10-K/earnings disclosures. If a third-party MAU figure contradicts the 10-K's DAU figure, either reconcile with the methodological difference or discard.
- **Private company labelling**: all financials for private companies (Epic Games, Valve, etc.) must be labelled "estimated" with source — "$6B estimated 2025 revenue (Sacra)", never "$6B revenue (2025)".
- **Source URL requirement**: every source line must end with the URL the model actually used. No exceptions for paywalled sources or SEC filings.
- **Banned source types**: Wikipedia, Reddit, listicles, "Top 10" articles, content-farm aggregators. The prompt explicitly forbids these.

### Recency

- Tavily search restricted to last 90 days (`SEARCH_DAYS = 90`).
- Today's date is injected into the system prompt at runtime so the model can flag stale sources that slip through Tavily's filter.

### Output conventions

- Briefs saved as `briefs_en/TICKER-slug-YYYYMMDD.md` or `briefs_ch/TICKER-slug-YYYYMMDD.md`, routed by `analyst.py:_briefs_dir_for(lang)` on the `--lang` flag. One file per run, no `.zh.md` sibling. YAML frontmatter carries `ticker`, `question`, `date`, `lang`, and `model` (the actual id used, e.g. `kimi-k2.6` or `deepseek-v4-pro`).
- Initiations saved as `reports/TICKER-initiation-YYYYMMDD.md` with the same frontmatter shape (sans `question`).
- Slug = question with stopwords stripped, first 3 meaningful tokens. The stopword filler list includes "roblox" (legacy from RBLX testing — leave it).

### Initiation report sections (in order)

1. Trading Snapshot (Price, Mkt Cap, 52W Range, Avg Daily Volume, Short Interest, EV/Rev, EV/Rev Fwd, EPS LTM, EPS Fwd, P/E LTM, P/E Fwd)
2. Business Overview
3. Financial Profile (Financial Summary [prior FY / LFY / LTM — trailing only], Forward Estimates [consensus FY+1 / FY+2 table + management guidance + gap commentary], Quality-of-Earnings Notes; net cash position called out inline in the intro — no separate balance-sheet table)
4. Sell-Side Q&A Analysis (firms participating + 3-5 themed sub-blocks of the most-probed Q&A topics from the latest earnings call: Probed by / Sharpest question / Management response / What it implies; capped ~500 words; replaces the old Management Commentary section — strategic priorities moved to Business Overview, forward guidance to Forward Estimates)
5. Market Opportunity (company TAM + independent TAM + implied share + methodology caveat)
6. Competitive Landscape (Stated Position + Independent Evidence table)
7. Valuation Context (opens with the primary forward valuation lens — forward P/E if comfortably profitable, else forward EV/Rev, both FY+1 and FY+2 on consensus, positioned vs peer median — then Peer Comp Table [LTM + FY+1E + FY+2E EV/Rev and P/E columns, 2-yr Rev CAGR, peer Median row], Forward Context, Historical Context)
8. Key Risks
9. Investment Framework (Bull/Bear/Base + Key Debates + Catalyst Calendar)
10. Open Questions
11. Sources

### Investment Framework discipline

- Never write "buy", "sell", "hold".
- Never output a specific dollar price target — express direction as percentage range from multiple re-rating scenarios.
- Bull and bear cases must have equal analytical rigor; don't signal which side you favor.
- Key Debates section must use real debates only — never manufacture to fill space.
- Catalyst Calendar thresholds must be numeric where possible, anchored to consensus or guidance midpoint.
- Forward estimates (revenue, EPS, EBITDA) come from Wall Street consensus gathered in Phase 1 — cite source, analyst count, and date, and frame as "the Street expects…", never as the tool's own forecast. P/E on negative EPS is "NM".
- The narrative is centered on the company's CURRENT (forward) valuation: forward P/E if comfortably profitable, otherwise forward EV/Revenue (or EV/EBITDA) — both on consensus, shown for FY+1 and FY+2. Valuation Context opens with this lens, the Trading Snapshot carries it, and the Investment Framework re-rates around it. Trailing/LTM and forward figures are kept front-and-center (Financial Summary and Valuation Context both span trailing → LTM → FY+1 → FY+2).

### Quality-of-Earnings Notes triggers

Add one-sentence commentary when:
- SBC-adjusted FCF is < 50% of reported FCF
- Adjusted EBITDA differs from GAAP operating income by > 25%
- FCF and net income differ materially (call out working capital, deferred revenue, non-cash drivers)
- Bookings growth diverges from revenue growth by > 5 pp

Do NOT narrate every metric. Only flag divergences.

## Recurring quirks to be aware of

- **Kimi can leak instruction phrases** ("Now I have gathered sufficient data...") above the title despite the prompt explicitly forbidding it. The fix has been mostly prompt-side; if it recurs, consider stripping any pre-`#` content before saving.
- **API connection drops mid-stream** happen occasionally on `api.moonshot.cn`. The `agentic_loop` in `analyst.py` catches `httpx.ReadError`, `RemoteProtocolError`, and `ReadTimeout`. If the model already signalled `stop` and output was streamed before the drop, treat as success.
- **Windows `cp1252` encoding crashes on non-ASCII output.** Fixed at the top of `analyst.py` with `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`. Don't remove it.
- **Watch for hallucinated headline numbers** when the model is reading an unverified third-party transcript. If a YoY growth number exceeds 100% or a margin exceeds 70%, the model has historically treated extreme outliers as canonical without flagging — worth a sanity check before committing to a sample. Phase 2 now enforces forward-estimate sanity checks (EBITDA ≤ gross profit; gross ≥ EBITDA ≥ operating ≥ net margin ordering; annualized-vs-consensus divergence > 20%), which catches the impossible-margin class — e.g. an early Micron run produced a ~$105B EBITDA on ~$95B revenue (110% margin).

## Cost ballpark

- `research` run: roughly $0.20-0.40 USD.
- `initiate` run: roughly $0.60-0.80 USD.
- ~96% prompt cache hit ratio is doing most of the work; if cache hit drops, the cost climbs ~5x.

## File layout

```
analyst.py            # entire tool
pyproject.toml        # deps, console script entry point
.env.example          # template for MOONSHOT_API_KEY / TAVILY_API_KEY
.env                  # (gitignored) actual keys
examples/             # committed sample reports
briefs_en/            # (gitignored) research output — English questions
briefs_ch/            # (gitignored) research output — Chinese questions
reports/              # (gitignored) initiation output
token_log/            # (gitignored) Kimi token usage spreadsheets
README.md             # user-facing
LICENSE               # MIT
```

## Things explicitly NOT worth doing right now

These have been considered and shelved — don't suggest them unprompted:

- **Multi-agent architecture**: discussed at length. The cache-invalidation cost on Kimi outweighs the parallelization wins for this workload. Targeted peer-comp parallelization is the only piece worth revisiting if it comes up.
- **Switching to Seeking Alpha for trading stats**: paywalled, aggressive bot detection, no data advantage over Yahoo Finance which we're already fetching.
- **Re-enabling Kimi thinking mode**: breaks the tool-calling round-trip. Don't attempt without solving the `reasoning_content` issue.
- **Adding a dedicated multi-file architecture**: stay single-file until it actively hurts.

## When in doubt

- Read `analyst.py` top to bottom — it's ~1,000 lines but linearly organized: constants → tool implementations → agent loop → output saving → translation → commands.
- The system prompt templates (`RESEARCH_SYSTEM_PROMPT_TEMPLATE` and `INITIATE_SYSTEM_PROMPT_TEMPLATE`) at the top of the file encode most of the report structure and rules. If a behavior issue is in the output, it's almost certainly fixable in those templates.
