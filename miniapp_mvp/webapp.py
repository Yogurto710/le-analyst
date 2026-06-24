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
import re
import secrets
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
import markdown
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

# ---------- config ----------

HERE = Path(__file__).parent
ANALYST_DIR = Path(os.environ.get("ANALYST_DIR", HERE.parent)).resolve()
ANALYST = ANALYST_DIR / "analyst.py"
# Briefs are language-routed by analyst.py: briefs_en/ holds English-question
# briefs (+ optional .zh.md translations); briefs_ch/ holds Chinese-question
# briefs (single .md, no translation pass).
BRIEFS_EN = ANALYST_DIR / "briefs_en"
BRIEFS_CH = ANALYST_DIR / "briefs_ch"


def _briefs_dir_for(lang: str) -> Path:
    """Mirror analyst.py's _briefs_dir_for routing so we look in the same
    folder the subprocess will write into. Keys on the language flag, not
    the question text (analyst.py changed to lang-flag routing as well)."""
    return BRIEFS_CH if lang == "zh" else BRIEFS_EN


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
JOB_TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "900"))  # 15 min hard cap (initiate runs ~11-12 min worst case)

# Per-job-type credit cost against DAILY_QUOTA. Initiate costs 3x research
# because the underlying CLI call is ~4-5x the API spend (more tool calls,
# longer Phase 3, and a Phase 4 review/revision pass).
JOB_COSTS = {"research": 1, "initiate": 3}

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
    # `question` is required for research, optional for initiate. Validation
    # of the question/report_type contract happens in submit_job.
    question: Optional[str] = Field(default=None, max_length=500)
    # Optional output language. If omitted, the backend auto-detects from
    # the question (CJK in question → zh, otherwise en). Mini-app submit
    # page sends this explicitly when the user uses the toggle.
    lang: Optional[str] = Field(default=None, pattern="^(en|zh)$")
    # Report type — "initiate" (thesis-shaped ~1300-word brief, ticker-only)
    # or "research" (question-driven ~500-word brief). Default initiate per
    # mini-app UX (primary product is the deep brief).
    report_type: str = Field(default="initiate", pattern="^(initiate|research)$")


class SubmitResp(BaseModel):
    job_id: str


