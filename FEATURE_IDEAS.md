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

## Speed / cost optimizations

Surfaced by the NTES test run (May 31, 2026 — **22.4 min total, 95 tool calls**, hit the budget cap).
Timing instrumentation (commit `286d949`) revealed:

| Component | Time | Share |
|---|---|---|
| Tool execution (all 95 calls) | 115s | **9%** |
| Model thinking between tool calls | ~590s | **~44%** |
| Phase 3 synthesis | ~636s | **~47%** |

**Headline insight:** tool execution is NOT the bottleneck. Wins come from (a) reducing tool call
count — kills tool time AND inter-batch model thinking, (b) tightening Phase 3 synthesis length —
linear in output tokens. Aggressive within-turn parallelization buys very little.

### ~~S1. Pre-fetch predictable documents in parallel before Phase 1~~ ✅ Shipped (commit `838e53b`, June 2, 2026)
8 deterministic subject-company fetches (4 Yahoo URLs + 4 EDGAR form searches: 10-K, 20-F, 10-Q, 6-K)
run via `ThreadPoolExecutor` before `agentic_loop` starts and inject as synthetic tool messages.

**Measured impact (NTES re-test, June 2, 2026):** 8 calls in **3.4s wall** vs ~160s sequential
in the loop — frees ~115s and 8 budget slots. Phase 1 in-loop reduced from ~10 min to ~8.4 min.

### ~~S2. Tighten Phase 1 search-attempt cap per data point~~ ✅ Shipped (commit `26ce4ff`, June 2, 2026)
Hard 2-attempt rule with concrete examples of what counts as an attempt; PRIMARY vs SECONDARY data
tiering; explicit "mark NM and move on" rule for unreachable peers.

**Measured impact:** NTES in-loop tool calls **77 vs 95 prior** (-19%). Model now correctly returns
NM for hard-to-find data (e.g. short interest) after 2 attempts instead of burning 5+ searches.

### ~~S3. Tighten section word caps in Phase 3 synthesis~~ ✅ Shipped (commit `26ce4ff`, June 2, 2026)
Explicit word caps across Business Overview (~250), Market Opportunity (~180), Comp Landscape (~80),
Historical Context (~120), Key Risks (5 risks × ~25-30 words), Bull/Bear/Base (~80-100 each).
Q&A Analysis was already at ~500.

**Measured impact:** Phase 3 synthesis **~4 min vs ~10.6 min prior** (-60%). Major win.

### ~~S7. Code-side `python_repl` cap enforcement~~ ✅ Shipped (commit pending, June 2, 2026)
- **Surfaced by:** the NTES re-test where the model ran **6 python_repl iterations** despite the
  prompt's "AT MOST 3" rule — debugging its own code errors. Cost ~15 min on a 32-min run.
- **Change:** `PHASE2_REPL_CAP = 3` constant; `agentic_loop` tracks `python_repl_calls_used` and
  returns a "budget exhausted, write the report with what you have" tool message on call #4+
  instead of executing. Prompt-only rules don't bite when the model thinks it has good cause; the
  cap had to move to code, mirroring how `tool_calls_used >= max_tool_calls` is already enforced.

### ~~S8. LFY column hardening (HARD CHECK in Financial Summary; LFY year stamp in Phase 2)~~ ✅ Shipped (commit pending, June 2, 2026)
- **Surfaced by:** both NTES runs (May 31 and June 2) labelled FY2024 as LFY when actual LFY is
  FY2025 — even though the FY2025 20-F was filed April 15, 2026 and cited in the report's own
  Sources. The Phase 1 step 11 rule ("LFY = most recent COMPLETED fiscal year") wasn't biting at
  the table-writing moment.
- **Change:** (a) Phase 3 Financial Summary section now has an inline `LFY HARD CHECK` with a
  worked example ("today is June 2026 → LFY = FY2025; showing FY2023 | FY2024 | LTM is WRONG").
  (b) Phase 2 subject-compute list now requires the first python_repl call to print
  `LFY = FYxxxx` based on today's date — anchors the year stamp in conversation before Phase 3
  writes the table.

### S4. Parallelize tool calls within an assistant turn — *queued*
- **Problem:** When the model emits multiple `tool_calls` in one response (e.g., a batch of 3 peer
  fetches), `agentic_loop` runs them sequentially.
- **Change:** Use `asyncio.gather` over the tool_calls list. Combine with a prompt nudge to batch
  ("when gathering peer data, emit all peer fetches in a single response").
- **Estimated savings:** modest — **~10-30s**. The model doesn't always batch, and tool execution
  is only ~9% of total wall time. Higher-leverage items above already shipped.
- **Effort:** ~2 hours (async refactor of the tool-execution loop).

### S5. Disk cache for SEC filings (TTL 90 days) — *queued*
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

### NTES re-test outcome (June 2, 2026) — what we measured

S1 + S2 + S3 all worked as designed. Phase 1 was ~1.5 min faster (S1 + S2); Phase 3 was ~6.5 min
faster (S3). The combined wins would have produced a **~17-18 min total** — squarely in the target
band of 13-17 min.

But the run hit **32.1 min** because of a Phase 2 regression: the model iterated `python_repl` 6
times (cap is 3), debugging its own code errors. That ate ~15 min. S7 (code-side enforcement) fixes
this directly. S8 fixes the LFY off-by-one that repeated in this run despite the `286d949` prompt
tightening.

Next NTES re-test (post S7 + S8) should land in the 17-18 min range with the correct LFY year stamp.

---

## Not yet queued (parked)

- **SBC dilution / share-count creep** as a Quality-of-Earnings element — share creep %, SBC vs. GAAP net
  loss, the "adj. EBITDA *ex-*SBC add-back" test (Freedom/RBLX Exhibit 17). Extends our existing QoE triggers.
- **GAAP↔non-GAAP bridge table** (Deutsche Bank/NetEase Fig 22) — structured SBC walk; related to the above.
- **"Positives / Concerns" scannable exec-summary** up front (Freedom/RBLX page 2) — overlaps Bull/Bear; low priority.
