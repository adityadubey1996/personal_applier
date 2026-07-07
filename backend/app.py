from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

ROOT = Path(__file__).resolve().parents[2]
SDK_DIR = ROOT / "sdk"
if str(SDK_DIR) not in sys.path:
    sys.path.append(str(SDK_DIR))

from steel_agent import EventBus, RunConfig, SteelConfig, SteelSession, run_task  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.append(str(APP_DIR))
from apply_harness import HarnessState, _close_extra_tabs, _is_protected, _norm, _save_learned_answer, run_apply  # noqa: E402
from platforms.linkedin import LinkedInAdapter  # noqa: E402

# Single stateless adapter; threaded into discovery + run_apply so the
# reconciliation core (reconcile, is_submitted gate, anomaly log) is live.
_ADAPTER = LinkedInAdapter()


# --------------------------------------------------------------------------- #
# Logging: make the terminal legible
#   - drop the per-poll access-log flood (frontend hits /api/status ~5x/sec)
#   - mirror the harness narrative (field/decision/reconcile/result) to stdout,
#     which otherwise only goes to the in-memory /api/logs stream
# --------------------------------------------------------------------------- #

class _DropPollingAccessLogs(logging.Filter):
    _NOISY = ("/api/status", "/api/logs/stream", "/api/login-status")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._NOISY)


logging.getLogger("uvicorn.access").addFilter(_DropPollingAccessLogs())

hlog = logging.getLogger("harness")
if not hlog.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("  ▸ %(message)s"))
    hlog.addHandler(_h)
    hlog.setLevel(logging.INFO)
    hlog.propagate = False


app = FastAPI(title="Workbench V5")

_DIST = ROOT / "v5" / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

LEDGER_PATH = APP_DIR / "ledger.json"
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_env() -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    load_dotenv(ROOT / ".env", override=False)


_load_env()


# --------------------------------------------------------------------------- #
# Ledger — dedup + outcome tracking across restarts
# --------------------------------------------------------------------------- #

def _ledger_load() -> dict[str, Any]:
    try:
        return json.loads(LEDGER_PATH.read_text()) if LEDGER_PATH.exists() else {}
    except Exception:
        return {}


def _ledger_save(data: dict[str, Any]) -> None:
    try:
        LEDGER_PATH.write_text(json.dumps(data, indent=2, default=str))
    except Exception:
        pass


def _ledger_key(platform: str, job_id: str) -> str:
    return f"{platform}_{job_id}"


def _ledger_seen(platform: str, job_id: str) -> bool:
    return _ledger_key(platform, job_id) in _ledger_load()


def _ledger_record(
    platform: str, job_id: str, company: str, title: str, status: str
) -> None:
    data = _ledger_load()
    data[_ledger_key(platform, job_id)] = {
        "platform": platform,
        "job_id": job_id,
        "company": company,
        "title": title,
        "status": status,
        "ts": _utc_now(),
    }
    _ledger_save(data)


def _classify_outcome(
    submitted: bool, submit_skipped: bool, needs_review: bool, status: str
) -> str:
    """Map a run's harness flags to one outcome status. Order matters: a real
    (adapter-verified) submission wins; then an explicit human skip; then an
    unresolved reconciler stall surfaces as needs_review (NOT submitted); else
    the raw agent status."""
    if submitted:
        return "submitted"
    if submit_skipped:
        return "skipped"
    if needs_review:
        return "needs_review"
    return status or "completed"


# --------------------------------------------------------------------------- #
# Runtime state
# --------------------------------------------------------------------------- #

