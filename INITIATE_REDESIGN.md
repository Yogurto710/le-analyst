# Initiate redesign — draft outline

**Status:** decisions locked, ready for implementation. Not yet coded.

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
- **The one number to watch from the next quarterly print** (1 sentence —
  e.g. "Q2 FY27 AI semi revenue: bulls want $11B+, bears flag anything
  below $9.5B"). Catalyst-shaped, not thesis-shaped: gives the reader a
  concrete bullish/bearish signal threshold to track on a specific
  upcoming event.

**Structural rules:**
- No buy/sell/hold phrasing, no dollar price target (existing CLAUDE.md
  rules apply harder here than anywhere else because the thesis is what
  retail readers anchor on).
- The "one number" must reappear in section 6 (Catalyst Calendar) as
  the bullish/bearish signal threshold on the relevant earnings event,
  so the reader can re-find it during the actual quarter.
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

### 3. What the Street thinks (~400 words)

Merges three things that used to live separately and were really one
flow: forward consensus estimates, the gap to management guidance, and
the three debates the Street is actively having. Anchoring the debates
on the consensus numbers grounds them in concrete expectations rather
than leaving them to read as abstract pro/con bullet points.

**3a. Street consensus (compact table + 1-2 sentence gap commentary)**

| | FY+1E | FY+2E |
|---|---|---|
| Revenue | $X.XB (+XX% YoY) | $X.XB (+XX% YoY) |
| EPS | $X.XX | $X.XX |

Footnote line: analyst count + as-of date + source. One footnote
sentence: "Consensus assumes [the load-bearing assumption]; management
guided to [midpoint] which implies [Y% vs consensus]." If the gap is
material (>5%), name the direction (Street ahead / behind).

**3b. Three debates** — each in this format:

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

**What got dropped:** the standalone Sell-Side Q&A Analysis section
(themed sub-blocks of "Probed by / Sharpest question / Management
response / What it implies") was good analyst-grade work but redundant
with Key Debates. The standalone Forward Estimates section (with
analyst-count and gap-commentary) is absorbed here where it does the
most work.

### 4. Bull / Bear / Base (~250 words)

**Purpose:** explicit, verifiable return math for three scenarios.
Same shape as the current `initiate_legacy` Bull/Bear/Base — no
probabilities, no anchor phrasing like "our base case." The case name
is the label.

Each case = 2-3 sentences, structured:

```
### Bull
[Multiple] × [EPS or Revenue assumption] = [implied % move from current]
[1-2 sentences explaining what has to be true for this case]
```

**Hard rules for this section:**
- Return math must be EXPLICIT and SHOWN (multiple × EPS = % move, not
  "the stock could appreciate 30-50%" without the underlying math), so
  future V-04 can verify the arithmetic actually computes the stated
  direction.
- Bull case and bear case get equal analytical rigor — don't signal which
  side you favor.
- No buy/sell/hold phrasing, no dollar price target. Direction expressed
  as % range from multiple re-rating only (existing CLAUDE.md rule).
- Multiples referenced must exist in the appendix Peer Comp Table or be
  explicitly sourced.

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

### 7. Appendix (~variable, ~600-800 words)

The three load-bearing sections that survive the cut, in this order:

1. **Financial Summary** — prior FY / LFY / LTM table. Anchors the
   company's financial baseline (revenue, gross margin, EBITDA margin,
   operating margin, net income, EPS, FCF). Trailing only — forward
   numbers live in Section 3.
2. **Competitive Landscape** — Stated Position + Independent Evidence
   table (same as today). Positions the company against named
   competitors.
3. **Peer Comp Table** — LTM + FY+1E + FY+2E EV/Rev and P/E columns,
   2-yr Rev CAGR, peer Median row (same as today's shape; C3 enforces
   median treatment when <3 peers have valid values).

The "narrowing zoom" logic: baseline financials → who they compete
against → how they're valued vs those competitors. Anything outside
this trio that the reader needs lives elsewhere in the report:
- Forward Estimates → Section 3 (where the debates actually use them)
- Headline trading multiples → Section 2 compact numbers row
- TAM / Market Opportunity → Section 1 thesis (one sentence)
- QoE flags → inline in Section 2 if any trigger ("FY26 GAAP includes
  $1.8B one-time gain; ex-gain forward P/E ~50x"), not a section
- Historical Context, full Trading Snapshot row, standalone Investment
  Framework header → dropped entirely

In the CLI output the appendix appears as `## Appendix` with H3
subsections. The mini-app can render it inline below the catalyst
calendar; collapsibility ("查看更多 / Show full report" toggle) is
deferred to a later UI pass.

### 8. Sources (kept, ~variable)

Numbered list, same as today. Every claim in the body, debates, scenarios,
risks, and appendix must cite a `[N]` that resolves to a Sources entry
with a real URL (C7 enforces).

---

## What's gone vs. what's compressed

| Old section | New location |
|---|---|
| Trading Snapshot (11 columns) | Section 2 (5-col compact) — full row dropped |
| Business Overview (~250 words) | Section 1 thesis + Section 2 (3 sentences) |
| Financial Profile / Financial Summary | Appendix (item 1) |
| Financial Profile / Forward Estimates | **Section 3a** (anchors the debates) |
| Financial Profile / QoE Notes | Inline flag in Section 2 if triggered — no section |
| Sell-Side Q&A Analysis (~500 words) | Merged into Section 3b (debates) |
| Market Opportunity (~180 words) | Section 1 thesis (one sentence) — full block dropped |
| Competitive Landscape | Appendix (item 2) |
| Valuation Context | Section 4 (return math) + Appendix Peer Comp Table |
| Key Risks (5 risks ~30 words each) | Section 5 (3 risks) |
| Investment Framework (header) | **Header dropped** — discipline propagates through Sections 1, 3, 4 |
| Open Questions | YAML frontmatter `open_questions:` list, not in rendered body |
| Historical Context | Dropped entirely |
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
   - Tool budget: ~50 calls (vs 95 today). Three appendix sections
     (Financial Summary, Competitive Landscape, Peer Comp Table) still
     get the per-peer gather work, but TAM / historical context / full
     trading snapshot no longer cost calls.
   - Phase 4 reviewer: C1-C7 mostly applicable, with two adjustments:
     - **C5 required-sections list updates** to: Thesis, Business
       snapshot, What the Street thinks, Bull / Bear / Base, Top 3 risks,
       Catalyst calendar, Appendix, Sources (8 sections, not 11).
     - **C4 forward valuation lens** now operates against Section 2's
       compact numbers row + Section 3a's consensus table.
     - C7 (citation completeness), C6 (gross ≥ EBITDA margin), C3 (peer
       median treatment), C1 (LFY year stamp) all apply unchanged.
   - New prompt template: `INITIATE_SYSTEM_PROMPT_TEMPLATE` rewritten
     for the new shape. `initiate_legacy` keeps the current template
     verbatim — easy revert path.
3. **Mini-app submit page:** ticker-only (no question textarea) for the
   default flow → `initiate`. If user expands "ask a specific question"
   → falls back to `research`. Decision deferred until v0 brief output
   lands.
4. **Validators TBD based on v0 output:** generate the new initiate on
   3-5 tickers (MRVL, MU, NFLX, BILI, AVGO), read carefully, then
   prioritize which of V-01 / V-04 / V-05 / V-08 to ship next based
   on what actually fails.

---

## Decisions locked in

1. **Section 1 anchor:** "the one number to watch from the next quarterly
   print" — catalyst-shaped, not thesis-shaped. Reappears in Section 6
   so the reader can find it during the actual quarter.
2. **Section 4 probabilities:** none. Same shape as `initiate_legacy`'s
   Bull/Bear/Base — case name is the label, equal rigor on bull vs bear,
   direction expressed as % range from multiple re-rating only.
3. **Section 7 Appendix:** three items (Financial Summary →
   Competitive Landscape → Peer Comp Table). Mini-app renders inline
   below catalyst calendar in v0; collapsible toggle deferred.
4. **Investment Framework header:** dropped. Discipline (no buy/sell/hold,
   no $ price target, equal rigor) propagates through Sections 1, 3, 4
   without needing the section label.
5. **Three risks**, not five.
6. **Open Questions sink:** YAML frontmatter `open_questions:` list.
   Stripped from rendered body before save. Future validators can parse.
7. **`research` command stays separate.** Mini-app keeps both paths: ticker
   alone → `initiate` (default, thesis-driven); ticker + question →
   `research` (reactive question-driven).
8. **Forward Estimates moves to Section 3** (was previously in Appendix).
   Anchors the Street debates with the concrete consensus numbers they
   pivot on.

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
