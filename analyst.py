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
INITIATE_MAX_TOOL_CALLS = 85
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
2. Use web_search and fetch_url to find and read the last two earnings call transcripts and the latest investor presentation.
3. Pull these from Yahoo Finance (https://finance.yahoo.com/quote/TICKER/key-statistics/) for the trading snapshot at the top of the report: current price, market cap, enterprise value, shares outstanding, 52-week range, average daily volume (3-month), and short interest as a percent of float.
4. Based on the business model you just learned, select 4-6 peers — a mix of direct competitors and business-model comparables. Note the rationale.
5. For each peer, gather: current market cap, EV, latest annual revenue, revenue growth rate, EBITDA, free cash flow, stock-based compensation. Run a DEDICATED search per peer — do not rely on listicles or aggregator articles that summarize many companies at once. Acceptable patterns: "{{peer name}} revenue {{latest fiscal year}}", "{{peer name}} 10-K", "{{peer name}} investor relations".
6. For each named competitor, run a separate search for user metrics: "{{competitor name}} monthly active users {{year}}" or "{{competitor name}} DAU {{year}}". Each metric must come from a search whose target was that specific competitor — never extract a competitor's number from an article about the subject company. ENTITY BINDING RULE: a number only belongs to a competitor if the sentence it appears in explicitly names that competitor as the subject. If the sentence is ambiguous about which entity it describes, discard the number.

   Source hierarchy for competitor data, prefer in this order: (1) the competitor's own SEC filings or earnings releases, (2) the competitor's official press releases or IR page, (3) reputable third-party estimates (Newzoo, Sensor Tower, data.ai) — labelled as "estimated", (4) news articles citing the above with the original source named. Never use numbers from listicles, blog posts, or aggregator "Top 10" articles without tracing to the original source. For private companies (Epic Games, Valve, etc.) every financial number is an estimate — always label it that way (e.g. "$6B estimated 2025 revenue (Sacra)").
7. Find at least one independent third-party TAM estimate for the industry, in addition to whatever the company cites.
8. Find the company's historical trading multiples — peak, trough, and current. If exact multiples aren't available, get stock prices at key dates (IPO, peak, recent trough) so you can approximate.
9. Find upcoming catalysts over the next 6-12 months: next earnings date, investor day or conferences, product launches, regulatory deadlines, debt maturities, lockup expirations. Search "{{company name}} next earnings date 2026", "{{company name}} investor day 2026", and check the 10-K for any disclosed forward dates. Include consensus estimates for the next earnings period if you can find them — they'll be used to anchor catalyst thresholds.
10. FISCAL CALENDAR CHECK. Record the fiscal year-end month for the subject company AND every peer. Companies that don't share the same fiscal year-end cannot be compared on "latest fiscal year" alone — their reported years cover different calendar periods. For any company whose latest reported fiscal year is stale relative to the others, OR whenever the subject's fiscal year doesn't align with the calendar year, gather the last 4 reported quarters of revenue (and EBITDA / FCF where available) so a trailing-twelve-month (LTM, a.k.a. TTM) figure can be built. For the subject company, capture BOTH (a) latest completed fiscal year (LFY) revenue and (b) LTM revenue (trailing 4 quarters as of the most recent reported quarter). Note where they materially differ.
11. CYCLICAL-INDUSTRY DATA — gather this ONLY if the subject operates in an industry with well-documented boom-bust cycles (memory/commodity semiconductors, commodity chemicals, shipping, energy, mining, and the like). Skip this item entirely for companies where cyclicality is not the primary analytical lens (e.g. Netflix, Roblox). When it applies, gather: (a) historical cycle duration for this industry (how many quarters past upcycles and downcycles have lasted); (b) 3-4 leading indicators of a cycle turn for this specific industry (e.g. inventory days, spot-vs-contract price spread, fab/plant utilization, book-to-bill, freight day-rates, rig counts) — for each, the current reading AND the historical level that has signalled prior turns; and (c) the subject company's CapEx and depreciation & amortization for the latest fiscal year (plus a couple of prior years if readily available) to support a supply-side CapEx/Depreciation ratio.

PHASE 1 BUDGET DISCIPLINE: Aim to finish gathering in under 55 tool calls. If you find yourself unable to locate a specific number after 2-3 search attempts, stop searching for it — note it as missing and move on. Missing data goes to Open Questions; it does not justify more searches.

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
- Forward EV/Revenue and forward EV/EBITDA using the midpoint of any guidance gathered in Phase 1 (skip if no guidance was issued)
- Implied current market share against each TAM estimate

For each peer:
- EV/Revenue, EV/EBITDA, P/FCF (use "NM" for negative EBITDA or negative FCF)

FISCAL BASIS RECONCILIATION (required — the comp table and snapshot depend on this):
- Choose ONE revenue basis that can be applied consistently to EVERY company in the comp table. Use LTM/TTM when fiscal years are misaligned (the usual case); LFY is acceptable only when all companies share the same fiscal year-end. Compute each company's comp-table multiples (EV/Revenue, EV/EBITDA, P/FCF) from THAT single basis. Record which basis you chose — you must label it in the report.
- The numerator period, the denominator period, and the printed multiple must all be the SAME period. NEVER pair a TTM/LTM multiple with a fiscal-year revenue figure, or vice versa.
- For the subject company, compute LTM revenue and LFY revenue separately and the percentage difference between them. If they differ by more than 15%, set a flag — the report must show both and explain the divergence.
- If the company has cyclical-industry data, compute the CapEx/Depreciation ratio (latest fiscal year CapEx / depreciation & amortization) as a supply-side indicator, plus the same ratio for any prior years gathered so the trend is visible. A ratio above ~1.5x historically signals capacity buildout that precedes oversupply.
- VERIFY THE MATH before writing. For every row that will appear in the comp table, recompute EV / (revenue on the chosen basis) and assert it equals the EV/Revenue multiple you will print (within rounding). Print, per row: company, EV, revenue (basis-tagged), EV/revenue division result, and the multiple you will publish — side by side. If any row fails to reconcile, fix the inputs and recompute. Do not publish a multiple that doesn't reconcile to the EV and revenue shown in its own row.
- SANITY-CHECK every computed figure, forward/guidance-based figures included. EBITDA can never exceed revenue, and gross profit (revenue x gross margin) caps EBITDA — if any EBITDA you computed is larger than its own revenue base, the inputs are wrong; recompute before writing. Apply the same reconciliation you used for the comp table to the forward block: forward EV/Revenue must equal current EV / forward revenue, and forward EV/EBITDA must equal current EV / forward EBITDA. Do not print a forward multiple that does not reconcile to the EV and the forward figure shown alongside it.

PHASE 2 STOP RULE: Once you have called python_repl, you are NOT permitted to call edgar_search, edgar_fetch, web_search, or fetch_url again. If a metric came out wrong because of bad input, fix the input in your next python_repl call — do not search the web. After at most 3 python_repl calls, proceed directly to Phase 3.

==============================================================
PHASE 3 — Write the report (no tool calls)
==============================================================

Synthesize everything into the report following the format below. Do not call any tool during Phase 3. Start writing immediately with the # heading.

Output format:

# [Ticker]: Initiation Report

## Trading Snapshot
A single-row markdown table with columns: Price | Mkt Cap | 52W Range | Avg Daily Volume | Short Interest | EV/Rev (trailing) | EV/Rev (Fwd). Use the data pulled from Yahoo Finance in Phase 1 and the multiples computed in Phase 2. The trailing EV/Rev column header MUST name the revenue basis it uses — write "EV/Rev (LTM)" or "EV/Rev (FY2025)", not a bare "EV/Rev". Use the SAME basis as the Peer Comp Table so the two cannot be conflated; if for any reason the snapshot uses a different basis than the comp table (e.g. snapshot LTM, comp table LFY), label each explicitly and add a one-line note flagging the difference. Format: price as $XX.XX, market cap as $X.XB, 52W range as $low – $high, avg daily volume as X.XM, short interest as X.X%, multiples as X.Xx. If forward EV/Rev is not available (no guidance), show "—". Add a single italicized line below: *Data as of [today's date].*

## Business Overview
What the company does, revenue model, segment breakdown, key operating metrics management tracks. Cite the 10-K Item 1.

## Financial Profile
Brief prose intro covering the most important takeaways from the year, then the three tables below. Numbers from filings only.

### YoY Financial Summary
Markdown table comparing the most recent fiscal year to the prior year, with a YoY change column. Required rows: Revenue, Gross Profit (or Gross Margin %), Operating Income/Loss, Net Income/Loss, Adjusted EBITDA (if reported), Free Cash Flow. If the company reports segments, include a Revenue by Segment block (one row per segment) within or immediately below this table. Format dollar values consistently (e.g. all $M or all $B).

### Balance Sheet Snapshot
Markdown table of balance sheet position at the most recent period end. Required rows: Cash & Equivalents, Short-term Investments, Long-term Investments, Total Cash & Investments, Total Debt, Net Cash Position (computed = Total Cash & Investments - Total Debt). All values in the same units.

### Key Ratios
Markdown table of derived metrics computed via python_repl. Include the subject company's growth rates, FCF margin/yield, SBC %, SBC-adjusted FCF yield, revenue/bookings per DAU, and net cash position.

### Quality-of-Earnings Notes
Whenever a derived metric materially diverges from the headline number, add one short sentence flagging the divergence. Examples of triggers (not exhaustive):
- SBC-adjusted FCF is less than 50% of reported FCF — note SBC dilution materially erodes economic FCF.
- Adjusted EBITDA differs from GAAP operating income by more than 25% — note what's being added back.
- FCF and net income differ significantly — note working capital, deferred revenue, or non-cash items driving the gap.
- Bookings growth diverges from revenue growth by more than 5 points — note what the deferred revenue dynamic implies.

Do not add commentary when the headline and derived numbers agree directionally. The point is to flag where the surface story misleads, not to narrate every metric.

## Management Commentary
Key themes from recent earnings calls, forward guidance, strategic priorities, and any analyst concerns the CEO/CFO addressed.

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
Subject company's current market cap, EV, and the multiples you computed. Show the inputs and arithmetic.

### Peer Comp Table
One-sentence rationale for peer selection, then a markdown table with the subject company in row 1 and 4-6 peers below. Columns: Company | EV ($B) | Rev ($B) | Rev Growth | EV/Rev | EV/EBITDA | P/FCF. Use "NM" for non-meaningful values (negative EBITDA, negative FCF). Add a one-line note below the table positioning the subject company vs peers (premium or discount, and why).

Single-basis rules (non-negotiable — these come straight from Phase 2):
- Every company in the table must use the SAME revenue basis (the one chosen in Phase 2). Label it in the Rev column header: "Rev (LTM, $B)" or "Rev (FY2025, $B)". A bare "Rev ($B)" is not acceptable when fiscal years are misaligned.
- The multiples must be computed from the SAME revenue basis shown in the table. If the Rev column is FY2025 revenue, EV/Rev must equal EV ÷ FY2025 revenue. Quote the verified numbers from Phase 2 — do not mix a trailing multiple with a fiscal-year revenue figure.
- When the subject company's LTM revenue differs from its LFY revenue by more than 15% (the flag set in Phase 2), add a row or a footnote immediately below the table showing BOTH figures and a one-line reason for the divergence — e.g. "LTM revenue of $58B vs FY2025 revenue of $37.4B reflects explosive QoQ growth in Q1-Q2 FY2026."

### Forward Context
If the company has issued forward guidance or pre-announced numbers (next quarter or full year), compute approximate forward multiples using the guidance midpoint via python_repl. Show the inputs (guided revenue, guided EBITDA if given, current EV) and the resulting forward EV/Revenue, EV/EBITDA, etc. The forward EBITDA you show MUST be less than forward revenue (and below implied forward gross profit) — an EBITDA above the revenue it is drawn from is impossible and means the input is wrong; recompute in python_repl before writing. Forward EV/EBITDA must equal current EV / forward EBITDA and forward EV/Revenue must equal current EV / forward revenue; both must reconcile to the figures you print. Add one sentence noting how the forward multiple compares to trailing — analysts shouldn't be working off stale numbers when newer ones are available. If no forward guidance exists, write "No forward guidance issued; trailing multiples shown above are the latest available." and skip the table.

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
- Bullish/Bearish columns must use concrete numeric thresholds where possible. Anchor thresholds to consensus, guidance midpoints, or trailing trends. Example: if trailing DAU growth was 65% and 2026 guidance implies 20-25%, write "Bullish: >30% / Bearish: <15%" — not "above/below expectations".
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

def _setup_clients() -> tuple[OpenAI, TavilyClient]:
    load_dotenv()
    moonshot_key = os.environ.get("MOONSHOT_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    if not moonshot_key:
        typer.echo("MOONSHOT_API_KEY not set (add it to .env)", err=True)
        raise typer.Exit(1)
    if not tavily_key:
        typer.echo("TAVILY_API_KEY not set (add it to .env)", err=True)
        raise typer.Exit(1)
    return (
        OpenAI(api_key=moonshot_key, base_url=BASE_URL),
        TavilyClient(api_key=tavily_key),
    )


# ---------- Commands ----------

@app.command()
def research(
    ticker: str = typer.Argument(..., help="Stock ticker, e.g. RBLX"),
    question: str = typer.Argument(..., help="Research question in quotes"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print each tool call"),
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

    _save(
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


@app.command()
def initiate(
    ticker: str = typer.Argument(..., help="Stock ticker, e.g. RBLX"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print each tool call"),
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

    _save(
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


if __name__ == "__main__":
    app()
