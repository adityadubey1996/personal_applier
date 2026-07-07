"""Platform-agnostic reconciliation core for the job-apply harness.

Wakes only on divergence or stall — the Actor (browser-use Agent) runs free
otherwise.  All heavy logic lives here so apply_harness.py stays thin.

Do NOT import from apply_harness or platforms/ at module load time — both are
injected at call time so this file stays import-cycle-free.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Honest-stop thresholds, read from browser-use's OWN counters on agent.state.
# A healthy run (even on a static review/submit page) has consecutive_failures==0
# and low repetition, so these never fire on the happy path — only a genuinely
# stuck Actor trips them. (Spec §6: reuse browser-use's loop counter, don't reinvent.)
_STALL_FAILS = 4      # consecutive failed actions (browser-use ends the run at max_failures=5)
_STALL_REPS = 8       # ActionLoopDetector.max_repetition_count — clearly looping
_DATA_DIR = (os.getenv("STEEL_BROWSER_DATA_MOUNT") or "/app/data").rstrip("/")
_ANOMALY_PATH = Path(_DATA_DIR) / "anomalies.jsonl"

# ---------------------------------------------------------------------------
# JS expression — reads all aria-invalid form fields + their inline errors
# ---------------------------------------------------------------------------

_JS_INVALID_FIELDS = r"""
(function() {
  var results = [];
  var inputs = document.querySelectorAll('input[aria-invalid="true"], textarea[aria-invalid="true"], select[aria-invalid="true"]');
  inputs.forEach(function(el) {
    var label = '';
    var id = el.id;
    if (id) {
      var lbl = document.querySelector('label[for="' + id + '"]');
      if (lbl) label = lbl.innerText.trim();
    }
    if (!label) label = el.getAttribute('aria-label') || el.name || '';
    var errEl = el.closest('.fb-dash-form-element, .artdeco-form__item') &&
                el.closest('.fb-dash-form-element, .artdeco-form__item').querySelector('.artdeco-inline-feedback--error');
    var errorText = errEl ? errEl.innerText.trim() : '';
    var constraints = {
      type: el.type || el.tagName.toLowerCase(),
      min: el.min || null,
      max: el.max || null,
      pattern: el.pattern || null,
      maxlength: el.maxLength > 0 ? el.maxLength : null,
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(function(o) { return o.text.trim(); }) : []
    };
    results.push({label: label, value: el.value, errorText: errorText, constraints: constraints});
  });
  return results;
})()
""".strip()


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Divergence:
    field_label: str
    intended: Optional[str]       # value we tried to set, if known
    observed: Optional[str]       # current value on page
    error_text: Optional[str]     # inline validation error text, if any
    constraints: dict             # {type, min, max, pattern, maxlength, options:[...]}
    kind: str                     # "mechanical" | "judgment" | "unknown"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_browser_session(agent: Any):
    """Return agent.browser_session (attribute name varies by browser-use version)."""
    for attr in ("browser_session", "_browser_session", "browser"):
        bs = getattr(agent, attr, None)
        if bs is not None:
            return bs
    return None


async def _cdp_evaluate(agent: Any, expression: str) -> Any:
    """Run a CDP Runtime.evaluate and return .result.value.  Returns None on any error."""
    try:
        browser_session = _get_browser_session(agent)
        if browser_session is None:
            return None
        cdp_session = await browser_session.get_or_create_cdp_session()
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=cdp_session.session_id,
        )
        return (result.get("result") or {}).get("value")
    except Exception:
        return None


def _is_stuck(agent: Any) -> bool:
    """True when the Actor is genuinely stuck — repeatedly failing or looping.

    Reads browser-use's own per-run counters off agent.state (ActionLoopDetector +
    consecutive_failures). These only climb when actions fail or repeat; on a page
    where the agent keeps making progress (filling fields, scrolling a review page,
    clicking toward Submit) they stay at 0. That is exactly why this replaces the
    old page-signature heuristic, which mistook a static review page for a stall and
    aborted healthy, ready-to-submit applications.
    """
    st = getattr(agent, "state", None)
    if st is None:
        return False
    if (getattr(st, "consecutive_failures", 0) or 0) >= _STALL_FAILS:
        return True
    ld = getattr(st, "loop_detector", None)
    reps = (getattr(ld, "max_repetition_count", 0) or 0) if ld is not None else 0
    return reps >= _STALL_REPS


def _append_anomaly(
    platform: str,
    job_url: str,
    step: int,
    div: Divergence,
    cls: str,
    resolution: str,
) -> None:
    """Best-effort append of one JSON line to anomalies.jsonl."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "platform": platform,
            "job_url": job_url,
            "step": step,
            "field": div.field_label,
            "intended": div.intended,
            "observed": div.observed,
            "class": cls,
            "resolution": resolution,
        }
        _ANOMALY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _ANOMALY_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # anomaly log is best-effort; never crash the agent


def _heal_input(div: Divergence) -> Optional[str]:
    """The value to feed the healer: the intended value when we know it, else the
    bad value still sitting in the field. For an aria-invalid field detected on the
    page we don't recover the original intent, but the REJECTED text (e.g. '₹30 LPA')
    is exactly what numeric/maxlength coercion needs to correct."""
    return div.intended if div.intended is not None else div.observed


# ---------------------------------------------------------------------------
# Core public functions
# ---------------------------------------------------------------------------

