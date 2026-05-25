# Le Analyst

A command-line research assistant for public equities. Two commands:

- `analyst research TICKER "QUESTION"` — produces a sourced, dated brief on a specific question.
- `analyst initiate TICKER` — produces a deep-dive initiation report with peer comps, valuation, risks, and an investment framework.

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
```

Output files use the convention `TICKER-slug-YYYYMMDD.md` (briefs) and `TICKER-initiation-YYYYMMDD.md` (reports).

A real sample initiation report on Netflix is in [`examples/NFLX-initiation-20260427.md`](examples/NFLX-initiation-20260427.md). It is committed as-produced (with its real data gaps and limitations flagged in the Open Questions section) so readers can see what the tool actually outputs, not a polished demo.

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

## Trading Snapshot          (price, mkt cap, 52W range, volume, short %, EV/Rev)
## Business Overview         (anchored in 10-K Item 1)
## Financial Profile         (YoY summary, balance sheet, key ratios, QoE notes)
## Management Commentary     (last 2 earnings calls, guidance)
## Market Opportunity        (company TAM vs. independent TAM, implied share)
## Competitive Landscape     (per-competitor table with entity-bound metrics)
## Valuation Context         (peer comp table, forward multiples, history)
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
examples/             # committed sample outputs
briefs/               # research output (gitignored)
reports/              # initiation output (gitignored)
```

## License

MIT
