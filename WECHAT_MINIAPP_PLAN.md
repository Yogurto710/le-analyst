# Le Analyst — WeChat Mini-App Planning Doc

Local planning doc, not yet committed. Sketches what it would take to ship a
WeChat 小程序 (mini-app) front-end backed by a cloud-hosted Le Analyst.

## What we're trying to build

A WeChat mini-app that lets a Chinese-audience user request a `research` brief
or `initiate` report on a public equity (NYSE / NASDAQ today; HKEX / A-shares
later), with the agentic loop running on a cloud backend rather than the
user's device. Reports default to Simplified Chinese via the existing
`translate` feature. Reports are stored per-user and re-readable.

The phone is the *trigger and the reading surface*; the work happens on a
server.

## Why this is a multi-month effort, not a sprint

Five things make this genuinely hard:

1. **The agent doesn't fit the request/response shape.** An `initiate` run is
   5-10 minutes and ~95 tool calls. Mini-app HTTP requests have ~60s
   timeouts. We need async: submit → status polling (or WebSocket) → fetch
   when done.
2. **WeChat platform constraints** are strict and externally gated:
   - Every domain the mini-app calls must be **pre-whitelisted** in the
     mini-app admin console. The mini-app talks ONLY to our backend.
   - Mini-app review for **financial categories** is heavier than average.
   - Code-size limits (2 MB main package; subpackages help).
3. **ICP filing (备案)** for any backend domain serving from mainland China
   takes 2-4 weeks and requires a registered business entity in China.
4. **Financial-content compliance.** Even with our existing discipline (no
   buy/sell/hold, no price target, "as-produced not polished demo"), the
   line between "data tool" and "investment advisory" (证券投资咨询) is
   regulator-defined. Disclaimer scaffolding is mandatory; whether we need
   a 投顾 license is a real question.
5. **Network paths.** Tavily and SEC EDGAR are US-based. If the backend
   sits in mainland China, both face Great-Firewall variability. The
   cleanest architecture puts the worker in **Hong Kong or Singapore**
   (Tencent Cloud has both), with the API gateway potentially in mainland
   China.

None of these are blockers individually; together they mean Phase 0
(decisions + external filings) is on the critical path before meaningful
code work begins.

## Decisions to make before writing any code

These are the questions that, if answered wrong, force expensive rework.
Recommendation column is my view; push back on any.

| Decision | Options | Recommendation |
|---|---|---|
| Hosting region for worker | Mainland China / HK / Singapore / hybrid | **Hong Kong** (Tencent Cloud) — clean access to Moonshot, Tavily, EDGAR; no Great Firewall variability |
| ICP-filed gateway domain | Required if serving mainland users | Yes — get filing started in Phase 0 since it's slowest |
| Compliance posture | "Data tool" framing + disclaimers / pursue 投顾 license / avoid mainland and serve HK+overseas only | Start with **disclaimer-heavy "data tool" framing**; revisit license if traction warrants |
| Monetization | Free (eat the cost) / freemium with credits / WeChat-Pay subscription | **Free credits → WeChat-Pay top-up.** ~$0.75/initiate compounds fast; even 100 users × 1 report/week = $300+/wk in API spend |
| Default language | zh-only / bilingual toggle | **zh-only by default** (translate runs automatically on save); English available via toggle |
| Reports library | Per-user persistent history / one-shot only | **Per-user**, makes the product actually useful and sharable in WeChat |
| Surface | Mini-app (小程序) / H5 page / WeChat Public Account | **Mini-app** as primary (your ask); H5 fallback later if review drags |
| Coverage scope at launch | US-listed only / + HK / + A-shares | **US-listed only** at launch — matches current tool & sources |

## Architecture sketch

