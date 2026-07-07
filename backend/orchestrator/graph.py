"""Assemble + run the Phase A orchestration StateGraph.

    login ──logged_in?──▶ discover ──has jobs?──▶ apply ──under max?──▶ cooldown ──▶ apply/discover
      └─no─▶ END              └─none─▶ END          └─done─▶ END

A MemorySaver checkpointer persists state per thread_id, so /api/orchestrate/status
can read live progress via get_state() and Phase B can add interrupt()-based HITL.
"""
from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from orchestrator import nodes
from orchestrator.state import OrchestratorState

THREAD_ID = "orchestrator"  # single active orchestration at a time (like the batch)

_GRAPH = None


def build_graph():
    g = StateGraph(OrchestratorState)
    g.add_node("login", nodes.login_node)
    g.add_node("discover", nodes.discover_node)
    g.add_node("apply", nodes.apply_node)
    g.add_node("cooldown", nodes.cooldown_node)

    g.add_edge(START, "login")
    g.add_conditional_edges("login", nodes.route_after_login,
                            {"discover": "discover", "end": END})
    g.add_conditional_edges("discover", nodes.route_after_discover,
                            {"apply": "apply", "discover": "discover", "end": END})
    g.add_conditional_edges("apply", nodes.route_after_apply,
                            {"cooldown": "cooldown", "end": END})
    g.add_conditional_edges("cooldown", nodes.route_after_cooldown,
                            {"apply": "apply", "discover": "discover"})

    return g.compile(checkpointer=MemorySaver())


def get_graph():
    """Compiled graph is a singleton so the checkpointer survives across requests."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


async def run_orchestration(req: Any, thread_id: str = THREAD_ID) -> dict:
    """Run the graph to completion. `req` is an ApplyBatchRequest (duck-typed)."""
    inputs: OrchestratorState = {
        "keywords": req.keywords,
        "location": req.location,
        "max_applications": req.max_applications,
        "submit_policy": req.submit_policy,
        "easy_apply_only": req.easy_apply_only,
        "date_posted": req.date_posted,
        "experience": req.experience,
        "workplace": req.workplace,
        "apply_engine": getattr(req, "apply_engine", "legacy") or "legacy",
        "current_node": "",
        "search_start": 0,
        "job_queue": [],
        "applied": 0,
        "skipped": 0,
        "outcomes": [],
        "missing_fields": [],
        "done_reason": "",
        # full reset — with a reused thread_id, unset keys would carry over from the
        # previous run's checkpoint (stale current_job/logged_in in /api/orchestrate/status)
        "logged_in": False,
        "viewer_url": None,
        "current_job": None,
        "last_result": None,
    }
    # recursion_limit scales with the job budget: ~2 super-steps per job (apply+cooldown)
    # plus a discover per page and login/routing slack — a constant cap breaks large runs.
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 4 * max(1, req.max_applications) + 40,
    }
    return await get_graph().ainvoke(inputs, config=config)
