import os
import re
import sys
import json
import time
import httpx
import typer
import datetime
import subprocess
import collections
import trafilatura
from pathlib import Path
from openai import OpenAI
from tavily import TavilyClient

# Windows consoles default to a legacy locale codec (cp1252, or GBK on Chinese
# systems) which crashes on non-ASCII model output like ®, —, or smart quotes,
# especially when stdout is piped (non-tty falls back to the locale encoding).
# Force UTF-8 so streaming never aborts the run before the report is saved.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

app = typer.Typer(add_completion=False, no_args_is_help=True)

BRIEFS_DIR_EN = Path("briefs_en")  # English-language briefs
BRIEFS_DIR_CH = Path("briefs_ch")  # Chinese-language briefs
REPORTS_DIR = Path("reports")      # Initiation reports (one .md per run)


def _briefs_dir_for(lang: str) -> Path:
    """Route by the explicit output language flag."""
    return BRIEFS_DIR_CH if lang == "zh" else BRIEFS_DIR_EN

# Model registry. Default is kimi (Chinese-native, cheap with prompt caching
# at ~96% hit rate). DeepSeek V4 Pro is an opt-in alternative — faster wall
# time per token and lower headline price, but a different cache strategy
# (DeepSeek does automatic context caching server-side, so the savings shape
# differs from Kimi's explicit cache_read_tokens). Switch via `--model
# deepseek` on the CLI or LE_ANALYST_MODEL=deepseek in env.
MODELS = {
    "kimi": {
        "id": "kimi-k2.6",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        # Disabling thinking is required: enabling it breaks the
        # OpenAI-compatible tool-calling round-trip on Moonshot.
        "extra_body": {"thinking": {"type": "disabled"}},
    },
    "deepseek": {
        "id": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        # V4 Pro is a reasoning model by default — it streams reasoning_content
        # alongside content and tool_calls, then expects the reasoning_content
        # to be passed back in subsequent assistant messages of the same
        # tool-call round. Our agentic_loop doesn't surface it (and matching
        # Kimi's posture, we don't want it). Happy coincidence: DeepSeek's
        # API accepts the same `thinking: {"type": "disabled"}` shape Kimi uses.
        "extra_body": {"thinking": {"type": "disabled"}},
    },
}
DEFAULT_MODEL = "kimi"
RESEARCH_MAX_TOOL_CALLS = 30
# New thesis-shaped initiate (8 sections, ~1,300 words core). Tool budget
# halved vs legacy because TAM-search, historical-multiple-search, and
# the long-form Sell-Side Q&A scaffold no longer drive Phase 1 fetches.
INITIATE_MAX_TOOL_CALLS = 50
# Legacy 11-section sell-side initiation kept as `initiate_legacy` for
# fallback / A/B comparison. Same tool budget as the original.
INITIATE_LEGACY_MAX_TOOL_CALLS = 95
PHASE2_REPL_CAP = 3  # python_repl calls per initiate run; enforced in agentic_loop so the model can't iterate past it
FETCH_CHAR_LIMIT = 10000  # default for news/Yahoo/blogs (mostly nav cruft after trafilatura strip)
TRANSCRIPT_FETCH_CHAR_LIMIT = 50000  # earnings calls — Q&A often holds the highest-impact content
EDGAR_FETCH_CHAR_LIMIT = 40000  # 10-K sections can be very long; allow more
SEARCH_DAYS = 90  # restrict web_search results to the last ~3 months; model is also told today's date so it can flag stale sources
SEC_USER_AGENT = "Le Analyst research@le-analyst.local"
PYTHON_REPL_TIMEOUT = 10
PYTHON_REPL_OUTPUT_LIMIT = 4000

RESEARCH_SYSTEM_PROMPT_TEMPLATE = """You are a senior equity research analyst. You produce concise, sourced research briefs on public companies.

Today's date is {today}.

Given a ticker and a research question, your process is:

1. Decompose the question into 3-5 specific, answerable sub-questions that together address the main question.
2. For each sub-question, **YOU MUST call web_search** to identify the best available sources, then **YOU MUST call fetch_url** to read the most promising results in depth. Do not write any sub-question's findings until you have ACTUALLY FETCHED the relevant sources. Do not exhaust your search budget before fetching. Prioritize depth over breadth: 2-3 well-read sources per sub-question beat 10 skimmed snippets.

HARD REQUIREMENT — automated check before saving: a research brief produced with ZERO tool calls will be REJECTED by the system and NOT saved. This catches drafts written from training-time knowledge without grounding in current sources. If the question references a specific date, a recent event, or anything within the past 12 months, you MUST call web_search at least 3 times before drafting any findings. Drafting time-sensitive content from training data alone produces hallucinated quotes, numbers, and URLs that look authoritative but are fabricated — the no-tool-calls check exists precisely to prevent this. The brief is worthless if its citations point to URLs you did not actually fetch.
3. Synthesize findings into a research brief in Markdown.

Output format:

# [Ticker]: [Question]

## Summary
2-3 sentences with your bottom-line view.

## Sub-questions
For each sub-question:
### [Sub-question]
Findings with inline citations like [1], [2]. Be specific with numbers, dates, and direct evidence. Call out uncertainty.

## Sources
Numbered list: [1] Title — Publication — Date — URL

Rules:
- Every non-obvious claim must have a citation.
- If evidence is thin or contradictory, say so — do not fabricate.
- Prefer sources published within the last 3 months. Check publication dates — search tools sometimes surface old articles with recent crawl dates. If a source is older than 6 months, flag it explicitly with its date and note that more recent data may exist.
- Preferred sources: SEC filings (10-K, 10-Q, 8-K), earnings call transcripts, investor presentations, Bloomberg, Reuters, WSJ, FT, Barron's, and company investor relations pages.
- Avoid: Wikipedia, Reddit, anonymous blogs, AI-generated content farms, and aggregator sites that repackage others' reporting without adding analysis. If no better source exists, note the limitation.
- Be concise. No filler.
- Do not include internal reasoning or process commentary in the output (e.g. "Now I have enough information...", "Let me search for...", "Based on my research..."). The brief is the only output — start directly with the # heading.
"""

