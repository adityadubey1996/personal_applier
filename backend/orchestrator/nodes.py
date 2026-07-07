"""Graph nodes + routers.

Each node is a thin adapter over an existing app.py seam — no new business logic.
All `app` imports are done lazily INSIDE the functions: app.py imports this package
to wire its endpoints, so a module-level `import app` here would be circular. Late
imports also match the codebase idiom (see app._navigate, apply_harness.on_step_end).
"""
from __future__ import annotations

import asyncio
import random

from orchestrator.state import OrchestratorState


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #

async def login_node(state: OrchestratorState) -> dict:
    """Ensure a Steel session exists (reuse a logged-in one if present) and check login."""
    from app import (
        _attach_logged_in_session,
        _check_login,
        _ensure_session,
        _interactive_viewer_url,
        state as rt,
    )

    if not (rt.cdp_url and rt.session_id):
        if not await _attach_logged_in_session():
            await _ensure_session()

    logged_in = await _check_login("linkedin")
    viewer = _interactive_viewer_url()
    if logged_in:
        rt.push_log("orchestrate", "[GRAPH] login: logged in ✓")
        return {"logged_in": True, "viewer_url": viewer, "current_node": "login"}

    rt.push_log("orchestrate", f"[GRAPH] login: NOT logged in — open viewer to log in: {viewer}")
    return {"logged_in": False, "viewer_url": viewer, "done_reason": "not_logged_in", "current_node": "login"}


async def discover_node(state: OrchestratorState) -> dict:
    """Fetch the next page of job IDs (only when the queue is empty) and dedup vs the ledger."""
    from app import _ADAPTER, _ledger_seen, state as rt

    if state.get("job_queue"):
        return {"current_node": "discover"}  # queue still has work; nothing to fetch

    start = state.get("search_start", 0)
    job_ids = await _ADAPTER.discover_jobs(
        {
            "cdp_url": rt.cdp_url,
            "keywords": state["keywords"],
            "location": state.get("location", "India"),
            "start": start,
            "log": rt.push_log,
            "easy_apply_only": state.get("easy_apply_only", True),
            "filters": {
                "f_TPR": state.get("date_posted", ""),
                "f_E": state.get("experience", ""),
                "f_WT": state.get("workplace", ""),
            },
        },
        10,
    )
    if not job_ids:
        rt.push_log("orchestrate", "[GRAPH] discover: no more jobs")
        return {"job_queue": [], "done_reason": "no_more_jobs", "current_node": "discover"}

    new_ids = [j for j in job_ids if not _ledger_seen("linkedin", j)]
    next_start = start + 10
    rt.push_log(
        "orchestrate",
        f"[GRAPH] discover: {len(new_ids)} new ({len(job_ids) - len(new_ids)} already in ledger)",
    )
    # All on this page were already applied → let the router loop back for the next
    # page, unless we've paged too far (mirror _run_batch's safety cap).
    done = "search_cap" if (not new_ids and next_start > 100) else ""
    return {"job_queue": new_ids, "search_start": next_start, "done_reason": done, "current_node": "discover"}


