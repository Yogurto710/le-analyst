import os
import re
import sys
import json
import httpx
import typer
import datetime
import subprocess
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

BRIEFS_DIR = Path("briefs")
REPORTS_DIR = Path("reports")

MODEL = "kimi-k2.6"
BASE_URL = "https://api.moonshot.cn/v1"
RESEARCH_MAX_TOOL_CALLS = 30
INITIATE_MAX_TOOL_CALLS = 95  # +10 vs original: +5 for subject consensus, +5 for per-peer consensus that powers the forward peer comp table
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
2. For each sub-question, run focused web_search queries to identify the best available sources. Then immediately use fetch_url to read the most promising results in depth — do not exhaust your search budget before fetching. Prioritize depth over breadth: 2-3 well-read sources per sub-question beat 10 skimmed snippets.
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

INITIATE_SYSTEM_PROMPT_TEMPLATE = """You are a senior equity research analyst producing a deep-dive initiation report on a public company.

Today's date is {today}.

Your process has three STRICTLY SEQUENTIAL phases. You must complete each phase fully before starting the next. Do not interleave phases. Do not return to a previous phase.

==============================================================
PHASE 1 — Gather all primary and peer data (use only edgar_search, edgar_fetch, web_search, fetch_url)
==============================================================

1. Use edgar_search to locate the most recent 10-K and 10-Q filings for the ticker. Then use edgar_fetch to read Item 1 (Business), Item 1A (Risk Factors), and Item 7 (MD&A) from the 10-K, and the MD&A section of the latest 10-Q.
2. Use web_search and fetch_url to find and read the last two earnings call transcripts and the latest investor presentation. For the MOST RECENT transcript specifically, pay close attention to the Q&A section — capture each analyst's firm name and the substance of their question. These power the Sell-Side Q&A Analysis section in Phase 3. Skip generic ("any color on the quarter") questions; identify the 3-5 most substantive debates analysts pushed on.
3. Pull these from Yahoo Finance (https://finance.yahoo.com/quote/TICKER/key-statistics/) for the trading snapshot at the top of the report: current price, market cap, enterprise value, shares outstanding, 52-week range, average daily volume (3-month), and short interest as a percent of float.
4. Based on the business model you just learned, select 4-6 peers — a mix of direct competitors and business-model comparables. Note the rationale.
5. For each peer, gather ALL financial data from Yahoo Finance as a single UNIFIED source — non-negotiable for comp-table peers, because apples-to-apples comparison requires apples-to-apples sources. Yahoo presents every ticker in the same schema, eliminating the cross-source distortion that happens when one peer's revenue comes from a press release and another's from a third-party aggregator. For each peer:
   - Market cap, EV, share price → Yahoo's quote / key-statistics page (https://finance.yahoo.com/quote/{{ticker}}/key-statistics/).
   - LFY and LTM revenue, gross profit, EBITDA, net income, FCF, diluted EPS → Yahoo's financials page (https://finance.yahoo.com/quote/{{ticker}}/financials/), reading the most recent annual column for LFY and the TTM column for LTM.
   - SBC and SBC-adjusted FCF → derive from the cash-flow statement on the same Yahoo page.
   - FY+1E and FY+2E consensus revenue and EPS → Yahoo's analysis page (https://finance.yahoo.com/quote/{{ticker}}/analysis/) — same page used for the subject company's consensus in step 10.
   Run a DEDICATED fetch per peer; do NOT rely on listicles or aggregator articles that summarize many companies at once. If a particular figure is missing from Yahoo for a peer (e.g., a private competitor with no listing, or a foreign listing Yahoo doesn't cover), note it explicitly and substitute the official 10-K / 20-F equivalent ONLY as a LABELLED exception — never mix sources silently. Mark "NM" in any comp-table cell where data genuinely isn't available.
6. For each named competitor, run a separate search for user metrics: "{{competitor name}} monthly active users {{year}}" or "{{competitor name}} DAU {{year}}". Each metric must come from a search whose target was that specific competitor — never extract a competitor's number from an article about the subject company. ENTITY BINDING RULE: a number only belongs to a competitor if the sentence it appears in explicitly names that competitor as the subject. If the sentence is ambiguous about which entity it describes, discard the number.

   Source hierarchy for competitor data, prefer in this order: (1) the competitor's own SEC filings or earnings releases, (2) the competitor's official press releases or IR page, (3) reputable third-party estimates (Newzoo, Sensor Tower, data.ai) — labelled as "estimated", (4) news articles citing the above with the original source named. Never use numbers from listicles, blog posts, or aggregator "Top 10" articles without tracing to the original source. For private companies (Epic Games, Valve, etc.) every financial number is an estimate — always label it that way (e.g. "$6B estimated 2025 revenue (Sacra)").
7. Find at least one independent third-party TAM estimate for the industry, in addition to whatever the company cites.
8. Find the company's historical trading multiples — peak, trough, and current. If exact multiples aren't available, get stock prices at key dates (IPO, peak, recent trough) so you can approximate.
9. Find upcoming catalysts over the next 6-12 months: next earnings date, investor day or conferences, product launches, regulatory deadlines, debt maturities, lockup expirations. Search "{{company name}} next earnings date 2026", "{{company name}} investor day 2026", and check the 10-K for any disclosed forward dates. (Wall Street consensus estimates are gathered in step 10 below and used to anchor catalyst thresholds.)
10. WALL STREET CONSENSUS — the PRIMARY source for all forward estimates. Do NOT derive forward figures by annualizing a single quarter. Search "{{ticker}} consensus estimates {{next fiscal year}}" or "{{ticker}} analyst estimates"; the preferred free source is Yahoo Finance (https://finance.yahoo.com/quote/{{ticker}}/analysis), which aggregates consensus revenue and EPS. If it's unavailable, fall back to Koyfin, WSJ Markets, or MarketBeat. Pull: current-fiscal-year and next-fiscal-year consensus REVENUE, current-FY and next-FY consensus EPS, and the NUMBER OF ANALYSTS covering (a quality signal — >10 = reliable, <5 = thin coverage; record it). For EVERY peer in the comp table, gather consensus FY+1 and FY+2 revenue AND EPS — these power the forward EV/Revenue and forward P/E columns plus the 2-year revenue CAGR in the peer comp table. Yahoo Finance's analyst page typically has all four data points per ticker in one fetch. If a peer's consensus isn't available, mark it and proceed — don't burn 3+ searches chasing one peer's forwards. Record the source, analyst count, and date for every consensus figure (e.g. "Yahoo Finance, 24 analysts, as of {today}"). If no consensus is findable, note that explicitly and fall back to guidance-derived forward estimates — but you MUST label them "derived from company guidance, not analyst consensus".
11. FISCAL CALENDAR + LTM SOURCING. Record the fiscal year-end month for the subject company AND every peer. Companies that don't share the same fiscal year-end cannot be compared on "latest fiscal year" alone — their reported years cover different calendar periods.

LFY DEFINITION (HARD RULE): LFY = the MOST RECENT COMPLETED fiscal year, period. Do NOT substitute an earlier "last comparable year" because of M&A, divestitures, discontinued operations, or restatements. If the most recent completed fiscal year was affected by a divestiture (e.g., APP's Apps business sold mid-2025 means FY2025 is reported on a continuing-ops basis), use the continuing-ops FY2025 figure as LFY — do not regress to FY2024. The trajectory across the trailing columns (prior FY → LFY → LTM) is itself how the report communicates a basis change; don't hide it by skipping a year.

For the SUBJECT company, gather:
(a) Latest completed fiscal year (LFY) figures from filings: revenue, gross profit, operating income, net income, diluted EPS, EBITDA (or adj. EBITDA where reported), free cash flow.
(b) LTM (trailing-twelve-month / TTM) figures for the same metrics — pull DIRECTLY from Yahoo Finance's financials page at https://finance.yahoo.com/quote/{{ticker}}/financials/, which presents a TTM column ready-made. This is more reliable than summing 4 quarters of disclosed line items, especially for metrics that aren't always quarterly disclosed (EBITDA, FCF).
(c) Quarterly REVENUE for two purposes: the completed quarters of the CURRENT fiscal year, AND the matching quarters of the PRIOR fiscal year. These power the Phase 2 LTM-revenue cross-check (LTM = LFY + current-FY stub quarters - matching prior-FY stub quarters), which catches off-by-one-quarter errors when Yahoo's TTM data is mislabelled.

(Peer historical and forward financials are gathered from Yahoo Finance per step 5 above, so the comp table is built on a unified source across all peers. Step 11 here only records peer fiscal year-ends to detect calendar mismatches that would distort LFY-on-LFY comparisons.)

Note where the subject's LFY and LTM revenue materially differ — this drives the Quality-of-Earnings flag in Phase 3.
12. CYCLICAL-INDUSTRY DATA — gather this ONLY if the subject operates in an industry with well-documented boom-bust cycles (memory/commodity semiconductors, commodity chemicals, shipping, energy, mining, and the like). Skip this item entirely for companies where cyclicality is not the primary analytical lens (e.g. Netflix, Roblox). When it applies, gather: (a) historical cycle duration for this industry (how many quarters past upcycles and downcycles have lasted); (b) 3-4 leading indicators of a cycle turn for this specific industry (e.g. inventory days, spot-vs-contract price spread, fab/plant utilization, book-to-bill, freight day-rates, rig counts) — for each, the current reading AND the historical level that has signalled prior turns; and (c) the subject company's CapEx and depreciation & amortization for the latest fiscal year (plus a couple of prior years if readily available) to support a supply-side CapEx/Depreciation ratio.

PHASE 1 BUDGET DISCIPLINE: Aim to finish gathering in under 65 tool calls. If you find yourself unable to locate a specific number after 2-3 search attempts, stop searching for it — note it as missing and move on. Missing data goes to Open Questions; it does not justify more searches.

==============================================================
PHASE 2 — Compute derived metrics (use ONLY python_repl)
==============================================================

Once Phase 1 is complete, use python_repl AT MOST 3 TIMES TOTAL to compute every derived metric below. Bundle all related computations into one block. Print every value cleanly so you can quote it in the report. If an input is missing from Phase 1, print "NM" or "N/A" — do not return to Phase 1 to fetch it.

For the subject company:
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
What the company does, revenue model, segment breakdown, key operating metrics management tracks, and the company's CURRENT strategic priorities (1-2 sentences distilled from the most recent investor day, earnings letter, or shareholder letter — what management says it's investing in and executing against right now). Cite the 10-K Item 1 plus the relevant earnings call or letter for the strategy framing. Numbers and forward guidance do not belong here — they live in Financial Profile / Forward Estimates.

## Financial Profile
Brief prose intro that frames the company's TRAJECTORY — not just the last fiscal year, but where it sits now (LTM) and where the Street expects it to go. Lead with the takeaways that matter most. Include a one-line balance-sheet callout in this intro paragraph: "Net cash of $X.XB on $X.XB cash and equivalents and $X.XB total debt as of [most recent period end]" (or "Net debt of $X.XB" when negative). Do not give the balance sheet its own table — that single sentence is the whole treatment.

Numbers in the Financial Summary come from filings (prior FY, LFY) and Yahoo Finance's TTM column (LTM); Forward Estimates come from Wall Street consensus and management guidance, both gathered in Phase 1.

### Financial Summary
A trailing-only markdown table — clean, no blanks. Columns, left to right: prior fiscal year | latest fiscal year (LFY) | LTM. Required rows: Revenue, Gross Profit (or Gross Margin %), Operating Income/Loss, Net Income/Loss, Adjusted EBITDA (if reported), Free Cash Flow, Diluted EPS. Add a YoY change column if helpful, but the LFY-vs-LTM column already conveys trajectory. Historical columns (prior FY, LFY) come from filings; the LTM column comes from Yahoo Finance's TTM, verified against the Phase 2 LTM revenue cross-check — if the cross-check failed, use the filing-derived LTM and footnote the source. If the company reports segments, include a Revenue by Segment block (one row per segment) within or immediately below this table. Format dollar values consistently (e.g. all $M or all $B).

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
Begin with the company's own TAM claim. Then present at least one independent third-party TAM estimate. If the figures diverge, explain likely reasons (different scope, adjacent markets, methodology). Compute and show the company's implied current market share against each TAM estimate. End with a one-sentence note on TAM methodology limitations for this industry.

## Competitive Landscape
Two parts:
(a) **Stated position**: How the company describes its competitive position (10-K Item 1 + recent commentary).
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
Concrete data points on where the current multiple sits versus history. Cite peak multiple and date, trough multiple and date, and current multiple. If exact multiples are unavailable, approximate from stock prices at those dates and contemporaneous revenue figures, and flag the approximation.

## Key Risks
From 10-K Item 1A plus any emerging risks raised on recent earnings calls.

## Investment Framework
Write this section LAST, after every other section above is complete. Read back through the prior sections before drafting — this is the synthesis layer that turns the report into a thinking tool. Three subsections:

### Bull / Bear / Base Cases
Three short paragraphs of 3-5 sentences each. Each follows the structure: thesis → supporting evidence with citations from elsewhere in this report → confirmation/invalidation trigger.

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

def agentic_loop(
    client: OpenAI,
    tavily: TavilyClient,
    messages: list[dict],
    tools: list[dict],
    max_tool_calls: int,
    verbose: bool,
) -> str:
    """Run the agentic tool-use loop until the model emits a final text response.
    Returns the assembled output text."""
    tool_calls_used = 0
    output_parts: list[str] = []

    while True:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            stream=True,
            extra_body={"thinking": {"type": "disabled"}},
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
                return "".join(output_parts)

        if finish_reason == "stop":
            sys.stdout.write("\n")
            return "".join(output_parts)

        if finish_reason != "tool_calls":
            typer.echo(f"\n\n[stopped with reason: {finish_reason}]", err=True)
            return "".join(output_parts)

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

            result = run_tool(tavily, tc["name"], tool_input)

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


def _save(directory: Path, ticker: str, slug: str, frontmatter: dict, content: str) -> Path:
    directory.mkdir(exist_ok=True)
    today = datetime.date.today()
    date_str = today.strftime("%Y%m%d")
    content = _strip_preamble(content)
    fm_lines = ["---"] + [f"{k}: {v}" for k, v in frontmatter.items()] + ["---", ""]
    header = "\n".join(fm_lines) + "\n"
    filename = directory / f"{ticker.upper()}-{slug}-{date_str}.md"
    filename.write_text(header + content, encoding="utf-8")
    typer.echo(f"\nSaved: {filename}", err=True)
    return filename


# ---------- Setup helper ----------

def _moonshot_client() -> OpenAI:
    load_dotenv()
    moonshot_key = os.environ.get("MOONSHOT_API_KEY")
    if not moonshot_key:
        typer.echo("MOONSHOT_API_KEY not set (add it to .env)", err=True)
        raise typer.Exit(1)
    return OpenAI(api_key=moonshot_key, base_url=BASE_URL)


def _setup_clients() -> tuple[OpenAI, TavilyClient]:
    client = _moonshot_client()
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not tavily_key:
        typer.echo("TAVILY_API_KEY not set (add it to .env)", err=True)
        raise typer.Exit(1)
    return client, TavilyClient(api_key=tavily_key)


# ---------- Translation ----------
#
# Localization is post-hoc and additive: the English report stays the canonical
# artifact (sources for US-listed names are English), and translation is a separate,
# re-runnable pass over the finished markdown. The translation model is Kimi itself —
# a Chinese-native model — so no new provider is needed.

TRANSLATE_LANG_NAMES = {"zh": "Simplified Chinese (简体中文)"}

TRANSLATE_SYSTEM_PROMPT_TEMPLATE = """You are a professional financial translator. Translate the equity-research report below from English into {language}. It will be read by {language} investors analyzing US-listed (NYSE / NASDAQ) equities.

