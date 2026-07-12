"""Run browser-use agent tasks against an active ``SteelSession``."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Optional

from .config import HitlCheckpoint, RunConfig
from .errors import SteelAgentError
from .events import AuditEvent, AuditPhase, EventBus, _safe_extra, _utc_iso
from .hitl import HumanGate, NullHumanGate
from .hooks import Hooks
from .llm import build_llm
from .markers import extract_browser_action_message, extract_form_question
from .results import TaskResult
from .run_summary import summarize_browser_use_result
from .session import SteelSession, get_system_message_patch

logger = logging.getLogger(__name__)


def _matches_checkpoint(url: str, checkpoints: list[HitlCheckpoint]) -> Optional[dict[str, str]]:
    for cp in checkpoints:
        for pattern in cp.patterns:
            if re.search(pattern, url or "", re.IGNORECASE):
                return {"reason": cp.reason, "message": cp.message}
    return None


def _actions_are_done_only(model_output: Any) -> bool:
    try:
        actions = model_output.action
    except Exception:
        return True
    if not actions:
        return True
    for a in actions:
        try:
            d = a.model_dump(exclude_none=True)
        except Exception:
            return False
        if not d:
            return False
        if set(d.keys()) != {"done"}:
            return False
    return True


def _format_planned_actions(model_output: Any, max_len: int = 1800) -> str:
    try:
        parts: list[Any] = []
        for a in model_output.action or []:
            try:
                parts.append(a.model_dump(exclude_none=True))
            except Exception:
                parts.append(str(a))
        text = json.dumps(parts, indent=2, default=str)
    except Exception as e:
        text = f"(could not serialize actions: {e})"
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _inject_message_manager_task(agent: Any, text: str) -> None:
    if not agent or not (text or "").strip():
        return
    mm = getattr(agent, "_message_manager", None)
    if mm and hasattr(mm, "add_new_task"):
        try:
            mm.add_new_task(text)
        except Exception as e:
            logger.warning("inject follow-up task: %s", e)


class _TaskRunner:
    def __init__(
        self,
        *,
        session: SteelSession,
        config: RunConfig,
        run_id: str,
        hooks: Optional[Hooks],
        human_gate: HumanGate,
        event_bus: Optional[EventBus],
        audit_events: list[AuditEvent],
    ) -> None:
        self.session = session
        self.config = config
        self.run_id = run_id
        self.hooks = hooks
        self.human_gate = human_gate
        self.event_bus = event_bus
        self.audit_events = audit_events
        self._paused = asyncio.Event()
        self._paused.set()
        self._stopped = False
        self.hitl_pauses = 0
        self.current_agent: Any = None

    async def _emit(
        self,
        phase: AuditPhase,
        *,
        step: Optional[int] = None,
        url: Optional[str] = None,
        action_summary: Optional[str] = None,
        thoughts: Optional[str] = None,
        hitl_reason: Optional[str] = None,
        hitl_message: Optional[str] = None,
        result: Optional[str] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> AuditEvent:
        ev = AuditEvent(
            phase=phase,
            ts=_utc_iso(),
            run_id=self.run_id,
            session_id=self.session.session_id,
            step=step,
            url=url,
            action_summary=action_summary,
            thoughts=thoughts,
            hitl_reason=hitl_reason,
            hitl_message=hitl_message,
            result=result,
            status=status,
            error=error,
            extra=_safe_extra(extra or {}),
        )
        self.audit_events.append(ev)
        if self.event_bus:
            await self.event_bus.publish(ev)
        if self.hooks and self.hooks.on_audit_event:
            await self.hooks.on_audit_event(ev)
        return ev

    async def _wait_paused(self) -> None:
        try:
            await self._paused.wait()
        finally:
            if not self._paused.is_set():
                self._paused.set()

    async def _hitl(
        self,
        checkpoint: dict[str, str],
        url: str,
        *,
        extra_meta: Optional[dict[str, Any]] = None,
    ) -> None:
        self._paused.clear()
        self.hitl_pauses += 1
        reason = checkpoint["reason"]
        message = checkpoint["message"]

        ev = await self._emit(
            AuditPhase.HITL,
            url=url or None,
            hitl_reason=reason,
            hitl_message=message,
            extra=_safe_extra(extra_meta or {}),
        )
        try:
            answer = await self.human_gate.wait_for_human(ev)
            if reason == "form_question" and answer:
                q = (extra_meta or {}).get("question") or message
                _inject_message_manager_task(
                    self.current_agent,
                    f"[User answer] Q: {q} | A: {answer!s} | "
                    "Fill the current form field. Do not emit HITL_QUESTION again for this key.",
                )
        finally:
            self._paused.set()

    async def _step_start_hook(self, agent: Any) -> None:
        current_url = ""
        step_num = getattr(agent.state, "step_number", 0) if hasattr(agent, "state") else 0
        try:
            if hasattr(agent, "browser_session"):
                state = await agent.browser_session.get_browser_state_summary()
                current_url = getattr(state, "url", "") or ""
        except Exception:
            pass

        await self._emit(
            AuditPhase.STEP_START,
            step=int(step_num) if step_num is not None else None,
            url=current_url or None,
        )

        cp = _matches_checkpoint(current_url, self.config.hitl_checkpoints)
        if cp:
            await self._hitl(cp, current_url)

        await self._wait_paused()
        if self._stopped:
            raise asyncio.CancelledError("Stopped by user")

        if self.hooks and self.hooks.on_step_start:
            await self.hooks.on_step_start(agent)

    async def _step_end_hook(self, agent: Any) -> None:
        step_num = getattr(agent.state, "step_number", 0) if hasattr(agent, "state") else 0
        action = None
        thoughts = None
        current_url = ""

        try:
            if hasattr(agent, "browser_session"):
                state = await agent.browser_session.get_browser_state_summary()
                current_url = getattr(state, "url", "") or ""
        except Exception:
            pass

        try:
            if hasattr(agent, "history"):
                history = agent.history
                actions = history.model_actions() if hasattr(history, "model_actions") else []
                thoughts_list = history.model_thoughts() if hasattr(history, "model_thoughts") else []
                if actions:
                    action = str(actions[-1])[:200]
                if thoughts_list:
                    thoughts = str(thoughts_list[-1])[:300]
        except Exception:
            pass

        await self._emit(
            AuditPhase.AFTER_STEP,
            step=step_num,
            url=current_url or None,
            action_summary=action or None,
            thoughts=thoughts or None,
        )

        cp = _matches_checkpoint(current_url, self.config.hitl_checkpoints)
        if cp:
            await self._hitl(cp, current_url)
            await self._wait_paused()
            if self._stopped:
                raise asyncio.CancelledError("Stopped by user")

        if self.hooks and self.hooks.on_step_end:
            await self.hooks.on_step_end(agent)

    async def _on_new_step_before_actions(
        self, browser_state_summary: Any, model_output: Any, n_steps: int
    ) -> None:
        if self._stopped:
            raise InterruptedError("Stopped by user")

        current_url = ""
        try:
            current_url = getattr(browser_state_summary, "url", "") or ""
        except Exception:
            pass

        step_num = n_steps + 1
        await self._emit(
            AuditPhase.BEFORE_ACTIONS,
            step=step_num,
            url=current_url or None,
            action_summary=_format_planned_actions(model_output)[:200] or None,
        )

        browser_msg = extract_browser_action_message(model_output)
        if browser_msg:
            await self._hitl(
                {"reason": "browser_action_required", "message": browser_msg},
                current_url,
                extra_meta={"source": "model_marker"},
            )
            await self._wait_paused()
            if self._stopped:
                raise InterruptedError("Stopped by user")

        form_q = extract_form_question(model_output)
        if form_q:
            await self._hitl(
                {"reason": "form_question", "message": form_q},
                current_url,
                extra_meta={"question": form_q},
            )
            await self._wait_paused()
            if self._stopped:
                raise InterruptedError("Stopped by user")

        if not self.config.step_approval:
            return
        if _actions_are_done_only(model_output):
            return

        plan_text = _format_planned_actions(model_output)
        agent_step = n_steps + 1
        message = (
            f"The agent plans to run the following next (agent step {agent_step}).\n"
            f"Page: {current_url or '(unknown)'}\n\n{plan_text}"
        )
        await self._hitl(
            {"reason": "step_approval", "message": message},
            current_url,
            extra_meta={"pending_actions": plan_text, "agent_step": agent_step},
        )
        await self._wait_paused()
        if self._stopped:
            raise InterruptedError("Stopped by user")

    async def run(self) -> TaskResult:
        t0 = time.perf_counter()
        status: str = "failed"
        output = ""
        err: Optional[str] = None
        steps = 0
        dur = 0.0
        run_end_extra: dict[str, Any] = {}

        await self._emit(
            AuditPhase.RUN_START,
            url=None,
            extra={
                "llm_model": self.config.llm_model,
                "max_steps_config": self.config.max_steps,
                "use_vision": self.config.use_vision,
                "use_judge": self.config.enable_judge,
                "step_approval": self.config.step_approval,
            },
        )

        try:
            from browser_use import Agent, Browser

            llm = build_llm(self.config)
            browser = Browser(cdp_url=self.session.cdp_url)
            afp = [p for p in (self.config.file_paths or []) if p]

            agent_kw: dict[str, Any] = dict(
                task=self.config.task,
                llm=llm,
                browser=browser,
                max_steps=self.config.max_steps,
                use_vision=self.config.use_vision,
                extend_system_message=get_system_message_patch(),
            )
            if self.config.agent_fs_dir:
                agent_kw["file_system_path"] = self.config.agent_fs_dir
            if afp:
                agent_kw["available_file_paths"] = afp
            if self.config.enable_judge:
                agent_kw["use_judge"] = True

            agent_kw["register_new_step_callback"] = self._on_new_step_before_actions
            self.current_agent = Agent(**agent_kw)

            raw_result = await self.current_agent.run(
                on_step_start=self._step_start_hook,
                on_step_end=self._step_end_hook,
            )

            output, status, run_end_extra = summarize_browser_use_result(
                raw_result,
                config_model=self.config.llm_model,
                config_max_steps=self.config.max_steps,
            )

            try:
                steps = int(getattr(self.current_agent.state, "step_number", 0) or 0)
                run_end_extra["step_count"] = steps
            except Exception:
                steps = 0

        except asyncio.CancelledError:
            status = "cancelled"
            output = "Cancelled"

        except InterruptedError as e:
            if self._stopped or "Stopped" in str(e):
                status = "cancelled"
                output = "Stopped by user"
            else:
                status = "failed"
                output = str(e)
                err = str(e)

        except Exception as e:
            msg = str(e)
            output = msg[:2000]
            err = msg
            if "max step" in msg.lower() or "max_steps" in msg.lower():
                status = "failed"
            logger.exception("run_task failed: %s", e)

        finally:
            self.current_agent = None
            dur = time.perf_counter() - t0
            await self._emit(
                AuditPhase.RUN_END,
                result=(output or "")[:500] if status == "completed" else None,
                status=status,
                error=err,
                extra=run_end_extra,
            )
            if self.event_bus:
                self.event_bus.unsubscribe_all()

        return TaskResult(
            status=status,  # type: ignore[arg-type]
            output=output,
            steps=steps,
            hitl_pauses=self.hitl_pauses,
            duration_s=dur,
            audit_events=list(self.audit_events),
        )


async def run_task(
    *,
    session: SteelSession,
    config: RunConfig,
    hooks: Optional[Hooks] = None,
    human_gate: Optional[HumanGate] = None,
    event_bus: Optional[EventBus] = None,
) -> TaskResult:
    """Run one task. Returns when the agent completes, fails, or stops."""
    if not session.session_id:
        raise SteelAgentError("SteelSession is not active (missing session_id)")

    run_id = str(uuid.uuid4())
    audit_events: list[AuditEvent] = []
    gate = human_gate or NullHumanGate()
    runner = _TaskRunner(
        session=session,
        config=config,
        run_id=run_id,
        hooks=hooks,
        human_gate=gate,
        event_bus=event_bus,
        audit_events=audit_events,
    )
    return await runner.run()


def run_task_sync(
    *,
    session: SteelSession,
    config: RunConfig,
    hooks: Optional[Hooks] = None,
    human_gate: Optional[HumanGate] = None,
    event_bus: Optional[EventBus] = None,
) -> TaskResult:
    """Sync wrapper: requires an already-active ``SteelSession`` (entered context)."""

    async def _inner() -> TaskResult:
        return await run_task(
            session=session,
            config=config,
            hooks=hooks,
            human_gate=human_gate,
            event_bus=event_bus,
        )

    return asyncio.run(_inner())
