# Le Analyst — WeChat Mini-App MVP

The smallest cut that runs end-to-end: `analyst research` only, closed beta,
no payment, in-memory state, single-file backend, three mini-app pages.

For the architectural target (queue + DB + object storage + WebSocket + ICP +
public review + WeChat Pay) see `../WECHAT_MINIAPP_PLAN.md`. This directory
deliberately skips all of that to ship something testable in ~1 week.

## What lives here

```
miniapp_mvp/
├── README.md                      # this file
├── webapp.py                      # FastAPI backend (~150 LOC)
├── webapp_requirements.txt        # fastapi, uvicorn, httpx
└── miniapp/                       # WeChat mini-app source
    ├── app.{js,json,wxss}
    ├── project.config.json
    ├── sitemap.json
    └── pages/{submit,status,report}/{wxml,wxss,js,json}
```

## Architecture (one screen)

```
WeChat client ──HTTPS──▶ FastAPI (webapp.py on HK VPS)
                              │
                              ├─ /auth/wx-login   wx.login code → openid
                              ├─ /jobs            submit research request
                              └─ /jobs/{id}       poll status / fetch result
                                     │
                                     └─ asyncio subprocess →
                                           python analyst.py research \
                                              TICKER "QUESTION" --translate zh
                                              │
                                              └─ writes briefs/*.md + .zh.md
                                                     │
                                                     └─ webapp reads both,
                                                        stuffs into job dict
```

All state is in-process Python dicts. Restart wipes sessions, jobs, and the
daily quota counter — fine for MVP.

## Prerequisites

- The existing `analyst.py` already works locally (Moonshot + Tavily keys in
  `../.env`). The mini-app backend runs the same script as a subprocess.
- A host where Moonshot, Tavily, and EDGAR are reachable. **Hong Kong** is the
  natural pick; Singapore works; mainland China will fight you on every call.
- Python 3.11+.

Two things gated externally (you mentioned you're thinking these through):

- **WeChat mini-app AppID + AppSecret.** Sign up at
  https://mp.weixin.qq.com/. The finance category review is heavier than
  average — but the *dev/experience build* (体验版) does not require category
  review, and you can invite up to ~100 testers by WeChat ID. The MVP targets
  experience build only.
- **A backend domain reachable from WeChat clients.** For *experience build*,
  the domain just needs HTTPS and to be whitelisted in the mini-app admin's
  `request合法域名` list. Mainland ICP filing is **only** required for the
  *production* (公开发布) build.

Set `DEV_MODE=1` to skip the WeChat login round-trip while you wait for an
AppID — useful for exercising the backend via curl and the mini-app via the
WeChat devtools "compile without verification" toggle.

## Backend setup

```bash
cd miniapp_mvp/
pip install -r webapp_requirements.txt

# Real WeChat login:
export WX_APPID="wx................"
export WX_SECRET="................................"
export DAILY_QUOTA=5
uvicorn webapp:app --host 0.0.0.0 --port 8000

# Or skip WeChat login while testing:
export DEV_MODE=1
uvicorn webapp:app --host 0.0.0.0 --port 8000
```

Smoke test in `DEV_MODE=1`:

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/wx-login \
  -H 'Content-Type: application/json' \
  -d '{"code":"smoketest"}' | python -c "import sys,json;print(json.load(sys.stdin)['token'])")

JOB=$(curl -s -X POST localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"AVGO","question":"Why did AVGO decline 12.6% on June 4, 2026?"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['job_id'])")

# Poll
curl -s localhost:8000/jobs/$JOB -H "Authorization: Bearer $TOKEN"
```

The job completes in ~2–3 min and the response carries `zh_md` and `en_md`.

## Mini-app setup

1. Install **WeChat DevTools** (微信开发者工具).
2. Open this directory's `miniapp/` folder as a mini-app project.
   - In `project.config.json`, replace `"appid": "YOUR_APPID_HERE"` with your
     real AppID. Or, while waiting, use the "无需 AppID 测试号" option in
     DevTools.
3. In `miniapp/app.js`, replace `apiBase` with your backend URL
   (e.g. `https://le-analyst.example.com`).
4. For experience build to talk to your domain, either:
   - Whitelist it in the mini-app admin → 开发管理 → 服务器域名 → request合法域名, **or**
   - In DevTools → 详情 → 本地设置 → check "不校验合法域名…" (dev only).

The three pages:

- **pages/submit** — ticker input + question textarea + submit button.
  Disclaimer modal on first launch (persisted via `wx.setStorageSync`).
- **pages/status** — polls `GET /jobs/{id}` every 4s; redirects to report on
  `done`, surfaces error on `failed`.
- **pages/report** — renders the markdown with a zh/EN toggle. The MVP uses a
  plain `<text>` block with `white-space: pre-wrap` (looks fine, no library
  needed). For prettier rendering, integrate [towxml](https://github.com/sbfkcel/towxml)
  later — drop it under `miniapp/towxml/` and swap the wxml.

## What's explicitly missing (MVP boundaries)

| Missing | Why deferred | When to add |
|---|---|---|
| Persistent job/user history (DB) | One report at a time is enough for smoke test | When testers want to re-read past briefs |
| Object storage for reports | Local FS + in-memory cache is enough at one-box scale | When you scale beyond a single VPS |
| Redis / Celery queue | `asyncio.create_task` handles 2-3 min jobs fine on one box | When concurrent submits ≥ ~5 |
| WebSocket status push | 4s polling is fine for 2-3 min jobs | When you want sub-second status updates |
| Full markdown rendering | `<text>` preserves newlines acceptably for Chinese prose | When testers ask for tables/headings styled |
| WeChat Pay credits | Daily quota of 5/openid caps cost | When public launch warrants it |
| `initiate` command | 10 min, 95 calls — needs queue + better UX than polling | Phase 2 of the broader plan |
| Public mini-app review (finance category) | Experience build covers closed beta | When the product survives a beta and you have disclaimers reviewed |

## Cost shape

Per-research run: ~$0.20–0.40 (same as CLI; Kimi cache hit ratio unchanged).
Per VPS: ~$5–10/mo on Tencent Cloud HK Lighthouse or equivalent.
Daily quota of 5/openid × 20 testers = ≤100 runs/day = ≤$40/day ceiling, but
realistic usage will be far lower.

## What changes when you outgrow this MVP

The shape stays. What you swap in:
- `_sessions` / `_jobs` / `_usage` dicts → Postgres + Redis
- Local `briefs/` reads → Tencent COS (S3-compatible) reads
- `asyncio.create_task` → RQ or Celery worker pool
- `subprocess` invocation of `analyst.py` → `from le_analyst import run_research`
  after the library refactor in `WECHAT_MINIAPP_PLAN.md` Phase 1
- 4s polling → WebSocket via `wx.connectSocket`
- Experience build → production build (requires ICP filing)
- DAILY_QUOTA → WeChat Pay credit ledger