Output ONLY the translated Markdown — no preamble, no explanation, no code fences. Start directly with the first heading.

PRESERVE EXACTLY — do not alter, reformat, or translate any of these:
- Numbers, dates, and units: $, %, x (as in 14.5x), B / M / bn / mn, basis points, ratios. NEVER convert currency — US dollars stay US dollars ($); do not restate as RMB / ¥.
- Every source URL, character for character. You may translate the title/publication text on a source line, but the URL itself must be byte-identical.
- Ticker symbols (e.g. RBLX, MU, NTES) — keep them in Latin letters.
- Markdown structure: keep every heading and its level, every table (same columns, same number of rows, same pipes and alignment), every list, and the section order identical to the source.
- YAML frontmatter is handled separately — it is NOT included below, so translate only the body.

TERMINOLOGY — use standard {language} financial terms, applied consistently throughout:
- Keep these acronyms as-is (optionally add the {language} term in parentheses on first use only): EBITDA, FCF, DCF, EV, TAM, GAAP, SBC, CAGR, YoY, QoQ, LTM, TTM, DAU, MAU, ARPU, ROE, ROIC.
- free cash flow → 自由现金流; enterprise value → 企业价值; bookings → 预订量（流水）; deferred revenue → 递延收入; gross margin → 毛利率; operating margin → 营业利润率; net cash → 净现金; dilution → 摊薄; guidance → 业绩指引; consensus → 市场一致预期; re-rating → 估值重估.
- Company names: use the established {language} name where one exists (e.g. NetEase → 网易, Micron → 美光); otherwise keep the English name.