@dataclass
class RuntimeState:
    steel_session: Optional[SteelSession] = None
    event_bus: EventBus = field(default_factory=EventBus)
    command_task: Optional[asyncio.Task[Any]] = None
    running: bool = False
    session_id: Optional[str] = None
    cdp_url: Optional[str] = None
    viewer_url: Optional[str] = None
    last_result: Optional[dict[str, Any]] = None
    apply_hstate: Optional[HarnessState] = None
    logs: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=1000))
    # Batch state
    batch_task: Optional[asyncio.Task[Any]] = None
    batch_running: bool = False
    batch_applied: int = 0
    batch_skipped: int = 0
    batch_queue: list[str] = field(default_factory=list)
    batch_current_job: Optional[str] = None
    batch_outcomes: list[dict[str, Any]] = field(default_factory=list)
    batch_missing_fields: list[str] = field(default_factory=list)
    batch_tokens: dict[str, Any] = field(
        default_factory=lambda: {"prompt": 0, "completion": 0, "total": 0, "cost": 0.0}
    )
    batch_stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    login_checked: Optional[bool] = None  # None = unknown, True/False = last check
    # LangGraph orchestrator (Phase A) — same login->discover->apply flow as the
    # batch, but driven by orchestrator/graph.py instead of _run_batch.
    orch_running: bool = False
    orch_task: Optional[asyncio.Task[Any]] = None
    orch_result: Optional[dict[str, Any]] = None
    orch_stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def push_log(self, level: str, message: str, **extra: Any) -> None:
        row: dict[str, Any] = {"ts": _utc_now(), "level": level, "message": message}
        if extra:
            row["extra"] = extra
        self.logs.append(row)
        hlog.info("%-10s %s", level, message)  # mirror to terminal


state = RuntimeState()

# ponytail: double-checked lock so concurrent /api/login-status + /api/status on page
# load don't each create their own Steel session.
_session_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #

class StartSessionResponse(BaseModel):
    session_id: str
    viewer_url: str
    cdp_url: str


class CommandRequest(BaseModel):
    instruction: str
    max_steps: int = 12


class StopRequest(BaseModel):
    reason: str = "user_stop"


class ApplyRequest(BaseModel):
    job_url: str
    max_steps: int = 30
    submit_policy: str = "hitl"


class ApplyBatchRequest(BaseModel):
    keywords: str
    location: str = "India"
    max_applications: int = 5
    submit_policy: str = "hitl"  # "auto_if_clean" | "hitl" | "auto"
    date_posted: str = ""      # LinkedIn f_TPR code, "" = any
    experience: str = ""       # LinkedIn f_E code, "" = any
    workplace: str = ""        # LinkedIn f_WT code, "" = any
    easy_apply_only: bool = True
    apply_engine: str = "legacy"  # "legacy" (agent.run) | "graph" (LangGraph take_step loop)


class HitlRequest(BaseModel):
    action: str = "answer"  # "answer" | "approve" | "skip"
    value: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _interactive_viewer_url() -> Optional[str]:
    if not state.session_id:
        return None
    base = (os.getenv("STEEL_VIEWER_BASE_URL") or "http://localhost:3000").rstrip("/")
    return f"{base}/v1/sessions/debug?sessionId={state.session_id}&interactive=true&showControls=true"


def _llm_config() -> tuple[str, str, str]:
    provider = (os.getenv("LLM_PROVIDER") or "groq").strip().lower()
    if provider == "google":
        api_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
        model = (os.getenv("GOOGLE_MODEL") or "gemini-2.5-flash").strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="GOOGLE_API_KEY is required for google provider")
        return provider, api_key, model
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    model = (os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="GROQ_API_KEY is required for groq provider")
    return "groq", api_key, model


async def _ensure_session() -> SteelSession:
    if state.steel_session and state.session_id:
        return state.steel_session
    async with _session_lock:
        if state.steel_session and state.session_id:
            return state.steel_session
        steel_cfg = SteelConfig(
            steel_http_url=(os.getenv("STEEL_BASE_URL") or "http://127.0.0.1:3000").strip(),
            steel_ws_url=(os.getenv("STEEL_WS_URL") or "ws://127.0.0.1:3000").strip(),
            release_on_exit=False,
        )
        session = await SteelSession.create(steel_cfg)
        state.steel_session = session
        state.session_id = session.session_id
        state.cdp_url = session.cdp_url
        state.viewer_url = session.viewer_url
        state.push_log("info", "Steel session started", session_id=session.session_id)
        return session