INITIATE_LEGACY_SYSTEM_PROMPT_TEMPLATE = """You are a senior equity research analyst producing a deep-dive initiation report on a public company.

Today's date is {today}.

Your process has three STRICTLY SEQUENTIAL phases. You must complete each phase fully before starting the next. Do not interleave phases. Do not return to a previous phase.

==============================================================
PHASE 1 — Gather all primary and peer data (use only edgar_search, edgar_fetch, web_search, fetch_url)
==============================================================

STARTING CONTEXT (already in your conversation): a pre-fetch round runs BEFORE this phase. It has parallel-fetched the deterministic subject-company foundations: edgar_search results for forms 10-K, 20-F, 10-Q, and 6-K (covering both US-domestic and foreign-private-issuer schemas), AND Yahoo Finance's quote / key-statistics / financials / analysis pages for the subject. These appear in your context as if you had called them. Do NOT re-call edgar_search for those four form types, and do NOT re-fetch those Yahoo pages for the subject company — the data is already in hand. Proceed directly to: (a) edgar_fetch on the relevant annual + quarterly filing URLs you can read out of the pre-fetched edgar_search results, and (b) the rest of Phase 1 below (transcripts, peer data, TAM, catalysts, consensus, fiscal calendar checks).

1. Use edgar_search to locate the most recent 10-K and 10-Q filings for the ticker. Then use edgar_fetch to read Item 1 (Business), Item 1A (Risk Factors), and Item 7 (MD&A) from the 10-K, and the MD&A section of the latest 10-Q.

   FOREIGN PRIVATE ISSUERS (China ADRs like NTES / BABA / JD, EU companies, etc.): the equivalent SEC filings are Form 20-F (annual) and Form 6-K (interim, irregular timing). The 20-F item numbering differs from 10-K — read Item 4 (Information on the Company, ≈ 10-K Item 1), Item 3.D (Risk Factors, ≈ 10-K Item 1A), and Item 5 (Operating and Financial Review, ≈ 10-K Item 7 / MD&A). If edgar_search returns no 10-K for a US-listed foreign ticker, search for 20-F instead. Quarterly results for foreign issuers come via 6-K (or via press release captured separately) — they do not file 10-Qs.
2. Use web_search and fetch_url to find and read the last two earnings call transcripts and the latest investor presentation. For the MOST RECENT transcript specifically, pay close attention to the Q&A section — capture each analyst's firm name and the substance of their question. These power the Sell-Side Q&A Analysis section in Phase 3. Skip generic ("any color on the quarter") questions; identify the 3-5 most substantive debates analysts pushed on.
3. Pull these from Yahoo Finance (https://finance.yahoo.com/quote/TICKER/key-statistics/) for the trading snapshot at the top of the report: current price, market cap, enterprise value, shares outstanding, 52-week range, average daily volume (3-month), and short interest as a percent of float.
4. Based on the business model you just learned, select 4-6 peers — a mix of direct competitors and business-model comparables. Note the rationale.
5. For each peer, gather ALL financial data from a UNIFIED source — non-negotiable for comp-table peers, because apples-to-apples comparison requires apples-to-apples sources. The chosen source depends on peer LISTING VENUE:

   **US-LISTED PEERS (default for most reports) → Yahoo Finance.** Yahoo presents every US-listed ticker in the same schema. For each US-listed peer:
   - Market cap, EV, share price → Yahoo's quote / key-statistics page (https://finance.yahoo.com/quote/{{ticker}}/key-statistics/).
   - LFY and LTM revenue, gross profit, EBITDA, net income, FCF, diluted EPS → Yahoo's financials page (https://finance.yahoo.com/quote/{{ticker}}/financials/), reading the most recent annual column for LFY and the TTM column for LTM.
   - SBC and SBC-adjusted FCF → derive from the cash-flow statement on the same Yahoo page.
   - FY+1E and FY+2E consensus revenue and EPS → Yahoo's analysis page (https://finance.yahoo.com/quote/{{ticker}}/analysis/) — same page used for the subject company's consensus in step 10.

   **HK-LISTED PEERS (e.g., Tencent 0700.HK, Kuaishou 1024.HK for a China-ADR report) → stockanalysis.com.** Yahoo's HK pages only cover the snapshot — `/financials/` and `/analysis/` return 404 for HK tickers. stockanalysis.com fills the gap with clean tabular data:
   - Quote page → https://stockanalysis.com/quote/hkg/{{code}}/ — market cap, revenue TTM, EPS TTM, PE, **Forward PE**, shares out, target price.
   - Financials page → https://stockanalysis.com/quote/hkg/{{code}}/financials/ — full income statement: TTM / FY-1 / FY-2 / FY-3 (revenue, gross profit, growth %).
   - Forecast page → https://stockanalysis.com/quote/hkg/{{code}}/forecast/ — analyst price target + recommendation breakdown. NOTE: only FY+1 forward EPS is reliably extractable (derive as `current price ÷ Forward PE`). FY+2 EPS for HK peers often isn't available — mark "NM" in the FY+2E P/E column for those peers; the median rule handles this cleanly.

   **MIXED BASKET (some US-listed + some HK-listed peers, e.g., NTES with Tencent + EA + Take-Two):** Yahoo for the US-listed members, stockanalysis.com for the HK-listed members. Footnote the source split below the comp table — e.g., "US peers from Yahoo Finance; HK peers from stockanalysis.com." This is a LABELLED, intentional split — NOT silent source mixing.

   Run a DEDICATED fetch per peer; do NOT rely on listicles or aggregator articles. If a particular figure is missing for a peer (private competitor, niche foreign listing not on either source), note it explicitly and substitute the official filing only as a LABELLED exception — never mix sources silently. Mark "NM" in any comp-table cell where data genuinely isn't available.
6. For each named competitor, run a separate search for user metrics: "{{competitor name}} monthly active users {{year}}" or "{{competitor name}} DAU {{year}}". Each metric must come from a search whose target was that specific competitor — never extract a competitor's number from an article about the subject company. ENTITY BINDING RULE: a number only belongs to a competitor if the sentence it appears in explicitly names that competitor as the subject. If the sentence is ambiguous about which entity it describes, discard the number.

   Source hierarchy for competitor data, prefer in this order: (1) the competitor's own SEC filings or earnings releases, (2) the competitor's official press releases or IR page, (3) reputable third-party estimates (Newzoo, Sensor Tower, data.ai) — labelled as "estimated", (4) news articles citing the above with the original source named. Never use numbers from listicles, blog posts, or aggregator "Top 10" articles without tracing to the original source. For private companies (Epic Games, Valve, etc.) every financial number is an estimate — always label it that way (e.g. "$6B estimated 2025 revenue (Sacra)").
7. Find at least one independent third-party TAM estimate for the industry, in addition to whatever the company cites.
8. Find the company's historical trading multiples — peak, trough, and current. If exact multiples aren't available, get stock prices at key dates (IPO, peak, recent trough) so you can approximate.
9. Find upcoming catalysts over the next 6-12 months: next earnings date, investor day or conferences, product launches, regulatory deadlines, debt maturities, lockup expirations. Search "{{company name}} next earnings date 2026", "{{company name}} investor day 2026", and check the 10-K for any disclosed forward dates. (Wall Street consensus estimates are gathered in step 10 below and used to anchor catalyst thresholds.)
10. WALL STREET CONSENSUS — the PRIMARY source for all forward estimates. Do NOT derive forward figures by annualizing a single quarter. Search "{{ticker}} consensus estimates {{next fiscal year}}" or "{{ticker}} analyst estimates"; the preferred free source is Yahoo Finance (https://finance.yahoo.com/quote/{{ticker}}/analysis), which aggregates consensus revenue and EPS. If it's unavailable, fall back to Koyfin, WSJ Markets, or MarketBeat. Pull: current-fiscal-year and next-fiscal-year consensus REVENUE, current-FY and next-FY consensus EPS, and the NUMBER OF ANALYSTS covering (a quality signal — >10 = reliable, <5 = thin coverage; record it). For EVERY peer in the comp table, gather consensus FY+1 and FY+2 revenue AND EPS — these power the forward EV/Revenue and forward P/E columns plus the 2-year revenue CAGR in the peer comp table. Yahoo Finance's analyst page typically has all four data points per ticker in one fetch. If a peer's consensus isn't available, mark it and proceed — don't burn 3+ searches chasing one peer's forwards. Record the source, analyst count, and date for every consensus figure (e.g. "Yahoo Finance, 24 analysts, as of {today}"). If no consensus is findable, note that explicitly and fall back to guidance-derived forward estimates — but you MUST label them "derived from company guidance, not analyst consensus".
11. FISCAL CALENDAR + LTM SOURCING. Record the fiscal year-end month for the subject company AND every peer. Companies that don't share the same fiscal year-end cannot be compared on "latest fiscal year" alone — their reported years cover different calendar periods.

LFY DEFINITION (HARD RULE): LFY = the MOST RECENT COMPLETED fiscal year, period. Do NOT substitute an earlier "last comparable year" because of M&A, divestitures, discontinued operations, restatements, OR DELAYED ANNUAL FILINGS. If the most recent completed fiscal year was affected by a divestiture (e.g., APP's Apps business sold mid-2025 means FY2025 is reported on a continuing-ops basis), use the continuing-ops FY2025 figure as LFY — do not regress to FY2024. The trajectory across the trailing columns (prior FY → LFY → LTM) is itself how the report communicates a basis change; don't hide it by skipping a year.

LATE ANNUAL FILINGS (especially foreign private issuers): foreign issuers often announce full-year results via press release / 6-K / earnings call MONTHS before the 20-F filing drops. The announced fiscal year is STILL LFY — anchor on "most recent completed fiscal year" not "most recent annual SEC filing." Example: as of May 2026, NetEase's FY2025 results were public via the February-2026 press release and Q4'25 earnings call, but the FY2025 20-F won't be filed until April-May. LFY is FY2025 — pull the numbers from the press release / 6-K / earnings call, NOT the still-most-recent FY2024 20-F. The NTES test run made this exact mistake (labelled FY2024 as LFY) — do not repeat it.

For the SUBJECT company, gather:
(a) Latest completed fiscal year (LFY) figures from filings: revenue, gross profit, operating income, net income, diluted EPS, EBITDA (or adj. EBITDA where reported), free cash flow.
(b) LTM (trailing-twelve-month / TTM) figures for the same metrics — pull DIRECTLY from Yahoo Finance's financials page at https://finance.yahoo.com/quote/{{ticker}}/financials/, which presents a TTM column ready-made. This is more reliable than summing 4 quarters of disclosed line items, especially for metrics that aren't always quarterly disclosed (EBITDA, FCF).
(c) Quarterly REVENUE for two purposes: the completed quarters of the CURRENT fiscal year, AND the matching quarters of the PRIOR fiscal year. These power the Phase 2 LTM-revenue cross-check (LTM = LFY + current-FY stub quarters - matching prior-FY stub quarters), which catches off-by-one-quarter errors when Yahoo's TTM data is mislabelled.

(Peer historical and forward financials are gathered from Yahoo Finance per step 5 above, so the comp table is built on a unified source across all peers. Step 11 here only records peer fiscal year-ends to detect calendar mismatches that would distort LFY-on-LFY comparisons.)

Note where the subject's LFY and LTM revenue materially differ — this drives the Quality-of-Earnings flag in Phase 3.
12. CYCLICAL-INDUSTRY DATA — gather this ONLY if the subject operates in an industry with well-documented boom-bust cycles (memory/commodity semiconductors, commodity chemicals, shipping, energy, mining, and the like). Skip this item entirely for companies where cyclicality is not the primary analytical lens (e.g. Netflix, Roblox). When it applies, gather: (a) historical cycle duration for this industry (how many quarters past upcycles and downcycles have lasted); (b) 3-4 leading indicators of a cycle turn for this specific industry (e.g. inventory days, spot-vs-contract price spread, fab/plant utilization, book-to-bill, freight day-rates, rig counts) — for each, the current reading AND the historical level that has signalled prior turns; and (c) the subject company's CapEx and depreciation & amortization for the latest fiscal year (plus a couple of prior years if readily available) to support a supply-side CapEx/Depreciation ratio.

PHASE 1 BUDGET DISCIPLINE: Aim to finish gathering in under 65 tool calls. Hard cap is 95.

HARD STOP RULE — 2 attempts per data point: after TWO unsuccessful web_search + fetch_url tries for any specific number, STOP and move on. Mark the data point as "not disclosed" or "not publicly available" and add it to Open Questions. Do NOT try a third query with different phrasing, a different source, or a different language. The NTES test run hit the 95-call cap largely from burning 3-5 searches each on hard-to-find Chinese-source numbers — every redundant search cost ~20 seconds of wall time including model thinking.

What counts as an "attempt": any web_search or fetch_url targeting the SAME specific number (e.g., "Tencent gaming revenue 2025" and "Tencent online games revenue FY25" are two attempts at the same data point). Different data points are independent budgets — gathering "Tencent revenue" and "Tencent EPS" do not share an attempt count.

What does NOT count as an attempt: confirming a number you already have against an authoritative second source (verification, not search). Fetching the next required document in your gathering plan (executing the plan, not searching).

Triage by criticality:
- PRIMARY data (must find — escalate to a third attempt only if missing would block a section): subject company's headline revenue / EPS / margins, current price / market cap, primary 10-K or 20-F filing.
- SECONDARY data (acceptable to miss after 2 attempts): peer detail beyond Yahoo / stockanalysis baseline, granular segment splits, historical trough multiples, specific transcript quotes. These go to Open Questions if not found.

When a peer is unreachable from the chosen source after 2 attempts, mark its row "NM" across the affected columns and move on — better to ship a comp table with one peer thin than burn 10 searches getting that peer to parity. The median-row HARD RULE (<3 valid non-NM values → "—") handles thinness gracefully.

==============================================================
PHASE 2 — Compute derived metrics (use ONLY python_repl)
==============================================================

Once Phase 1 is complete, use python_repl AT MOST 3 TIMES TOTAL to compute every derived metric below. Bundle all related computations into one block. Print every value cleanly so you can quote it in the report. If an input is missing from Phase 1, print "NM" or "N/A" — do not return to Phase 1 to fetch it.

For the subject company:
- **First python_repl call must declare the LFY year stamp.** Print `LFY = FYxxxx` based on today's date and the company's fiscal year-end. For a calendar-FY company in June 2026, that's `LFY = FY2025`. This is a discipline anchor for the Phase 3 Financial Summary table — the LFY column MUST match this year, even if the audited 20-F / 10-K for that year hasn't dropped yet (use press-release / 6-K numbers when needed). Foreign-private-issuer 20-F delays do NOT change the LFY year.
- Revenue YoY growth
- Bookings YoY growth (if applicable)
- DAU YoY growth (if applicable)
- FCF margin (FCF / Revenue)
- FCF yield (FCF / Market Cap)
- SBC as % of Revenue
- SBC-adjusted FCF = FCF - SBC; SBC-adjusted FCF yield = (FCF - SBC) / Market Cap
- Annual Revenue per DAU and Bookings per DAU (if applicable)
- Net cash position = total cash & investments - total debt
- EV/Revenue, EV/EBITDA, P/FCF, EV/Bookings (if applicable)
- Diluted EPS: latest fiscal year (LFY) and LTM (sum the trailing 4 quarters). If the company also reports adjusted / non-GAAP EPS, compute both and record what the adjusted figure excludes (typically SBC, intangible amortization, restructuring).
- EPS YoY growth (LFY vs prior fiscal year)
- Forward EPS = Wall Street consensus EPS from Phase 1, for BOTH forward years (FY+1 and FY+2, e.g. FY2026 and FY2027). If no consensus was found, print "NM" and note it is unavailable — do not invent one.
- P/E (LTM) = price / LTM diluted EPS; P/E (Forward) = price / forward consensus EPS, computed for BOTH FY+1 and FY+2. If the relevant EPS is negative or zero, the P/E is "NM".
- Forward EV/Revenue, forward EV/EBITDA, and forward P/E for BOTH forward years (FY+1 and FY+2) using Wall Street consensus from Phase 1 (consensus revenue and EPS) as the PRIMARY source. Use a guidance midpoint only if no consensus exists, and label it guidance-derived.
- Implied current market share against each TAM estimate

For each peer (and the subject, for the comp-table row):
- 2-year Revenue CAGR = (FY+2E revenue / LFY revenue) ** (1/2) - 1. Use LFY actual (from filings) and FY+2E consensus (from Phase 1) as the two endpoints — clean year-to-year growth that isolates the forward trajectory and avoids LTM-quarter noise.
- EV/Revenue (LTM), EV/Revenue (FY+1E), EV/Revenue (FY+2E) — divide EV by LTM revenue, FY+1E consensus revenue, and FY+2E consensus revenue respectively.
- P/E (LTM), P/E (FY+1E), P/E (FY+2E) — divide price by LTM diluted EPS, FY+1E consensus EPS, and FY+2E consensus EPS respectively. "NM" for any P/E where the relevant EPS is negative or zero.
- If consensus is unavailable for a peer's FY+1E or FY+2E figure, mark that specific cell "NM" — do not invent a number to make the column complete, and do not extrapolate one peer's growth onto another.
- MEDIAN row (HARD RULE — the model has previously violated this): compute the median of each multiple/CAGR column across the PEERS ONLY (exclude the subject). NEVER compute a median from fewer than 3 valid non-"NM" values. The median of two values is NOT a median — it's an average. When fewer than 3 valid values exist for a column, you MUST write "—" in that column's median cell AND add the footnote "insufficient peer coverage" below the table naming which columns were affected. On the APP test run the model averaged 2 valid P/E values across 4 peers and printed it as the median — do not do this.

FISCAL BASIS RECONCILIATION (required — the comp table and snapshot depend on this):
- Choose ONE revenue basis that can be applied consistently to EVERY company in the comp table. Use LTM/TTM when fiscal years are misaligned (the usual case); LFY is acceptable only when all companies share the same fiscal year-end. Compute each company's comp-table multiples (EV/Revenue, EV/EBITDA, P/FCF) from THAT single basis. Record which basis you chose — you must label it in the report.
- The numerator period, the denominator period, and the printed multiple must all be the SAME period. NEVER pair a TTM/LTM multiple with a fiscal-year revenue figure, or vice versa.
- For the subject company, compute LTM revenue and LFY revenue separately and the percentage difference between them. If they differ by more than 15%, set a flag — the report must show both and explain the divergence.
- If the company has cyclical-industry data, compute the CapEx/Depreciation ratio (latest fiscal year CapEx / depreciation & amortization) as a supply-side indicator, plus the same ratio for any prior years gathered so the trend is visible. A ratio above ~1.5x historically signals capacity buildout that precedes oversupply.
- VERIFY THE MATH before writing. For every row that will appear in the comp table, recompute EV / (revenue on the chosen basis) and assert it equals the EV/Revenue multiple you will print (within rounding). Print, per row: company, EV, revenue (basis-tagged), EV/revenue division result, and the multiple you will publish — side by side. If any row fails to reconcile, fix the inputs and recompute. Do not publish a multiple that doesn't reconcile to the EV and revenue shown in its own row.
- SANITY-CHECK every computed figure before writing — forward and consensus-derived figures included. Show each check's labeled inputs in your python_repl output; NEVER present a forward estimate as a bare number without the arithmetic that produced it. Apply ALL of the following and recompute anything that fails:
  (a) EBITDA-margin check: for any EBITDA estimate, verify EBITDA / Revenue <= Gross Margin. EBITDA can never exceed gross profit (or revenue). If it does, the inputs are wrong — recompute as (Revenue x Gross Margin) - OpEx + D&A and show the inputs. (This is exactly the failure that produced a ~$105B EBITDA on ~$95B revenue once — a 110% margin is impossible.)
  (b) Margin-ordering check: no forward margin may exceed the line above it in the P&L. Enforce Gross margin >= EBITDA margin >= Operating margin >= Net margin. If any estimate breaks this ordering, flag it and recompute.
  (c) Annualization check: if you ever derive a forward figure by annualizing a single quarter, compare it against the Phase 1 consensus. If they diverge by more than 20%, flag the divergence and PREFER the consensus figure, with a one-line note explaining the difference.
  (d) Multiple reconciliation: forward EV/Revenue must equal current EV / forward revenue, forward EV/EBITDA must equal current EV / forward EBITDA, and forward P/E must equal price / forward EPS. Do not print a multiple that does not reconcile to the figures shown alongside it.
  (e) LTM revenue cross-check: verify the Yahoo Finance LTM revenue against the filing-derived equivalent — LFY revenue + sum of current-FY completed quarter revenues - sum of matching prior-FY quarter revenues (e.g. for a calendar-year company reporting through Q1: LTM = FY-1 + Q1-current - Q1-prior).

      BASIS CONSISTENCY (critical when a divestiture or spin-off occurred in the trailing twelve months): ALL periods in the cross-check MUST be on the SAME continuing-operations basis. For the prior-year stub quarter, ALWAYS pull the restated comparable period from the MOST RECENT 10-Q/10-K — NOT the original quarter from the older filing. Example: APP sold its Apps business mid-2025; the Q1 2025 figure used in the cross-check must come from the Q1 2026 10-Q (which restates Q1 2025 on a continuing-ops basis to ~$1.16B), NOT the original Q1 2025 10-Q (which still included Apps revenue at ~$1.48B). Mixing as-reported with restated bases produces a PHANTOM discrepancy and falsely flags Yahoo's TTM as wrong — this happened on the first APP test run and is the canonical failure mode of this check.

      Tolerance ±2%. If the discrepancy still exceeds 2% AFTER confirming basis consistency, PREFER the filing-derived figure and add a one-line footnote in the Financial Summary explaining the source. This catches the off-by-one-quarter class, where Yahoo's TTM column has accidentally been mislabelled or aggregated incorrectly.

PHASE 2 STOP RULE: Once you have called python_repl, you are NOT permitted to call edgar_search, edgar_fetch, web_search, or fetch_url again. If a metric came out wrong because of bad input, fix the input in your next python_repl call — do not search the web. After at most 3 python_repl calls, proceed directly to Phase 3.

==============================================================
PHASE 3 — Write the report (no tool calls)
==============================================================

Synthesize everything into the report following the format below. Do not call any tool during Phase 3. Start writing immediately with the # heading.

Output format:

# [Ticker]: Initiation Report

## Trading Snapshot
A single-row markdown table with columns: Price | Mkt Cap | 52W Range | Avg Daily Volume | Short Interest | EV/Rev (trailing) | EV/Rev (Fwd) | EPS (LTM, dil.) | EPS (Fwd, cons.) | P/E (LTM) | P/E (Fwd). Use the Yahoo Finance data from Phase 1 and the figures computed in Phase 2. The trailing EV/Rev column header MUST name the revenue basis it uses — write "EV/Rev (LTM)" or "EV/Rev (FY2025)", not a bare "EV/Rev" — and use the SAME basis as the Peer Comp Table; if they must differ, label each and add a one-line note. Forward EPS and P/E (Fwd) use Wall Street consensus from Phase 1, not guidance. Format: price and EPS as $XX.XX, market cap as $X.XB, 52W range as $low – $high, avg daily volume as X.XM, short interest as X.X%, multiples as X.Xx. Show "NM" for any P/E whose EPS is negative or zero. Show "—" where a forward figure has no consensus (and no guidance). Below the table add a single italicized line: *Data as of [today's date]; forward figures are Wall Street consensus ([N] analysts, [source], [date]).*

## Business Overview
What the company does, revenue model, segment breakdown, key operating metrics management tracks, and the company's CURRENT strategic priorities (1-2 sentences distilled from the most recent investor day, earnings letter, or shareholder letter — what management says it's investing in and executing against right now). Cite the 10-K Item 1 plus the relevant earnings call or letter for the strategy framing. Numbers and forward guidance do not belong here — they live in Financial Profile / Forward Estimates. **Cap: ~250 words.** This section is contextual orientation, not the analytical core.

## Financial Profile
Brief prose intro that frames the company's TRAJECTORY — not just the last fiscal year, but where it sits now (LTM) and where the Street expects it to go. Lead with the takeaways that matter most. Include a one-line balance-sheet callout in this intro paragraph: "Net cash of $X.XB on $X.XB cash and equivalents and $X.XB total debt as of [most recent period end]" (or "Net debt of $X.XB" when negative). Do not give the balance sheet its own table — that single sentence is the whole treatment.

Numbers in the Financial Summary come from filings (prior FY, LFY) and Yahoo Finance's TTM column (LTM); Forward Estimates come from Wall Street consensus and management guidance, both gathered in Phase 1.

### Financial Summary
A trailing-only markdown table — clean, no blanks. Columns, left to right: prior fiscal year | latest fiscal year (LFY) | LTM.

**LFY HARD CHECK** (the model has repeatedly violated this — read carefully): LFY = the most recent COMPLETED fiscal year, per the Phase 1 step 11 rule. For a calendar-FY company, if today is on or after February of year N+1, **LFY = FY-N**. Example: today is June 2026, calendar FY-end company → LFY = **FY2025**. This is true **regardless of whether the FY2025 annual filing (10-K / 20-F) has dropped**. If FY2025 numbers are only available via press release / 6-K / earnings call, USE THOSE for the LFY column and footnote the source (e.g. "FY2025 from Q4'25 press release; 20-F not yet filed"). The prior-FY column is then FY2024. Showing `FY2023 | FY2024 | LTM` and skipping FY2025 is **WRONG** — even if FY2024 is the latest year you have a full audited filing for. The NTES test runs have made this exact mistake twice; do not repeat it.

Required rows: Revenue, Gross Profit (or Gross Margin %), Operating Income/Loss, Net Income/Loss, Adjusted EBITDA (if reported), Free Cash Flow, Diluted EPS. Add a YoY change column if helpful, but the LFY-vs-LTM column already conveys trajectory. Historical columns (prior FY, LFY) come from filings, press releases, or 6-K interim reports — whichever has the most recent numbers for that fiscal year; the LTM column comes from Yahoo Finance's TTM, verified against the Phase 2 LTM revenue cross-check (if the cross-check failed, use the filing-derived LTM and footnote the source). If the company reports segments, include a Revenue by Segment block (one row per segment) within or immediately below this table. Format dollar values consistently (e.g. all $M or all $B).

### Forward Estimates
This section has TWO parts — consensus, then management guidance — and ends with a one-line gap commentary. This is the dedicated home for forward operating expectations; do not duplicate these figures in the Financial Summary or in Forward Context (which uses them downstream to compute multiples).

**Wall Street consensus** — markdown table with two columns: FY+1E and FY+2E. Rows where consensus exists: Revenue, Revenue Growth %, Gross Margin % (if covered), Adjusted EBITDA (if covered), EBITDA Margin % (if covered), Operating Margin % (if covered), Net Income (if covered), Diluted EPS. At minimum Revenue and Diluted EPS will always be present; the other rows depend on how richly the company is covered. Use "—" only where consensus is genuinely unavailable. Cite the source (Yahoo Finance, Visible Alpha, Koyfin, etc.), analyst count, and date in a single italicized line below the table.

**Management guidance** — short bulleted list of every forward guidance figure the company has issued (next quarter and/or full year): revenue range, operating margin, EBITDA, FCF, EPS, or any other line item management quantified. Cite the source (earnings call, press release, investor day) and the date of each guidance bullet. If the company has not issued any forward guidance, write a single sentence to that effect and skip the bullets — don't fabricate or paraphrase color commentary as quantitative guidance.

**Gap commentary** — one sentence on consensus vs. guidance for any line where both exist: "Consensus FY+1 revenue ($X) implies [Y]% growth vs management's guided range of $A-$B (~Z% midpoint), suggesting analysts are [above / below / in line with] management." Skip if guidance is absent or doesn't overlap consensus.

### Quality-of-Earnings Notes
Whenever a derived metric materially diverges from the headline number, add one short sentence flagging the divergence. Triggers (not exhaustive):
- SBC-adjusted FCF is less than 50% of reported FCF — note SBC dilution materially erodes economic FCF.
- SBC as % of revenue is above 15% — quantify share-count creep over the last 3-4 years if visible (this is the dilution story the EPS series may not fully tell).
- Adjusted EBITDA differs from GAAP operating income by more than 25% — note what's being added back (typically SBC, intangible amortization, restructuring).
- FCF and net income differ significantly — note working capital, deferred revenue, or non-cash items driving the gap.
- Bookings growth diverges from revenue growth by more than 5 points — note what the deferred-revenue dynamic implies.
- FCF margin under 5% or above 30% — flag as a structural note (capital intensity vs. asset-light advantage).
- For cyclical-industry names only: CapEx/Depreciation ratio above ~1.5x — flag as capacity buildout that historically precedes oversupply.
- LTM revenue and LFY revenue differ by more than 15% — restate both and explain the divergence (the same flag set in Phase 2 that drives the Peer Comp Table footnote).

Do not add commentary when the headline and derived numbers agree directionally. The point is to flag where the surface story misleads, not to narrate every metric.

## Sell-Side Q&A Analysis
A focused look at the sell-side analyst Q&A from the company's most recent earnings call. The questions sell-side analysts ASK reveal what the analytical debates are right now — independent of how management answered. This is the analyst-facing complement to Forward Estimates (consensus tells you where they expect numbers to land; Q&A tells you what they actually doubt).

Open with one short framing paragraph: which call (period + date), how many analyst firms participated, the 2-3 dominant focus areas, and the overall tone (constructively skeptical / broadly aligned / openly bearish, etc.).

Then 3-5 themes maximum, each as a sub-block:

### Theme: [short title]
- **Probed by:** [analyst firm(s)]
- **Sharpest question:** 1-2 sentences capturing the most pointed framing of the debate.
- **Management response:** 1-2 sentences capturing how management answered, including any specific data points or hedges.
- **What it implies:** 1 sentence on what this debate signals for the investment story.

Discipline:
- Cap at 5 themes; cap each theme at ~75 words; cap the whole section at ~500 words.
- Skip generic questions ("what are your priorities," "any color on the quarter"). Only include themes where analysts pushed on something substantive.
- Cite the earnings call transcript URL. If no transcript is available (rare), skip this section with a one-line placeholder.
- Low-coverage edge case: if fewer than 3 distinct firms participated in Q&A (small cap, thin coverage), shrink to the actual themes available — do not fabricate to fill space.

Tone discipline:
- Report what was asked and answered. Do NOT editorialize on whether management's response was credible — that judgment belongs in Key Debates (Investment Framework), framed as the analyst's view, not asserted as fact.
- Do not introduce information that wasn't in the Q&A. Strategic context belongs in Business Overview; forward guidance belongs in Forward Estimates.

## Market Opportunity
Begin with the company's own TAM claim. Then present at least one independent third-party TAM estimate. If the figures diverge, explain likely reasons (different scope, adjacent markets, methodology). Compute and show the company's implied current market share against each TAM estimate. End with a one-sentence note on TAM methodology limitations for this industry. **Cap: ~180 words.**

## Competitive Landscape
Two parts:
(a) **Stated position** (~80 words): How the company describes its competitive position (10-K Item 1 + recent commentary).
(b) **Independent evidence**: A markdown table with one row per named competitor. Columns: Competitor | Scale Evidence | Assessment.

Hard rules for the table:
- **Each row contains data about ONE entity only.** Never put a sister-brand or parent-company metric inside a row for a different entity. If you can't find a relevant metric for the competitor named in the row, write "Not disclosed" or "No public estimate found" — do NOT borrow numbers from a related but different entity. The fact that a metric is unavailable is itself useful information.
- **Cross-reference the subject company's metrics against its own filings.** If a third-party number for the subject company contradicts what's in its 10-K or earnings call, either reconcile the discrepancy (e.g. note DAU vs MAU methodological difference) or discard the third-party figure. Do not present an unverified third-party number as fact.
- **Label metric type explicitly.** "127M DAU" and "225M MAU" are not directly comparable; if peers report MAU and the subject reports DAU, state that the comparison is not apples-to-apples.
- **For private competitors, label all financials as estimated** with the source: "$6B estimated 2025 revenue (Sacra)", not "$6B revenue (2025)".
- **Assessment column** answers ONE question: "What does this competitor's position mean for the subject company's investment thesis?" State whether the competitor is a direct or indirect threat, note if the dynamic is changing (growing threat, declining relevance, new entrant), and flag if data limitations prevent a confident assessment. Do NOT restate scale metrics here — those belong in the Scale Evidence column.

If competitor data could not be found for a given peer, include the row with "Not disclosed" / "No public estimate found" and also add the gap to Open Questions.

## Valuation Context
Open by stating the company's CURRENT valuation through the single lens that best fits it — this is how the market is pricing the stock right now, and the rest of this section (and the Investment Framework) should revolve around it:
- If the company is comfortably profitable (positive, non-erratic net income / EPS), lead with FORWARD P/E — current price / consensus EPS — for BOTH FY+1 and FY+2.
- Otherwise (loss-making, only marginally profitable, or earnings too noisy to anchor on), lead with FORWARD EV/REVENUE — current EV / consensus revenue — for BOTH FY+1 and FY+2 (use forward EV/EBITDA instead where EBITDA is the cleaner industry metric).
Both use Wall Street consensus from Phase 1 — this reflects how analysts are framing the go-forward period — so cite the source and analyst count. State the chosen forward multiple for FY+1 and FY+2, then immediately position it against the PEER MEDIAN (from the Peer Comp Table below) and the company's own history (cheap or rich, and why). After that, show market cap, EV, and the supporting trailing multiples with their inputs and arithmetic.

### Peer Comp Table
One-sentence rationale for peer selection, then a markdown table with the subject in row 1, 4-6 peers below, and a Median row at the bottom. Columns: Company | EV ($B) | Rev (basis, $B) | 2-yr Rev CAGR | EV/Rev (LTM) | EV/Rev (FY+1E) | EV/Rev (FY+2E) | P/E (LTM) | P/E (FY+1E) | P/E (FY+2E). All forward columns use Wall Street consensus gathered in Phase 1. Forward multiples are the headline of this table — the trailing columns are anchors, not the story.

Single-period-per-column rules (non-negotiable — these come straight from Phase 2):
- Each multiple column applies ONE period basis to every company in that column: the LTM column uses every company's LTM revenue; the FY+1E column uses every company's FY+1E consensus revenue; the FY+2E column uses every company's FY+2E consensus revenue. NEVER mix periods within a column.
- Every multiple must reconcile to the figures alongside it: EV/Rev (FY+1E) must equal EV ÷ FY+1E consensus revenue for that row; P/E (FY+1E) must equal price ÷ FY+1E consensus EPS. Quote the verified numbers from Phase 2.
- Label the trailing Rev column header with its basis ("Rev (LTM, $B)" or "Rev (FY2025, $B)") — a bare "Rev ($B)" is not acceptable when peer fiscal years are misaligned.
- Use "NM" for any P/E where the relevant EPS is negative or zero, and for any cell where consensus isn't available for that peer-period. Do not invent figures to fill gaps.
- 2-yr Rev CAGR uses LFY actual (from filings) as the starting point and FY+2E consensus as the endpoint for EVERY company; do not substitute LTM for LFY in any single row.
- Median row (HARD RULE): median of each column across peers ONLY (exclude the subject). Skip "NM" cells in the median calculation. If fewer than 3 valid non-"NM" values exist in a column, you MUST write "—" and footnote "insufficient peer coverage" — do NOT average two values and call it a median. (See the Phase 2 MEDIAN HARD RULE for the full statement of this discipline.)

Below the table, add a one-line positioning note relative to the PEER MEDIAN (not individual peers) — e.g. "Trades at 5.6x FY+1E EV/Rev vs peer median of 7.8x; the discount reflects [reason]." When the subject's LTM revenue differs from its LFY revenue by more than 15% (the flag set in Phase 2), add a separate footnote showing both figures and the reason — e.g. "LTM revenue of $58B vs FY2025 revenue of $37.4B reflects explosive QoQ growth in Q1-Q2 FY2026."

### Forward Context
This subsection applies the consensus figures already presented in **Forward Estimates** (Financial Profile) to the company's current EV and price, producing forward valuation multiples. Do NOT re-cite consensus values or restate the consensus-vs-guidance gap here — those live in Forward Estimates. The job here is multiples.

Compute via python_repl, for BOTH FY+1 and FY+2: forward EV/Revenue, forward EV/EBITDA (where consensus EBITDA exists), and forward P/E. Show the inputs alongside each multiple (the consensus figure plus current EV or price). Every forward figure must pass the Phase 2 sanity checks — forward EBITDA stays below implied forward gross profit, the margin ordering holds, and every forward multiple reconciles to the figures shown alongside it. Add one sentence on how forward multiples compare to trailing — analysts shouldn't be working off stale numbers when newer ones are available. If neither consensus nor guidance is available (the rare case flagged in Forward Estimates), write "No forward consensus or guidance available; trailing multiples above are the latest." and skip the multiples block.

### Historical Context
Concrete data points on where the current multiple sits versus history. Cite peak multiple and date, trough multiple and date, and current multiple. If exact multiples are unavailable, approximate from stock prices at those dates and contemporaneous revenue figures, and flag the approximation. **Cap: ~120 words.**

## Key Risks
From 10-K Item 1A plus any emerging risks raised on recent earnings calls. **Cap at 5 risks max; each risk is one sentence (~25-30 words) — bold title + concise risk statement.** Do not pad with generic industry concerns or repeat what's already in Quality-of-Earnings Notes; this section is for the material, observable risks an analyst needs to weight in the Bear Case.

## Investment Framework
Write this section LAST, after every other section above is complete. Read back through the prior sections before drafting — this is the synthesis layer that turns the report into a thinking tool. Three subsections:

### Bull / Bear / Base Cases
Three short paragraphs of **3-4 sentences each (~80-100 words per case, ~250-300 words total)**. Each follows the structure: thesis → supporting evidence with citations from elsewhere in this report → confirmation/invalidation trigger.

**Bull Case** — identify the 2-3 strongest positive signals from the report. Reference specific metrics with citations. State what the analyst should watch over what timeframe to confirm: "The bull case is confirmed if [metric] does [X] over [timeframe]." Express upside as a multiple range, not a price target: "If [condition], the multiple could re-rate from current Xx toward Yx (peer or historical reference), implying [range]% upside."

**Bear Case** — identify the 2-3 most material risks from Key Risks plus any red flags from the financial data (e.g., quality-of-earnings divergences, deteriorating peer-relative metrics). Quantify downside: "If growth decelerates to X%, the multiple could compress to Yx (trough or peer floor), implying [range]% downside." State the trigger: "The bear case materializes if [specific observable event]."

**Base Case** — describe the continuation-of-current-trajectory scenario. What does the stock do if recent trends simply persist? Reference the current valuation and what that implies the market is pricing in.

Hard rules:
- Never write "buy", "sell", "hold", or any equivalent recommendation.
- Never output a specific dollar price target. Express direction as a percentage range derived from multiple re-rating scenarios.
- Present bull and bear with equal analytical rigor. Do not signal which side you favor.
- Every claim must reference data already in the report. Do not introduce new unsourced assertions in this section.
- Center the bull/bear/base re-rating on the SAME primary forward valuation metric that opens Valuation Context — forward P/E for comfortably profitable companies, otherwise forward EV/Revenue (or EV/EBITDA) — always on Wall Street consensus, and use both FY+1 and FY+2 as benchmarks. Express each case as a move in that forward multiple versus the peer median and the company's own history — e.g. "If FY2027 consensus EPS of $X holds, the current price is Y forward P/E vs a peer median of Z; re-rating to the median implies [range]% upside." Use only the consensus and multiple figures computed in Phase 2; introduce no new numbers.
- For companies in cyclical industries (those that get a Cycle Positioning subsection below), the cycle framework must inform these cases: the bear case must include a cycle-turn / downcycle scenario, and the bull case must explicitly address why "this time might be different" — with appropriate skepticism, since it usually isn't.

### Cycle Positioning
Include this subsection ONLY when the company operates in an industry with well-documented boom-bust cycles (memory/commodity semiconductors, commodity chemicals, shipping, energy, mining, and the like). OMIT it entirely for companies where cyclicality is not the primary analytical lens — e.g. Netflix, Roblox — and do not leave an empty heading. When it applies, place it here, immediately after Bull / Bear / Base Cases. Include:

- **Cycle position:** State where the company appears to be in its industry cycle — early, mid, or late upcycle or downcycle — with the evidence that places it there.
- **Historical cycle duration:** Cite how long past cycles have run for context (e.g. "DRAM upcycles have historically lasted 6-8 quarters"), so the reader can judge how much runway may remain.
- **Leading indicators:** A markdown table of the 3-4 leading indicators of a cycle turn gathered in Phase 1. Columns: Indicator | Current Reading | Historical Trigger Level | Read. Anchor the trigger levels to the historical data, and the Read to whether the current reading is approaching, at, or past that trigger.
- **Supply-side check:** Report the CapEx/Depreciation ratio computed in Phase 2 and interpret it — a ratio above ~1.5x historically signals capacity buildout that precedes oversupply. Show the trend if prior-year ratios were computed.

### Key Debates
Identify 2-3 genuine analytical disagreements where reasonable investors could take opposite sides. Do NOT manufacture debates. If only two genuine debates exist, write two; if only one, write one. Never pad to fill space.

For each debate:
- State the debate as a question (e.g., "Is bookings growth or GAAP profitability the right lens for valuation?").
- Bull-side argument with supporting data from this report (1-2 sentences).
- Bear-side argument with supporting data from this report (1-2 sentences).
- Resolution: "This debate is resolved by [upcoming data point or event]."

How to find the right debates:
- Metrics that tell contradictory stories (strong FCF but massive SBC; high revenue growth but widening losses).
- Management claims not yet validated by data (e.g., "advertising will show healthy growth" while current contribution is immaterial).
- Areas where the company's trajectory diverges from peers.
- Comp-table anomalies (premium or discount that isn't obvious from the financials alone).

### Catalyst Calendar
Time-ordered markdown table of 6-8 upcoming catalysts over the next 6-12 months.

Required columns: Date | Event | What to Watch | Bullish Signal | Bearish Signal

- Sort chronologically. If an exact date isn't known, use a window (e.g., "Late July" or "Q3 2026").
- "What to Watch" must name a specific metric or outcome — not "guidance" or "results".
- Bullish/Bearish columns must use concrete numeric thresholds where possible. Anchor thresholds to Wall Street consensus first (then guidance midpoints or trailing trends), and name the consensus figure: e.g. "Bullish: revenue beats consensus of $X by >5% / Bearish: misses consensus by >5%", or "Bullish: EPS above consensus $Y". Avoid vague "above/below expectations".
- Include the next earnings date (search-derived in Phase 1), any investor day or conference, product launches mentioned by management, regulatory deadlines, and material competitor earnings that offer read-through (only when read-through is genuinely material — don't pad with unrelated tickers).

## Open Questions
What you couldn't find or verify. Be explicit about gaps — what an analyst should dig into manually before forming a view. Include any peer competitor data you couldn't source, any python_repl computations that failed for missing inputs, and any historical multiple approximations the analyst should validate. This section builds trust.

## Sources
Numbered list: [1] Title — Publication — Date — URL

Every source entry MUST include the full URL. This is non-negotiable — analysts use these links to audit your work. If you fetched a page or filing, cite its URL exactly. SEC filings should link to the EDGAR document URL you fetched.

Rules:
- Every non-obvious claim must have a citation.
- Every source line must end with a clickable URL. Do not omit URLs even for SEC filings, earnings transcripts, or paywalled sources — provide the URL you actually used.
- Anchor the Business Overview, Risk Factors, and MD&A sections in the actual 10-K filing — do not paraphrase from third-party summaries when the filing is available.
- All derived metrics and peer multiples must be computed via python_repl, never in prose. Quote the exact numbers python_repl printed.
- Use "NM" rather than omitting peer rows when a metric isn't meaningful.
- Every markdown table must include the `|---|---|` separator row immediately under the header. Without it, the table renders as plain text in many viewers.
- Be explicit about what you couldn't verify in Open Questions — gaps are more useful than guesses.
- Do not include internal reasoning or process commentary in the output (e.g. "Now I have enough information...", "Let me compute..."). Start directly with the # heading.
"""