DISCIPLINE — this report deliberately follows these rules, and your translation must preserve them:
- Never introduce a buy / sell / hold recommendation (买入 / 卖出 / 持有) or any directional rating, even where it would read naturally. The English contains none; neither should the translation.
- Never introduce a specific price target. Keep valuation framed exactly as the English does — percentage ranges and scenarios.
- Translate faithfully and completely. Do not add, drop, soften, or sharpen any claim, and do not summarize.
"""


def _translate_text(client: OpenAI, content: str, lang: str) -> str:
    """Single-shot translation of finished report markdown. No tools; one retry on the
    mid-stream connection drops that occasionally hit api.moonshot.cn."""
    language = TRANSLATE_LANG_NAMES.get(lang, lang)
    system_prompt = TRANSLATE_SYSTEM_PROMPT_TEMPLATE.format(language=language)
    last_err = None
    for _attempt in range(2):
        parts: list[str] = []
        try:
            stream = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                stream=True,
                extra_body={"thinking": {"type": "disabled"}},
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    sys.stdout.write(delta.content)
                    sys.stdout.flush()
                    parts.append(delta.content)
            return "".join(parts)
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
            last_err = e
            typer.echo(f"\n[connection dropped, retrying translation] {e}", err=True)
    raise last_err


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). The block keeps its --- fences and trailing
    newline; '' if the file has no frontmatter."""
    m = re.match(r"(---\n.*?\n---\n)(.*)", text, re.DOTALL)
    return (m.group(1), m.group(2)) if m else ("", text)


