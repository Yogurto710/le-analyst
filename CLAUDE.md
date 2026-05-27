# Le Analyst — Project Context for Claude Code

## What this is

A single-file Python CLI (`analyst.py`) that produces sourced markdown research on public equities. Three commands:

- **`analyst research TICKER "QUESTION"`** — focused brief on a specific question. Output → `briefs/`.
- **`analyst initiate TICKER`** — deep-dive initiation report (10-K parsing, peer comps, valuation, investment framework). Output → `reports/`.
- **`analyst translate PATH [--lang zh]`** — translate a finished report into another language; the English original is kept. See **Localization** below.

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
1. **Gather** (Phase 1): SEC filings, transcripts, Yahoo key-stats, per-peer financials, TAM, historical multiples, catalysts, Wall Street consensus (revenue + EPS). Aim under 60 calls.
2. **Compute** (Phase 2): `python_repl` for every derived metric and peer multiple. Hard cap of 3 calls. **Once Phase 2 begins, no more searches allowed.**
3. **Synthesize** (Phase 3): write the report. No tool calls.

Total `initiate` tool budget: 90 (raised from 85 to fund consensus-estimate searches).

## Localization (`analyst translate`)

`analyst translate PATH [--lang zh]` writes a translated sibling of a finished report (e.g. `reports/MU-initiation-20260525.zh.md`); `initiate`/`research` also take a `--translate zh` flag to do it right after saving. Design decisions, don't undo without understanding:

- **English stays canonical.** Sources for US-listed names are English, so the English report is the source of truth and translation is a separate, re-runnable pass over the finished markdown — NOT a second generation. Don't move analysis into the translation step.
- **Reuses Kimi.** Translation is the same `kimi-k2.6` (a Chinese-native model) via one non-agentic streaming call (no tools), `thinking` disabled, with one retry on the usual `api.moonshot.cn` mid-stream drops. No new provider or dependency.
- **Preserve-exactly rules live in `TRANSLATE_SYSTEM_PROMPT_TEMPLATE`.** Numbers/units pass through verbatim; `$` is never converted to ¥/RMB; source URLs stay byte-identical; tables/headings/section order preserved; tickers stay Latin; a financial-term glossary keeps terminology consistent.
- **The Investment Framework discipline is re-stated in the translation prompt** — the model must not introduce 买入/卖出/持有 or a price target, because Chinese makes those phrasings very natural.
- **Frontmatter preserved verbatim** with a `lang:` line added; only the body is translated (`_split_frontmatter` / `_frontmatter_with_lang`).
- **`_verify_translation` is a guardrail, not a gate.** It warns (stderr) if the URL set, table-row count, or heading count drifts from the English — catching dropped sources or mangled tables — but never blocks the save.
- Only `zh` has a tuned glossary today; other codes fall back to a generic prompt with a heads-up. Cost ~$0.05-0.15 per report.

## Critical conventions that aren't obvious from the code

These are the rules and design decisions accumulated over several rounds of iteration. Don't undo them without understanding why they're there.

### Model and pricing

- **Model is `kimi-k2.6`**. Do NOT switch to Claude/GPT without discussing — the cost story depends on Kimi's prompt caching (cached input ~17% of uncached, and we get ~96% cache hit rate on a typical run).
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

- Briefs saved as `briefs/TICKER-slug-YYYYMMDD.md` with YAML frontmatter (ticker, question, date, model).
- Initiations saved as `reports/TICKER-initiation-YYYYMMDD.md` with YAML frontmatter.
- Slug = question with stopwords stripped, first 3 meaningful tokens. The stopword filler list includes "roblox" (legacy from RBLX testing — leave it).
- Translations are saved beside the original as `…-YYYYMMDD.<lang>.md` (e.g. `.zh.md`), with the English frontmatter preserved plus a `lang:` line; only the body is translated.

### Initiation report sections (in order)

1. Trading Snapshot (Price, Mkt Cap, 52W Range, Avg Daily Volume, Short Interest, EV/Rev, EV/Rev Fwd, EPS LTM, EPS Fwd, P/E LTM, P/E Fwd)
2. Business Overview
3. Financial Profile (Financial Summary [prior FY / LFY / LTM / FY+1E / FY+2E, consensus forward cols], Balance Sheet Snapshot, Key Ratios, Quality-of-Earnings Notes)
4. Management Commentary
5. Market Opportunity (company TAM + independent TAM + implied share + methodology caveat)
6. Competitive Landscape (Stated Position + Independent Evidence table)
7. Valuation Context (opens with the primary forward valuation lens — forward P/E if comfortably profitable, else forward EV/Rev, both FY+1 and FY+2 on consensus — then Peer Comp Table, Forward Context, Historical Context)
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
briefs/               # (gitignored) research output
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