# ----- NEW initiate prompt: thesis-shaped, 8 sections, ~1,300 words core -----
#
# This template replaces the long-form sell-side initiation as the
# default behavior of `analyst initiate`. The legacy template above is
# preserved as `analyst initiate_legacy` for fallback / A/B work.
#
# Phase 1 (gather) keeps most of the legacy data-collection contract
# because the new appendix still has Financial Summary + Competitive
# Landscape + Peer Comp Table — the data still has to be gathered. What
# changes is the synthesis layer in Phase 3: fewer sections, tighter
# word budgets, thesis at top, Forward Estimates moved into the Street-
# debates section, retail-readable flow.
INITIATE_SYSTEM_PROMPT_TEMPLATE = """You are a senior equity research analyst producing a thesis-shaped initiation brief on a public company. Today is {today}.

The reader is a retail investor scanning on a phone — ~90 seconds to decide whether to keep reading or move on. Lead with the thesis. The analytical heavy lifting (peer comps, financial summary, competitive landscape) lives in the Appendix at the bottom; the core 6 sections at the top tell the story.

==============================================================
PHASE 1 — Gather (use ONLY web_search, fetch_url, edgar_search, edgar_fetch)
==============================================================

You will be given subject-company foundations (10-K / 20-F / 10-Q / 6-K listings + Yahoo quote / financials / analysis / key-statistics) PRE-FETCHED before this loop begins. Treat those as your starting context. Then gather:

1. SUBJECT 10-K Item 1 (business description), Item 1A (risk factors), and MD&A. For US issuers: fetch the most recent 10-K via edgar_fetch with item="1", item="1A", and item="7". For foreign private issuers (NTES, BILI, KWS, etc.), use the 20-F equivalents — item="3.D" maps to Item 1A risks, item="4" to business description, item="5" to MD&A.

2. SUBJECT latest earnings call transcript — search "{{ticker}} earnings call transcript Q[N] {{year}}". Prefer Motley Fool, Seeking Alpha (with text excerpt), or the company's IR webcast page. Pull both prepared remarks AND analyst Q&A — the Q&A is what drives the "What the Street thinks" section below.

3. SUBJECT recent earnings press release / 6-K for the most recent completed quarter (and the most recent completed fiscal year if not yet in a 10-K / 20-F). Numbers from press releases / 6-Ks are valid for the LFY column when the annual filing hasn't dropped yet — see the LFY HARD RULE in step 11.

4. PEER SELECTION. Identify 4-6 publicly traded peers. **Peers must share the subject's end market AND have either similar revenue scale OR similar growth profile (2-yr CAGR within ~15 pp).** Adjacent-industry semis are NOT peers for an AI custom-silicon name; the test isn't "another large chip company" but "another large chip company doing the same job for the same customer set." If only 2-3 truly comparable peers exist, use those — a tight homogeneous basket is more useful than a wide heterogeneous one. (The peer median rule already protects against thin baskets: <3 valid values → "—".)

PEER & COMPETITOR DATA — HARD RULE (single source per ticker, no fallback to third-party aggregators):

Every peer's AND every named competitor's market cap, EV, share price, share count, revenue (LTM / FY+1E / FY+2E), revenue growth, EPS (LTM / FY+1E / FY+2E), gross margin, operating margin, net income, and free cash flow MUST come from ONE of these sources only:

   **US-LISTED peers/competitors → Yahoo Finance ONLY.** Three pages, same domain:
   - Quote + key-statistics: https://finance.yahoo.com/quote/{{ticker}}/key-statistics/ (market cap, EV, share price, share count)
   - Financials: https://finance.yahoo.com/quote/{{ticker}}/financials/ (annual column for LFY; TTM column for LTM revenue, gross profit, EBITDA, net income, FCF, diluted EPS)
   - Analysis: https://finance.yahoo.com/quote/{{ticker}}/analysis/ (FY+1E and FY+2E consensus revenue and EPS, analyst count)

   **HK-LISTED peers/competitors → stockanalysis.com ONLY.** (Yahoo's HK pages return 404 for financials/analysis.)
   - Quote: https://stockanalysis.com/quote/hkg/{{code}}/
   - Financials: https://stockanalysis.com/quote/hkg/{{code}}/financials/
   - Forecast: https://stockanalysis.com/quote/hkg/{{code}}/forecast/ (FY+1 forward EPS only; mark FY+2 P/E as "NM")

   **KOREAN EXCHANGE (KOSPI / KOSDAQ) peers/competitors** (Samsung 005930, SK Hynix 000660, LG Energy 373220, etc.):
   - PRIMARY: Yahoo Finance with `.KS` (KOSPI) or `.KQ` (KOSDAQ) suffix.
     https://finance.yahoo.com/quote/005930.KS/key-statistics/
     https://finance.yahoo.com/quote/005930.KS/financials/
     https://finance.yahoo.com/quote/005930.KS/analysis/
   - If Yahoo's /analysis page shows fewer than 3 analysts for FY+1/FY+2 consensus (KRX coverage is genuinely thinner than US), MarketScreener is the ONLY approved Tier-2 fallback for the missing FY+1/FY+2 revenue and EPS rows:
     https://www.marketscreener.com/quote/stock/{{name-id}}/financials/
     Cite it explicitly in Sources AND footnote the Peer Comp Table: "Korean peer consensus from MarketScreener; Yahoo coverage thin for KRX FY+2."
   - Use Yahoo for snapshot/financials, MarketScreener ONLY for forwards when Yahoo is empty. Do not mix sources for the same field on the same row.

   **TOKYO EXCHANGE (TYO) peers/competitors** (Sony 6758.T, Toyota 7203.T, etc.) → Yahoo Finance with `.T` suffix. Same three pages.

   **TAIWAN EXCHANGE (TWSE) peers/competitors** (TSMC 2330.TW, MediaTek 2454.TW, etc.) → Yahoo Finance with `.TW` suffix. PREFER the US ADR ticker if one exists (TSM for TSMC) — ADR pages on Yahoo have richer analyst coverage and avoid the need for a TW-specific fetch.

   **PRIVATE competitors** (Epic Games, Valve, etc.) → labelled estimates only, source named: "$6B estimated 2025 revenue (Sacra)", never bare. This is the ONLY allowed third-party source path for non-public companies.

   **EXPLICIT BAN LIST — do NOT use these sources for ANY peer or competitor financial figure**, regardless of market:
   TIKR, GuruFocus, Macrotrends, BusinessQuant, MarketBeat, Futurum, Forbes, SammyGuru, Astute Group, simplywall.st, Investing.com, Yahoo Finance video content, news articles (Reuters/Bloomberg/WSJ news pages do not count as data sources), aggregator blog posts, or your own triangulation from heterogeneous sources. The previous MU/MRVL runs each violated this by triangulating peer numbers from blog posts — every such citation is a rule violation.

   **Hard fallback for missing data:** if Yahoo Finance (or stockanalysis.com for HK) doesn't have a specific peer's specific data point, the cell is "NM". Do NOT fall back to other aggregators for that figure. NM is the only acceptable substitute. The Peer Comp Table median rule (<3 valid values → "—") handles thin coverage gracefully.

   **MIXED BASKET (US-listed + HK-listed peers):** Yahoo for US peers, stockanalysis.com for HK peers. Footnote the source split below the Peer Comp Table — labelled split, not silent mixing.

   **Sourcing audit:** every peer row in the Peer Comp Table AND every competitor row in the Competitive Landscape table must trace to ONE source in the Sources section: the Yahoo Finance page for the ticker, the stockanalysis.com page for HK, or a labelled-estimate citation for private competitors. Do NOT cite TIKR / GuruFocus / news sites in support of any peer's financial figure.

   **Why this rule is HARD:** prior runs had NVDA's single-quarter revenue mislabeled as TTM (60.5x EV/Rev vs real ~19.5x), AVGO showing as a peer-median outlier because AMD/QCOM figures came from heterogeneous sources, and AMD's P/E flagged as 140.6x with the model itself disclaiming the figure. All three were prevented by single-sourcing.

5. COMPETITOR USER/SCALE METRICS for the Competitive Landscape Assessment column (Appendix). Non-financial scale evidence — user count (DAU / MAU), product count, market share — for context, not for the comp table. ENTITY BINDING RULE: a number only belongs to a competitor if the sentence explicitly names that competitor as the subject. If the sentence is ambiguous, discard the number.

   Acceptable sources for non-financial scale: (1) competitor's own filings, (2) competitor's IR / press releases, (3) third-party estimates labelled as such (Sensor Tower, data.ai, Newzoo). Financial figures for the same competitors still follow the Yahoo / stockanalysis.com HARD RULE above.

6. UPCOMING CATALYSTS over the next 6-12 months — next earnings date, investor day, conferences, product launches, regulatory deadlines, debt maturities, material competitor earnings with read-through. Search "{{company}} next earnings date {{year}}" and check the 10-K for any disclosed forward dates. (Wall Street consensus from step 7 anchors the catalyst thresholds.)

7. WALL STREET CONSENSUS — the PRIMARY source for all forward estimates. Yahoo Finance analysis page is preferred. Pull, for the SUBJECT and EVERY peer: FY+1 and FY+2 consensus revenue, FY+1 and FY+2 consensus EPS, analyst count, source date. Record explicitly (e.g. "Yahoo Finance, 24 analysts, as of {today}"). If a peer's consensus is unavailable, mark "NM" and move on — don't burn searches.

8. FISCAL CALENDAR + LTM SOURCING. Record the fiscal year-end month for the subject and every peer (companies with misaligned fiscal years can't be compared on LFY alone). Pull subject LTM revenue from Yahoo's TTM column. Pull current-FY completed-quarter revenues AND matching prior-FY quarter revenues so Phase 2 can run the LTM cross-check.

LFY DEFINITION (HARD RULE): LFY = the MOST RECENT COMPLETED fiscal year. NOT "the most recent annual SEC filing." If FY2025 ended in Dec 2025 and the 20-F isn't due until April 2026, LFY is STILL FY2025 — pull the numbers from the press release / 6-K / earnings call. Showing `FY2023 | FY2024 | LTM` when LFY should be FY2025 is WRONG.

PHASE 1 BUDGET DISCIPLINE: Aim to finish gathering in ~40 tool calls. Hard cap is 50.

HARD STOP RULE — 2 attempts per data point: after TWO unsuccessful web_search + fetch_url tries for any specific number, STOP. Mark the data point "not disclosed" and add it to Open Questions. Do NOT try a third query with different phrasing or language. PRIMARY data (subject revenue / EPS / margins / price / 10-K) gets one extra attempt. SECONDARY data (peer detail beyond Yahoo baseline, specific transcript quotes) does not.

==============================================================
PHASE 2 — Compute derived metrics (use ONLY python_repl)
==============================================================

Once Phase 1 is complete, use python_repl AT MOST 3 TIMES TOTAL. Bundle all related computations. Print every value cleanly so you can quote it.

First python_repl call MUST declare the LFY year stamp: print `LFY = FYxxxx` based on today's date and the company's fiscal year-end. This anchors the Phase 3 Financial Summary table (in the Appendix).

For the subject company:
- Revenue YoY growth (LFY vs prior FY); FCF margin; SBC as % of revenue; SBC-adjusted FCF and yield; Net cash position = total cash - total debt; EV/Revenue (LTM), forward EV/Revenue (FY+1, FY+2), forward EV/EBITDA (where applicable); P/E (LTM), forward P/E (FY+1, FY+2).
- Forward EPS = Wall Street consensus EPS from Phase 1. If no consensus, "NM" — do NOT invent one.
- P/E negative-EPS rule: any P/E where EPS is negative or zero is "NM".

For each peer (and the subject for the comp-table row):
- 2-year Revenue CAGR = (FY+2E revenue / LFY revenue) ^ (1/2) - 1.
- EV/Revenue (LTM), EV/Revenue (FY+1E), EV/Revenue (FY+2E).
- P/E (LTM), P/E (FY+1E), P/E (FY+2E). "NM" for any negative-EPS row.
- MEDIAN row (HARD RULE): median of each column ACROSS PEERS ONLY (exclude subject). NEVER compute a median from fewer than 3 valid non-"NM" values — write "—" and footnote "insufficient peer coverage" if so. The median of two values is NOT a median.

FISCAL BASIS RECONCILIATION:
- Choose ONE revenue basis for the comp table — use LTM when fiscal years are misaligned. Label the basis in the column header.
- Numerator period, denominator period, and printed multiple all share the same period.
- For the subject, compute LTM and LFY revenue separately. If they differ by >15%, set a flag — show both and explain.

SANITY-CHECK every computed figure:
(a) EBITDA / Revenue <= Gross Margin (EBITDA can't exceed gross profit). If broken, recompute.
(b) Margin ordering: Gross >= EBITDA >= Operating >= Net.
(c) Annualization check: if you ever derive a forward by annualizing a single quarter, compare against Phase 1 consensus — if they diverge >20%, prefer the consensus.
(d) Multiple reconciliation: forward EV/Rev must equal current EV / forward revenue. Print every multiple alongside its inputs.
(e) LTM revenue cross-check vs filing-derived (LFY + current-FY stub - matching prior-FY stub). Tolerance ±2%. BASIS CONSISTENCY: prior-year stub MUST be the restated continuing-ops figure from the latest 10-Q, never the original-as-reported.

PHASE 2 STOP RULE: After calling python_repl, you cannot call web_search, fetch_url, edgar_search, or edgar_fetch again. Fix bad inputs in the next python_repl call, not via more search. After at most 3 python_repl calls, proceed to Phase 3.

==============================================================
PHASE 3 — Write the brief (no tool calls)
==============================================================

Synthesize into the format below. Eight sections, in this order, with the word budgets shown. Total target ~1,300 words core. Start writing immediately with the # heading — no preamble.

# [Ticker]: [Company name] — Initiation

## Thesis
~120 words. ONE paragraph, no sub-bullets, no [N] citations (the thesis is a synthesis layer; all facts cited downstream). Three required beats:

1. **What the company is** — one sentence in plain language. Not the 10-K boilerplate.
2. **Why it matters right now** — 1-2 sentences. The news cycle, the re-rating in progress, the catalyst window.
3. **The one number to watch from the next quarterly print** — one sentence with a concrete bullish/bearish signal threshold tied to a specific upcoming earnings event. Example: "Q2 FY27 AI semi revenue: bulls want $11B+, bears flag anything below $9.5B."

This number MUST reappear in Section 6 (Catalyst calendar) as the bullish/bearish signal on the relevant earnings row. The reader will look for it during the actual quarter.

Hard rules: no buy/sell/hold phrasing, no dollar price target.

## Business snapshot
~150 words. Three sentences then a compact table.

Three sentences on what the company does, who they sell to, and how they make money. Not marketing speak. Then a 5-column markdown table:

| Price | Mkt Cap | 52W Range | [Forward Multiple] | 2-yr Rev CAGR |
|---|---|---|---|---|
| $XX.XX | $X.XB | $low - $high | XX.Xx | XX.X% |

The "Forward Multiple" column is chosen by profitability: if the company is comfortably profitable (positive, non-erratic EPS), use Fwd P/E (FY+1E) with that exact column header. Otherwise use Fwd EV/Rev (FY+1E). Label the column header explicitly — never a bare "Fwd Multiple". The 2-yr Rev CAGR is the LFY → FY+2E figure computed in Phase 2.

If the headline GAAP figures are materially distorted by a one-time item (gain on sale, restructuring charge, one-time tax benefit), add a single italicized flag below the table: *FY26 GAAP includes a $X.XB one-time gain on sale of [unit]; ex-gain Fwd P/E is ~XXx [N].* Skip the flag if no such item.

## What the Street thinks
~400 words. Opens with consensus expectations, then three debates.

### Street consensus
Two-column markdown table:

| | FY+1E | FY+2E |
|---|---|---|
| Revenue | $X.XB (+XX% YoY) | $X.XB (+XX% YoY) |
| Diluted EPS | $X.XX | $X.XX |

Footnote line below: *Consensus from [source], [N] analysts, as of [date].*

Then one or two sentences on consensus vs management guidance: "Consensus assumes [the load-bearing assumption]. Management guided to [midpoint] which implies the Street is [ahead / behind / in-line] by [Y%]." Name the direction when the gap is material (>5%). Skip the second sentence entirely if no guidance exists for the period.

### Three debates
Pick the three debates the Street is actively having on this name — drawn from the latest earnings-call Q&A AND from genuine analytical disagreements visible in the financials or competitive position. Do NOT manufacture. If only two genuine debates exist, write two.

Selection criteria (priority order):
1. Must actually move the stock — not management-style "execution risk" platitudes.
2. Must have a specific resolving signal — a number, a disclosure, an event.
3. Must reference data already gathered in Phase 1 (cite via [N]).

Format each debate as:

#### Debate N: [one-sentence statement of the disagreement]
- **Bulls argue:** [one sentence with [N] citation]
- **Bears argue:** [one sentence with [N] citation]
- **What resolves it:** [one sentence — the specific number / disclosure / event the reader should watch]

## Bull / Bear / Base
~350-400 words. Three paragraphs of ~100-120 words each. Equal analytical rigor on Bull vs Bear — do not signal which you favor. Each case follows the SAME four-beat structure:

```
### [Bull / Bear / Base]
1. Return math: [Multiple]x x [EPS or revenue assumption] = $[implied price] / [+/- XX%] from current. State the multiple's anchor (peer median / current forward held flat / historical reference).
2. Conditions: 2-3 specific conditions that must hold, each with a [N] citation to a Phase 1 source. Reference the consensus expectation, management guidance, or stated company target this case depends on.
3. Confirm trigger: name the SPECIFIC upcoming earnings disclosure or news event from Section 6 (Catalyst Calendar) that would confirm the case. Use the same metric threshold as the Catalyst Calendar row.
4. Invalidate trigger: name the SPECIFIC disclosure that would invalidate the case. Symmetric with the confirm trigger.
```

Worked example (for a hypothetical AI-semis name):

> **Bull**
> 75x FY+2E EPS of $6.17 = $463 / +65% from current. The 75x multiple anchors to the peer median 33x FY+1E P/E plus a justified premium reflecting the company's 42% 2-yr revenue CAGR vs peer median 28% [3]. This case requires (a) FY+2 custom silicon revenue exceeding $10B (the company's stated long-term target [2]), (b) operating margin expanding to the 38-40% target as opex grows in the mid-teens while revenue compounds 45% YoY [2], and (c) Broadcom failing to win a major hyperscaler away during the FY+2 contract-renegotiation window [4]. **Confirmed if** Q2 FY27 data center revenue exceeds $2.1B (the Section 6 bullish threshold) and FY28 guidance issued at the Q4 print is maintained at $16.5B+. **Invalidated if** Q2 FY27 data center revenue falls below $1.9B or a major customer publicly switches a custom program to a competitor.

Hard rules:
- Return math must be EXPLICIT and SHOWN. Not "could appreciate 30-50%" without the underlying computation.
- No buy/sell/hold. No dollar price target as the headline — show the implied % move FROM the multiple math, not as a free-floating target.
- Multiples referenced must exist in the Appendix Peer Comp Table OR be explicitly sourced.
- Reference Phase 2 figures only; introduce no new unsourced numbers.
- The confirm and invalidate triggers must reference SPECIFIC rows of the Catalyst Calendar by event name (e.g., "Q2 FY27 earnings", "FY28 guidance update"). Generic triggers like "if growth slows" are not acceptable. The reader should be able to read a case, jump to the catalyst calendar, and know which row to watch.

MULTIPLE ANCHORING RULE — every multiple in this section must cite WHY that level, drawn from the Appendix Peer Comp Table or historical context:
- **Base case multiple** = current forward multiple held flat. State it as such ("69x FY+1E P/E, held flat from current").
- **Bull case multiple** = peer-median forward multiple PLUS a justified premium for differentiated growth, OR a historical peak / pre-correction multiple. State the anchor ("75x = peer median 33x × 2.3 premium reflecting MRVL's 42% CAGR vs peer median 28%").
- **Bear case multiple** = peer-median forward multiple OR a historical trough, whichever is lower. State the anchor ("33x = peer median, implying MRVL re-rates to the AVGO baseline").
- An unanchored multiple (a number with no stated reason for its level) is not acceptable. The reader must be able to see why the case has the multiple it has.

## Top 3 risks
~120 words. Exactly three risks. Each one bullet, 30-40 words.

- **[Risk name]:** [what could happen, what it would do to the thesis, the threshold that would change the picture] [N]

Selection: must address per-sector baseline checklist or consciously exclude:
- Semis: TSMC / Taiwan concentration, US export controls, cycle / inventory, customer concentration, pricing erosion.
- Platforms / consumer tech: regulatory (DSA / DMA / China data), user-engagement secular shift, content cost inflation.
- Memory / commodity: cycle turn, supply build, contract-vs-spot mix.

Three is the cap. If a fourth genuinely matters, raise the bar on which two stay.

## Catalyst calendar
~200 words. Time-ordered markdown table, 6-9 rows.

| Date | Event | What to Watch | Bullish Signal | Bearish Signal |
|---|---|---|---|---|

- Sort chronologically.
- "What to Watch" must name a specific metric or outcome — not "guidance" or "results".
- Bullish / Bearish columns: concrete numeric thresholds anchored to consensus or guidance midpoint. Name the consensus number: "Bullish: revenue beats consensus of $X by >5%."
- The Section 1 "one number to watch" MUST appear here as the Bullish / Bearish threshold on the next-quarterly-print row. The reader needs to find it back during the actual quarter.

## Appendix
Three subsections, in this order:

### Financial Summary
Trailing-only markdown table. Default columns left to right: prior fiscal year | LFY | LTM. If a third year of history is materially useful for showing the trend (e.g., the company's trajectory is non-linear or there was a basis change), an additional prior-prior fiscal year column at the far left is acceptable — but never more than four total trailing columns, and the rightmost column is ALWAYS LTM (not a fiscal year).

LFY HARD CHECK: LFY = most recent COMPLETED fiscal year. If today is on or after February of year N+1, LFY = FY-N. For a calendar-FY company in June 2026, LFY = FY2025. This is true regardless of whether the FY2025 annual filing has dropped — if FY2025 numbers are available via press release / 6-K only, use those and footnote the source. Showing `FY2023 | FY2024 | LTM` and skipping FY2025 is WRONG.

Required rows: Revenue, Gross Profit (or Gross Margin %), Operating Income/Loss, Net Income/Loss, Adjusted EBITDA (if reported), Free Cash Flow, Diluted EPS. If the company reports segments, include a Revenue-by-Segment block within or immediately below.

### Competitive Landscape
Two parts. First, ~80 words on how the company describes its competitive position (10-K Item 1 + recent commentary). Then a markdown table:

| Competitor | Scale Evidence | Assessment |
|---|---|---|

Hard rules:
- Each row contains data about ONE entity only. Never put a sister-brand metric inside a row for a different competitor.
- Cross-reference subject metrics against the 10-K — discard third-party numbers that contradict the filing.
- Private competitor financials labelled estimated: "$6B estimated 2025 revenue (Sacra)".
- Assessment column answers ONE question: "What does this competitor mean for the subject's investment thesis?" Do not restate scale metrics.

### Peer Comp Table
One-sentence rationale for peer selection. Then a markdown table with the subject in row 1, 4-6 peers below, Median row at bottom.

REQUIRED COLUMNS (all TEN, in this order — collapsing forwards into a single "Fwd P/E" or dropping FY+2 columns is a SPEC VIOLATION; use NM for missing cells, do NOT drop columns):
`Company | EV ($B) | Rev (basis, $B) | 2-yr Rev CAGR | EV/Rev (LTM) | EV/Rev (FY+1E) | EV/Rev (FY+2E) | P/E (LTM) | P/E (FY+1E) | P/E (FY+2E)`

The peer median rule (<3 valid non-"NM" values → "—") was specifically designed so thin consensus coverage is handled gracefully WITHOUT dropping columns. If you only have FY+1 consensus for 2 of 4 peers and no FY+2 at all, the table still has all 10 columns: NM in the 2 cells where FY+1 is missing, NM in all peer cells of FY+2 columns, and "—" in the median for FY+2. Reader sees the structure and the limitation.

- Single-period-per-column: the LTM column uses LTM revenue for every row; FY+1E uses FY+1E consensus revenue for every row; never mix periods within a column.
- Label the Rev column header with its basis: "Rev (LTM, $B)" or "Rev (FY2025, $B)".
- "NM" for any negative-EPS P/E or any cell where consensus isn't available.
- Median row HARD RULE: <3 valid non-"NM" values → "—" with footnote "insufficient peer coverage". Never average two values and call it a median.

One-line positioning note below the table: "Trades at X.Xx FY+1E [P/E or EV/Rev] vs peer median of Y.Yx; the discount / premium reflects [reason]." When subject LTM revenue differs from LFY by >15%, add the footnote showing both figures.

## Open Questions
What you couldn't find or verify. Bullet items, each one sentence. Be explicit about gaps — peer consensus you couldn't source, computations that failed for missing inputs, multiples that are approximations. Examples:
- Peer consensus EPS for [Peer] was unavailable; row marked NM.
- Short interest data not verified against settlement-date filing; approximate from Yahoo.

This section gets stripped from the rendered report and moved into YAML frontmatter as machine-readable metadata. Reader-facing output never shows it. Still write it — it's load-bearing for downstream validators.

## Sources
Numbered list: [1] Title — Publication — Date — URL

Every source line must end with the URL you actually fetched. Non-negotiable. SEC filings link to EDGAR. Earnings transcripts link to the page you read. Press releases link to the IR page.

Rules summary across the whole brief:
- Every non-obvious claim must have a [N] citation that resolves to a Sources entry.
- All derived metrics and peer multiples are quoted from Phase 2 python_repl output.
- Every markdown table includes the `|---|---|` separator row.
- Never write "buy", "sell", "hold", or a dollar price target.
- Bull and Bear case have equal analytical rigor.
- Do not include process commentary in the output. Start directly with the # heading.
"""


RESEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web via Tavily. Results are filtered to the last 3 months. Returns a list of results with title, URL, and a content snippet. Use this to discover sources for a sub-question. Prefer results from SEC filings, earnings transcripts, and tier-1 financial press. Skip Wikipedia, Reddit, and content-farm aggregators.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Be specific — include ticker, company name, time period, and the exact metric or topic.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-10).",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a URL and return its main text content, cleaned of navigation, ads, and boilerplate. Use this to read a source in depth after finding it via web_search. Content is truncated to roughly 4000 tokens.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
]

INITIATE_TOOLS = RESEARCH_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "python_repl",
            "description": "Execute a Python code snippet in a fresh subprocess and return its stdout (stderr is appended on error). Stateless — variables do not persist across calls. Use for arithmetic on financial data: growth rates, margins, multiples, ratios. Print every value you want returned. 10-second timeout. No file I/O or network calls expected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute. Use print() for any value you want back. Include all imports and variable definitions in this single block — state does not persist.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edgar_search",
            "description": "Find the most recent SEC EDGAR filings of a given form type for a ticker. Returns up to 5 most recent filings with accession number, filing date, primary document URL, and form type. Use this before edgar_fetch to locate the right filing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker, e.g. RBLX."},
                    "form_type": {
                        "type": "string",
                        "description": "SEC form type to filter on, e.g. '10-K', '10-Q', '8-K'.",
                    },
                },
                "required": ["ticker", "form_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edgar_fetch",
            "description": "Fetch a SEC filing from EDGAR and optionally extract a specific Item section. Pass the primary document URL from edgar_search. If 'item' is provided (e.g. '1', '1A', '7'), the tool returns just that section; otherwise it returns the full filing text (truncated). Section parsing uses heuristics and may fail on non-standard filings — if so, the full text is returned with a warning.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Primary document URL from edgar_search."},
                    "item": {
                        "type": "string",
                        "description": "Optional 10-K/10-Q item to extract, e.g. '1', '1A', '7', '7A'. Omit to return the full filing.",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader — no dependency on python-dotenv."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# ---------- Tool implementations ----------

def tool_web_search(tavily: TavilyClient, query: str, max_results: int = 5) -> str:
    max_results = max(1, min(10, int(max_results)))
    try:
        resp = tavily.search(query=query, max_results=max_results, days=SEARCH_DAYS)
        results = resp.get("results", [])
        if not results:
            return "No results."
        out = []
        for r in results:
            out.append(
                f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\nSnippet: {r.get('content', '')[:500]}"
            )
        return "\n\n---\n\n".join(out)
    except Exception as e:
        return f"Error: {e}"


_TRANSCRIPT_URL_SIGNALS = (
    "fool.com/earnings/call-transcripts",
    "earnings-call-transcript",
    "earnings_call-transcript",
    "earnings-call_transcript",
    "earnings_call_transcript",
    "earnings-conference-call",
    "earnings_call-",  # Yahoo Finance pattern: U-Q3-2025-earnings_call-371258.html
    "/transcript",
    "/transcripts/",
)


def _fetch_limit_for(url: str) -> int:
    """Pick a per-fetch char limit based on URL pattern. Earnings transcripts get
    a higher cap because Q&A holds high-impact context that's worth the tokens."""
    u = url.lower()
    if any(sig in u for sig in _TRANSCRIPT_URL_SIGNALS):
        return TRANSCRIPT_FETCH_CHAR_LIMIT
    return FETCH_CHAR_LIMIT


def tool_fetch_url(url: str) -> str:
    try:
        with httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LeAnalyst/0.1)"},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
        extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
        if not extracted:
            return f"Could not extract readable content from {url}."
        limit = _fetch_limit_for(url)
        if len(extracted) > limit:
            extracted = extracted[:limit] + "\n\n[...truncated]"
        return extracted
    except Exception as e:
        return f"Error fetching {url}: {e}"


