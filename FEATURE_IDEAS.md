# Le Analyst — Feature Backlog

Running list of candidate improvements to the `initiate` (and where noted, `research`)
output, sourced from teardowns of professional sell-side initiation reports plus
discoveries from our own test runs. Each section below tracks one class of work.

**Constraints every item must respect** (from CLAUDE.md): no buy/sell/hold; no dollar price
target (express as % ranges / "what's priced in"); stay single-file; prefer prompt-template
edits; mind the Phase-1 (~65 call) and Phase-2 (3 `python_repl`) budgets; total budget cap 95.

---

## Done

### ~~1. Peer Comp Table — forward multiples + median row~~ ✅ Shipped (commit `8230575`, May 30, 2026)
Final shape:
- 10 columns: Company | EV | Rev (LTM) | 2-yr Rev CAGR | EV/Rev (LTM) | EV/Rev (FY+1E) | EV/Rev (FY+2E) | P/E (LTM) | P/E (FY+1E) | P/E (FY+2E)
- Peer Median row (HARD RULE: fewer than 3 valid non-NM values → `—` with "insufficient peer coverage" footnote)
- Dropped EV/EBITDA and P/FCF
- Forward columns from Wall Street consensus, FY+1 and FY+2 both required
- 2-yr Rev CAGR = `(FY+2E revenue / LFY revenue)^(1/2) − 1`

Margin columns and multi-peer-basket support were considered but **deferred**. Re-open if a future
report's "premium for what?" framing demands it.

---

## Queued — analytical depth

### 2. Numeric scenario-assumptions table behind Bull / Base / Bear
- **Source:** Deutsche Bank, NetEase initiation (Fig 16 — assumption transparency).
- **Problem:** The Investment Framework states a "% re-rating range" in prose; the
  revenue-growth / margin / exit-multiple inputs that produce it are never shown, so the
  range isn't auditable and bull-vs-bear rigor can't be checked.
- **Change:** Add a small table — one column per scenario, rows for the key drivers
  (revenue/bookings growth, margin, exit multiple) → implied % move. Enforces equal-rigor
  by construction and keeps us within the no-price-target rule. Pairs naturally with showing
  the *implied multiple at each scenario* (the sell-side "at PT" row, minus the point target).

### 3. Primary-metric detection + metric-aware valuation
- **Source:** Freedom Capital Markets, Roblox initiation (page 1 KPIs; Exhibit 1 comp table; income-statement bookings bridge).
- **Problem:** We default to GAAP revenue and GAAP earnings. For bookings / GMV / ARR / RPO-driven
  businesses that misstates both growth and value — RBLX FY25 revenue grew ~38% vs. **bookings ~53%**,
  and the company was GAAP loss-making while adj. EBITDA and FCF were strongly positive. EV/Rev and P/E
  would mis-rank it against peers and miss the thesis.
- **Change:** Detect the company's primary operating metric in Phase 1; build growth, the comp-table
  multiples, and the valuation lens around it (EV/Bookings, EV/Adj. EBITDA, …) when it diverges from
  GAAP revenue; show a metric↔GAAP-revenue bridge (e.g. revenue + Δdeferred revenue = bookings) and flag
  the gap. Makes the comp basis metric-aware.

### 4. Guidance vs. consensus vs. seasonal-setup triangulation
- **Source:** Freedom/RBLX (page 1 estimate-vs-consensus-vs-guidance; page 21 Q4 seasonality).
- **Status:** *Partially addressed.* Consensus-vs-guidance gap commentary now lives in
  Forward Estimates (shipped May 2026). What's still missing is the **seasonal-setup**
  third leg — comparing guidance to the company's own historical seasonal/sequential
  pattern to flag conservative or stretched guidance.
- **Remaining change:** In Phase 1, gather 2–3 years of quarterly history; in the writeup,
  triangulate guidance vs. consensus vs. seasonal pattern and state whether the setup looks
  conservative / in-line / stretched, with numbers. Framed as "the debate," never a call.

### 5. Unit-economics / take-rate walk
- **Source:** Freedom/RBLX (Exhibit 5 consumer-dollar split; Exhibit 4 DevEx per developer over time).
- **Problem:** For platform / marketplace / transactional businesses the take-rate and per-unit economics
  *are* the thesis, and we have no dedicated treatment.
- **Change:** When the model takes a cut of GMV/spend, add a unit-economics block: where each dollar goes
  (take rate vs. partner / processing / cost), take-rate trend over time, and a per-unit productivity metric
  (per user / per seller / per transaction). Conditional — skip for non-platform names.

### 6. Governance & ownership lens
- **Source:** Freedom/RBLX (page 12 dual-class & founder control; Exhibits 7–8 management/board bios).
- **Problem:** We surface management commentary but nothing on control structure, insider ownership, or
  key-person risk — material for founder-controlled or dual-class names (Baszucki holds 100% of the
  20-votes/share Class B; CEO/President/Chair combined).
- **Change:** Add a short governance/ownership note: share-class & voting structure, founder/insider
  economic vs. voting control, CEO-Chair combination, key-person dependence, notable board/management
  background. State asymmetries factually; no editorializing.

