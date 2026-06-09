"""
webapp.py — minimal FastAPI wrapper around `analyst.py research` for a
WeChat mini-app closed beta.

Single-file, in-memory state (sessions, jobs, daily quota); subprocess
execution of the existing CLI; no DB, no queue, no object storage. Restart
wipes all state — that's the MVP boundary.

Run:
    pip install -r webapp_requirements.txt
    WX_APPID=... WX_SECRET=... uvicorn webapp:app --host 0.0.0.0 --port 8000

Env vars:
    WX_APPID, WX_SECRET   mini-app admin → 开发管理 → 开发设置
    DAILY_QUOTA           runs per openid per day (default 5)
    DEV_MODE=1            skip WeChat code2session, mint a dev token for any
                          posted code; lets you smoke-test before AppID is in hand
    ANALYST_DIR           override location of analyst.py + briefs/
                          (defaults to the parent of this file)
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

# ---------- config ----------

HERE = Path(__file__).parent
ANALYST_DIR = Path(os.environ.get("ANALYST_DIR", HERE.parent)).resolve()
ANALYST = ANALYST_DIR / "analyst.py"
BRIEFS = ANALYST_DIR / "briefs"


def _load_dotenv(path: Path) -> None:
    """Mirror analyst.py's zero-dep .env loader. Existing env wins, so a
    systemd EnvironmentFile= or a one-shot `export` doesn't get clobbered."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Load secrets BEFORE reading os.environ below. We try the local .env first
# (so the dev experience matches analyst.py), then /etc/le-analyst/env for
# the systemd path on a server.
_load_dotenv(ANALYST_DIR / ".env")
_load_dotenv(Path("/etc/le-analyst/env"))

WX_APPID = os.environ.get("WX_APPID", "")
WX_SECRET = os.environ.get("WX_SECRET", "")
DAILY_QUOTA = int(os.environ.get("DAILY_QUOTA", "5"))
DEV_MODE = os.environ.get("DEV_MODE") == "1"
JOB_TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "600"))  # 10 min hard cap

app = FastAPI(title="Le Analyst — mini-app backend (MVP)")

# In-memory state. Process restart wipes everything.
_sessions: dict[str, str] = {}                           # token -> openid
_jobs: dict[str, dict] = {}                              # job_id -> dict
_usage: dict[tuple[str, str], int] = defaultdict(int)    # (openid, YYYY-MM-DD)


# ---------- models ----------

class WxLoginReq(BaseModel):
    code: str = Field(..., min_length=1, max_length=256)


class WxLoginResp(BaseModel):
    token: str


class SubmitReq(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=10)
    question: str = Field(..., min_length=5, max_length=500)


class SubmitResp(BaseModel):
    job_id: str


class JobResp(BaseModel):
    status: str        # pending | running | done | failed
    elapsed_s: float
    zh_md: Optional[str] = None
    en_md: Optional[str] = None
    error: Optional[str] = None


# ---------- auth ----------