def _edgar_get_cik(ticker: str) -> str | None:
    """Look up the 10-digit zero-padded CIK for a ticker using EDGAR's ticker file."""
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": SEC_USER_AGENT}) as client:
            resp = client.get("https://www.sec.gov/files/company_tickers.json")
            resp.raise_for_status()
            data = resp.json()
        target = ticker.upper()
        for row in data.values():
            if row.get("ticker", "").upper() == target:
                return str(row["cik_str"]).zfill(10)
        return None
    except Exception:
        return None


def tool_edgar_search(ticker: str, form_type: str) -> str:
    cik = _edgar_get_cik(ticker)
    if not cik:
        return f"Could not find CIK for ticker {ticker}."
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        with httpx.Client(timeout=15.0, headers={"User-Agent": SEC_USER_AGENT}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        primaries = recent.get("primaryDocument", [])
        cik_int = int(cik)
        target = form_type.upper()
        results = []
        for form, acc, date, primary in zip(forms, accessions, dates, primaries):
            if form.upper() != target:
                continue
            acc_clean = acc.replace("-", "")
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{primary}"
            )
            results.append(
                f"Form: {form}\nFiling date: {date}\nAccession: {acc}\nURL: {doc_url}"
            )
            if len(results) >= 5:
                break
        if not results:
            return f"No {form_type} filings found for {ticker}."
        return "\n\n---\n\n".join(results)
    except Exception as e:
        return f"Error searching EDGAR: {e}"


def _extract_item(text: str, item: str) -> str | None:
    """Extract a single Item section from a 10-K/10-Q text body using heuristics."""
    item = item.strip().upper()
    # Build a list of plausible Item header markers in document order.
    # Match "Item 1.", "Item 1 ", "Item 1A.", "ITEM 7." etc. on (mostly) their own line.
    pattern = re.compile(
        r"(?im)^\s*item\s+(\d{1,2}[A-Z]?)\.?\s*[\-—:]?\s*(.*)$"
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    # Find the first match where group(1) == item, then extract up to the next Item header.
    target_idx = None
    for i, m in enumerate(matches):
        if m.group(1).upper() == item:
            target_idx = i
            break
    if target_idx is None:
        return None
    start = matches[target_idx].start()
    # Next "real" Item header is the next match with a different number — skip TOC repeats
    # by requiring at least 500 chars of content before accepting it as the end boundary.
    end = len(text)
    for m in matches[target_idx + 1 :]:
        if m.start() - start > 500 and m.group(1).upper() != item:
            end = m.start()
            break
    section = text[start:end].strip()
    return section if len(section) > 200 else None


def tool_edgar_fetch(url: str, item: str | None = None) -> str:
    try:
        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": SEC_USER_AGENT},
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
        extracted = trafilatura.extract(html, include_comments=False, include_tables=True)
        if not extracted:
            return f"Could not extract readable content from {url}."
        if item:
            section = _extract_item(extracted, item)
            if section:
                if len(section) > EDGAR_FETCH_CHAR_LIMIT:
                    section = section[:EDGAR_FETCH_CHAR_LIMIT] + "\n\n[...truncated]"
                return f"[EDGAR Item {item.upper()} from {url}]\n\n{section}"
            # Fallback: return truncated full text with warning
            full = extracted
            if len(full) > EDGAR_FETCH_CHAR_LIMIT:
                full = full[:EDGAR_FETCH_CHAR_LIMIT] + "\n\n[...truncated]"
            return (
                f"[Section extraction failed for Item {item} — returning full filing text]\n\n"
                + full
            )
        if len(extracted) > EDGAR_FETCH_CHAR_LIMIT:
            extracted = extracted[:EDGAR_FETCH_CHAR_LIMIT] + "\n\n[...truncated]"
        return extracted
    except Exception as e:
        return f"Error fetching EDGAR filing {url}: {e}"


def tool_python_repl(code: str) -> str:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=PYTHON_REPL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Error: python_repl timed out after {PYTHON_REPL_TIMEOUT}s."
    except Exception as e:
        return f"Error invoking python_repl: {e}"

    out = proc.stdout or ""
    err = proc.stderr or ""
    if len(out) > PYTHON_REPL_OUTPUT_LIMIT:
        out = out[:PYTHON_REPL_OUTPUT_LIMIT] + "\n[...stdout truncated]"
    if proc.returncode != 0:
        return f"[exit {proc.returncode}]\nstdout:\n{out}\nstderr:\n{err}"
    if err:
        return f"stdout:\n{out}\nstderr:\n{err}"
    return out if out else "(no output)"


def run_tool(tavily: TavilyClient, name: str, tool_input: dict) -> str:
    if name == "web_search":
        return tool_web_search(
            tavily,
            tool_input["query"],
            tool_input.get("max_results", 5),
        )
    if name == "fetch_url":
        return tool_fetch_url(tool_input["url"])
    if name == "edgar_search":
        return tool_edgar_search(tool_input["ticker"], tool_input["form_type"])
    if name == "edgar_fetch":
        return tool_edgar_fetch(tool_input["url"], tool_input.get("item"))
    if name == "python_repl":
        return tool_python_repl(tool_input["code"])
    return f"Unknown tool: {name}"


# ---------- Agent loop ----------

def _prefetch_subject_docs(ticker: str) -> list[dict]:
    """Pre-fetch the subject company's predictable foundational documents in
    parallel before Phase 1 starts. Returns a list of synthetic messages
    (one assistant + N tool results) to inject into the conversation so the
    model starts Phase 1 with these docs already in context.

    The deterministic prerequisites (Yahoo Finance pages + EDGAR submissions
    searches for the standard form types) are the same for every initiation
    report. Running them sequentially inside the agent loop costs ~20s/call
    of inter-batch model thinking time; running them in parallel up front
    costs ~3-5s wall and gives the model the data immediately.
    """
    from concurrent.futures import ThreadPoolExecutor

    ticker_u = ticker.upper()
    tasks = [
        ("fetch_url",    {"url": f"https://finance.yahoo.com/quote/{ticker_u}/"}),
        ("fetch_url",    {"url": f"https://finance.yahoo.com/quote/{ticker_u}/key-statistics/"}),
        ("fetch_url",    {"url": f"https://finance.yahoo.com/quote/{ticker_u}/financials/"}),
        ("fetch_url",    {"url": f"https://finance.yahoo.com/quote/{ticker_u}/analysis/"}),
        ("edgar_search", {"ticker": ticker_u, "form_type": "10-K"}),
        ("edgar_search", {"ticker": ticker_u, "form_type": "20-F"}),
        ("edgar_search", {"ticker": ticker_u, "form_type": "10-Q"}),
        ("edgar_search", {"ticker": ticker_u, "form_type": "6-K"}),
    ]

    typer.echo(
        f"[prefetch] fanning out {len(tasks)} predictable subject-company fetches in parallel...",
        err=True,
    )
    start = time.monotonic()

    def _run_one(task: tuple[str, dict]):
        tool_name, tool_args = task
        t0 = time.monotonic()
        try:
            if tool_name == "fetch_url":
                r = tool_fetch_url(tool_args["url"])
            elif tool_name == "edgar_search":
                r = tool_edgar_search(tool_args["ticker"], tool_args["form_type"])
            else:
                r = f"unknown prefetch tool: {tool_name}"
        except Exception as e:
            r = f"prefetch error ({type(e).__name__}): {e}"
        return tool_name, tool_args, r, time.monotonic() - t0

    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        results = list(ex.map(_run_one, tasks))

    elapsed = time.monotonic() - start

    for tool_name, tool_args, _, t in results:
        if tool_name == "fetch_url":
            short = tool_args["url"].split("/quote/", 1)[-1].strip("/") or "quote"
        else:
            short = f"form={tool_args.get('form_type','?')}"
        typer.echo(f"[prefetch]   {tool_name:14s} {short:30s} took {t:.1f}s", err=True)
    typer.echo(
        f"[prefetch] done in {elapsed:.1f}s wall (vs ~{len(tasks) * 20}s if requested sequentially in the agent loop)",
        err=True,
    )

    # Build the synthetic assistant + tool messages
    tc_meta = []
    tool_msgs: list[dict] = []
    for i, (tool_name, tool_args, result, _) in enumerate(results):
        tc_id = f"prefetch_{i:02d}"
        tc_meta.append({
            "id": tc_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
        })
        tool_msgs.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": result,
        })

    assistant_msg = {
        "role": "assistant",
        "content": (
            "Starting by pre-fetching the foundational documents for this ticker in parallel: "
            "EDGAR submissions searches for forms 10-K, 20-F, 10-Q, and 6-K (covering both "
            "US-domestic and foreign-private-issuer schemas), plus Yahoo Finance "
            "quote / key-statistics / financials / analysis pages for the subject company. "
            "These are deterministic prerequisites for any initiation report; the rest of Phase 1 "
            "will focus on peer data, transcripts, TAM, catalysts, and Wall Street consensus."
        ),
        "tool_calls": tc_meta,
    }

    return [assistant_msg] + tool_msgs


def _log_loop_timing(loop_start: float, tool_stats: dict, tool_calls_used: int) -> None:
    """Print a per-tool timing breakdown to stderr at the end of an agentic_loop run."""
    total = time.monotonic() - loop_start
    typer.echo(
        f"\n[timing] total wall time: {total/60:.1f} min ({total:.0f}s); {tool_calls_used} tool calls",
        err=True,
    )
    for name in sorted(tool_stats, key=lambda n: -tool_stats[n]["total_seconds"]):
        s = tool_stats[name]
        avg = s["total_seconds"] / max(s["count"], 1)
        typer.echo(
            f"[timing]   {name:14s}: {s['count']:3d} calls, {s['total_seconds']:6.1f}s total, {avg:5.1f}s avg",
            err=True,
        )


def agentic_loop(
    client: OpenAI,
    tavily: TavilyClient,
    messages: list[dict],
    tools: list[dict],
    max_tool_calls: int,
    verbose: bool,
    model_cfg: dict,
) -> tuple[str, int]:
    """Run the agentic tool-use loop until the model emits a final text response.
    Returns (assembled_output_text, tool_calls_used). The tool-call count lets
    callers enforce minimum-use policies (e.g., research rejects 0-call drafts
    as likely hallucinated). Per-tool timings are logged to stderr, and a
    breakdown by tool name is printed at the end."""
    tool_calls_used = 0
    python_repl_calls_used = 0  # tracked separately for the Phase 2 hard cap (see PHASE2_REPL_CAP)
    output_parts: list[str] = []
    loop_start = time.monotonic()
    tool_stats: dict = collections.defaultdict(lambda: {"count": 0, "total_seconds": 0.0})

    while True:
        stream = client.chat.completions.create(
            model=model_cfg["id"],
            messages=messages,
            tools=tools,
            tool_choice="auto",
            stream=True,
            extra_body=model_cfg["extra_body"],
        )

        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason = None

        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta.content:
                    sys.stdout.write(delta.content)
                    sys.stdout.flush()
                    content_parts.append(delta.content)
                    output_parts.append(delta.content)

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        slot = tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["name"] += tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments

                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
            # Connection dropped mid-stream. If the model already signalled "stop"
            # and we have output, treat as success — the report is complete.
            if finish_reason == "stop" and output_parts:
                typer.echo(f"\n[note: stream connection dropped after completion — {type(e).__name__}]", err=True)
            else:
                typer.echo(f"\n[stream error: {type(e).__name__}: {e}]", err=True)
                _log_loop_timing(loop_start, tool_stats, tool_calls_used)
                return "".join(output_parts), tool_calls_used

        if finish_reason == "stop":
            sys.stdout.write("\n")
            _log_loop_timing(loop_start, tool_stats, tool_calls_used)
            return "".join(output_parts), tool_calls_used

        if finish_reason != "tool_calls":
            typer.echo(f"\n\n[stopped with reason: {finish_reason}]", err=True)
            _log_loop_timing(loop_start, tool_stats, tool_calls_used)
            return "".join(output_parts), tool_calls_used

        assistant_tool_calls = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            for tc in tool_calls.values()
        ]
        messages.append(
            {
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": assistant_tool_calls,
            }
        )

        for tc in tool_calls.values():
            if tool_calls_used >= max_tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"Tool call budget exhausted ({max_tool_calls}). Write the report with what you have.",
                    }
                )
                continue

            tool_calls_used += 1
            try:
                tool_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError as e:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"Invalid JSON arguments: {e}",
                    }
                )
                continue

            if verbose:
                typer.echo(
                    f"\n[tool #{tool_calls_used}] {tc['name']}({json.dumps(tool_input)})",
                    err=True,
                )

            # Hard cap on Phase 2 python_repl iteration. Without this, the model
            # debugs its own code errors indefinitely — the prompt's "AT MOST 3"
            # is just a hint. See NTES re-test (June 2, 2026) where the model
            # ran 6 python_repl iterations, eating ~15 min on code-error rework.
            if tc["name"] == "python_repl" and python_repl_calls_used >= PHASE2_REPL_CAP:
                cap_msg = (
                    f"Phase 2 python_repl budget exhausted ({PHASE2_REPL_CAP} calls already used). "
                    "Do NOT call python_repl again — additional calls will return this same message. "
                    "Proceed directly to Phase 3 (synthesis) and write the report with the data you "
                    "already have. If a metric came out wrong in an earlier python_repl call, footnote "
                    "the issue in the Open Questions section — do not iterate further."
                )
                typer.echo(
                    f"[T+{time.monotonic() - loop_start:6.1f}s] tool#{tool_calls_used:>3d} python_repl    CAPPED (>= {PHASE2_REPL_CAP} prior calls)",
                    err=True,
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": cap_msg}
                )
                continue

            if tc["name"] == "python_repl":
                python_repl_calls_used += 1

            tool_start = time.monotonic()
            result = run_tool(tavily, tc["name"], tool_input)
            tool_elapsed = time.monotonic() - tool_start
            stats = tool_stats[tc["name"]]
            stats["count"] += 1
            stats["total_seconds"] += tool_elapsed
            typer.echo(
                f"[T+{time.monotonic() - loop_start:6.1f}s] tool#{tool_calls_used:>3d} {tc['name']:14s} took {tool_elapsed:5.1f}s",
                err=True,
            )

            if verbose:
                # Truncate long results so the log stays readable; python_repl
                # outputs are capped at 4KB so they nearly always fit, while
                # fetch_url / edgar_fetch can return up to 50K.
                limit = 1500
                snippet = result if len(result) <= limit else result[:limit] + f"\n... [truncated; {len(result)} total chars]"
                typer.echo(f"  -> {snippet}", err=True)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )


# ---------- Phase 4: code-side review + targeted revision ----------
#
# After Phase 3 produces a draft, run deterministic checks (C1-C6) against the
# markdown. If any fire, ask the model for a TARGETED revision that fixes only
# the listed findings — not a full rewrite. Saves the revised version.
# Cost: $0 when no findings, ~$0.20 + 3-5 min when findings exist.