```
┌──────────────────┐
│  WeChat          │           HTTPS (whitelisted domain)
│  Mini-App        │ ───────────────────────────────────┐
│  (WXML/WXSS/JS)  │                                    │
└──────────────────┘                                    ▼
         ▲                                    ┌──────────────────┐
         │ poll / WebSocket                   │  API Gateway     │
         │                                    │  (ICP-filed,     │
         │                                    │   mainland or HK)│
         │                                    └────────┬─────────┘
         │                                             │ auth (wx.login → openid)
         │                                             ▼
         │                                    ┌──────────────────┐
         │                                    │  Job Service     │
         │                                    │  (FastAPI)       │
         │                                    │  - submit        │
         │                                    │  - status        │
         │                                    │  - fetch report  │
         │                                    └────────┬─────────┘
         │                                             │ enqueue
         │                                             ▼
         │                                    ┌──────────────────┐
         │                                    │  Queue + DB      │
         │                                    │  (Redis + Postgres)
         │                                    │  job state + meta│
         │                                    └────────┬─────────┘
         │                                             │
         │                                             ▼
         │                                    ┌──────────────────┐
         │                                    │  Worker pool     │
         │                                    │  (HK or SG)      │
         │                                    │  - imports       │
         │                                    │    analyst as lib│
         │                                    │  - Moonshot      │
         │                                    │  - Tavily        │
         │                                    │  - EDGAR         │
         │                                    └────────┬─────────┘
         │                                             │ on completion
         │                                             ▼
         │                                    ┌──────────────────┐
         └─── push (WeChat service notif) ◀── │  Object Storage  │
                                              │  (Tencent COS)   │
                                              │  report .md + .zh│
                                              └──────────────────┘
```

Key shapes:
- Mini-app talks ONLY to the API gateway. All upstream calls
  (Moonshot, Tavily, EDGAR) happen worker-side.
- Worker is in HK/SG specifically so Tavily/EDGAR are first-class.
- API gateway is ICP-filed (mainland) to satisfy WeChat domain-whitelist
  requirements without firewall variability — or both sit in HK if we
  decide to gate at the WeChat side.
- Storage holds both the English original (canonical) and the auto-generated
  `.zh.md` translation; the mini-app reads zh by default.

## Phased plan

### Phase 0 — Validation & external filings (~1-2 weeks calendar, mostly waiting)

- Lock the decisions table above.
- File ICP for the gateway domain (in parallel with Phase 1 work).
- Open Tencent Cloud account; verify Tavily + EDGAR + Moonshot reachable
  from chosen region.
- Read WeChat mini-app review guidelines for finance category; if needed,
  consult a Chinese compliance lawyer on the data-tool-vs-advisory line.
- Draft disclaimer text and "not investment advice" UX scaffolding.

### Phase 1 — Backend extraction (~2-3 weeks of focused work)

The existing `analyst.py` was designed as a CLI, not a callable library.
Refactor needed but most of the substance stays:

- **Split `analyst.py`** into:
  - `le_analyst/` package with `run_initiate(ticker) -> str` and
    `run_research(ticker, question) -> str` as callable functions.
  - `cli.py` becomes a thin wrapper (existing UX unchanged).
  - Keep single-file simplicity per CLAUDE.md? Probably no longer
    appropriate for a hosted service — accept the architectural shift,
    document the change in CLAUDE.md.
- **API layer (FastAPI)**:
  - `POST /jobs` — submit a research/initiate request
  - `GET /jobs/:id` — status (pending / running / failed / done)
  - `GET /jobs/:id/report` — fetch the saved markdown (English + zh)
  - `GET /users/:id/jobs` — list user's reports
- **Job queue + persistence**: Redis + RQ or Celery for the queue;
  Postgres for user/job metadata; Tencent COS (S3-compatible) for report blobs.
- **Secrets**: Moonshot + Tavily keys move from `.env` to Tencent Cloud
  Secrets Manager (or equivalent).
- **Per-user cost accounting + rate limits**: from day one. The $0.75/initiate
  cost compounds fast.
- **Error handling**: the existing `httpx.ReadError` retry is good; extend
  with job-level retry-on-failure semantics so partial runs don't ghost users.
- **Translation runs on save**: every English report gets a `.zh.md`
  generated automatically; mini-app reads whichever the user selects.

### Phase 2 — Mini-app MVP (~1-2 weeks for someone fluent in WXML/WXSS)

Minimum viable surface:
- WeChat login (`wx.login` → backend exchange for session token).
- Submit page: ticker input + report-type toggle (research / initiate);
  optional question field for research.
- Jobs page: list of user's jobs with status badges; tap to view.
- Report page: markdown rendering (towxml or wxParse); language toggle
  (zh default, EN option).
- Disclaimer modal on first launch + footer on every report page.

