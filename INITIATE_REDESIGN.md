# Initiate redesign — draft outline

**Status:** draft v0 for review. Not implemented yet.

**Goal:** repurpose `analyst initiate TICKER` from the current 5,000-word
sell-side-style long form into a retail-shaped ~1,200-word thesis brief.
The current implementation is preserved as `analyst initiate_legacy` for
fallback and reference.

**Audience shift:** the current report assumes an analyst-level reader
who'll read 25KB top-to-bottom. The new shape is sized for a Xueqiu /
Yahoo-Finance companion on a phone — 90-second read, thesis up top, no
column-scroll on a 6-inch screen.

**Reading order — what changes:** old order buries the payoff (bull/bear/
base, debates, catalysts) in section 9 of 11. New order leads with the
30-second thesis and the catalyst calendar, demotes everything analytical
to the back. The 11-section sell-side furniture (Trading Snapshot,
Business Overview, Financial Profile, Sell-Side Q&A, Market Opportunity,
Competitive Landscape, Valuation Context, Key Risks, Investment Framework,
Open Questions, Sources) becomes 8 sections, several of which are
compressed substantially.

---

## Section-by-section spec

Word budgets are targets, not caps. Total target: ~1,200 words core
(vs. ~3,500 today).

### 1. Thesis (new, top of report, ~120 words)

**Purpose:** what a reader gets from this report in one paragraph.
Mandatory. Single paragraph, no sub-bullets. Three required beats:

- **What the company is** (1 sentence, plain language; not the 10-K
  boilerplate description).
- **Why it matters right now** (1-2 sentences — the catalyst window, the
  re-rating in progress, the news cycle the reader is in).
- **The one number that decides the debate** (1 sentence — e.g. "if AI
  semi revenue hits $10B in FY27 the bull case works; if it slips below
  $8B the multiple compresses").

**Structural rules:**
- No buy/sell/hold phrasing, no dollar price target (existing CLAUDE.md
  rules apply harder here than anywhere else because the thesis is what
  retail readers anchor on).
- The "one number" must reappear in section 4 (Bull/Bear/Base) as a
  threshold that distinguishes the cases. Validator-able later.
- No citations in the thesis paragraph itself — it's a synthesis layer.
  All facts cited in their respective downstream sections.

### 2. Business snapshot (~150 words)

Three sentences, then a five-column numbers table.

- **What they do, who they sell to, how they make money** — 3 sentences,
  not the marketing-speak company-overview prose.
- **Numbers table (mobile-safe, 5 columns):**

  | Price | Mkt Cap | 52W Range | Fwd P/E (or EV/Rev) | 2yr Rev CAGR |

  The "Fwd P/E or EV/Rev" column auto-chooses based on profitability
  (C4 already enforces this in the current system). 2yr Rev CAGR is the
  consensus FY+1 / FY+2 figure we already gather.

**What got dropped:** Trading Snapshot's 11-column row (EV, Avg Daily
Volume, Short Interest, EPS LTM, EPS Fwd, P/E LTM, etc.) — full row
moves to appendix or a "more numbers" disclosure.

### 3. What the Street is debating (~300 words)

**Purpose:** merges the old Sell-Side Q&A Analysis + Key Debates into
one cohesive section. Three items, in this format:

```
### Debate 1: [one-sentence statement of the disagreement]
- **Bulls argue:** [one sentence, with [N] citation]
- **Bears argue:** [one sentence, with [N] citation]
- **What resolves it:** [one sentence — the specific number or
  disclosure the reader should watch for]
```

**Selection criteria for the three debates (priority order):**
1. Must be debates that actually move the stock (not management-style
   "execution risk" platitudes).
2. Must have at least one specific resolving signal (a number, a
   disclosure date, an event).
3. Drawn from the latest earnings call Q&A topics — not invented to
   fill space.