def _frontmatter_with_lang(fm_block: str, lang: str) -> str:
    """Re-emit the frontmatter verbatim with a `lang:` line added before the closing ---."""
    if not fm_block or re.search(r"^lang:", fm_block, re.MULTILINE):
        return fm_block
    lines = fm_block.rstrip("\n").split("\n")  # [..., 'k: v', '---']
    lines.insert(len(lines) - 1, f"lang: {lang}")
    return "\n".join(lines) + "\n"


def _verify_translation(english: str, translated: str) -> list[str]:
    """Cheap structural checks for the classic translation failures: a dropped source,
    a mangled table, a lost section. Warnings only — never blocks the save."""
    warnings: list[str] = []
    url_re = r"https?://[^\s)\]]+"
    en_urls, zh_urls = re.findall(url_re, english), re.findall(url_re, translated)
    if len(en_urls) != len(zh_urls):
        warnings.append(f"source URL count differs: English {len(en_urls)} vs translated {len(zh_urls)}")
    missing = set(en_urls) - set(zh_urls)
    if missing:
        warnings.append(f"{len(missing)} source URL(s) missing from translation, e.g. {sorted(missing)[0]}")

    def rows(t: str) -> int:
        return sum(1 for ln in t.splitlines() if ln.lstrip().startswith("|"))

    def heads(t: str) -> int:
        return sum(1 for ln in t.splitlines() if re.match(r"#{1,6}\s", ln))

    if rows(english) != rows(translated):
        warnings.append(f"table row count differs: English {rows(english)} vs translated {rows(translated)}")
    if heads(english) != heads(translated):
        warnings.append(f"heading count differs: English {heads(english)} vs translated {heads(translated)}")
    return warnings