def current_openid(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> str:
    """Bearer-token → openid; 401 on miss. Sessions never expire in MVP."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    openid = _sessions.get(token)
    if not openid:
        raise HTTPException(401, "invalid or expired session")
    return openid


@app.post("/auth/wx-login", response_model=WxLoginResp)
async def wx_login(req: WxLoginReq):
    """Exchange wx.login `code` for openid (via WeChat code2session), mint
    a local bearer token. DEV_MODE=1 short-circuits the WeChat call."""
    if DEV_MODE:
        openid = "dev-" + req.code[:16]
    else:
        if not WX_APPID or not WX_SECRET:
            raise HTTPException(500, "WX_APPID / WX_SECRET not configured")
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.weixin.qq.com/sns/jscode2session",
                params={
                    "appid": WX_APPID,
                    "secret": WX_SECRET,
                    "js_code": req.code,
                    "grant_type": "authorization_code",
                },
            )
        data = r.json()
        openid = data.get("openid")
        if not openid:
            # WeChat error responses look like {"errcode":..., "errmsg":...}
            raise HTTPException(400, f"wx login failed: {data}")
    token = secrets.token_urlsafe(24)
    _sessions[token] = openid
    return WxLoginResp(token=token)


# ---------- jobs ----------

@app.post("/jobs", response_model=SubmitResp)
async def submit_job(req: SubmitReq, openid: str = Depends(current_openid)):
    today_key = (openid, date.today().isoformat())
    if _usage[today_key] >= DAILY_QUOTA:
        raise HTTPException(429, f"daily quota of {DAILY_QUOTA} runs exceeded")

    ticker = req.ticker.strip().upper()
    question = req.question.strip()
    if not ticker.isalnum():
        raise HTTPException(400, "ticker must be alphanumeric")

    _usage[today_key] += 1
    job_id = secrets.token_urlsafe(12)
    _jobs[job_id] = {
        "openid": openid,
        "ticker": ticker,
        "question": question,
        "status": "pending",
        "start": time.monotonic(),
        # Snapshot existing files so _run_job can identify the newly-written one
        "files_at_start": (
            {p.name for p in BRIEFS.glob("*.md")} if BRIEFS.exists() else set()
        ),
        "zh_md": None,
        "en_md": None,
        "error": None,
    }
    asyncio.create_task(_run_job(job_id))
    return SubmitResp(job_id=job_id)


@app.get("/jobs/{job_id}", response_model=JobResp)
async def get_job(job_id: str, openid: str = Depends(current_openid)):
    job = _jobs.get(job_id)
    if not job or job["openid"] != openid:
        raise HTTPException(404, "job not found")
    return JobResp(
        status=job["status"],
        elapsed_s=round(time.monotonic() - job["start"], 1),
        zh_md=job["zh_md"],
        en_md=job["en_md"],
        error=job["error"],
    )


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "analyst_exists": ANALYST.exists(),
        "dev_mode": DEV_MODE,
        "active_jobs": sum(1 for j in _jobs.values() if j["status"] == "running"),
    }


# ---------- WeChat domain ownership verification ----------

@app.get("/MP_verify_{token}.txt", response_class=PlainTextResponse)
async def wx_verify_file(token: str):
    """Serve the WeChat domain-ownership verification file. Drop the file
    WeChat hands you (e.g. MP_verify_AbC123xYz.txt) into miniapp_mvp/wx_verify/
    on the server, then click 提交 in the mini-app admin. The URL pattern
    constrains `token` to a path segment, so traversal isn't possible."""
    path = HERE / "wx_verify" / f"MP_verify_{token}.txt"
    if not path.exists():
        raise HTTPException(404, "verification file not present on server")
    return path.read_text(encoding="utf-8")


# ---------- job execution ----------

async def _run_job(job_id: str) -> None:
    """Spawn `python analyst.py research TICKER "Q" --translate zh`, wait for
    completion, locate the new brief file, read EN + zh into the job dict."""
    job = _jobs[job_id]
    job["status"] = "running"
    try:
        BRIEFS.mkdir(exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ANALYST),
            "research",
            job["ticker"],
            job["question"],
            "--translate", "zh",
            cwd=str(ANALYST_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=JOB_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"job exceeded {JOB_TIMEOUT_S}s timeout")

        if proc.returncode != 0:
            # analyst.py's own ABORTED-with-0-tool-calls message lands here
            tail = (stderr or b"").decode("utf-8", "replace").strip()[-500:]
            raise RuntimeError(f"analyst exited {proc.returncode}: {tail}")

        # Pick the brief file: starts with TICKER-, ends with .md (not .zh.md),
        # didn't exist before the run started.
        candidates = [
            p for p in BRIEFS.glob(f"{job['ticker']}-*.md")
            if p.name not in job["files_at_start"]
            and not p.name.endswith(".zh.md")
        ]
        if not candidates:
            raise RuntimeError("brief file not found after run")
        en_path = max(candidates, key=lambda p: p.stat().st_mtime)
        zh_path = en_path.with_name(en_path.stem + ".zh.md")

        job["en_md"] = en_path.read_text(encoding="utf-8")
        job["zh_md"] = (
            zh_path.read_text(encoding="utf-8") if zh_path.exists() else job["en_md"]
        )
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:500]