---

## Queued — speed / cost optimizations

Surfaced by the NTES test run (May 31, 2026 — **22.4 min total, 95 tool calls**, hit the budget cap).
The timing instrumentation we added (commit `286d949`) reveals the cost breakdown:

| Component | Time | Share |
|---|---|---|
| Tool execution (all 95 calls) | 115s | **9%** |
| Model thinking between tool calls | ~590s | **~44%** |
| Phase 3 synthesis | ~636s | **~47%** |

**The headline insight:** tool execution is NOT the bottleneck. Wins come from (a) reducing tool call
count — which kills both tool time AND inter-batch model thinking, (b) tightening Phase 3 synthesis
length — linear in output tokens. Aggressive within-turn parallelization buys very little.

### S1. Pre-fetch predictable documents in parallel before Phase 1
- **Problem:** Every `initiate` run burns 5-10 tool calls on the same predictable fetches: 10-K/20-F,
  latest 10-Q/6-K, last 2 transcripts, Yahoo quote/financials/analysis for the subject.
- **Change:** Before `agentic_loop` starts, fan out `asyncio` fetches for these known-needed docs and
  inject them into the initial prompt as pre-gathered context. The model starts Phase 1 with the
  foundational data already in hand.
- **Estimated savings:** ~60-120s per run + cleaner prompt-cache invariant prefix.
- **Effort:** ~2 hours.

### S2. Tighten Phase 1 search-attempt cap per data point
- **Problem:** The model sometimes burns 3-5 searches chasing one number that isn't accessible.
  NTES hit the 95-call cap largely from redundant searches against Chinese-source data.
- **Change:** Prompt instruction: "after 2 unsuccessful attempts to find any specific number, mark it
  'not disclosed' and move on." Concrete examples of what counts as an attempt vs. a productive search.
- **Estimated savings:** ~3-5 min on hard cases (cuts inter-batch model thinking dramatically — every
  avoided batch ≈ ~20s of model reasoning).
- **Effort:** ~30 min prompt edit.

### S3. Tighten section word caps in Phase 3 synthesis
- **Problem:** Synthesis wall time is roughly linear in output tokens. We've kept adding sections
  (Q&A Analysis, Forward Estimates, etc.) without enforcing per-section length discipline.
  Current reports run ~20-24 KB; could be ~15-18 KB without losing analytical depth.
- **Change:** Add explicit word caps to Business Overview, Market Opportunity, Competitive Landscape,
  and each Investment Framework subsection. The Q&A section already has one (~500 words).
- **Estimated savings:** ~1.5-2.5 min (cuts ~25% of synthesis time).
- **Effort:** ~30 min for the prompt edits; iterating to find right caps without losing depth.

### S4. Parallelize tool calls within an assistant turn
- **Problem:** When the model emits multiple `tool_calls` in one response (e.g., a batch of 3 peer
  fetches), `agentic_loop` runs them sequentially.
- **Change:** Use `asyncio.gather` over the tool_calls list. Combine with a prompt nudge to batch
  ("when gathering peer data, emit all peer fetches in a single response").
- **Estimated savings:** modest — **~10-30s**. The model doesn't always batch, and tool execution
  is only ~9% of total wall time. Higher-leverage items above first.
- **Effort:** ~2 hours (async refactor of the tool-execution loop).

### S5. Disk cache for SEC filings (TTL 90 days)
- **Problem:** SEC filing content doesn't change. Repeat-ticker runs re-fetch identical documents.
- **Change:** Simple disk cache keyed by URL with 90-day TTL on `edgar_fetch` and the relevant
  `fetch_url` paths to SEC.gov / EDGAR.
- **Estimated savings:** ~10-20s on repeat-ticker runs; protects against SEC rate-limiting at scale
  (relevant once we have a hosted backend).
- **Effort:** ~1 hour.

### S6. (Architectural — deferred) Parallel Phase 3 synthesis
- For a hosted backend (see `WECHAT_MINIAPP_PLAN.md`), Phase 3 synthesis could be split into two
  parallel model calls — e.g., (Business + Financial Profile + Market + Competition) and
  (Valuation + Risks + Framework + Open Questions + Sources) — running concurrently with shared
  Phase-1+2 context. Stitched at the end.
- **Estimated savings:** could nearly **halve synthesis time** on hard cases (saving 5+ min on
  the NTES-class workload).
- **Effort:** significant — needs the library refactor planned in `WECHAT_MINIAPP_PLAN.md`.
- **Defer until** the hosted-backend architecture lands.

---

## Not yet queued (parked)

- **SBC dilution / share-count creep** as a Quality-of-Earnings element — share creep %, SBC vs. GAAP net
  loss, the "adj. EBITDA *ex-*SBC add-back" test (Freedom/RBLX Exhibit 17). Extends our existing QoE triggers.
- **GAAP↔non-GAAP bridge table** (Deutsche Bank/NetEase Fig 22) — structured SBC walk; related to the above.
- **"Positives / Concerns" scannable exec-summary** up front (Freedom/RBLX page 2) — overlaps Bull/Bear; low priority.