async def apply_node(state: OrchestratorState) -> dict:
    """Apply to the next queued job. Engine is APPLY_ENGINE: 'legacy' (browser-use's
    agent.run loop, default) or 'graph' (Phase B LangGraph take_step supervisor). Both
    share _build_apply/_finalize_apply, so the outcome dict is identical."""
    import os

    from app import (
        HarnessState,
        _ADAPTER,
        _classify_outcome,
        _close_extra_tabs,
        _ledger_record,
        _utc_now,
        state as rt,
    )

    # Engine: per-run request field (graph state) first, APPLY_ENGINE env as fallback
    engine = (state.get("apply_engine") or os.getenv("APPLY_ENGINE") or "legacy").strip().lower()
    if engine == "graph":
        from orchestrator.apply_graph import run_apply_graph as apply_fn
    else:
        from app import run_apply as apply_fn

    queue = list(state.get("job_queue", []))
    job_id = queue.pop(0)
    job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    rt.push_log("orchestrate", f"[GRAPH] apply: {job_url}")

    cdp_url = rt.cdp_url
    assert cdp_url is not None  # login node ran first — session exists

    hstate = HarnessState(submit_policy=state.get("submit_policy", "hitl"))
    rt.apply_hstate = hstate  # makes the existing /api/status + /api/hitl work as-is

    applied = state.get("applied", 0)
    skipped = state.get("skipped", 0)
    missing = list(state.get("missing_fields", []))
    try:
        out = await apply_fn(
            cdp_url,
            job_url,
            rt.push_log,
            hstate,
            max_steps=40,
            adapter=_ADAPTER,
            easy_apply_only=state.get("easy_apply_only", True),
        )
        outcome_status = _classify_outcome(
            hstate.submitted,
            hstate.submit_skipped,
            bool(out.get("needs_review")),
            out.get("status", "completed"),
        )
        _ledger_record("linkedin", job_id, "", "", outcome_status)
        if outcome_status == "submitted":
            applied += 1
        else:
            skipped += 1
        job_missing = out.get("fields_not_in_profile", []) or []
        missing = sorted(set(missing) | set(job_missing))
        outcome = {
            "job_id": job_id,
            "job_url": job_url,
            "status": outcome_status,
            "needs_review": bool(out.get("needs_review")),
            "missing_fields": job_missing,
            "tokens": out.get("tokens") or {},
            "ts": _utc_now(),
        }
        result = out
        rt.push_log("job_outcome", f"[GRAPH] {job_id}: {outcome_status}", job_id=job_id, status=outcome_status)
    except Exception as exc:  # noqa: BLE001 — one bad job must not kill the run
        rt.push_log("error", f"[GRAPH] apply failed {job_id}: {exc}")
        _ledger_record("linkedin", job_id, "", "", "failed")
        skipped += 1
        outcome = {"job_id": job_id, "job_url": job_url, "status": "failed", "missing_fields": [], "ts": _utc_now()}
        result = {"status": "failed", "error": str(exc)}
    finally:
        rt.apply_hstate = None
        if rt.cdp_url:
            await _close_extra_tabs(rt.cdp_url)

    return {
        "job_queue": queue,
        "current_node": "apply",
        "current_job": job_url,
        "applied": applied,
        "skipped": skipped,
        "outcomes": state.get("outcomes", []) + [outcome],
        "missing_fields": missing,
        "last_result": result,
    }


async def cooldown_node(state: OrchestratorState) -> dict:
    """Rate-limit gap between applications (ToS / anti-bot), interruptible by stop."""
    from app import state as rt

    delay = random.uniform(30, 90)  # ponytail: same jitter as _run_batch
    rt.push_log("orchestrate", f"[GRAPH] cooldown {delay:.0f}s before next application")
    try:
        await asyncio.wait_for(rt.orch_stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
    return {"current_node": "cooldown"}


# --------------------------------------------------------------------------- #
# Routers (conditional edges) — pure reads of graph state, plus the stop flag
# --------------------------------------------------------------------------- #

def route_after_login(state: OrchestratorState) -> str:
    return "discover" if state.get("logged_in") else "end"


def route_after_discover(state: OrchestratorState) -> str:
    if state.get("job_queue"):
        return "apply"
    if state.get("done_reason"):
        return "end"
    return "discover"  # page held only already-applied jobs → fetch the next page


def route_after_apply(state: OrchestratorState) -> str:
    from app import state as rt

    if rt.orch_stop_event.is_set():
        return "end"
    if state.get("applied", 0) >= state.get("max_applications", 1):
        return "end"
    return "cooldown"


def route_after_cooldown(state: OrchestratorState) -> str:
    return "apply" if state.get("job_queue") else "discover"