def _translate_file(client: OpenAI, path: Path, lang: str) -> Path:
    """Translate an existing English report, writing a sibling `<name>.<lang>.md`."""
    if path.name.endswith(f".{lang}.md"):
        typer.echo(f"{path.name} is already a .{lang}.md file; skipping.", err=True)
        return path
    text = path.read_text(encoding="utf-8")
    fm_block, body = _split_frontmatter(text)
    typer.echo(f"\nTranslating {path.name} -> {lang} ...\n", err=True)
    zh_body = _strip_preamble(_translate_text(client, body, lang))
    for w in _verify_translation(body, zh_body):
        typer.echo(f"  [translation check] {w}", err=True)
    out_text = _frontmatter_with_lang(fm_block, lang)
    out_text += ("\n" if fm_block else "") + zh_body.lstrip("\n")
    if not out_text.endswith("\n"):
        out_text += "\n"
    out_path = path.with_suffix(f".{lang}.md")
    out_path.write_text(out_text, encoding="utf-8")
    typer.echo(f"\nSaved: {out_path}", err=True)
    return out_path


# ---------- Commands ----------

@app.command()
def research(
    ticker: str = typer.Argument(..., help="Stock ticker, e.g. RBLX"),
    question: str = typer.Argument(..., help="Research question in quotes"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print each tool call"),
    translate: str = typer.Option(None, "--translate", help="Also save a translated copy, e.g. 'zh'"),
):
    """Research a public company and produce a sourced brief."""
    client, tavily = _setup_clients()
    today = datetime.date.today().strftime("%B %d, %Y")
    system_prompt = RESEARCH_SYSTEM_PROMPT_TEMPLATE.format(today=today)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Ticker: {ticker}\nResearch question: {question}"},
    ]

    output = agentic_loop(
        client, tavily, messages, RESEARCH_TOOLS, RESEARCH_MAX_TOOL_CALLS, verbose
    )

    saved = _save(
        BRIEFS_DIR,
        ticker,
        _slug(question),
        {
            "ticker": ticker.upper(),
            "question": question,
            "date": datetime.date.today().strftime("%Y-%m-%d"),
            "model": MODEL,
        },
        output,
    )
    if translate:
        _translate_file(client, saved, translate)


