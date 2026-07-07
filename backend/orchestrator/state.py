"""Graph state — the single dict threaded through every node.

All keys are initialized by run_orchestration's inputs dict (required keys). Nodes
still return only the keys they changed (typed as plain dict); LangGraph merges them
(last-write-wins per key, which is all this linear flow needs).
"""
from __future__ import annotations

from typing import Any, Optional, TypedDict


class OrchestratorState(TypedDict):
    # --- inputs (search config, mirrors ApplyBatchRequest) ---
    keywords: str
    location: str
    max_applications: int
    submit_policy: str          # "hitl" | "auto" | "auto_if_clean"
    easy_apply_only: bool
    date_posted: str            # LinkedIn f_TPR code
    experience: str             # LinkedIn f_E code
    workplace: str              # LinkedIn f_WT code
    apply_engine: str           # "legacy" (agent.run) | "graph" (take_step loop)

    # --- login ---
    logged_in: bool
    viewer_url: Optional[str]

    # --- discovery / queue ---
    search_start: int           # pagination offset into LinkedIn search
    job_queue: list[str]        # job IDs awaiting apply

    # --- progress ---
    current_job: Optional[str]
    applied: int
    skipped: int
    outcomes: list[dict[str, Any]]
    missing_fields: list[str]
    last_result: Optional[dict[str, Any]]

    # --- termination ---
    done_reason: str            # "" while running; set when the graph should stop
    current_node: str           # last node that ran (drives the UI graph trace)
