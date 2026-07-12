from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_SENSITIVE_KEY = re.compile(r"(password|token|key|secret|credential)", re.I)


def _safe_extra(d: dict[str, Any]) -> dict[str, Any]:
    """Strip keys that look like secrets from extension metadata."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if _SENSITIVE_KEY.search(k):
            continue
        out[k] = v
    return out


class AuditPhase(str, Enum):
    RUN_START = "run_start"
    STEP_START = "step_start"
    BEFORE_ACTIONS = "before_actions"
    AFTER_STEP = "after_step"
    HITL = "hitl"
    RUN_END = "run_end"


@dataclass
class AuditEvent:
    phase: AuditPhase
    ts: str
    run_id: str
    session_id: str
    step: Optional[int] = None
    url: Optional[str] = None
    action_summary: Optional[str] = None
    thoughts: Optional[str] = None
    hitl_reason: Optional[str] = None
    hitl_message: Optional[str] = None
    result: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "phase": self.phase.value,
            "ts": self.ts,
            "run_id": self.run_id,
            "session_id": self.session_id,
        }
        if self.step is not None:
            d["step"] = self.step
        if self.url is not None:
            d["url"] = self.url
        if self.action_summary is not None:
            d["action_summary"] = self.action_summary
        if self.thoughts is not None:
            d["thoughts"] = self.thoughts
        if self.hitl_reason is not None:
            d["hitl_reason"] = self.hitl_reason
        if self.hitl_message is not None:
            d["hitl_message"] = self.hitl_message
        if self.result is not None:
            d["result"] = self.result
        if self.status is not None:
            d["status"] = self.status
        if self.error is not None:
            d["error"] = self.error
        if self.extra:
            d["extra"] = self.extra
        return d


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventBus:
    """Fan-out audit events to subscribers via unbounded asyncio queues."""

    def __init__(self, lag_warn_at: int = 100) -> None:
        self._lag_warn_at = lag_warn_at
        self._queues: list[asyncio.Queue[AuditEvent | None]] = []

    async def publish(self, event: AuditEvent) -> None:
        for q in list(self._queues):
            if q.qsize() > self._lag_warn_at:
                logger.warning(
                    "EventBus subscriber lag: queue size %s (threshold %s)",
                    q.qsize(),
                    self._lag_warn_at,
                )
            q.put_nowait(event)

    async def subscribe(self) -> AsyncGenerator[AuditEvent, None]:
        q: asyncio.Queue[AuditEvent | None] = asyncio.Queue()
        self._queues.append(q)
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield item
        finally:
            if q in self._queues:
                self._queues.remove(q)

    def unsubscribe_all(self) -> None:
        queues = list(self._queues)
        self._queues.clear()
        for q in queues:
            q.put_nowait(None)


async def _maybe_call(fn: Optional[Callable[..., Awaitable[None]]], *args: Any) -> None:
    if fn is None:
        return
    await fn(*args)
