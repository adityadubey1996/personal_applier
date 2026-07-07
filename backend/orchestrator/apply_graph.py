"""Phase B — the inner apply loop as a LangGraph supervisor.

Replaces browser-use's autonomous `agent.run()` with an explicit cyclic graph:

    act(take_step) ──▶ observe(reconcile) ──route──▶ act        (not done, more steps)
                                              └─────▶ END        (done / stalled / max_steps)

browser-use is still the hands — `agent.take_step()` runs one perceive→decide→act step
and its tools do the field-filling / HITL / submit-gating exactly as before. LangGraph
just owns the *loop*: it drives each step and runs the reconciler (the existing
`on_step_end` hook) between steps.

HITL still rides the harness's `asyncio.Event` + `/api/hitl` (it fires inside a tool,
inside `take_step`, below the graph's node boundary — so LangGraph `interrupt()` can't
reach it without pulling the tools out of browser-use; that's out of scope here).

No checkpointer: the state carries the live (unpicklable) Agent, so this graph is
in-memory only — which is correct for a short-lived per-job loop with no mid-job resume.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class ApplyState(TypedDict):
    # all keys are set at the single ainvoke() construction site — required by design
    agent: Any          # browser-use Agent (live object — never serialized)
    on_step_end: Any    # the reconcile callback from apply_harness._build_apply
    hstate: Any         # HarnessState (submitted/needs_review/HITL live here)
    step: int
    max_steps: int
    is_done: bool


async def _act(state: ApplyState) -> dict:
    """One browser-use step. take_step handles first-step init + done detection.
    Mirrors run()'s per-step timeout (service.py:2443) — a hung step must not hang the graph."""
    from browser_use.agent.views import AgentStepInfo

    agent = state["agent"]
    i = state.get("step", 0)
    n = state["max_steps"]
    timeout = getattr(getattr(agent, "settings", None), "step_timeout", None) or 1800
    try:
        is_done, _ = await asyncio.wait_for(
            agent.take_step(AgentStepInfo(step_number=i, max_steps=n)), timeout=timeout
        )
    except asyncio.TimeoutError:
        # Same bookkeeping as run()'s timeout path (service.py:2448-2458): count the
        # failure and record the error so the reconciler + failure-stop can see it.
        from browser_use import ActionResult

        msg = f"Step {i + 1} timed out after {timeout} seconds"
        agent.state.consecutive_failures = (getattr(agent.state, "consecutive_failures", 0) or 0) + 1
        agent.state.last_result = [ActionResult(error=msg)]
        log_fn = getattr(state.get("hstate"), "log_fn", None)
        if log_fn:
            log_fn("warn", f"[GRAPH-APPLY] {msg}")
        return {"is_done": False, "step": i + 1}
    return {"is_done": bool(is_done), "step": i + 1}


async def _observe(state: ApplyState) -> dict:
    """Run the reconciler — the SAME on_step_end hook the legacy path fires each step
    (observe divergences → classify → heal via add_new_task / HITL / honest-stop)."""
    await state["on_step_end"](state["agent"])
    return {}


def _route(state: ApplyState) -> str:
    if state.get("is_done"):
        return "end"
    if state.get("step", 0) >= state.get("max_steps", 0):
        return "end"
    agent = state["agent"]
    ast = getattr(agent, "state", None)
    # honor browser-use's own stop flag (reconciler's honest-stop drives the agent to done,
    # but this is a belt-and-suspenders guard against a runaway loop)
    if getattr(ast, "stopped", False):
        return "end"
    # run()'s hard failure-stop (service.py:2575): N consecutive failures ends the run
    settings = getattr(agent, "settings", None)
    max_fails = (getattr(settings, "max_failures", None) or 3) + int(
        bool(getattr(settings, "final_response_after_failure", False))
    )
    if (getattr(ast, "consecutive_failures", 0) or 0) >= max_fails:
        return "end"
    return "act"


_APPLY_GRAPH = None


def build_apply_graph():
    g = StateGraph(ApplyState)
    g.add_node("act", _act)
    g.add_node("observe", _observe)
    g.add_edge(START, "act")
    g.add_edge("act", "observe")  # step first, then reconcile — mirrors run()'s on_step_end order
    g.add_conditional_edges("observe", _route, {"act": "act", "end": END})
    return g.compile()  # no checkpointer: state holds the live Agent


def get_apply_graph():
    global _APPLY_GRAPH
    if _APPLY_GRAPH is None:
        _APPLY_GRAPH = build_apply_graph()
    return _APPLY_GRAPH


async def run_apply_graph(
    cdp_url: str,
    job_url: str,
    log: Any,
    hstate: Any,
    max_steps: int = 40,
    adapter: Any = None,
    easy_apply_only: bool = True,
) -> dict:
    """LangGraph-driven apply engine. Signature-compatible with apply_harness.run_apply
    so orchestrator.apply_node can swap engines behind a flag."""
    from apply_harness import _build_apply, _close_extra_tabs, _finalize_apply

    built = await _build_apply(cdp_url, job_url, log, hstate, max_steps, adapter, easy_apply_only)
    if isinstance(built, dict):
        return built  # external pre-click skip — same early result as legacy
    agent, on_step_end, effective_steps = built

    log("info", "[GRAPH-APPLY] LangGraph inner loop starting (take_step supervisor)")
    # run() starts the browser session before its loop; take_step does not, so we do it.
    await agent.browser_session.start()
    try:
        final = await get_apply_graph().ainvoke(
            {
                "agent": agent,
                "on_step_end": on_step_end,
                "hstate": hstate,
                "step": 0,
                "max_steps": effective_steps,
                "is_done": False,
            },
            # each iteration is act+observe (2 super-steps); +buffer for START/routing
            config={"recursion_limit": effective_steps * 2 + 10},
        )
        if final.get("is_done"):
            status = "completed"
        elif final.get("step", 0) >= effective_steps:
            status = "max_steps"
        else:
            status = "stalled"  # ended early: failure-stop or browser-use stop flag
    finally:
        # mirror run()'s finally: close browser-use's session wrapper (Steel persists),
        # then the same tab hygiene the legacy path does after agent.run().
        with contextlib.suppress(Exception):
            await agent.close()
        await _close_extra_tabs(cdp_url)

    return await _finalize_apply(agent, hstate, status, log)