@app.command()
def initiate(
    ticker: str = typer.Argument(..., help="Stock ticker, e.g. RBLX"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print each tool call"),
    translate: str = typer.Option(None, "--translate", help="Also save a translated copy, e.g. 'zh'"),
):
    """Produce a deep-dive initiation report on a public company."""
    client, tavily = _setup_clients()
    today = datetime.date.today().strftime("%B %d, %Y")
    system_prompt = INITIATE_SYSTEM_PROMPT_TEMPLATE.format(today=today)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Ticker: {ticker}\n\nProduce an initiation report following the format above."},
    ]

    output = agentic_loop(
        client, tavily, messages, INITIATE_TOOLS, INITIATE_MAX_TOOL_CALLS, verbose
    )

    saved = _save(
        REPORTS_DIR,
        ticker,
        "initiation",
        {
            "ticker": ticker.upper(),
            "report_type": "initiation",
            "date": datetime.date.today().strftime("%Y-%m-%d"),
            "model": MODEL,
        },
        output,
    )
    if translate:
        _translate_file(client, saved, translate)


@app.command()
def translate(
    path: str = typer.Argument(..., help="Path to an existing English report (.md)"),
    lang: str = typer.Option("zh", "--lang", "-l", help="Target language code (currently: zh)"),
):
    """Translate an existing report into another language; keeps the English original."""
    report = Path(path)
    if not report.is_file():
        typer.echo(f"File not found: {report}", err=True)
        raise typer.Exit(1)
    if lang not in TRANSLATE_LANG_NAMES:
        typer.echo(f"Language '{lang}' has no tuned glossary; supported: {', '.join(TRANSLATE_LANG_NAMES)}", err=True)
    client = _moonshot_client()
    _translate_file(client, report, lang)


if __name__ == "__main__":
    app()