class JobResp(BaseModel):
    status: str        # pending | running | done | failed
    elapsed_s: float
    lang: Optional[str] = None       # "en" or "zh" — echoes back what was used
    report_type: Optional[str] = None  # "initiate" or "research"
    # Phase substate while status="running". Parsed live from analyst.py
    # stderr by _classify_stderr_line so the mini-app can show "正在检索
    # 资料 (12次)" instead of a static "研究中" for an 8-12 minute run.
    # null until the first tool call lands. Enum:
    #   prefetch | gather | compute | synthesize | review | revise | save
    phase: Optional[str] = None
    tool_count: Optional[int] = None
    md: Optional[str] = None         # markdown source in the chosen language
    html: Optional[str] = None       # styled HTML for <rich-text> rendering
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
    ticker = req.ticker.strip().upper()
    if not ticker.isalnum():
        raise HTTPException(400, "ticker must be alphanumeric")

    report_type = req.report_type
    question = (req.question or "").strip()

    # Research requires a question; initiate ignores any question that was sent.
    if report_type == "research" and (not question or len(question) < 5):
        raise HTTPException(400, "research requires a question (min 5 chars)")

    # Cost-based quota: initiate burns 3 credits, research 1.
    cost = JOB_COSTS[report_type]
    today_key = (openid, date.today().isoformat())
    if _usage[today_key] + cost > DAILY_QUOTA:
        used = _usage[today_key]
        remaining = max(0, DAILY_QUOTA - used)
        raise HTTPException(
            429,
            f"daily quota exhausted: this {report_type} request costs {cost} credit(s); "
            f"{used}/{DAILY_QUOTA} used today, {remaining} remaining.",
        )

    # Lang resolution: explicit req.lang wins; otherwise auto-detect from
    # question (CJK → zh, else en). For initiate without a question, default
    # to en — the toggle on the submit page sends explicit lang anyway.
    if req.lang:
        lang = req.lang
    elif question:
        lang = "zh" if _is_chinese_question(question) else "en"
    else:
        lang = "en"

    _usage[today_key] += cost
    job_id = secrets.token_urlsafe(12)

    # File output routing differs by report type:
    #   - research → briefs_en/ or briefs_ch/
    #   - initiate → reports/ (always; reports/ is single-folder for both langs)
    if report_type == "research":
        output_dir = _briefs_dir_for(lang)
    else:
        output_dir = ANALYST_DIR / "reports"

    _jobs[job_id] = {
        "openid": openid,
        "ticker": ticker,
        "question": question,
        "lang": lang,
        "report_type": report_type,
        "status": "pending",
        "start": time.monotonic(),
        "briefs_dir": output_dir,
        # Snapshot existing files so _run_job can identify the newly-written one
        "files_at_start": (
            {p.name for p in output_dir.glob("*.md")} if output_dir.exists() else set()
        ),
        # Phase substate updated by _classify_stderr_line as analyst.py runs.
        "phase": None,
        "tool_count": 0,
        "md": None,
        "html": None,
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
        lang=job.get("lang"),
        report_type=job.get("report_type"),
        phase=job.get("phase"),
        tool_count=job.get("tool_count") or None,
        md=job["md"],
        html=job["html"],
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


# ---------- markdown rendering for <rich-text> ----------

# WeChat <rich-text> silently strips `margin` and `border-bottom` from
# block elements (h1-h6, p, ul, ol) even though the docs claim margin is
# supported. Observed consistently in iOS WeChat 8.x — the font-size and
# font-weight survive but vertical spacing collapses to zero. The reliable
# workaround is to set margin:0 on every block and inject explicit empty
# <div> spacers between elements (rich-text DOES honor `height` on divs).
_MD_TAG_STYLES = {
    "<h1>":   '<h1 style="font-size:38rpx;font-weight:700;line-height:1.35;color:#1f2329;margin:0;padding:0;">',
    "<h2>":   '<h2 style="font-size:32rpx;font-weight:600;line-height:1.4;color:#1f2329;margin:0;padding:0;">',
    "<h3>":   '<h3 style="font-size:30rpx;font-weight:600;line-height:1.4;color:#1f2329;margin:0;padding:0;">',
    "<p>":    '<p style="font-size:28rpx;line-height:1.85;color:#1f2329;margin:0;padding:0;">',
    "<ul>":   '<ul style="font-size:28rpx;line-height:1.85;padding-left:36rpx;margin:0;color:#1f2329;">',
    "<ol>":   '<ol style="font-size:28rpx;line-height:1.85;padding-left:36rpx;margin:0;color:#1f2329;">',
    "<li>":   '<li style="margin:0;padding:6rpx 0;">',
    "<a ":    '<a style="color:#165dff;word-break:break-all;text-decoration:underline;" ',
    "<strong>": '<strong style="font-weight:600;color:#1f2329;">',
    "<em>":   '<em style="font-style:italic;color:#4e5969;">',
    "<hr>":   '<hr style="border:none;border-top:1rpx solid #e5e6eb;margin:0;" />',
    "<hr />": '<hr style="border:none;border-top:1rpx solid #e5e6eb;margin:0;" />',
    "<blockquote>": '<blockquote style="border-left:6rpx solid #c9cdd4;padding:4rpx 0 4rpx 20rpx;margin:0;color:#4e5969;">',
    "<code>": '<code style="background:#f7f8fa;padding:2rpx 8rpx;border-radius:6rpx;font-family:Menlo,Consolas,monospace;font-size:26rpx;color:#4e5969;">',
    "<pre>":  '<pre style="background:#f7f8fa;padding:20rpx;border-radius:8rpx;overflow:auto;font-size:24rpx;line-height:1.55;color:#1f2329;margin:0;">',
    "<table>": '<table style="border-collapse:collapse;width:100%;font-size:26rpx;margin:0;color:#1f2329;">',
    "<th>":   '<th style="border:1rpx solid #e5e6eb;padding:10rpx 12rpx;background:#f7f8fa;text-align:left;font-weight:600;">',
    "<td>":   '<td style="border:1rpx solid #e5e6eb;padding:10rpx 12rpx;vertical-align:top;">',
}

# Spacers around each block. Two-column (before / after) tuning so the
# H2 "section break" gap is large but the "heading→body" gap inside a
# section stays tight. Each spacer carries a non-breaking space and
# explicit display:block — empty <div>s collapse to zero height in WeChat
# rich-text on some builds, the &nbsp; + matching line-height forces the
# div to render at the requested rpx.
# Effective vertical gaps:
#   H1 → H2 (direct):        20 + 36 = 56rpx
#   P  → H2:                 14 + 36 = 50rpx
#   H2 → P (heading→body):              22rpx
#   P  → H3:                 14 + 22 = 36rpx
#   H3 → P (heading→body):              16rpx
#   P  → P:                  14 + 14 = 28rpx
def _spacer(h: int) -> str:
    # <div> here (not <p>) because the spacer's closing tag must not
    # match any rule in _SPACERS — a </p> spacer would itself match
    # the </p> rule on the next pass and double up. The original empty
    # <div></div> collapsed to zero height on WeChat; adding &#160;
    # content + display:block + font-size:1rpx forces the block to
    # render at the requested height while the nbsp stays visually
    # invisible (1rpx font on a non-breaking space is sub-pixel).
    return (
        f'<div style="height:{h}rpx;line-height:{h}rpx;display:block;'
        f'margin:0;padding:0;font-size:1rpx;">&#160;</div>'
    )

_SPACERS = [
    ("</h1>",         "</h1>" + _spacer(20)),
    ("<h2 ",          _spacer(36) + "<h2 "),
    ("</h2>",         "</h2>" + _spacer(22)),
    ("<h3 ",          _spacer(22) + "<h3 "),
    ("</h3>",         "</h3>" + _spacer(16)),
    ("</p>",          "</p>" + _spacer(14)),
    ("</ul>",         "</ul>" + _spacer(14)),
    ("</ol>",         "</ol>" + _spacer(14)),
    ("</blockquote>", "</blockquote>" + _spacer(14)),
    ("</pre>",        "</pre>" + _spacer(14)),
]

# YAML frontmatter block (ticker / question / date / model / lang lines
# between --- delimiters) is metadata for analyst.py's storage layer,
# not for human readers. Strip it before rendering.
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n+", re.DOTALL)

# Question is treated as Chinese if it contains any CJK Unified
# Ideograph. Mixed-script questions (e.g. "AVGO的AI战略") count as
# Chinese — the user wants a Chinese answer.
_CJK_RE = re.compile(r"[一-鿿]")


def _strip_frontmatter(md: str) -> str:
    return _FRONTMATTER_RE.sub("", md, count=1)


def _is_chinese_question(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _render_md_to_styled_html(md: str) -> str:
    """Convert markdown to inline-styled HTML for WeChat <rich-text>.
    Strips YAML frontmatter defensively, applies inline styles for fonts
    + colors, then injects empty-div spacers between block elements to
    work around rich-text's silent margin-stripping behavior."""
    md = _strip_frontmatter(md)
    html = markdown.markdown(md, extensions=["extra", "sane_lists", "nl2br"])
    for old, new in _MD_TAG_STYLES.items():
        html = html.replace(old, new)
    for old, new in _SPACERS:
        html = html.replace(old, new)
    return html


# ---------- live phase classification ----------
#
# analyst.py emits a fairly stable set of stderr markers (`[prefetch]`,
# `[T+...s] tool#NN <tool_name>`, `[review] ...`, `Saved: ...`) plus
# Phase 3/4 brief content streamed to stdout. We tap both streams line-
# by-line and project them onto a small phase state machine. The mini-app
# polls `phase` + `tool_count` and renders a Chinese live message — what
# the agent is doing right now, not just "研究中".

# Phase ordering is monotonic: once we advance past gather, we never fall
# back, even if a later stderr line matches an earlier pattern (the model
# isn't supposed to call web_search after python_repl, but we don't want
# UI flicker if it ever does).
_PHASE_RANK = {
    None: 0,
    "prefetch":   1,
    "gather":     2,
    "compute":    3,
    "synthesize": 4,
    "review":     5,
    "revise":     6,
    "save":       7,
}

# Matches `tool# 12 fetch_url      took   3.4s` and the CAPPED variant.
_TOOL_LINE_RE = re.compile(r"\btool#\s*(\d+)\s+(\w+)\s")
_PREFETCH_RE  = re.compile(r"^\[prefetch\]")
_REVIEW_RE    = re.compile(r"^\[review\]\s+(?:no issues|\d+ issue)")
_REVISE_RE    = re.compile(r"^\[review\]\s+requesting targeted revision")
_SAVE_RE      = re.compile(r"^Saved:")


def _maybe_advance(job: dict, target: str) -> None:
    """Move to `target` phase only if it's strictly ahead of the current
    one. Keeps the UI monotonic even if the model emits markers out of
    the documented order."""
    if _PHASE_RANK[target] > _PHASE_RANK.get(job.get("phase")):
        job["phase"] = target


def _classify_stderr_line(line: str, job: dict) -> None:
    """Project one analyst.py stderr line onto the phase state machine."""
    if _PREFETCH_RE.search(line):
        _maybe_advance(job, "prefetch")
        return
    m = _TOOL_LINE_RE.search(line)
    if m:
        n, tool = int(m.group(1)), m.group(2)
        # tool_count tracks the highest seen index — the agentic loop
        # numbers calls monotonically so this is also the running total.
        if n > (job.get("tool_count") or 0):
            job["tool_count"] = n
        if tool == "python_repl":
            _maybe_advance(job, "compute")
        else:
            _maybe_advance(job, "gather")
        return
    # `[review] requesting…` is more specific than `[review] <count> issue`,
    # so test the revise pattern first.
    if _REVISE_RE.search(line):
        _maybe_advance(job, "revise")
        return
    if _REVIEW_RE.search(line):
        _maybe_advance(job, "review")
        return
    if _SAVE_RE.search(line):
        _maybe_advance(job, "save")


async def _drain_stderr(stream: asyncio.StreamReader, buf: list, job: dict) -> None:
    """Read stderr line-by-line until EOF; classify each line and keep
    the raw text in `buf` for the failure-tail message."""
    while True:
        raw = await stream.readline()
        if not raw:
            return
        line = raw.decode("utf-8", "replace")
        buf.append(line)
        _classify_stderr_line(line, job)


async def _drain_stdout(stream: asyncio.StreamReader, buf: list, job: dict) -> None:
    """Drain stdout in chunks (not lines — analyst.py streams Phase 3
    content character-by-character, so newlines are sparse). Any non-
    whitespace stdout means the model has begun writing the brief →
    synthesize. We don't need the content here; analyst.py writes the
    final file to disk and we read it from there."""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        buf.append(chunk)
        if chunk.strip():
            _maybe_advance(job, "synthesize")


# ---------- job execution ----------

async def _run_job(job_id: str) -> None:
    """Spawn the appropriate analyst.py subcommand based on report_type:
    `analyst.py research TICKER "Q" --lang ...` or
    `analyst.py initiate TICKER --lang ...`. Stream stdout/stderr line-
    by-line so the phase classifier can update job['phase'] live, wait
    for completion, locate the newly-written .md file, read into the
    job dict."""
    job = _jobs[job_id]
    job["status"] = "running"
    try:
        job["briefs_dir"].mkdir(exist_ok=True)
        # Build the subprocess command based on report_type.
        # research: needs a question; initiate: ticker-only, longer run.
        report_type = job.get("report_type", "research")
        if report_type == "initiate":
            args = [
                sys.executable, str(ANALYST), "initiate",
                job["ticker"],
                "--lang", job["lang"],
            ]
        else:
            args = [
                sys.executable, str(ANALYST), "research",
                job["ticker"], job["question"],
                "--lang", job["lang"],
            ]
        # Optional model override (kimi | deepseek). Set LE_ANALYST_MODEL in
        # .env or /etc/le-analyst/env to swap the default backend without
        # editing this file. analyst.py defaults to kimi when unset.
        if model := os.environ.get("LE_ANALYST_MODEL"):
            args.extend(["--model", model])
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(ANALYST_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_buf: list = []
        stderr_buf: list = []
        # Concurrently: drain stdout, drain stderr (classifying each line),
        # and wait for the subprocess to exit. wait_for caps the whole
        # gather so a hung process still trips JOB_TIMEOUT_S.
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _drain_stdout(proc.stdout, stdout_buf, job),
                    _drain_stderr(proc.stderr, stderr_buf, job),
                    proc.wait(),
                ),
                timeout=JOB_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"job exceeded {JOB_TIMEOUT_S}s timeout")

        if proc.returncode != 0:
            # analyst.py's own ABORTED-with-0-tool-calls message lands here
            tail = "".join(stderr_buf).strip()[-500:]
            raise RuntimeError(f"analyst exited {proc.returncode}: {tail}")

        # Pick the brief file: starts with TICKER-, ends with .md, didn't
        # exist before the run started. Only one file is produced per run
        # now (no .zh.md sibling) — the chosen language is set up-front.
        candidates = [
            p for p in job["briefs_dir"].glob(f"{job['ticker']}-*.md")
            if p.name not in job["files_at_start"]
        ]
        if not candidates:
            raise RuntimeError("brief file not found after run")
        path = max(candidates, key=lambda p: p.stat().st_mtime)

        # Strip YAML frontmatter so it doesn't leak into the rendered output.
        raw = path.read_text(encoding="utf-8")
        job["md"] = _strip_frontmatter(raw)
        job["html"] = _render_md_to_styled_html(job["md"])
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:500]