async def _attach_logged_in_session() -> bool:
    """Reuse an already-logged-in live Steel session (li_at cookie)."""
    base = (os.getenv("STEEL_BASE_URL") or "http://127.0.0.1:3000").rstrip("/")
    ws = (os.getenv("STEEL_WS_URL") or "ws://127.0.0.1:3000").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/v1/sessions")
            live = [s for s in r.json().get("sessions", []) if s.get("status") != "released"]
            for s in live:
                ctx = await c.get(f"{base}/v1/sessions/{s['id']}/context")
                cookies = ctx.json().get("cookies", []) or []
                if any(ck.get("name") == "li_at" for ck in cookies):
                    state.session_id = s["id"]
                    state.cdp_url = f"{ws}/ws?sessionId={s['id']}"
                    state.viewer_url = state.cdp_url
                    state.push_log("info", f"Attached to logged-in Steel session {s['id'][:8]}")
                    return True
    except Exception as e:  # noqa: BLE001
        state.push_log("warn", f"attach check failed: {e}")
    return False


async def _check_login(platform: str = "linkedin") -> bool:
    """Check if the current Steel session has a valid login cookie."""
    if not state.session_id:
        return False
    base = (os.getenv("STEEL_BASE_URL") or "http://127.0.0.1:3000").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            ctx = await c.get(f"{base}/v1/sessions/{state.session_id}/context")
            cookies = ctx.json().get("cookies", []) or []
            if platform == "linkedin":
                return any(ck.get("name") == "li_at" for ck in cookies)
    except Exception as e:  # noqa: BLE001
        state.push_log("warn", f"login check failed: {e}")
    return False


async def _navigate(url: str) -> None:
    if not state.cdp_url:
        return
    try:
        from browser_use import Browser  # noqa: PLC0415
        b = Browser(cdp_url=state.cdp_url)
        await b.start()
        try:
            await b.navigate_to(url)
        finally:
            with contextlib.suppress(Exception):
                await b.stop()
    except Exception as e:  # noqa: BLE001
        state.push_log("warn", f"navigate to {url} failed: {e}")


async def _release_session_by_id(sid: str) -> None:
    if not sid:
        return
    base = (os.getenv("STEEL_BASE_URL") or "http://127.0.0.1:3000").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.delete(f"{base}/v1/sessions/{sid}")
    except Exception as e:  # noqa: BLE001
        state.push_log("warn", f"release session {sid[:8]} failed: {e}")


async def _release_all_sessions() -> int:
    """Best-effort release of ALL live Steel sessions. Returns count attempted."""
    base = (os.getenv("STEEL_BASE_URL") or "http://127.0.0.1:3000").rstrip("/")
    attempted = 0
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/v1/sessions")
            live = [s for s in r.json().get("sessions", []) if s.get("status") != "released"]
        for s in live:
            attempted += 1
            await _release_session_by_id(s["id"])
    except Exception as e:  # noqa: BLE001
        state.push_log("warn", f"release-all sessions failed: {e}")
    return attempted


# --------------------------------------------------------------------------- #
# Brain loop — batch multi-job apply
#   (job discovery now lives on the platform adapter: _ADAPTER.discover_jobs)
# --------------------------------------------------------------------------- #

