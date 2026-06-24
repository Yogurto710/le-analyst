# Le Analyst

A command-line research assistant for public equities, with a WeChat mini-app companion product for Chinese retail investors. Two main commands:

- `analyst research TICKER "QUESTION" [--lang en|zh]` — produces a sourced, dated brief on a specific question. ~$0.20–0.40 per run, ~2–3 min.
- `analyst initiate TICKER [--lang en|zh]` — produces a thesis-shaped 8-section initiation brief with peer comps, valuation, risks, and a Bull/Bear/Base framework. ~$0.60–0.80 per run, ~8–12 min.

Output goes to `briefs_en/` or `briefs_ch/` (research, language-routed) and `reports/` (initiation). The model writes natively in the chosen language on the first pass — there is no separate translation step. Sources stay English regardless (US-listed names, English filings) and the Chinese-mode prompt preserves numbers, `$`, URLs, and tickers byte-for-byte.

## Requirements

- Python 3.10+
- A [Moonshot AI](https://platform.moonshot.cn/) API key for Kimi K2.6 (default) or a [DeepSeek](https://platform.deepseek.com/) API key (`--model deepseek` for V4 Pro)
- A [Tavily](https://tavily.com/) API key for web search

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate    # Windows PowerShell
# source .venv/bin/activate    # macOS/Linux
pip install -e .
copy .env.example .env
# edit .env to add your API keys
```

## Usage

```powershell
# A focused brief on a single question — defaults to English
analyst research RBLX "Is user growth sustainable?"

# Same question, Chinese-native output (saves to briefs_ch/)
analyst research RBLX "近期股价波动的原因?" --lang zh

# A full initiation report (slower, more expensive)
analyst initiate RBLX

# Initiation in Chinese
analyst initiate RBLX --lang zh

# Show each tool call as it happens
analyst initiate RBLX --verbose

# Opt in to DeepSeek V4 Pro instead of Kimi
analyst initiate RBLX --model deepseek
```

Output files use the convention `TICKER-slug-YYYYMMDD.md` (research) and `TICKER-initiation-YYYYMMDD.md` (initiation). YAML frontmatter records `ticker`, `date`, `lang`, `model`, and (for initiations) an `open_questions:` list of gaps the analyst should verify manually.

Real sample initiations live in [`reports/`](reports/) and recent Chinese research briefs in [`briefs_ch/`](briefs_ch/) — both are tracked and viewable on GitHub. The pre-rebuild 11-section sell-side format is archived in the gitignored `reports_legacy/`.

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

The new thesis-shaped layout (since the 2026-06-15 rebuild). Lead with the thesis; the analytical heavy lifting lives in the Appendix at the bottom.

```
# TICKER: [Company] — Investment Brief

## Thesis                    (1 paragraph + next datapoint to watch)
## Business snapshot         (what the company does + a small trading-snapshot table)
## What the Street thinks    (consensus FY+1/FY+2 + three real debates: bull / bear / what resolves it)
## Bull / Bear / Base        (each: return math, conditions, confirming trigger, invalidating trigger)
## Top 3 risks               (ordered by severity, each with a [citation])
## Catalyst calendar         (dated, numerically-anchored thresholds where possible)
## Appendix
  ### Financial Summary       (Prior FY / LFY / LTM trailing + Forward Estimates [consensus FY+1/FY+2])
  ### Competitive Landscape   (Stated Position + Independent Evidence)
  ### Peer Comp Table         (10 cols: Company | EV | Rev (LTM) | 2-yr Rev CAGR | EV/Rev x3 | P/E x3, with peer Median row)
## Open Questions            (gaps for the analyst to verify manually)
## Sources                   (numbered, every entry has a URL)
```

Chinese-language initiations follow the same structure with locked translations — the title becomes `投资简报`, section headers translate to fixed Chinese strings (`投资论点 / 业务概览 / 市场观点 / 看多看空基准 / 三大风险 / 催化剂日历 / 附录 / 未解决问题 / 数据来源`) so downstream validators work bilingually. The legacy 11-section sell-side format is preserved as `analyst initiate-legacy` for fallback.

## Design notes

A few choices that shaped how the tool works:

**Single-file Python.** `analyst.py` holds the whole thing — Typer commands, prompt templates, tool implementations, and the agent loop. Refactor when something hurts.

**Kimi K2.6 over Claude/GPT.** The OpenAI-compatible endpoint at `api.moonshot.cn` plus aggressive prompt-cache pricing (~96% cache hit rate on a typical run, cached input at ~17% of uncached) makes long agentic loops cheap. A full initiation report runs ~$0.75 USD.

**Native-language generation, not translation.** `--lang en` (default) and `--lang zh` switch the system prompt up-front; Kimi writes the brief natively in the chosen language on the first pass. There is no separate translation step. This replaced a prior translate flow after empirical observation that Kimi's first-pass Chinese reads better than a translation of its own English. Sources stay English regardless (the model reads English 10-Ks / transcripts and cites them with English titles and original URLs); the Chinese-mode prompt preserves numbers, `$` (never converted to ¥), tickers, source URLs byte-for-byte, and table structure, and re-states the no-rating / no-price-target discipline. Saved files record `lang:` in YAML frontmatter and route by language: `briefs_en/` vs. `briefs_ch/`.

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

## WeChat mini-app

A closed-beta mini-app companion lives under [`miniapp_mvp/`](miniapp_mvp/) — a FastAPI backend that wraps the CLI as a subprocess plus a 3-page WeChat mini-app (submit / status / report) that targets Chinese retail investors on mobile. The submit page exposes an explicit 投资简报 / 问题研究 toggle and a 中文 / English language toggle. The status page renders a Lottie robot mascot plus a live phase-aware message (`正在检索资料 → 正在计算估值倍数 → 正在撰写报告 → 正在审核数据一致性`) parsed from analyst.py's stderr stream. See [`miniapp_mvp/README.md`](miniapp_mvp/README.md) for setup.

## Project layout

```
analyst.py            # CLI tool — research / initiate / all validators
legacy_prompts.py     # archived 11-section initiate prompt (initiate-legacy command)
pyproject.toml        # Python deps + console script entry point
.env.example          # template for MOONSHOT_API_KEY / TAVILY_API_KEY
reports/              # initiation output (tracked, public)
reports_legacy/       # pre-rebuild initiation archive (gitignored)
briefs_en/            # English research briefs (gitignored)
briefs_ch/            # Chinese research briefs (tracked, public)
miniapp_mvp/          # WeChat mini-app — FastAPI backend + WXML pages
```

## License

MIT
