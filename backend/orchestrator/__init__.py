"""LangGraph orchestration layer for v5.

Phase A: a StateGraph that owns the top-level flow login -> discover -> apply -> loop,
calling the EXISTING backend seams (app._ensure_session / discover_jobs / run_apply /
ledger) as nodes. browser-use and the reconciler harness are untouched — the graph is
the brain, they are the hands.
"""