Stylistic: clean, minimal, Tencent-app-like. Avoid investment-app patterns
that look like "advice" surfaces (no buy/sell buttons, no chart UI).

### Phase 3 — Compliance review & deploy (~2-4 weeks, externally gated)

- Set up production Tencent Cloud env; deploy gateway, job service, worker pool.
- WeChat mini-app review submission. Expect at least one rejection round on
  finance-category specifics; budget accordingly.
- Live disclaimer + ToS + privacy policy review.
- Soft launch to a small whitelist before public visibility.

### Phase 4 — Polish & monetization (~ongoing after launch)

- WeChat Pay for credit top-up (paid runs above the free quota).
- Push notifications via WeChat service notification (服务通知): "your
  $TICKER initiation is ready."
- Sharing: report-summary card optimized for WeChat share-to-chat.
- Saved library, search, ticker watchlist.
- Performance tuning (cold-start, queue depth, regional latency).

## What changes for the existing codebase

| Today | After Phase 1 |
|---|---|
| Single-file `analyst.py` | `le_analyst/` package (lib) + `cli.py` (thin wrapper) |
| `.env` keys read at startup | Server-side secrets manager |
| Output to `reports/` / `briefs/` on disk | Object storage (COS), per-user prefix |
| Implicit auto-translation via `--translate` flag | Always runs on save, both `.md` and `.zh.md` persisted |
| CLI is the only surface | CLI still works; FastAPI + mini-app are additional surfaces |
| `agentic_loop` streams to stdout | `agentic_loop` writes intermediate state to a job-status store for polling/WebSocket consumption |

The translation work we just shipped is *exactly* the foundation this needs.
zh output is already a first-class deliverable; the mini-app just makes it
default.

## What we explicitly defer

- Multi-region deployment (start with HK or SG only)
- iOS / Android native apps outside WeChat
- A standalone web app
- B2B / institutional API access
- HK and A-share coverage (US-listed only at launch)
- Custom chart rendering (markdown tables are enough at MVP)
- Real-time intraday data (initiation reports are not real-time products)

## Open questions to research before Phase 1

- **Tavily from chosen region.** Verify reachability + latency from
  Tencent Cloud HK/SG. If poor, swap to Bing/Bocha/Tongyi-style provider.
- **WeChat mini-app finance-category specifics.** Read the latest 类目要求.
  Identify any mandatory disclosures or KYC steps we'd need.
- **ICP filing path.** Which business entity files? Hong Kong corp can
  hold mainland ICP via subsidiary; getting that wrong adds weeks.
- **Cost ceiling per user.** Define free quota explicitly. 3 free
  initiations/month? 1? Affects both economics and product positioning.
- **Streaming vs. polling for status.** Polling is simpler to ship;
  WebSocket gives better UX. Start polling, plan WebSocket for v2.

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Mini-app review rejection (finance category) | Medium | High | Disclaimer scaffolding upfront; H5 fallback architecture |
| Tavily blocked / unreliable from chosen region | Medium | Medium | Test in Phase 0; swap provider if needed |
| ICP filing delays | High | Schedule-only | File in Phase 0, work in parallel |
| Cost runaway from a viral moment | Low at first, grows | High | Per-user quotas + rate limits from day 1 |
| Regulator views as advisory, requires license | Low-Medium | Existential | Disclaimer + clearly-not-advice framing; consult counsel before launch |
| Compaction of the conversation context loses CLAUDE.md conventions | (internal) Low | Medium | CLAUDE.md remains in repo and is loaded each session |

## What I'd recommend doing in the next 1-2 weeks

1. **Settle the decisions table** above (region, monetization, scope) —
   that's 1-2 hours of conversation, not weeks.
2. **Start ICP filing in parallel** — it's the longest lead time and
   gates Phase 3.
3. **Spike: refactor analyst.py to a callable library** without
   committing — just confirm the surface area is clean and the existing
   tests/runs still work. ~1 day.
4. **Spike: deploy a "hello world" FastAPI on Tencent Cloud HK** and
   confirm Moonshot/Tavily/EDGAR all work from there. ~1 day.

After those, we'd have enough evidence to decide whether to commit to
Phase 1 in earnest or pivot (e.g., to an H5 page in WeChat as a faster
path to validate demand).
