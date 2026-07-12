"""Extract a structured, human-readable summary from a browser-use AgentHistoryList.

Duck-types the raw result so we do not import browser-use types at module load time.
All attribute access is wrapped in try/except so this degrades gracefully across
browser-use patch releases.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

_MAX_OUTPUT = 800
_MAX_REASON = 300
_MAX_CONTENT = 500


def _get_all_results(raw: Any) -> list[Any]:
    try:
        return list(raw.all_results or [])
    except Exception:
        return []


def _last_done(results: list[Any]) -> Optional[Any]:
    """Return the last ActionResult where is_done is True, or None."""
    for r in reversed(results):
        try:
            if r.is_done:
                return r
        except Exception:
            continue
    return None


def _done_text(done_result: Any) -> str:
    """Prefer extracted_content; fall back to long_term_memory."""
    for attr in ("extracted_content", "long_term_memory"):
        try:
            v = getattr(done_result, attr, None)
            if v and str(v).strip():
                return str(v).strip()[:_MAX_OUTPUT]
        except Exception:
            continue
    return ""


def _done_success(done_result: Any) -> Optional[bool]:
    try:
        return bool(done_result.success) if done_result.success is not None else None
    except Exception:
        return None


def _judge_fields(done_result: Any) -> dict[str, Any]:
    """Extract judge verdict from the ActionResult.judgement (JudgementResult)."""
    out: dict[str, Any] = {}
    try:
        j = done_result.judgement
        if j is None:
            return out
        verdict = getattr(j, "verdict", None)
        if verdict is not None:
            out["judge_verdict"] = bool(verdict)
        reason = getattr(j, "failure_reason", None) or getattr(j, "reasoning", None)
        if reason and str(reason).strip():
            out["judge_failure_reason"] = str(reason).strip()[:_MAX_REASON]
        impossible = getattr(j, "impossible_task", None)
        if impossible is not None:
            out["judge_impossible_task"] = bool(impossible)
        captcha = getattr(j, "reached_captcha", None)
        if captcha is not None:
            out["judge_reached_captcha"] = bool(captcha)
    except Exception:
        pass
    return out


def summarize_browser_use_result(
    raw_result: Any,
    config_model: str,
    config_max_steps: int,
) -> tuple[str, Literal["completed", "failed"], dict[str, Any]]:
    """Return (human_output, status, extra_dict) from a browser-use AgentHistoryList.

    ``config_model`` and ``config_max_steps`` come straight from ``RunConfig`` and
    are included in ``extra`` so NDJSON files are self-describing.

    Status rules (in order):
    1. ``judge_verdict is False`` → failed
    2. ``done.success is False``  → failed
    3. else                       → completed
    """
    results = _get_all_results(raw_result)
    done = _last_done(results)

    human_output = ""
    extra: dict[str, Any] = {
        "llm_model": config_model,
        "max_steps_config": config_max_steps,
        "step_count": len(results),
        "last_action_is_done": done is not None,
    }

    if done is not None:
        human_output = _done_text(done)
        done_success = _done_success(done)
        if done_success is not None:
            extra["agent_done_success"] = done_success
        extra.update(_judge_fields(done))
    else:
        # No done action found; use last extracted_content available
        for r in reversed(results):
            try:
                v = getattr(r, "extracted_content", None) or getattr(r, "long_term_memory", None)
                if v and str(v).strip():
                    human_output = str(v).strip()[:_MAX_OUTPUT]
                    break
            except Exception:
                continue

    if not human_output:
        human_output = str(raw_result)[:_MAX_OUTPUT] if raw_result is not None else "No output"

    # Determine status
    judge_verdict = extra.get("judge_verdict")
    done_success = extra.get("agent_done_success")
    if judge_verdict is False or done_success is False:
        status: Literal["completed", "failed"] = "failed"
    else:
        status = "completed"

    return human_output, status, extra