def _section_text(draft: str, name: str, level: int = 2) -> str:
    """Return the text of a `{level}` markdown section by its heading text.
    Slice from the heading to the next same-level heading or EOF."""
    hashes = "#" * level
    pattern = rf"^{hashes}\s+{re.escape(name)}.*?(?=^{hashes}\s|\Z)"
    m = re.search(pattern, draft, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""


def _check_c1_lfy(draft: str, today: datetime.date) -> dict | None:
    """C1 — LFY column year in Financial Summary. Most LFY-bug cases produce
    `FY{today.year - 2}` in the latest-historical slot when the right answer is
    `FY{today.year - 1}` (or close to it for non-calendar FY companies)."""
    fp = _section_text(draft, "Financial Profile")
    fs_match = re.search(r"###\s+Financial Summary.*?\n(\|[^\n]+\|)", fp, re.DOTALL)
    if not fs_match:
        return None
    header = fs_match.group(1)
    fy_years = [int(m.group(1)) for m in re.finditer(r"FY\s*(\d{4})", header)]
    if not fy_years:
        return None
    lfy_year = max(fy_years)
    expected_min = today.year - 1
    if lfy_year < expected_min:
        return {
            "check_id": "C1",
            "severity": "HIGH",
            "fix": (
                f"Financial Summary LFY column shows FY{lfy_year}; expected at least "
                f"FY{expected_min} based on today's date ({today.isoformat()}). The most "
                f"recent COMPLETED fiscal year for a calendar-FY company is FY{expected_min} "
                f"(and for non-calendar companies the latest completed FY is almost always "
                f"FY{expected_min} as well by mid-{today.year}). MOVE FY{expected_min} numbers "
                f"into the LFY column — pull them from the press release / 6-K / earnings "
                f"call if the annual 10-K/20-F hasn't been filed yet — and SHIFT the prior-FY "
                f"column to FY{expected_min - 1}. The intro paragraph and Quality-of-Earnings "
                f"notes likely already cite the FY{expected_min} figures; use those."
            ),
        }
    return None


_NUM_RE = re.compile(r"^([-–]?)\$?([\d.,]+)([BMK]?)")


def _parse_dollar_signed(s: str) -> float | None:
    """Parse `$1.57`, `-$1.57`, `–$1.57`, `$3.2B`, etc. Returns signed float in raw units."""
    s = s.strip().replace(",", "")
    if "NM" in s or not s:
        return None
    m = _NUM_RE.match(s)
    if not m:
        return None
    sign = -1 if m.group(1) in ("-", "–") else 1
    val = float(m.group(2))
    mult = {"B": 1e9, "M": 1e6, "K": 1e3, "": 1.0}[m.group(3)]
    return sign * val * mult


def _parse_multiple(s: str) -> float | None:
    """Parse `13.3x`, `13.3x (FY+1E)`, etc. Returns float or None for NM."""
    s = s.strip()
    if "NM" in s or not s:
        return None
    m = re.match(r"^([\d.]+)x", s)
    return float(m.group(1)) if m else None


def _check_c2_snapshot_recon(draft: str) -> list[dict]:
    """C2 — Trading Snapshot reconciliation. Verifies price/EPS arithmetic
    against the printed P/E, and NM consistency between EPS and P/E columns."""
    ts = _section_text(draft, "Trading Snapshot")
    if not ts:
        return []
    rows = [ln for ln in ts.splitlines() if ln.lstrip().startswith("|") and "---" not in ln]
    if len(rows) < 2:
        return []
    cells = [c.strip() for c in rows[1].strip("|").split("|")]
    # Snapshot column order: Price | Mkt Cap | 52W Range | Vol | Short Int |
    # EV/Rev (LTM) | EV/Rev (Fwd) | EPS (LTM) | EPS (Fwd) | P/E (LTM) | P/E (Fwd)
    if len(cells) < 11:
        return []
    price = _parse_dollar_signed(cells[0])
    eps_ltm = _parse_dollar_signed(cells[7])
    eps_fwd = _parse_dollar_signed(cells[8])
    pe_ltm = _parse_multiple(cells[9])
    pe_fwd = _parse_multiple(cells[10])

    findings = []
    # LTM reconciliation
    if price and eps_ltm and eps_ltm > 0 and pe_ltm:
        expected = price / eps_ltm
        if abs(expected - pe_ltm) / pe_ltm > 0.03:  # >3% off → flag
            findings.append({
                "check_id": "C2",
                "severity": "MEDIUM",
                "fix": (
                    f"Trading Snapshot P/E (LTM) printed as {pe_ltm:.1f}x but "
                    f"${price:.2f} ÷ ${eps_ltm:.2f} = {expected:.1f}x. Recompute and "
                    f"reconcile — either correct the P/E or correct the EPS."
                ),
            })
    # Fwd reconciliation
    if price and eps_fwd and eps_fwd > 0 and pe_fwd:
        expected = price / eps_fwd
        if abs(expected - pe_fwd) / pe_fwd > 0.03:
            findings.append({
                "check_id": "C2",
                "severity": "MEDIUM",
                "fix": (
                    f"Trading Snapshot P/E (Fwd) printed as {pe_fwd:.1f}x but "
                    f"${price:.2f} ÷ ${eps_fwd:.2f} = {expected:.1f}x. Recompute and "
                    f"reconcile — either correct the P/E or correct the forward EPS."
                ),
            })
    # NM consistency: negative LTM EPS but non-NM P/E (LTM)
    if eps_ltm is not None and eps_ltm <= 0 and "NM" not in cells[9]:
        findings.append({
            "check_id": "C2",
            "severity": "MEDIUM",
            "fix": (
                f"Trading Snapshot EPS (LTM) is {cells[7]} (non-positive) but "
                f"P/E (LTM) shows {cells[9]} — should be NM when EPS is non-positive."
            ),
        })
    return findings


def _check_c3_peer_median(draft: str) -> list[dict]:
    """C3 — Peer Comp Table median row: a column with <3 valid (non-NM) peer
    values must show `—` in the median cell, not an average of 2 values."""
    pct = re.search(
        r"###\s+Peer Comp Table.*?\n(\|[^\n]+\|)\n\|[\s\-:|]+\|\n((?:\|[^\n]+\|\n?)+)",
        draft,
        re.DOTALL,
    )
    if not pct:
        return []
    header_line = pct.group(1)
    body = pct.group(2)
    body_rows = [r for r in body.splitlines() if r.lstrip().startswith("|")]
    if len(body_rows) < 3:  # need at least subject + 1 peer + median
        return []

    parsed = []
    for r in body_rows:
        cells = [c.strip() for c in r.strip("|").split("|")]
        parsed.append(cells)

    # Find median row (last row containing "median")
    median_idx = None
    for i, row in enumerate(parsed):
        if any("median" in (c or "").lower() for c in row):
            median_idx = i
            break
    if median_idx is None:
        return []

    median_row = parsed[median_idx]
    # Subject is the first row; peers are rows 1..median_idx-1
    peer_rows = parsed[1:median_idx]
    if len(peer_rows) == 0:
        return []

    header_cells = [c.strip() for c in header_line.strip("|").split("|")]
    findings = []
    # Skip the first 3 columns (Company, EV, Rev) — not medianed normally
    for col in range(3, len(median_row)):
        median_cell = median_row[col]
        peer_cells = [(p[col] if col < len(p) else "") for p in peer_rows]
        valid = sum(
            1 for c in peer_cells
            if c and c not in ("NM", "—", "-", "") and re.search(r"\d", c)
        )
        is_blank = median_cell.strip("* ") in ("—", "-", "", "NM")
        if valid < 3 and not is_blank:
            col_name = header_cells[col] if col < len(header_cells) else f"column {col}"
            findings.append({
                "check_id": "C3",
                "severity": "MEDIUM",
                "fix": (
                    f"Peer Comp Table column \"{col_name}\" has only {valid} valid "
                    f"(non-NM) peer value(s) across {len(peer_rows)} peers, but the Median "
                    f"cell shows \"{median_cell}\" — HARD RULE requires \"—\" when fewer "
                    f"than 3 valid values exist. Replace with \"—\" and add or extend the "
                    f"\"insufficient peer coverage\" footnote to name this column."
                ),
            })
    return findings


def _check_c4_lens(draft: str) -> dict | None:
    """C4 — Forward valuation lens matches LTM profitability. Loss-making
    (negative/NM LTM EPS) → lead with forward EV/Revenue. Comfortably profitable
    → lead with forward P/E."""
    ts = _section_text(draft, "Trading Snapshot")
    if not ts:
        return None
    rows = [ln for ln in ts.splitlines() if ln.lstrip().startswith("|") and "---" not in ln]
    if len(rows) < 2:
        return None
    cells = [c.strip() for c in rows[1].strip("|").split("|")]
    if len(cells) < 11:
        return None
    eps_ltm_cell = cells[7]
    is_negative = eps_ltm_cell.startswith("-") or eps_ltm_cell.startswith("–")
    is_nm = "NM" in eps_ltm_cell
    is_loss_making = is_negative or is_nm

    vc = _section_text(draft, "Valuation Context")
    if not vc:
        return None
    # Take the first 2KB as "lead" — opening framing
    lead = vc[:2000].lower()
    leads_pe = "forward p/e" in lead
    leads_evrev = "forward ev/revenue" in lead or "forward ev/rev" in lead
    leads_evebitda = "forward ev/ebitda" in lead

    # Mismatch: loss-making but leads with P/E only (no EV/Rev or EV/EBITDA)
    if is_loss_making and leads_pe and not (leads_evrev or leads_evebitda):
        return {
            "check_id": "C4",
            "severity": "HIGH",
            "fix": (
                "Valuation Context leads with forward P/E but LTM EPS is non-positive "
                f"({eps_ltm_cell}). Per the Phase 3 rule, loss-making / marginally-profitable "
                "companies should lead with forward EV/Revenue (or forward EV/EBITDA where "
                "EBITDA is the cleaner industry metric). Re-open Valuation Context with "
                "FY+1E and FY+2E forward EV/Revenue framing and position vs the peer median; "
                "demote the forward P/E discussion to a secondary lens."
            ),
        }
    return None


_REQUIRED_SECTIONS_NEW = [
    "Thesis",
    "Business snapshot",
    "What the Street thinks",
    "Bull / Bear / Base",
    "Top 3 risks",
    "Catalyst calendar",
    "Appendix",
    "Open Questions",
    "Sources",
]

_REQUIRED_SECTIONS_LEGACY = [
    "Trading Snapshot",
    "Business Overview",
    "Financial Profile",
    "Sell-Side Q&A Analysis",
    "Market Opportunity",
    "Competitive Landscape",
    "Valuation Context",
    "Key Risks",
    "Investment Framework",
    "Open Questions",
    "Sources",
]

# Canonical Chinese translations of new-shape section/subsection headers.
# Locked in _LANG_INSTRUCTION_ZH; validators and the open-questions
# extractor accept either the English name or its Chinese alias.
_SECTION_NAMES_ZH = {
    # H2 section headers (new shape)
    "Thesis": "投资论点",
    "Business snapshot": "业务概览",
    "What the Street thinks": "市场观点",
    "Bull / Bear / Base": "看多 / 看空 / 基准情景",
    "Top 3 risks": "三大风险",
    "Catalyst calendar": "催化剂日历",
    "Appendix": "附录",
    "Open Questions": "未解决问题",
    "Sources": "数据来源",
    # H3 subsection headers used inside the appendix / debates
    "Street consensus": "市场一致预期",
    "Three debates": "三大争议",
    "Financial Summary": "财务摘要",
    "Competitive Landscape": "竞争格局",
    "Peer Comp Table": "同业估值比较",
}


def _check_c5_sections(draft: str, legacy: bool = False) -> list[dict]:
    """C5 — required H2 sections all present. The required list depends
    on which template produced the draft; legacy=True checks the
    11-section sell-side layout, default checks the 8-section thesis
    shape (plus Open Questions, which is emitted in the body and then
    extracted to frontmatter post-review).

    For the new shape, both the English heading text AND its locked
    Chinese alias (see _SECTION_NAMES_ZH) count as present — so a
    Chinese-output report with `## 投资论点` satisfies the `Thesis`
    requirement without needing a separate zh validator path."""
    required = _REQUIRED_SECTIONS_LEGACY if legacy else _REQUIRED_SECTIONS_NEW
    findings = []
    for name in required:
        if f"## {name}" in draft:
            continue
        # New shape only: accept the locked Chinese alias as equivalent.
        if not legacy:
            zh = _SECTION_NAMES_ZH.get(name)
            if zh and f"## {zh}" in draft:
                continue
        findings.append({
            "check_id": "C5",
            "severity": "HIGH",
            "fix": f"Required H2 section \"## {name}\" is missing. Add it with the content the section format calls for.",
        })
    return findings


def _check_c6_margin_sanity(draft: str) -> list[dict]:
    """C6 — Forward Estimates consensus table: gross margin ≥ EBITDA margin
    (EBITDA cannot exceed gross profit). Belt-suspenders for Phase 2 rule."""
    fp = _section_text(draft, "Financial Profile")
    fe_match = re.search(r"###\s+Forward Estimates.*?(?=^###\s|\Z)", fp, re.MULTILINE | re.DOTALL)
    if not fe_match:
        return []
    fe = fe_match.group(0)

    def find_margin_row(label: str) -> tuple[float | None, float | None]:
        m = re.search(
            rf"\|\s*{label}\s*\|\s*[~]?([\d.]+)\s*%?\s*\|\s*[~]?([\d.]+)\s*%?\s*\|",
            fe,
            re.IGNORECASE,
        )
        if not m:
            return None, None
        return float(m.group(1)), float(m.group(2))

    gm_y1, gm_y2 = find_margin_row(r"Gross Margin\s*%?")
    em_y1, em_y2 = find_margin_row(r"EBITDA Margin\s*%?")

    findings = []
    if gm_y1 is not None and em_y1 is not None and em_y1 > gm_y1 + 0.5:
        findings.append({
            "check_id": "C6",
            "severity": "HIGH",
            "fix": (
                f"Forward Estimates FY+1E EBITDA margin ({em_y1}%) exceeds gross margin "
                f"({gm_y1}%) — impossible. Recompute EBITDA, drop the row, or correct the gross margin."
            ),
        })
    if gm_y2 is not None and em_y2 is not None and em_y2 > gm_y2 + 0.5:
        findings.append({
            "check_id": "C6",
            "severity": "HIGH",
            "fix": (
                f"Forward Estimates FY+2E EBITDA margin ({em_y2}%) exceeds gross margin "
                f"({gm_y2}%) — impossible. Recompute EBITDA, drop the row, or correct the gross margin."
            ),
        })
    return findings


def _check_c7_citations(draft: str) -> list[dict]:
    """C7 — Citation completeness. Every [N] inline citation in the body
    must have a matching [N] entry in the Sources section, and the Sources
    section must not be truncated mid-entry (the highest-indexed source
    must contain a URL). Catches:
      - Body-vs-Sources index mismatch (KM-07 off-by-one regression)
      - Mid-stream truncation of the Sources list (DS-02 failure mode where
        DeepSeek's revision pass cut off at "[1] Marvell ... (filed March")"""
    findings: list[dict] = []

    if "## Sources" not in draft:
        # C5 already catches a missing Sources section; don't double-flag.
        return findings
    body, sources = draft.rsplit("## Sources", 1)

    inline_indices = {int(n) for n in re.findall(r"\[(\d+)\]", body)}
    entry_indices = {int(n) for n in re.findall(r"^\s*\[(\d+)\]", sources, re.MULTILINE)}

    # Body has substantial content but cites nothing — the model didn't
    # follow the citation contract. (Observed: DeepSeek MRVL Phase 3 draft
    # produced 4,800+ words with zero [N] citations.)
    if len(body.split()) > 500 and not inline_indices:
        findings.append({
            "check_id": "C7",
            "severity": "HIGH",
            "fix": (
                "Body contains substantial content but no [N] inline citations. "
                "Every factual claim about the subject or peers must end with a "
                "[N] reference that resolves to an entry in the Sources section. "
                "Re-emit the body with inline citations attached to every sourced claim."
            ),
        })

    # Body-vs-Sources index mismatch: every [N] cited must have a [N] entry.
    missing = inline_indices - entry_indices
    if missing:
        findings.append({
            "check_id": "C7",
            "severity": "HIGH",
            "fix": (
                f"Citation indices {sorted(missing)} appear in the body as [N] "
                "but have no matching entry in the Sources section. Add a source "
                "line — Title — Publication — Date — URL — for each missing index."
            ),
        })

    # Truncation: the highest-numbered source entry must contain a URL.
    # The DS-02 failure mode was the entire Sources list cut off mid-source-1.
    if entry_indices:
        last_idx = max(entry_indices)
        # Capture the text of the [last_idx] entry up to the next "[N]" line
        # start or end of section.
        m = re.search(
            rf"\[{last_idx}\][^\n]*(?:\n(?!\s*\[\d+\]).*)*",
            sources, re.MULTILINE,
        )
        if m and not re.search(r"https?://[^\s)\]]+", m.group(0)):
            findings.append({
                "check_id": "C7",
                "severity": "HIGH",
                "fix": (
                    f"Source entry [{last_idx}] appears truncated — it contains no URL. "
                    "Each source line must end with the URL the claim was actually "
                    "fetched from. Re-emit the entire Sources list completely."
                ),
            })

    return findings


# ---------- Financial-table parsing helper (shared by C8 / C9 / C10) ----------

def _find_section(draft: str, name: str) -> str:
    """Find a section by name at H2 OR H3 level. The new shape puts
    Financial Summary / Peer Comp Table as H3 inside Appendix; legacy
    puts Financial Summary as H3 inside Financial Profile. Either way,
    locating by name without committing to a heading level avoids
    coupling these checks to layout.

    Falls back to the locked Chinese alias (see _SECTION_NAMES_ZH) when
    the English heading isn't present — so a Chinese-output report with
    `### 财务摘要` is found by `_find_section(draft, "Financial Summary")`."""
    for level in (2, 3):
        text = _section_text(draft, name, level=level)
        if text:
            return text
    # Chinese alias fallback
    zh = _SECTION_NAMES_ZH.get(name)
    if zh:
        for level in (2, 3):
            text = _section_text(draft, zh, level=level)
            if text:
                return text
    return ""


def _parse_table_row(section: str, label: str) -> list[float | None]:
    """Extract numeric cells from a markdown table row whose first cell
    matches `label`. Handles $ B M %, parens-for-negative accounting,
    NA / NM / —, plus mixed cells like `$11.52B (+41% YoY)` by extracting
    just the leading numeric token."""
    pattern = rf"\|\s*{re.escape(label)}[^|]*\|(.+?)\|\s*$"
    m = re.search(pattern, section, re.MULTILINE)
    if not m:
        return []
    cells = [c.strip() for c in m.group(1).split("|")]
    vals: list[float | None] = []
    for raw in cells:
        # Strip markdown decoration first
        cleaned = raw.replace("**", "").replace("*", "").replace(",", "").strip()
        # Accounting-style negative: (1.23) at start of cell -> -1.23
        m_neg = re.match(r"^\$?\(\s*(\d+(?:\.\d+)?)\s*\)", cleaned)
        if m_neg:
            try:
                vals.append(-float(m_neg.group(1)))
                continue
            except ValueError:
                pass
        # Otherwise: first signed decimal anywhere in the cell. Skips $
        # prefix, ignores trailing "(+41% YoY)" style suffixes, ignores
        # currency / scale letters (B, M, %) — we just take the leading
        # numeric magnitude.
        m_num = re.search(r"(-?\d+(?:\.\d+)?)", cleaned)
        if m_num:
            try:
                vals.append(float(m_num.group(1)))
                continue
            except ValueError:
                pass
        vals.append(None)
    return vals


# ---------- C8 / C9 / C10: validators tuned for the new (8-section) shape ----------

def _check_c8_eps_footing(draft: str) -> list[dict]:
    """C8 — for each Financial Summary column, recompute implied diluted
    share count = Net Income / |Diluted EPS|. Assert the implied count is
    consistent across columns (within ~50% to accommodate one-time-item
    noise on the LFY column). Catches the 10x-EPS error class observed on
    the MRVL 2026-06-15 v0: FY24 NI=$0.39B with EPS=$0.05 implies 7,800M
    shares against a real diluted count of ~865M."""
    findings: list[dict] = []
    section = _find_section(draft, "Financial Summary")
    if not section:
        return findings

    ni = _parse_table_row(section, "Net Income")
    eps = _parse_table_row(section, "Diluted EPS")
    if not ni or not eps:
        return findings

    # NI in $B (typical), EPS in $; implied shares in M = (NI / |EPS|) * 1000
    implied: list[float] = []
    for n, e in zip(ni, eps):
        if n is None or e is None or e == 0:
            continue
        implied.append(abs(n / e) * 1000)

    if len(implied) < 2:
        return findings

    spread = max(implied) / min(implied)
    if spread > 1.5:
        findings.append({
            "check_id": "C8",
            "severity": "HIGH",
            "fix": (
                f"Financial Summary EPS does not foot: implied diluted share count varies "
                f"from {min(implied):.0f}M to {max(implied):.0f}M across columns ({spread:.1f}x spread). "
                "Recompute EPS = Net Income / diluted shares using a SINGLE share count source. "
                "The most common cause is a loss-year EPS shown ~10x too small "
                "(e.g., ($0.10) where ($1.00) is correct). Check the negative-EPS rows first."
            ),
        })
    return findings


def _check_c9_peer_ltm_outlier(draft: str) -> list[dict]:
    """C9 — for each peer in the Peer Comp Table, flag when EV/Rev (LTM)
    is materially above the peer median. The most common cause is a
    single-quarter revenue value mislabeled as TTM (NVDA shown at 60.5x
    EV/Rev (LTM) on the MRVL 2026-06-15 v0 because $81.6B was Q1 FY27
    single-quarter, not real TTM ~$253B)."""
    findings: list[dict] = []
    section = _find_section(draft, "Peer Comp Table")
    if not section:
        return findings

    # Find the table header row to locate the EV/Rev (LTM) column index.
    header_re = re.compile(r"^\|\s*Company\s*\|(.+?)\|\s*$", re.MULTILINE)
    hm = header_re.search(section)
    if not hm:
        return findings
    headers = [h.strip() for h in hm.group(1).split("|")]
    target_idx = next(
        (i for i, h in enumerate(headers) if re.search(r"EV/Rev\s*\(LTM", h, re.I)),
        None,
    )
    if target_idx is None:
        return findings

    # Walk subsequent rows; collect (name, ev_rev_ltm) tuples.
    rows: list[tuple[str, float]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line or line == hm.group(0):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < target_idx + 2:
            continue
        name = cells[0].replace("**", "").strip()
        if not name or name.lower() == "company":
            continue
        if name.lower() == "median" or "median" in name.lower():
            continue
        raw = cells[target_idx + 1].replace("x", "").replace("**", "").strip()
        try:
            val = float(raw)
        except ValueError:
            continue
        rows.append((name, val))

    if len(rows) < 3:
        return findings

    # Compute peer median (exclude row 0 = subject by convention).
    peer_vals = sorted(v for _, v in rows[1:])
    n = len(peer_vals)
    median = (
        peer_vals[n // 2]
        if n % 2
        else (peer_vals[n // 2 - 1] + peer_vals[n // 2]) / 2
    )

    for name, val in rows[1:]:
        if median > 0 and val > 2 * median:
            findings.append({
                "check_id": "C9",
                "severity": "HIGH",
                "fix": (
                    f"Peer Comp Table: {name} shown at {val:.1f}x EV/Rev (LTM) — more than 2x "
                    f"the peer median of {median:.1f}x. The most common cause is a single-quarter "
                    f"revenue mislabeled as TTM; verify by computing TTM = sum of last 4 reported "
                    f"quarter revenues. If the LTM value is wrong, every {name} multiple in the row "
                    f"is wrong and must be recomputed."
                ),
            })
    return findings


def _check_c10_ebitda_basis(draft: str) -> list[dict]:
    """C10 — for each Financial Summary column, assert EBITDA > Net Income.
    A column where EBITDA roughly equals or is below Net Income signals a
    basis mismatch (e.g., NI includes a one-time gain that EBITDA excludes,
    leaving the two on incompatible bases) — observed on MRVL 2026-06-15
    v0 where FY26 EBITDA $2.71B sits just above NI $2.67B because NI
    includes the $1.8B gain and EBITDA does not."""
    findings: list[dict] = []
    section = _find_section(draft, "Financial Summary")
    if not section:
        return findings

    ebitda = _parse_table_row(section, "EBITDA")
    if not ebitda:
        ebitda = _parse_table_row(section, "Adjusted EBITDA")
    ni = _parse_table_row(section, "Net Income")
    if not ebitda or not ni:
        return findings

    for col_idx, (e, n) in enumerate(zip(ebitda, ni)):
        if e is None or n is None:
            continue
        if e <= 0 or n <= 0:
            # Loss-year rows: EBITDA can plausibly be below NI absolute value
            # in edge cases (impairment writedown impacts NI but not EBITDA).
            # Skip the check on negative rows.
            continue
        if n > 0.95 * e:
            findings.append({
                "check_id": "C10",
                "severity": "MEDIUM",
                "fix": (
                    f"Financial Summary column {col_idx + 1}: Net Income (${n:.2f}B) is more than "
                    f"95% of EBITDA (${e:.2f}B) — implies D&A + Interest + Tax under 5% of EBITDA, "
                    f"which is structurally unlikely. Most common cause: NI includes a one-time gain "
                    f"that EBITDA excludes, leaving the two on incompatible bases. State the EBITDA "
                    f"basis explicitly (GAAP vs Adjusted; with vs without one-time items) and ensure "
                    f"NI and EBITDA on the same row share basis."
                ),
            })
    return findings


def _check_c11_peer_comp_columns(draft: str) -> list[dict]:
    """C11 — Peer Comp Table must have all 10 required columns. The new
    template was specifically designed so thin consensus coverage is
    handled by NM-filling cells, not by dropping columns — but the model
    has been observed compressing forwards into a single 'Fwd P/E' column
    (MU 2026-06-16 v0.2 shipped with only 3 multiple columns instead of
    6). This check fires when the table is shorter than spec."""
    findings: list[dict] = []
    section = _find_section(draft, "Peer Comp Table")
    if not section:
        return findings

    header_re = re.compile(r"^\|\s*(Company|公司)\s*\|(.+?)\|\s*$", re.MULTILINE)
    hm = header_re.search(section)
    if not hm:
        return findings
    headers = [h.strip() for h in hm.group(2).split("|")]
    n_cols = len(headers) + 1  # +1 for the Company/公司 column

    if n_cols < 10:
        # List what's missing — checking for the three forward multiple columns
        # that are most often dropped.
        joined = " | ".join(headers).lower()
        missing = []
        if "ev/rev (fy+1e)" not in joined and "fy+1e" not in joined.replace("p/e", ""):
            missing.append("EV/Rev (FY+1E)")
        if "ev/rev (fy+2e)" not in joined and not re.search(r"fy\+2", joined):
            missing.append("EV/Rev (FY+2E)")
        if not re.search(r"p/e\s*\(fy\+2e\)", joined):
            missing.append("P/E (FY+2E)")
        missing_str = ", ".join(missing) if missing else "(one or more of the FY+1E / FY+2E forward columns)"
        findings.append({
            "check_id": "C11",
            "severity": "HIGH",
            "fix": (
                f"Peer Comp Table has only {n_cols} columns (spec requires 10). "
                f"Likely missing: {missing_str}. The spec requires all 10 columns "
                f"(Company | EV | Rev | 2-yr Rev CAGR | EV/Rev LTM/FY+1E/FY+2E | "
                f"P/E LTM/FY+1E/FY+2E). Use NM for cells where consensus isn't "
                f"available — do NOT drop columns. The peer median rule (<3 valid "
                f"values → \"—\") handles thin coverage gracefully without "
                f"changing the table shape."
            ),
        })
    return findings


def _check_c12_ltm_cross_check(draft: str) -> list[dict]:
    """C12 — for each row in Financial Summary, LTM should not be more than
    2.5× LFY (suggesting a single recent quarter was annualized). On the
    MU 2026-06-16 v0.2 run, LTM Net Income was $32B against FY25 LFY of
    $8.5B (3.76x) — likely Q2 FY26 NI of ~$7.5B × 4. Real LTM ending Feb
    2026 was ~$19B."""
    findings: list[dict] = []
    section = _find_section(draft, "Financial Summary")
    if not section:
        return findings

    # Check the most-watched rows for a giant LTM/LFY jump.
    # Use multiple label aliases (English + locked Chinese) so this works
    # in either language.
    row_labels = {
        "Revenue": ["Revenue", "营收", "营业收入"],
        "Net Income": ["Net Income", "净利润"],
        "Free Cash Flow": ["Free Cash Flow", "FCF", "自由现金流"],
        "Diluted EPS": ["Diluted EPS", "摊薄EPS"],
    }

    for canon, aliases in row_labels.items():
        cells: list[float | None] = []
        for alias in aliases:
            cells = _parse_table_row(section, alias)
            if cells:
                break
        if not cells or len(cells) < 2:
            continue
        # Identify LFY (second-to-last) and LTM (last) — Financial Summary
        # convention is: prior FY | LFY | LTM (or prior FY | prior FY | LFY | LTM
        # with the 4-column variant).
        lfy_val = cells[-2]
        ltm_val = cells[-1]
        if lfy_val is None or ltm_val is None or lfy_val <= 0 or ltm_val <= 0:
            # Skip loss-years — the ratio check is only meaningful when both
            # values are positive.
            continue
        ratio = ltm_val / lfy_val
        if ratio > 2.5:
            findings.append({
                "check_id": "C12",
                "severity": "HIGH",
                "fix": (
                    f"Financial Summary {canon}: LTM (${ltm_val:.2f}B) is {ratio:.1f}x "
                    f"LFY (${lfy_val:.2f}B). A {ratio:.1f}x jump in 1-2 quarters of TTM "
                    f"roll is structurally implausible — the most common cause is "
                    f"annualizing a single recent strong quarter (e.g., Q2 NI × 4) "
                    f"instead of summing the last 4 reported quarters. Recompute LTM "
                    f"as sum of the last 4 quarterly disclosures and cite the source. "
                    f"If a one-time gain caused a legitimate jump, footnote it."
                ),
            })
    return findings


def _check_c13_cagr_validation(draft: str) -> list[dict]:
    """C13 — recompute 2-yr Rev CAGR from LFY revenue (Financial Summary)
    and FY+2E consensus revenue (Street consensus table), and compare to
    the stated value in the Business snapshot or Peer Comp Table. Flag
    if the diff is more than 3 percentage points. Catches the MU
    2026-06-16 case where stated CAGR was 10.6% but recomputed from
    LFY $37.4B → FY+2E $112B = (112/37.4)^0.5 - 1 = 73%."""
    findings: list[dict] = []
    fs = _find_section(draft, "Financial Summary")
    sc = _find_section(draft, "Street consensus")
    if not fs or not sc:
        return findings

    # LFY revenue = second-to-last column of Revenue row in Financial Summary
    rev_cells: list[float | None] = []
    for alias in ("Revenue", "营收", "营业收入"):
        rev_cells = _parse_table_row(fs, alias)
        if rev_cells:
            break
    if not rev_cells or len(rev_cells) < 2:
        return findings
    lfy_rev = rev_cells[-2]
    if lfy_rev is None or lfy_rev <= 0:
        return findings

    # FY+2E revenue = second cell of Revenue row in Street consensus
    sc_cells: list[float | None] = []
    for alias in ("Revenue", "营收"):
        sc_cells = _parse_table_row(sc, alias)
        if sc_cells:
            break
    if not sc_cells or len(sc_cells) < 2:
        return findings
    fy2e_rev = sc_cells[1]
    if fy2e_rev is None or fy2e_rev <= 0:
        return findings

    implied_cagr = ((fy2e_rev / lfy_rev) ** 0.5 - 1) * 100

    # Extract stated 2-yr CAGR from the Business snapshot 5-column
    # table — locate the CAGR column header, then read the cell at the
    # same index in the subsequent data row. This avoids matching debate
    # thresholds like "increase by 250%" that contain growth-rate words
    # without being the canonical stated CAGR.
    snapshot = _find_section(draft, "Business snapshot")
    if not snapshot:
        return findings

    stated: float | None = None
    snapshot_lines = snapshot.splitlines()
    for i, line in enumerate(snapshot_lines):
        if not line.strip().startswith("|"):
            continue
        # Look for a header row containing CAGR (English or Chinese)
        if not re.search(r"CAGR", line, re.IGNORECASE):
            continue
        headers = [h.strip() for h in line.strip().strip("|").split("|")]
        cagr_idx = next(
            (j for j, h in enumerate(headers) if re.search(r"CAGR", h, re.I)),
            None,
        )
        if cagr_idx is None:
            continue
        # Walk forward to the first data row (not separator, not blank)
        for next_line in snapshot_lines[i + 1:]:
            ln = next_line.strip()
            if not ln.startswith("|") or "---" in ln:
                continue
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if cagr_idx < len(cells):
                m_pct = re.search(r"(-?\d+(?:\.\d+)?)\s*%", cells[cagr_idx])
                if m_pct:
                    try:
                        stated = float(m_pct.group(1))
                    except ValueError:
                        stated = None
            break
        if stated is not None:
            break

    if stated is None:
        return findings

    diff = abs(stated - implied_cagr)
    if diff > 3.0:
        findings.append({
            "check_id": "C13",
            "severity": "HIGH",
            "fix": (
                f"Stated 2-yr Rev CAGR ({stated:.1f}%) does not match the value "
                f"implied by LFY revenue (${lfy_rev:.2f}B) and FY+2E consensus "
                f"revenue (${fy2e_rev:.2f}B) — recomputed CAGR = "
                f"(${fy2e_rev:.2f}/{lfy_rev:.2f})^(1/2) - 1 = {implied_cagr:.1f}%. "
                f"Diff = {diff:.1f}pp. Recompute the CAGR using LFY actual (from "
                f"Financial Summary) and FY+2E consensus (from Street consensus "
                f"table) as the two endpoints. Update both the Business snapshot "
                f"table and the Peer Comp Table subject row."
            ),
        })
    return findings


def _review_draft(
    draft: str,
    ticker: str,
    today: datetime.date,
    legacy: bool = False,
) -> list[dict]:
    """Run Phase 4 checks against the draft. Returns a list of findings;
    empty list = clean.

    For the legacy 11-section initiation, all C1-C7 checks run — they were
    tuned against that layout. For the new 8-section initiate, only the
    shape-agnostic checks (C5 with the new section list, C7 citation
    completeness) fire. C1-C4 and C6 were tuned to legacy headings
    ("Trading Snapshot", "Financial Summary" as H2, "Forward Estimates"
    as H3 inside Financial Profile) and would produce noise on the new
    shape; they get re-added once we see what actually fails on the new
    layout in production."""
    findings: list[dict] = []
    if legacy:
        c1 = _check_c1_lfy(draft, today)
        if c1:
            findings.append(c1)
        findings.extend(_check_c2_snapshot_recon(draft))
        findings.extend(_check_c3_peer_median(draft))
        c4 = _check_c4_lens(draft)
        if c4:
            findings.append(c4)
        findings.extend(_check_c5_sections(draft, legacy=True))
        findings.extend(_check_c6_margin_sanity(draft))
    else:
        findings.extend(_check_c5_sections(draft, legacy=False))
        # New-shape-only validators (C8-C13 parse the new Financial
        # Summary + Peer Comp Table layout inside Appendix at H3).
        findings.extend(_check_c8_eps_footing(draft))
        findings.extend(_check_c9_peer_ltm_outlier(draft))
        findings.extend(_check_c10_ebitda_basis(draft))
        findings.extend(_check_c11_peer_comp_columns(draft))
        findings.extend(_check_c12_ltm_cross_check(draft))
        findings.extend(_check_c13_cagr_validation(draft))
    findings.extend(_check_c7_citations(draft))
    return findings


def _log_review(findings: list[dict]) -> None:
    """Print review findings to stderr."""
    if not findings:
        typer.echo("[review] no issues found in Phase 3 draft", err=True)
        return
    typer.echo(f"[review] {len(findings)} issue(s) found in Phase 3 draft:", err=True)
    for f in findings:
        # Single-line summary; full fix text goes into the revision prompt
        head = f["fix"].split(". ")[0][:200]
        typer.echo(f"[review]   [{f['check_id']} {f['severity']:6s}] {head}", err=True)


def _revise_draft(
    client: OpenAI,
    messages: list[dict],
    draft: str,
    findings: list[dict],
    ticker: str,
    model_cfg: dict,
) -> str:
    """Targeted revision: ask the model to fix ONLY the listed findings and
    output the full revised report. Falls back to the original draft if the
    revision looks broken (too short, dropped headings, stream errored)."""
    findings_block = "\n".join(
        f"- [{f['check_id']} {f['severity']}] {f['fix']}" for f in findings
    )
    revision_prompt = (
        "Phase 4 — TARGETED REVISION\n\n"
        "An automated review of your Phase 3 draft caught the following specific "
        "issues:\n\n"
        f"{findings_block}\n\n"
        "Produce a REVISED version of the ENTIRE report that fixes ONLY these "
        "specific issues. Do NOT re-do the analysis, do NOT rewrite sections that "
        "aren't affected by the listed issues, and do NOT introduce new sections. "
        "Preserve all original section headings, tables, sources, and analytical "
        "content that wasn't called out above.\n\n"
        "ACTION-DIRECTED FIXES (do this, do not just rewrite prose):\n"
        "- **C8 EPS footing failure:** the loss-year EPS row is likely wrong by ~10x. "
        "Recompute every column's EPS = Net Income / diluted shares using a SINGLE "
        "share count (the one already used for market cap). Update the EPS row "
        "directly — do not change the surrounding prose.\n"
        "- **C9 peer outlier in EV/Rev (LTM):** the flagged peer is most likely a "
        "single-quarter revenue mislabeled as TTM. EITHER (a) drop that peer from the "
        "Peer Comp Table entirely (and from the Competitive Landscape table) and "
        "remove the row's contribution to the median, OR (b) replace the row's LTM "
        "revenue with a value that reconciles to the other forward multiples in the "
        "same row (FY+1E revenue / (1 + LTM-to-FY+1 growth)). Recompute that row's "
        "EV/Rev, P/E, and update the Median row. Do NOT just rewrite the positioning "
        "note — the underlying number is wrong and the table must change.\n"
        "- **C10 EBITDA basis mismatch:** state the EBITDA basis explicitly (GAAP vs "
        "Adjusted; with vs without one-time items) in the Financial Summary table "
        "footnote. If FY26 EBITDA excludes a one-time gain that NI includes, either "
        "(a) restate EBITDA on the same basis as NI for that column, or (b) label "
        "both rows with their basis so the reader sees they aren't comparable.\n"
        "- **C7 citation missing/truncated:** add the missing source line(s) — Title "
        "— Publication — Date — URL. Do not strip in-text [N] citations.\n"
        "- **All other findings:** apply the fix as written. The reader should be "
        "able to read the revised report and not find the originally-flagged issue.\n\n"
        "Output the FULL revised report starting with the original `#` heading. No "
        "preamble, no commentary, no \"here is the revised report\" prefix — just "
        "the revised markdown beginning with `#`."
    )

    messages.append({"role": "assistant", "content": draft})
    messages.append({"role": "user", "content": revision_prompt})

    typer.echo(
        f"[review] requesting targeted revision from model ({len(findings)} fixes)...",
        err=True,
    )
    revise_start = time.monotonic()

    revised_parts: list[str] = []
    try:
        stream = client.chat.completions.create(
            model=model_cfg["id"],
            messages=messages,
            stream=True,
            extra_body=model_cfg["extra_body"],
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                sys.stdout.write(delta.content)
                sys.stdout.flush()
                revised_parts.append(delta.content)
    except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
        typer.echo(
            f"\n[review] stream error during revision ({type(e).__name__}); keeping original draft",
            err=True,
        )
        return draft

    revised = "".join(revised_parts).strip()
    revise_elapsed = time.monotonic() - revise_start
    revised = _strip_preamble(revised)  # reuse the existing preamble-strip safety

    if not revised:
        typer.echo("[review] revision empty — keeping original draft", err=True)
        return draft

    if len(revised) < len(draft) * 0.70:
        typer.echo(
            f"[review] revision too short ({len(revised)} vs draft {len(draft)} chars) "
            f"— keeping original draft to avoid content loss",
            err=True,
        )
        return draft

    draft_h2 = set(re.findall(r"^##\s+(.+?)$", draft, re.MULTILINE))
    revised_h2 = set(re.findall(r"^##\s+(.+?)$", revised, re.MULTILINE))
    if len(revised_h2) + 1 < len(draft_h2):  # tolerate at most 1 missing heading
        typer.echo(
            f"[review] revision dropped section headings ({len(draft_h2)} → {len(revised_h2)}) "
            f"— keeping original draft",
            err=True,
        )
        return draft

    typer.echo(
        f"\n[review] revision complete in {revise_elapsed:.1f}s ({len(revised)} chars)",
        err=True,
    )
    return revised


# ---------- Output saving ----------

FILLER = {"a","an","the","is","are","was","were","do","does","did","will",
          "would","can","could","should","of","in","on","at","to","for",
          "and","or","but","with","about","what","how","why","when",
          "where","who","its","it","this","that","these","those","has",
          "have","had","be","been","being","does","roblox"}


def _slug(text: str, max_keywords: int = 3) -> str:
    tokens = re.sub(r"[^\w\s]", "", text.lower()).split()
    keywords = [t for t in tokens if t not in FILLER][:max_keywords]
    return "-".join(keywords) if keywords else "brief"


def _strip_preamble(content: str) -> str:
    """Kimi occasionally leaks a sentence of tool-use narration ("Now I have gathered
    sufficient data...", "I'll begin Phase 1...") above the report's "# Title", despite
    the prompt forbidding it. Both report formats are guaranteed to open with a markdown
    heading, so drop everything before the first heading marker. The search is NOT anchored
    to line-start on purpose: the leak often runs straight into the title with no newline
    ("...SEC filings.# MU: Initiation Report"), which both hides the leak and breaks the
    title's rendering. Cutting from the "#" preserves the title and re-floats it to the
    start of the file. If no heading exists (a rare degenerate output), leave the content
    untouched rather than risk emptying the file."""
    match = re.search(r"#{1,6}\s", content)
    return content[match.start():] if match else content


def _fmt_frontmatter_value(v) -> str:
    """YAML-render a frontmatter value. Lists become block-style with
    quoted items so embedded colons / quotes survive a round-trip."""
    if isinstance(v, list):
        if not v:
            return "[]"
        lines = [""]
        for item in v:
            esc = str(item).replace('"', '\\"')
            lines.append(f"  - \"{esc}\"")
        return "\n".join(lines)
    return str(v)


def _save(directory: Path, ticker: str, slug: str, frontmatter: dict, content: str) -> Path:
    directory.mkdir(exist_ok=True)
    today = datetime.date.today()
    date_str = today.strftime("%Y%m%d")
    content = _strip_preamble(content)
    fm_lines = (
        ["---"]
        + [f"{k}: {_fmt_frontmatter_value(v)}" for k, v in frontmatter.items()]
        + ["---", ""]
    )
    header = "\n".join(fm_lines) + "\n"
    filename = directory / f"{ticker.upper()}-{slug}-{date_str}.md"
    filename.write_text(header + content, encoding="utf-8")
    typer.echo(f"\nSaved: {filename}", err=True)
    return filename


def _extract_open_questions(draft: str) -> tuple[list[str], str]:
    """Pull the ## Open Questions section out of the Phase 3 draft, parse
    bullet items into a list, and return (items, draft_without_section).
    Used to route the model's data-gap confessions into YAML frontmatter
    metadata instead of the reader-facing body.

    Accepts both the English heading `## Open Questions` and the locked
    Chinese alias `## 未解决问题` so the extraction works regardless of
    the report's output language."""
    aliases = ["Open Questions", _SECTION_NAMES_ZH.get("Open Questions", "未解决问题")]
    m = None
    for alias in aliases:
        m = re.search(
            rf"\n## {re.escape(alias)}\n(.*?)(?=\n## |\Z)",
            draft, re.DOTALL,
        )
        if m:
            break
    if not m:
        return [], draft
    items: list[str] = []
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if line.startswith("- "):
            # Strip leading "- " and any bold/markdown decoration
            item = line[2:].strip()
            item = re.sub(r"\*\*([^*]+)\*\*:?\s*", r"\1: ", item).strip()
            items.append(item)
    draft_without = draft[:m.start()] + draft[m.end():]
    return items, draft_without


# ---------- Setup helper ----------

def _resolve_model(name: str) -> dict:
    """Look up a model entry from MODELS, with a clear error if the name
    is unknown. Returns the full config dict (id, base_url, api_key_env,
    extra_body)."""
    if name not in MODELS:
        typer.echo(
            f"Unknown model '{name}'. Available: {', '.join(MODELS)}",
            err=True,
        )
        raise typer.Exit(1)
    return MODELS[name]


def _make_llm_client(model_name: str = DEFAULT_MODEL) -> tuple[OpenAI, dict]:
    """Returns (OpenAI client pointed at the right base_url, model_cfg).
    Loads .env so callers don't have to."""
    load_dotenv()
    cfg = _resolve_model(model_name)
    key = os.environ.get(cfg["api_key_env"])
    if not key:
        typer.echo(
            f"{cfg['api_key_env']} not set (add it to .env)",
            err=True,
        )
        raise typer.Exit(1)
    return OpenAI(api_key=key, base_url=cfg["base_url"]), cfg


def _setup_clients(model_name: str = DEFAULT_MODEL) -> tuple[OpenAI, TavilyClient, dict]:
    """Returns (llm_client, tavily_client, model_cfg)."""
    client, cfg = _make_llm_client(model_name)
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        typer.echo("TAVILY_API_KEY not set (add it to .env)", err=True)
        raise typer.Exit(1)
    return client, TavilyClient(api_key=tavily_key), cfg


# ---------- Language ----------
#
# Output language is chosen up-front via --lang and the model writes natively.
# No post-hoc translation pass: Kimi's first-pass Chinese reads better than a
# translation of its own English, and DeepSeek handles either side well.
# Sources for US-listed names remain English; the rules for preserving them
# verbatim (URLs, numbers, $, tickers, no buy/sell/hold) live in the
# language-instruction block that gets appended to the system prompt only
# when lang=zh, so English runs (the common case) keep their prompt cache.

LANG_NAMES = {"en": "English", "zh": "Simplified Chinese (简体中文)"}

_LANG_INSTRUCTION_ZH = """

WRITE THIS REPORT ENTIRELY IN SIMPLIFIED CHINESE (简体中文). The reader is a Chinese-speaking investor analyzing US-listed equities.

DOCUMENT TITLE (locked): the H1 title MUST follow this exact pattern:
`# [Ticker]: [Chinese company name] ([English company name]) — 投资简报`
e.g. `# MRVL: 美满电子 (Marvell Technology) — 投资简报`. The phrase 投资简报
("investment brief") is the canonical Chinese label for this artifact;
do NOT substitute alternatives like 首次覆盖 / 公司深度研究 / 投资简介.

SECTION HEADERS (locked Chinese translations — use these EXACT strings):
- ## Thesis           → ## 投资论点
- ## Business snapshot → ## 业务概览
- ## What the Street thinks → ## 市场观点
  - ### Street consensus → ### 市场一致预期
  - ### Three debates    → ### 三大争议
    - #### Debate N: ... → #### 争议N: ...
    - **Bulls argue:**   → **看多观点：**
    - **Bears argue:**   → **看空观点：**
    - **What resolves it:** → **如何验证：**
- ## Bull / Bear / Base → ## 看多 / 看空 / 基准情景
  - ### Bull → ### 看多情景
  - ### Bear → ### 看空情景
  - ### Base → ### 基准情景
- ## Top 3 risks → ## 三大风险
- ## Catalyst calendar → ## 催化剂日历
- ## Appendix → ## 附录
  - ### Financial Summary → ### 财务摘要
  - ### Competitive Landscape → ### 竞争格局
  - ### Peer Comp Table → ### 同业估值比较
- ## Open Questions → ## 未解决问题
- ## Sources → ## 数据来源

Stick to these exact translations every run. Downstream validators key on
them.

Preserve EXACTLY (do not translate, transliterate, or convert):
- Numbers, dates, and units verbatim: $, %, x (as in 14.5x), B / M / bn / mn, basis points, ratios.
- Currency: US dollars stay US dollars ($). NEVER restate as RMB / ¥.
- Source URLs — byte-identical. Source titles may be in English; do not translate them. Publication names may be transliterated if a standard Chinese form is well established, otherwise keep English.
- Ticker symbols in Latin letters (e.g. RBLX, MU, NTES).
- Markdown structure: tables (same columns, same number of rows, same alignment), lists, section order — identical to what the English version would have.

Terminology — use standard Chinese financial terms, consistently:
- Keep these acronyms as-is (optionally add Chinese term in parentheses on first use only): EBITDA, FCF, DCF, EV, TAM, GAAP, SBC, CAGR, YoY, QoQ, LTM, TTM, DAU, MAU, ARPU, ROE, ROIC.
- free cash flow → 自由现金流; enterprise value → 企业价值; bookings → 预订量（流水）; deferred revenue → 递延收入; gross margin → 毛利率; operating margin → 营业利润率; net cash → 净现金; dilution → 摊薄; guidance → 业绩指引; consensus → 市场一致预期; re-rating → 估值重估.
- Company names: use the established Chinese name where one exists (e.g. NetEase → 网易, Micron → 美光, Marvell → 美满电子); otherwise keep the English name.

The Investment Framework discipline applies in Chinese exactly as it does in English:
- Never write 买入 / 卖出 / 持有 (or any directional rating phrasing). Chinese makes these phrasings very natural — resist.
- Never output a specific 价格目标 (price target). Express direction as percentage ranges and scenarios, exactly as the English-mode prompt instructs above.
"""


def _lang_instruction(lang: str) -> str:
    """Returns the additional system-prompt block for the requested output
    language. Empty for English (so the prompt cache stays warm on the
    common case); a discipline-preserving block for Chinese."""
    if lang == "zh":
        return _LANG_INSTRUCTION_ZH
    return ""


# ---------- Commands ----------

@app.command()
def research(
    ticker: str = typer.Argument(..., help="Stock ticker, e.g. RBLX"),
    question: str = typer.Argument(..., help="Research question in quotes"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print each tool call"),
    lang: str = typer.Option(
        "en", "--lang", "-l",
        help=f"Output language: {' | '.join(LANG_NAMES)} (default: en)",
    ),
    model: str = typer.Option(
        DEFAULT_MODEL, "--model", "-m",
        help=f"Model: {' | '.join(MODELS)} (default: {DEFAULT_MODEL})",
    ),
):
    """Research a public company and produce a sourced brief."""
    if lang not in LANG_NAMES:
        typer.echo(f"Unknown lang '{lang}'. Supported: {', '.join(LANG_NAMES)}", err=True)
        raise typer.Exit(1)
    client, tavily, model_cfg = _setup_clients(model)
    today = datetime.date.today().strftime("%B %d, %Y")
    system_prompt = RESEARCH_SYSTEM_PROMPT_TEMPLATE.format(today=today) + _lang_instruction(lang)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Ticker: {ticker}\nResearch question: {question}"},
    ]

    output, tool_calls_used = agentic_loop(
        client, tavily, messages, RESEARCH_TOOLS, RESEARCH_MAX_TOOL_CALLS, verbose,
        model_cfg=model_cfg,
    )

    # Hard zero-tool-calls guard: a research brief produced without ANY search
    # or fetch was drafted purely from training-time knowledge. For questions
    # about current events the result is hallucinated quotes / numbers / URLs
    # that look authoritative. Refuse to save.
    if tool_calls_used == 0:
        typer.echo(
            "\n[research] ABORTED: agentic loop completed with 0 tool calls. The model "
            "drafted the brief from training-time knowledge without searching or fetching "
            "ANY source. For questions about recent events this produces hallucinated "
            "content (fabricated quotes, numbers, and URLs). No file saved. Re-run with a "
            "more specific question, or check the model's behavior.",
            err=True,
        )
        raise typer.Exit(1)

    saved = _save(
        _briefs_dir_for(lang),
        ticker,
        _slug(question),
        {
            "ticker": ticker.upper(),
            "question": question,
            "date": datetime.date.today().strftime("%Y-%m-%d"),
            "lang": lang,
            "model": model_cfg["id"],
        },
        output,
    )


@app.command()
def initiate(
    ticker: str = typer.Argument(..., help="Stock ticker, e.g. RBLX"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print each tool call"),
    lang: str = typer.Option(
        "en", "--lang", "-l",
        help=f"Output language: {' | '.join(LANG_NAMES)} (default: en)",
    ),
    model: str = typer.Option(
        DEFAULT_MODEL, "--model", "-m",
        help=f"Model: {' | '.join(MODELS)} (default: {DEFAULT_MODEL})",
    ),
):
    """Produce a thesis-shaped initiation brief on a public company.

    Eight sections, ~1,300 words core: thesis / business snapshot / what
    the Street thinks (consensus + 3 debates) / Bull-Bear-Base / 3 risks
    / catalyst calendar / appendix (Financial Summary + Competitive
    Landscape + Peer Comp Table) / sources. See INITIATE_REDESIGN.md.

    The pre-redesign 11-section sell-side initiation is preserved as
    `initiate_legacy` for fallback / A/B comparison.
    """
    if lang not in LANG_NAMES:
        typer.echo(f"Unknown lang '{lang}'. Supported: {', '.join(LANG_NAMES)}", err=True)
        raise typer.Exit(1)
    client, tavily, model_cfg = _setup_clients(model)
    today = datetime.date.today().strftime("%B %d, %Y")
    system_prompt = INITIATE_SYSTEM_PROMPT_TEMPLATE.format(today=today) + _lang_instruction(lang)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Ticker: {ticker}\n\nProduce an initiation brief following the format above."},
    ]

    # Pre-fetch the deterministic subject-company foundations in parallel before
    # the agent loop. Same prefetch as legacy — the underlying data needs are
    # similar even though the synthesis layer is much lighter.
    messages.extend(_prefetch_subject_docs(ticker))

    output, _ = agentic_loop(
        client, tavily, messages, INITIATE_TOOLS, INITIATE_MAX_TOOL_CALLS, verbose,
        model_cfg=model_cfg,
    )

    # Phase 4: only the shape-agnostic checks fire on the new template
    # (C5 with the new 8-section list, C7 citation completeness). C1-C4
    # and C6 were tuned to the legacy 11-section layout and would produce
    # noise; they get re-added once we see what actually fails on the
    # new shape in production runs.
    findings = _review_draft(output, ticker, datetime.date.today(), legacy=False)
    _log_review(findings)
    if findings:
        output = _revise_draft(client, messages, output, findings, ticker, model_cfg=model_cfg)

    # Extract the ## Open Questions section into structured frontmatter
    # metadata instead of shipping it to the reader.
    open_questions, output = _extract_open_questions(output)

    saved = _save(
        REPORTS_DIR,
        ticker,
        "initiation",
        {
            "ticker": ticker.upper(),
            "report_type": "initiation",
            "date": datetime.date.today().strftime("%Y-%m-%d"),
            "lang": lang,
            "model": model_cfg["id"],
            "open_questions": open_questions,
        },
        output,
    )


@app.command()
def initiate_legacy(
    ticker: str = typer.Argument(..., help="Stock ticker, e.g. RBLX"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print each tool call"),
    lang: str = typer.Option(
        "en", "--lang", "-l",
        help=f"Output language: {' | '.join(LANG_NAMES)} (default: en)",
    ),
    model: str = typer.Option(
        DEFAULT_MODEL, "--model", "-m",
        help=f"Model: {' | '.join(MODELS)} (default: {DEFAULT_MODEL})",
    ),
):
    """Produce the pre-redesign 11-section sell-side initiation (fallback).

    Preserves the original long-form analyst-grade output. Use `initiate`
    (without the suffix) for the new thesis-shaped retail brief.
    """
    if lang not in LANG_NAMES:
        typer.echo(f"Unknown lang '{lang}'. Supported: {', '.join(LANG_NAMES)}", err=True)
        raise typer.Exit(1)
    client, tavily, model_cfg = _setup_clients(model)
    today = datetime.date.today().strftime("%B %d, %Y")
    system_prompt = INITIATE_LEGACY_SYSTEM_PROMPT_TEMPLATE.format(today=today) + _lang_instruction(lang)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Ticker: {ticker}\n\nProduce an initiation report following the format above."},
    ]

    messages.extend(_prefetch_subject_docs(ticker))

    output, _ = agentic_loop(
        client, tavily, messages, INITIATE_TOOLS, INITIATE_LEGACY_MAX_TOOL_CALLS, verbose,
        model_cfg=model_cfg,
    )

    # Legacy shape: full C1-C7 review against the 11-section layout.
    findings = _review_draft(output, ticker, datetime.date.today(), legacy=True)
    _log_review(findings)
    if findings:
        output = _revise_draft(client, messages, output, findings, ticker, model_cfg=model_cfg)

    saved = _save(
        REPORTS_DIR,
        ticker,
        "initiation",
        {
            "ticker": ticker.upper(),
            "report_type": "initiation-legacy",
            "date": datetime.date.today().strftime("%Y-%m-%d"),
            "lang": lang,
            "model": model_cfg["id"],
        },
        output,
    )


if __name__ == "__main__":
    app()
