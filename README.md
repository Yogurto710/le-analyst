# Le Analyst

A command-line research assistant for public equities. Three commands:

- `analyst research TICKER "QUESTION"` — produces a sourced, dated brief on a specific question.
- `analyst initiate TICKER` — produces a deep-dive initiation report with peer comps, valuation, risks, and an investment framework.
- `analyst translate PATH` — translates an existing English report into another language (Chinese today), saving a sibling file and leaving the English original as the source of truth.

Output is saved as Markdown to `briefs/` (research) and `reports/` (initiation).

## Requirements

- Python 3.10+
- A [Moonshot AI](https://platform.moonshot.cn/) API key for Kimi K2.6
- A [Tavily](https://tavily.com/) API key for web search

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate    # Windows
# source .venv/bin/activate    # macOS/Linux
pip install -e .
cp .env.example .env
# edit .env to add your two API keys
```

## Usage

```bash
# A focused brief on a single question
analyst research RBLX "Is user growth sustainable?"

# A full initiation report (slower, more expensive)
analyst initiate RBLX

# Show each tool call as it happens
analyst initiate RBLX --verbose

# Generate and localize in one step (English original is still saved)
analyst initiate RBLX --translate zh

# Or translate an existing report on its own
analyst translate reports/RBLX-initiation-20260426.md --lang zh
```

Output files use the convention `TICKER-slug-YYYYMMDD.md` (briefs) and `TICKER-initiation-YYYYMMDD.md` (reports).

Real sample initiation reports are in [`reports/`](reports/) — committed as-produced (including the data gaps flagged in each file's frontmatter `open_questions:` metadata). The pre-rebuild 11-section sell-side format is preserved in the (gitignored) `reports_legacy/` archive.

## Sample brief structure

```
# RBLX: Is user growth sustainable?

## Summary
2-3 sentences with the bottom-line view.

## Sub-questions
3-5 sub-questions with cited findings.

## Sources
Numbered list of sources, each with a URL.
```

## Sample initiation report structure

```
# NFLX: Initiation Report

## Trading Snapshot          (price, mkt cap, 52W range, volume, short %, EV/Rev, EPS, P/E)
## Business Overview         (anchored in 10-K Item 1)
## Financial Profile         (financial summary [trailing only], forward estimates [consensus + management guidance], QoE notes; net cash inline)
## Sell-Side Q&A Analysis    (themes sell-side analysts probed in the latest Q&A: firm | sharpest Q | mgmt response | implication)
## Market Opportunity        (company TAM vs. independent TAM, implied share)
## Competitive Landscape     (per-competitor table with entity-bound metrics)
## Valuation Context         (leads with forward P/E or EV/Rev on consensus, vs peer median; peer comp w/ FY+1E + FY+2E columns and 2-yr Rev CAGR; forward context; history)
## Key Risks                 (10-K Item 1A + emerging risks)
## Investment Framework      (bull/bear/base, cycle positioning*, key debates, catalyst calendar)
                            (* cycle positioning only for cyclical industries)
## Open Questions            (gaps for the analyst to verify manually)
## Sources                   (numbered, every entry has a URL)
```

## Design notes

A few choices that shaped how the tool works:

**Single-file Python.** `analyst.py` holds the whole thing — Typer commands, prompt templates, tool implementations, and the agent loop. Refactor when something hurts.

**Kimi K2.6 over Claude/GPT.** The OpenAI-compatible endpoint at `api.moonshot.cn` plus aggressive prompt-cache pricing (~96% cache hit rate on a typical run, cached input at ~17% of uncached) makes long agentic loops cheap. A full initiation report runs ~$0.75 USD.

**Localization is post-hoc and additive.** `analyst translate` (and the `--translate` flag) run the finished English report back through Kimi — itself a Chinese-native model, so no new dependency — as a single non-agentic pass, saving a `.zh.md` sibling and keeping the English version canonical (sources for US-listed names are English). The translation prompt preserves numbers, `$` (never converted to ¥), source URLs byte-for-byte, and table structure, and re-states the no-rating / no-price-target discipline so it survives into Chinese. A verification step warns on any URL/table/heading drift from the original.

**Three-phase initiation flow.** The `initiate` command enforces strict phase boundaries via the system prompt:
1. **Gather** — `edgar_search`, `edgar_fetch`, `web_search`, `fetch_url` to pull 10-K sections, transcripts, peer financials, TAM, historical multiples, and catalysts.
2. **Compute** — `python_repl` (max 3 calls) for every derived metric and peer multiple. After the first `python_repl`, no more searches allowed.
3. **Synthesize** — write the report with no tool calls.

Without explicit phase rules the model interleaved gathering, computing, and writing, and ran out of tool budget mid-synthesis.

**Recency-filtered search.** Tavily searches are restricted to the last 90 days by default. The model is also told today's date in the system prompt so it can flag stale sources that slip through.

**Per-source fetch limits.** Earnings call transcripts (50K char cap) get more room than news/Yahoo pages (10K cap) because Q&A sections hold disproportionate signal. SEC filing sections get 40K. The cap is selected from the URL pattern.

**Entity-binding rule for competitive data.** Competitor metrics are only accepted if the source sentence explicitly names the competitor as the subject. This prevents Fortnite's MAU from ending up in Unity's row, which used to happen when peers were extracted from articles about the subject company.

**Agentic loop, not multi-agent.** All work runs in one growing conversation. This maximizes prompt-cache hit rate and keeps debugging linear (one transcript to read top-to-bottom). Multi-agent was considered and shelved — the cache invalidation cost on Kimi outweighs the parallelization wins for this workload.

**SEC EDGAR direct.** `edgar_search` and `edgar_fetch` go to `data.sec.gov` and `www.sec.gov` directly using the documented public APIs (CIK lookup → submissions JSON → primary document URL). No third-party financial-data vendor.

**`python_repl` for arithmetic.** Stateless subprocess running `python -c <code>`, 10s timeout, 4KB output cap. The model is instructed to compute every derived metric (growth rates, margins, multiples, market shares) via this tool rather than in prose, so the report's numbers are reproducible and the math is visible.

## Project layout

```
analyst.py            # everything
pyproject.toml        # dependencies, console script entry point
.env.example          # template for API keys
reports/              # initiation output (tracked, public)
reports_legacy/       # pre-rebuild initiation archive (gitignored)
briefs_en/            # English research briefs (gitignored)
briefs_ch/            # Chinese research briefs (tracked, public)
```

## License

MIT