async def _run_batch(req: ApplyBatchRequest) -> None:
    state.batch_running = True
    state.batch_applied = 0
    state.batch_skipped = 0
    state.batch_outcomes = []
    state.batch_missing_fields = []
    state.batch_tokens = {"prompt": 0, "completion": 0, "total": 0, "cost": 0.0}
    state.batch_queue = []
    state.batch_stop_event.clear()

    search_start = 0
    all_missing: set[str] = set()

    try:
        # Ensure we have a session (login already verified by caller)
        if not state.cdp_url:
            if not await _attach_logged_in_session():
                await _ensure_session()

        state.push_log(
            "info",
            f"[BATCH] Starting: {req.keywords!r} in {req.location!r} | "
            f"max={req.max_applications} | policy={req.submit_policy}",
        )

        while state.batch_applied < req.max_applications:
            if state.batch_stop_event.is_set():
                state.push_log("info", "[BATCH] Stop requested — exiting loop")
                break

            # Refill queue from next search page
            if not state.batch_queue:
                job_ids = await _ADAPTER.discover_jobs(
                    {
                        "cdp_url": state.cdp_url,
                        "keywords": req.keywords,
                        "location": req.location,
                        "start": search_start,
                        "log": state.push_log,
                        "easy_apply_only": req.easy_apply_only,
                        "filters": {
                            "f_TPR": req.date_posted,
                            "f_E": req.experience,
                            "f_WT": req.workplace,
                        },
                    },
                    10,
                )
                if not job_ids:
                    state.push_log("info", "[BATCH] No more jobs found — done")
                    break
                search_start += 10
                new_ids = [jid for jid in job_ids if not _ledger_seen("linkedin", jid)]
                state.push_log(
                    "info",
                    f"[BATCH] {len(new_ids)} new IDs "
                    f"({len(job_ids) - len(new_ids)} already in ledger, skipped)",
                )
                state.batch_queue = new_ids

            if not state.batch_queue:
                # All found jobs were already seen; fetch next page
                if search_start > 100:  # safety cap
                    state.push_log("info", "[BATCH] Reached search page cap — done")
                    break
                continue

            job_id = state.batch_queue.pop(0)
            job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
            state.batch_current_job = job_url
            state.push_log("info", f"[JOB] Applying to {job_url}")

            hstate = HarnessState(submit_policy=req.submit_policy)
            state.apply_hstate = hstate

            try:
                out = await run_apply(
                    state.cdp_url, job_url, state.push_log, hstate,
                    max_steps=40, adapter=_ADAPTER,
                    easy_apply_only=req.easy_apply_only,
                )

                outcome_status = _classify_outcome(
                    hstate.submitted,
                    hstate.submit_skipped,
                    bool(out.get("needs_review")),
                    out.get("status", "completed"),
                )
                if outcome_status == "submitted":
                    state.batch_applied += 1
                else:
                    state.batch_skipped += 1

                _ledger_record("linkedin", job_id, "", "", outcome_status)

                job_tokens = out.get("tokens") or {}
                for k in ("prompt", "completion", "total", "cost"):
                    state.batch_tokens[k] = round(state.batch_tokens.get(k, 0) + (job_tokens.get(k) or 0), 4)

                outcome = {
                    "job_id": job_id,
                    "job_url": job_url,
                    "status": outcome_status,
                    "needs_review": bool(out.get("needs_review")),
                    "tokens": job_tokens,
                    "missing_fields": out.get("fields_not_in_profile", []),
                    "ts": _utc_now(),
                }
                state.batch_outcomes.append(outcome)
                all_missing.update(out.get("fields_not_in_profile", []))

                state.push_log(
                    "job_outcome",
                    f"[JOB DONE] {job_id}: {outcome_status}",
                    job_id=job_id,
                    job_url=job_url,
                    status=outcome_status,
                )

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                state.push_log("error", f"[JOB] Failed {job_id}: {exc}")
                _ledger_record("linkedin", job_id, "", "", "failed")
                state.batch_outcomes.append({
                    "job_id": job_id,
                    "job_url": job_url,
                    "status": "failed",
                    "ts": _utc_now(),
                })
                state.batch_skipped += 1
            finally:
                state.apply_hstate = None
                state.batch_current_job = None
                if state.cdp_url:
                    await _close_extra_tabs(state.cdp_url)

            if state.batch_applied >= req.max_applications:
                break

            # Rate-limit: 30–90s jitter between applications (ToS / anti-bot)
            if not state.batch_stop_event.is_set():
                delay = random.uniform(30, 90)
                state.push_log("info", f"[RATE] Waiting {delay:.0f}s before next application")
                try:
                    await asyncio.wait_for(state.batch_stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

        state.batch_missing_fields = sorted(all_missing)
        state.push_log(
            "info",
            f"[BATCH] Done: {state.batch_applied} submitted, {state.batch_skipped} skipped",
        )
        if all_missing:
            state.push_log(
                "flag-missing",
                "Fields not found in profile (fill profile.yaml to improve auto-submit rate): "
                + "; ".join(sorted(all_missing)[:20]),
            )

    except asyncio.CancelledError:
        state.push_log("warning", "[BATCH] Cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        state.push_log("error", f"[BATCH] Fatal error: {exc}")
    finally:
        state.batch_running = False
        state.batch_task = None
        state.batch_queue = []
        state.batch_current_job = None
        state.apply_hstate = None


# --------------------------------------------------------------------------- #
# LangGraph orchestrator background task (Phase A)
#   Same login -> discover -> apply loop as _run_batch, but the control flow lives
#   in orchestrator/graph.py. Nodes call the same seams (discover_jobs, run_apply,
#   ledger); HITL still rides the existing apply_hstate + /api/hitl path.
# --------------------------------------------------------------------------- #

async def _run_orchestrator(req: ApplyBatchRequest) -> None:
    from orchestrator.graph import run_orchestration  # late: avoids import cycle at load

    state.orch_running = True
    state.orch_result = None
    state.orch_stop_event.clear()
    try:
        final = await run_orchestration(req)
        state.orch_result = {
            "applied": final.get("applied", 0),
            "skipped": final.get("skipped", 0),
            "outcomes": final.get("outcomes", []),
            "missing_fields": final.get("missing_fields", []),
            "done_reason": final.get("done_reason", ""),
            "logged_in": final.get("logged_in"),
        }
        state.push_log(
            "info",
            f"[GRAPH] Done: {final.get('applied', 0)} submitted, {final.get('skipped', 0)} skipped"
            + (f" ({final['done_reason']})" if final.get("done_reason") else ""),
        )
    except asyncio.CancelledError:
        state.push_log("warning", "[GRAPH] Cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        state.push_log("error", f"[GRAPH] Fatal error: {exc}")
    finally:
        state.orch_running = False
        state.orch_task = None


# --------------------------------------------------------------------------- #
# Single-job apply background task
# --------------------------------------------------------------------------- #

async def _run_apply(job_url: str, max_steps: int, submit_policy: str) -> None:
    assert state.cdp_url is not None
    hstate = HarnessState(submit_policy=submit_policy)
    state.apply_hstate = hstate
    state.running = True
    try:
        out = await run_apply(
            state.cdp_url, job_url, state.push_log, hstate,
            max_steps=max_steps, adapter=_ADAPTER,
        )
        state.last_result = out
    except asyncio.CancelledError:
        state.push_log("warning", "Apply cancelled by user")
        raise
    except Exception as exc:  # noqa: BLE001
        state.last_result = {"status": "failed", "error": str(exc)}
        state.push_log("error", "Apply failed", error=str(exc))
    finally:
        state.running = False
        state.command_task = None
        state.apply_hstate = None


# --------------------------------------------------------------------------- #
# Command runner (generic browser-use task)
# --------------------------------------------------------------------------- #

async def _run_command(req: CommandRequest) -> None:
    provider, api_key, model = _llm_config()
    assert state.steel_session is not None
    cfg = RunConfig(
        task=req.instruction,
        llm_provider=provider,
        llm_api_key=api_key,
        llm_model=model,
        llm_base_url=(os.getenv("GROQ_BASE_URL") or "https://api.groq.com").strip(),
        max_steps=req.max_steps,
        step_approval=False,
        use_vision=False,
        enable_judge=False,
    )
    state.push_log("info", "Command started", instruction=req.instruction, max_steps=req.max_steps)
    state.running = True
    try:
        result = await run_task(session=state.steel_session, config=cfg, event_bus=state.event_bus)
        state.last_result = {
            "status": result.status,
            "output": result.output,
            "steps": result.steps,
            "duration_s": result.duration_s,
            "hitl_pauses": result.hitl_pauses,
        }
        state.push_log("info", "Command completed", status=result.status, steps=result.steps)
    except asyncio.CancelledError:
        state.push_log("warning", "Command cancelled by user")
        raise
    except Exception as exc:
        state.last_result = {"status": "failed", "error": str(exc)}
        state.push_log("error", "Command failed", error=str(exc))
    finally:
        state.running = False
        state.command_task = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "running": state.running or state.batch_running, "has_session": bool(state.session_id)}


@app.get("/api/login-status")
async def login_status(platform: str = "linkedin") -> dict[str, Any]:
    """Check whether the active Steel session has a valid login cookie for the platform."""
    # Attach to existing logged-in session if we don't have one
    if not state.session_id:
        attached = await _attach_logged_in_session()
        if not attached:
            # Create a new empty session so user has something to log into
            try:
                await _ensure_session()
            except Exception:
                return {"logged_in": False, "platform": platform,
                        "session_id": None, "interactive_viewer_url": None}

    logged_in = await _check_login(platform)
    if logged_in != state.login_checked:
        if logged_in:
            state.push_log("info", f"Login check: logged_in=True ({platform})")
        else:
            state.push_log("info", f"Login check: NOT logged in to {platform} — viewer shown for manual login")
    state.login_checked = logged_in

    return {
        "logged_in": logged_in,
        "platform": platform,
        "session_id": state.session_id,
        "interactive_viewer_url": _interactive_viewer_url(),
    }


@app.post("/api/session/start", response_model=StartSessionResponse)
async def start_session() -> StartSessionResponse:
    if state.running:
        raise HTTPException(status_code=409, detail="Cannot start session while command is running")
    session = await _ensure_session()
    return StartSessionResponse(
        session_id=session.session_id,
        viewer_url=session.viewer_url,
        cdp_url=session.cdp_url,
    )


@app.post("/api/session/restart")
async def restart_session() -> dict[str, Any]:
    """Tear down current + orphaned Steel sessions, create a fresh one, navigate to LinkedIn login."""
    if state.command_task and not state.command_task.done():
        state.command_task.cancel()
        with contextlib.suppress(Exception):
            await state.command_task
    if state.batch_task and not state.batch_task.done():
        state.batch_stop_event.set()
        state.batch_task.cancel()
        with contextlib.suppress(Exception):
            await state.batch_task
    if state.orch_task and not state.orch_task.done():
        state.orch_stop_event.set()
        state.orch_task.cancel()
        with contextlib.suppress(Exception):
            await state.orch_task

    if state.steel_session:
        with contextlib.suppress(Exception):
            await state.steel_session.release()
    await _release_all_sessions()

    state.steel_session = None
    state.session_id = None
    state.cdp_url = None
    state.viewer_url = None
    state.login_checked = None
    state.last_result = None
    state.apply_hstate = None
    state.running = False
    state.batch_running = False
    state.orch_running = False
    state.orch_task = None

    try:
        await _ensure_session()
    except Exception as e:  # noqa: BLE001
        state.push_log("error", f"restart: could not create session: {e}")
        raise HTTPException(status_code=503, detail=f"Could not create a new Steel session: {e}")

    await _navigate(LINKEDIN_LOGIN_URL)
    state.push_log("info", "Session restarted (fresh) — navigated to LinkedIn login")

    return {
        "session_id": state.session_id,
        "interactive_viewer_url": _interactive_viewer_url(),
        "cdp_url": state.cdp_url,
    }


@app.delete("/api/session")
async def stop_session() -> dict[str, bool]:
    if state.running and state.command_task:
        state.command_task.cancel()
        with contextlib.suppress(Exception):
            await state.command_task
    if state.steel_session:
        with contextlib.suppress(Exception):
            await state.steel_session.release()
    elif state.session_id:
        await _release_session_by_id(state.session_id)
    state.steel_session = None
    state.session_id = None
    state.cdp_url = None
    state.viewer_url = None
    state.login_checked = None
    state.push_log("info", "Steel session released")
    return {"released": True}


@app.post("/api/browser/hygiene")
async def browser_hygiene() -> dict[str, bool]:
    """Close extra browser tabs without destroying the session or losing login cookies."""
    if not state.cdp_url:
        raise HTTPException(status_code=503, detail="No active session")
    await _close_extra_tabs(state.cdp_url)
    state.push_log("info", "[HYGIENE] Extra tabs closed")
    return {"ok": True}


@app.post("/api/command")
async def run_command_endpoint(req: CommandRequest) -> dict[str, str]:
    if not req.instruction.strip():
        raise HTTPException(status_code=400, detail="instruction is required")
    if state.running or state.batch_running or state.orch_running:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    await _ensure_session()
    state.command_task = asyncio.create_task(_run_command(req))
    return {"status": "started"}


@app.post("/api/command/stop")
async def stop_command(req: StopRequest) -> dict[str, str]:
    if not state.running or not state.command_task:
        return {"status": "idle"}
    state.push_log("warning", "Stop requested", reason=req.reason)
    state.command_task.cancel()
    return {"status": "stopping"}


@app.post("/api/apply")
async def apply_endpoint(req: ApplyRequest) -> dict[str, str]:
    """Single-job apply (debug / manual use)."""
    if not req.job_url.strip():
        raise HTTPException(status_code=400, detail="job_url is required")
    if state.running or state.batch_running or state.orch_running:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    if not (state.cdp_url and state.session_id):
        if not await _attach_logged_in_session():
            await _ensure_session()
    state.push_log("info", "Apply requested", job_url=req.job_url)
    state.command_task = asyncio.create_task(
        _run_apply(req.job_url, req.max_steps, req.submit_policy)
    )
    state.running = True
    return {"status": "started", "interactive_viewer_url": _interactive_viewer_url() or ""}


@app.post("/api/apply-batch")
async def apply_batch_endpoint(req: ApplyBatchRequest) -> dict[str, str]:
    """Multi-job batch apply: search LinkedIn → apply → ledger → rate-limit → repeat."""
    if not req.keywords.strip():
        raise HTTPException(status_code=400, detail="keywords is required")
    if state.running or state.batch_running or state.orch_running:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    if not (state.cdp_url and state.session_id):
        if not await _attach_logged_in_session():
            await _ensure_session()
    logged_in = await _check_login("linkedin")
    if not logged_in:
        raise HTTPException(
            status_code=403,
            detail="Not logged in to LinkedIn. Open the interactive viewer and log in first.",
        )
    state.push_log(
        "info",
        f"[BATCH] Requested: {req.keywords!r} | {req.location} | max={req.max_applications}",
    )
    state.batch_task = asyncio.create_task(_run_batch(req))
    return {"status": "started", "interactive_viewer_url": _interactive_viewer_url() or ""}


@app.post("/api/batch/stop")
async def batch_stop() -> dict[str, str]:
    if not state.batch_running:
        return {"status": "idle"}
    state.push_log("info", "[BATCH] Stop requested")
    state.batch_stop_event.set()
    if state.batch_task and not state.batch_task.done():
        state.batch_task.cancel()
    return {"status": "stopping"}


@app.post("/api/orchestrate/start")
async def orchestrate_start(req: ApplyBatchRequest) -> dict[str, str]:
    """Start the LangGraph orchestrator (login -> discover -> apply loop).

    Unlike /api/apply-batch, login is a graph node: if not logged in, the graph ends
    at the login node and reports it via status, rather than 403-ing here."""
    if not req.keywords.strip():
        raise HTTPException(status_code=400, detail="keywords is required")
    if state.running or state.batch_running or state.orch_running:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    if not (state.cdp_url and state.session_id):
        if not await _attach_logged_in_session():
            await _ensure_session()
    state.push_log(
        "info",
        f"[GRAPH] Requested: {req.keywords!r} | {req.location} | max={req.max_applications}",
    )
    state.orch_task = asyncio.create_task(_run_orchestrator(req))
    return {"status": "started", "interactive_viewer_url": _interactive_viewer_url() or ""}


@app.get("/api/orchestrate/status")
async def orchestrate_status() -> dict[str, Any]:
    # Live progress read straight from the graph checkpointer (per thread_id).
    snap: dict[str, Any] = {}
    try:
        from orchestrator.graph import THREAD_ID, get_graph
        st = get_graph().get_state({"configurable": {"thread_id": THREAD_ID}})
        snap = dict(st.values) if st and st.values else {}
    except Exception:  # noqa: BLE001
        snap = {}
    h = state.apply_hstate
    awaiting = bool(h and getattr(h, "awaiting_human", False))
    return {
        "orch_running": state.orch_running,
        "applied": snap.get("applied", 0),
        "skipped": snap.get("skipped", 0),
        "current_node": snap.get("current_node", ""),
        "current_job": snap.get("current_job"),
        "queue_size": len(snap.get("job_queue", []) or []),
        "outcomes": (snap.get("outcomes", []) or [])[-20:],
        "missing_fields": snap.get("missing_fields", []),
        "done_reason": snap.get("done_reason", ""),
        "logged_in": snap.get("logged_in"),
        "orch_result": state.orch_result,
        # HITL rides the same apply_hstate + /api/hitl path as the batch.
        "awaiting_human": awaiting,
        "hitl_type": (h.hitl_type if awaiting else None),
        "hitl_question": (h.hitl_question if awaiting else None),
        "hitl_options": (h.hitl_options if awaiting else None),
        "hitl_field_label": (h.hitl_field_label if awaiting else None),
    }


@app.post("/api/orchestrate/stop")
async def orchestrate_stop() -> dict[str, str]:
    if not state.orch_running:
        return {"status": "idle"}
    state.push_log("info", "[GRAPH] Stop requested")
    state.orch_stop_event.set()
    if state.orch_task and not state.orch_task.done():
        state.orch_task.cancel()
    return {"status": "stopping"}


@app.post("/api/hitl")
async def hitl_endpoint(req: HitlRequest) -> dict[str, str]:
    h = state.apply_hstate
    if not h or not getattr(h, "awaiting_human", False):
        return {"status": "not_waiting"}

    if req.action == "approve":
        answer = "approve"
    elif req.action == "skip":
        answer = "skip"
    else:
        answer = req.value

    state.push_log("info", f"Human HITL: action={req.action} value={req.value[:80]!r}")

    # Write-back learned answer for non-protected field answers
    h_type = getattr(h, "hitl_type", None)
    field_label = getattr(h, "hitl_field_label", None)
    if h_type == "field" and field_label and req.action == "answer" and answer:
        if not _is_protected(_norm(field_label)):
            _save_learned_answer(_norm(field_label), answer)
            state.push_log("info", f"Learned answer saved for: '{field_label[:60]}'")

    h.answer(answer)
    return {"status": "ok"}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    h = state.apply_hstate
    awaiting = bool(h and getattr(h, "awaiting_human", False))
    return {
        "running": state.running,
        "session_id": state.session_id,
        "viewer_url": state.viewer_url,
        "interactive_viewer_url": _interactive_viewer_url(),
        "cdp_url": state.cdp_url,
        "last_result": state.last_result,
        "login_checked": state.login_checked,
        # Reconciler honest-stop: surfaced so operators see jobs that need review
        "apply_needs_review": bool(h and getattr(h, "needs_review", False)),
        # HITL
        "awaiting_human": awaiting,
        "hitl_type": (h.hitl_type if awaiting else None),
        "hitl_question": (h.hitl_question if awaiting else None),
        "hitl_options": (h.hitl_options if awaiting else None),
        "hitl_field_label": (h.hitl_field_label if awaiting else None),
        # Batch
        "batch_running": state.batch_running,
        "batch_applied": state.batch_applied,
        "batch_skipped": state.batch_skipped,
        "batch_queue_size": len(state.batch_queue),
        "batch_current_job": state.batch_current_job,
        "batch_outcomes": state.batch_outcomes[-20:],  # last 20
        "batch_missing_fields": state.batch_missing_fields,
        "batch_tokens": state.batch_tokens,
    }


@app.get("/api/ledger")
async def get_ledger() -> dict[str, Any]:
    return _ledger_load()


@app.get("/api/logs/recent")
async def recent_logs() -> list[dict[str, Any]]:
    return list(state.logs)


@app.get("/api/logs/stream")
async def stream_logs() -> EventSourceResponse:
    async def gen() -> AsyncGenerator[dict[str, str], None]:
        idx = 0
        while True:
            rows = list(state.logs)
            while idx < len(rows):
                row = rows[idx]
                idx += 1
                yield {"event": "log", "data": json.dumps(row, default=str)}
            await asyncio.sleep(0.3)

    return EventSourceResponse(gen())


@app.get("/api/audit/stream")
async def stream_audit() -> EventSourceResponse:
    async def gen() -> AsyncGenerator[dict[str, str], None]:
        async for evt in state.event_bus.subscribe():
            yield {"event": "audit", "data": json.dumps(evt.to_json_dict(), default=str)}

    return EventSourceResponse(gen())


@app.get("/")
async def ui() -> FileResponse:
    dist_html = _DIST / "index.html"
    if dist_html.exists():
        return FileResponse(str(dist_html))
    return FileResponse(str(ROOT / "v5" / "frontend" / "index.html"))


@app.on_event("shutdown")
async def _shutdown() -> None:
    if state.command_task and not state.command_task.done():
        state.command_task.cancel()
    if state.batch_task and not state.batch_task.done():
        state.batch_task.cancel()
    if state.orch_task and not state.orch_task.done():
        state.orch_task.cancel()
    if state.steel_session:
        await state.steel_session.release()