async def observe(agent: Any, hstate: Any) -> tuple[list[Divergence], bool]:
    """Pure read — no mutation.

    Returns (divergences, stalled).

    - divergences: fields the page actively rejected (aria-invalid + inline error).
      A bare transient action error (stale index, "page changed") is NOT a
      divergence — escalating those to HITL is wrong and the Actor self-recovers;
      repeated failures instead surface through the stall signal below.
    - stalled: the Actor is genuinely stuck (browser-use's own failure/loop
      counters), not merely sitting on a static page.
    """
    divergences: list[Divergence] = []

    # aria-invalid fields via CDP — the real, healable/escalatable divergences.
    invalid_fields: list[dict] = []
    try:
        raw = await _cdp_evaluate(agent, _JS_INVALID_FIELDS)
        if isinstance(raw, list):
            invalid_fields = raw
    except Exception:
        invalid_fields = []

    # intended is unknown from the DOM alone; classify()/reconcile() heal from the
    # rejected on-page value (observed) via _heal_input().
    for item in invalid_fields:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        value = item.get("value")
        error_text = item.get("errorText") or ""
        constraints = item.get("constraints") or {}
        divergences.append(
            Divergence(
                field_label=label,
                intended=None,
                observed=str(value) if value is not None else None,
                error_text=str(error_text) if error_text else None,
                constraints=constraints if isinstance(constraints, dict) else {},
                kind="unknown",   # classify() will refine this
            )
        )

    return divergences, _is_stuck(agent)


def classify(div: Divergence, resolver: Any) -> str:
    """Return "mechanical" or "judgment".

    A divergence is mechanical when resolver.heal() returns a concrete value.
    heal() is added to Resolver in Task 4; we handle AttributeError gracefully.
    """
    try:
        value = resolver.heal(
            div.field_label,
            _heal_input(div),
            div.error_text,
            div.constraints,
        )
        if value is not None:
            return "mechanical"
        return "judgment"
    except AttributeError:
        # resolver.heal() not yet implemented (Task 4)
        return "judgment"
    except Exception:
        return "judgment"


async def reconcile(
    agent: Any,
    hstate: Any,      # HarnessState from apply_harness
    resolver: Any,    # Resolver from apply_harness
    adapter: Any,     # PlatformAdapter
    log: Callable,    # LogFn callable
) -> None:
    """Called from on_step_end.  Wakes only when divergence or stall is detected.

    Fast path: no divergences AND not stalled → return immediately.
    """
    divergences, stalled = await observe(agent, hstate)

    if not divergences and not stalled:
        return  # fast path — Actor runs free

    platform = getattr(adapter, "name", "unknown") or "unknown"
    raw_task = getattr(agent, "task", "") or ""
    # Trim to the first URL-like token for the anomaly log
    job_url = ""
    for token in raw_task.split():
        if token.startswith("http"):
            job_url = token
            break
    step = getattr(getattr(agent, "state", None), "n_steps", 0) or 0

    any_resolved = False

    for div in divergences:
        kind = classify(div, resolver)
        div.kind = kind  # update in-place so anomaly log sees refined kind

        if kind == "mechanical":
            try:
                value = resolver.heal(
                    div.field_label,
                    _heal_input(div),
                    div.error_text,
                    div.constraints,
                )
            except Exception:
                value = None

            if value is not None:
                try:
                    agent.add_new_task(
                        f"Fix field '{div.field_label}': enter '{value}'. "
                        f"The field currently shows error: {div.error_text or '(validation error)'}. "
                        "After entering the value, verify the error is gone before proceeding."
                    )
                except Exception as e:
                    log("warn", f"reconcile: add_new_task failed: {e}")

                log(
                    "reconcile",
                    f"healed '{div.field_label}': {div.intended!r} → {value!r}",
                )
                _append_anomaly(platform, job_url, step, div, "mechanical", "healed")
                any_resolved = True
            else:
                # heal returned None unexpectedly — treat as judgment
                div.kind = "judgment"
                kind = "judgment"

        if kind == "judgment":
            question = (
                f'Field "{div.field_label}" shows error: {div.error_text or "(validation error)"}. '
                "What value should be entered?"
            )
            try:
                answer = await hstate.wait_for_human(
                    question=question,
                    hitl_type="field",
                    field_label=div.field_label,
                )
            except asyncio.TimeoutError:
                answer = ""
            except Exception as e:
                log("warn", f"reconcile: wait_for_human failed: {e}")
                answer = ""

            if answer:
                try:
                    agent.add_new_task(
                        f"Fix field '{div.field_label}': enter '{answer}'."
                    )
                except Exception as e:
                    log("warn", f"reconcile: add_new_task failed: {e}")
                any_resolved = True
                _append_anomaly(platform, job_url, step, div, "judgment", "escalated")
            else:
                _append_anomaly(platform, job_url, step, div, "judgment", "skipped")

    # ------------------------------------------------------------------
    # Stall handling — if we had a stall but nothing was resolved
    # ------------------------------------------------------------------
    if stalled and not any_resolved:
        hstate.needs_review = True
        log("reconcile", "Stall unresolved — marking job needs_review, stopping agent")
        try:
            agent.add_new_task(
                "STOP. This application cannot be completed automatically — the form is stuck. "
                "Call done without clicking Submit."
            )
        except Exception as e:
            log("warn", f"reconcile: add_new_task (stop) failed: {e}")
        # Log a synthetic divergence representing the stall
        stall_div = Divergence(
            field_label="(stall)",
            intended=None,
            observed=None,
            error_text="Agent stalled: repeated action failures or action loop detected",
            constraints={},
            kind="unknown",
        )
        _append_anomaly(platform, job_url, step, stall_div, "unknown", "skipped")