**What got dropped:** the existing Sell-Side Q&A Analysis section
(themed sub-blocks of "Probed by / Sharpest question / Management
response / What it implies") was good analyst-grade work but redundant
with Key Debates. The new format is one section, not two.

### 4. Bull / Bear / Base (~250 words)

**Purpose:** explicit, verifiable return math for three scenarios.

Each case = 2-3 sentences, structured:

```
### Bull (assumed probability: ~X%)
[Multiple] × [EPS or Revenue assumption] = [implied % move]
[1-2 sentences explaining what has to be true for this case]
```

**Hard rules for this section:**
- Return math must be EXPLICIT and SHOWN (so C7 / future V-04 can verify
  it; not "the stock could appreciate 30-50%" — the actual computation).
- Bull return > base return > bear return (V-04).
- Probabilities must sum to ~100%.
- Any multiple referenced must exist in the appendix comp table OR be
  explicitly sourced (V-04).
- Holding the current forward multiple flat is fine as a base case —
  but the assumption must be stated, not implicit.

### 5. Top 3 risks (~120 words)

Three risks, 30-40 words each. Format:

```
- **[Risk name]:** [what could happen, what it would do to the
  thesis, the threshold that would change the picture] [N]
```

**Selection rules:**
- Per-sector baseline checklist applies (semis: TSMC/Taiwan
  concentration, export controls, cycle/inventory, customer
  concentration, pricing erosion). Model must address or
  consciously exclude.
- 3 is the cap. If a fourth genuinely matters, raise it. Otherwise
  the long-tail goes into appendix.

### 6. Catalyst calendar (kept full, ~200 words)

**Unchanged from current shape** — this is the most actionable section
in the old report and survives intact. Table:

| Date | Event | What to Watch | Bullish Signal | Bearish Signal |

7-9 rows covering ~12 months out. Earnings dates, investor events,
competitor announcements, and product/customer milestones. Thresholds
must be numeric where possible.

### 7. Appendix (collapsible / "show more" in mini-app, ~variable)

Everything from the old structure that's still load-bearing for the
serious reader but doesn't fit the 90-second flow:

- **Full numbers row** (Trading Snapshot's 11-column shape)
- **Financial Summary** (prior FY / LFY / LTM table)
- **Forward Estimates** (consensus FY+1 / FY+2 + management guidance +
  gap commentary)
- **Quality-of-Earnings notes** — compressed to one or two flags only
  when they materially change the headline multiple (the standing
  trigger rules in CLAUDE.md still apply).
- **Peer Comp Table** (LTM + FY+1E + FY+2E EV/Rev and P/E columns,
  2-yr Rev CAGR, peer Median row — same as today).
- **Competitive Landscape** (Stated Position + Independent Evidence
  table — same as today).
- **Market Opportunity** (company TAM + independent TAM + implied
  share + methodology caveat — same as today).
- **Historical Context** (multi-year price/multiple cycle).

In the CLI output, the appendix appears as `## Appendix` with H3
subsections inside. The mini-app renders it as a collapsible "查看更多 /
Show full report" block after the catalyst calendar.

### 8. Sources (kept, ~variable)

Numbered list, same as today. Every claim in the body, debates, scenarios,
risks, and appendix must cite a `[N]` that resolves to a Sources entry
with a real URL (C7 enforces).

---

## What's gone vs. what's compressed

| Old section | New location |
|---|---|
| Trading Snapshot (11 columns) | Section 2 (compact) + Appendix (full) |
| Business Overview (~250 words) | Section 1 thesis + Section 2 (3 sentences) |
| Financial Profile / Financial Summary | Appendix |
| Financial Profile / Forward Estimates | Appendix |
| Financial Profile / QoE Notes | Appendix (compressed to flags only) |
| Sell-Side Q&A Analysis (~500 words) | Merged into Section 3 (debates) |
| Market Opportunity (~180 words) | Section 1 thesis (one sentence) + Appendix |
| Competitive Landscape | Appendix |
| Valuation Context (~variable) | Section 4 (return math) + Appendix (comp table) |
| Key Risks (5 risks ~30 words each) | Section 5 (3 risks) |
| Investment Framework | Sections 1 + 3 + 4 (split across thesis, debates, scenarios) |
| Open Questions | NOT in user-facing output — routed to validator metadata only |
| Sources | Section 8 (unchanged shape) |

**Most significant decision:** Open Questions section is removed from the
shipped report entirely. It's the model confessing data gaps — valuable
internally for validators but trust-eroding for a retail reader who
encounters it on page 1 of their first interaction. Internal validators
(future V-03) consume it; the user never sees it.

---

## Implementation plan

1. **Rename current `initiate` to `initiate_legacy`** — preserves the
   ~3,500-word sell-side shape as a fallback. Same code, same prompt,
   same Phase 4 checks. Available via `analyst initiate_legacy MRVL`.
2. **New `initiate` command** with the structure above.
   - Tool budget: ~50 calls (vs 95 today). The deeper sections (peer
     comps, full financial summary, TAM) still get gathered for the
     appendix, but the synthesis layer is lighter.
   - Phase 4 reviewer: C1-C7 mostly applicable. C5 (required sections)
     needs updating to the new section list. C4 (forward valuation lens)
     applies to Section 2. C7 applies as today.
   - New prompt template: `INITIATE_SYSTEM_PROMPT_TEMPLATE` rewritten
     for the new shape.
3. **Mini-app submit page:** ticker-only (no question textarea) for the
   default flow → `initiate`. If user expands "ask a specific question"
   → falls back to `research`. Decision deferred until v0 brief output
   lands.
4. **Validators TBD based on v0 output:** generate the new initiate on
   3-5 tickers (MRVL, MU, NFLX, BILI, AVGO), read carefully, then
   prioritize which of V-01 / V-04 / V-05 / V-08 to ship next based
   on what actually fails.

---

## Open design questions for review

1. **Section 1 "one number that decides the debate"** — is this the right
   load-bearing element? Alternative: "the one number to watch from the
   next quarterly print." The first is thesis-anchoring, the second is
   catalyst-anchoring. Pick one.
2. **Section 4 explicit probabilities** — should probabilities be
   numeric ("Bull: ~25%") or qualitative ("plausible / our base /
   downside")? Numeric is V-04-checkable and reads decisively; qualitative
   avoids false precision.
3. **Section 7 Appendix in mini-app** — collapsible "show full" feels
   right but adds UI work. Acceptable to ship v0 without it (appendix
   just shows below the catalyst calendar, no toggle) and add the
   collapse later? My vote: yes.
4. **Investment Framework section name vanishes** — the discipline (no
   buy/sell/hold, no $ PT, equal rigor on bull/bear) propagates through
   sections 1, 3, 4. Do we lose any structural enforcement by removing
   the section header? My read: no, but worth confirming.
5. **Open Questions sink** — agreed it doesn't ship to users. But where
   does the model emit it for validator consumption? Options: (a) inside
   a `<!-- internal -->` HTML comment block stripped before save; (b) in
   a separate metadata field in YAML frontmatter; (c) the model just
   prints it to stderr/log. My vote: (b), but minimal infrastructure
   either way.
6. **Research command stays as-is** — the question-driven brief
   (`research TICKER "Q"`) is still useful for "why did AVGO drop today"
   reactive use. The mini-app keeps it as a "ask a specific question"
   path alongside the default initiate. Agreed?

---

## Not changed by this redesign

- `research` command (question-driven brief, ~5KB) stays as-is.
- `analyst translate` is already gone (retired in the language refactor).
- File routing (`briefs_en/` / `briefs_ch/` / `reports/`) stays as-is.
  New initiate output still lands in `reports/`.
- `--lang en|zh` and `--model kimi|deepseek` flags apply to the new
  initiate same as everywhere else.
- Phase 4 review architecture stays; C-checks slot into the new shape
  with C5's required-sections list updated.
