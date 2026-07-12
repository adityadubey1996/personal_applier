"""Human-in-the-loop gate protocol and implementations."""
from __future__ import annotations

import asyncio
import sys
from typing import Optional, Protocol

from .events import AuditEvent


class HumanGate(Protocol):
    async def wait_for_human(self, event: AuditEvent) -> Optional[str]:
        """Block until human acts. Return typed answer for form_question; None otherwise."""


class NullHumanGate:
    """Tests / fully-automated runs: immediately continue."""

    async def wait_for_human(self, event: AuditEvent) -> Optional[str]:
        return None


class StdinHumanGate:
    """CLI: print HITL message to stderr, read one line from stdin."""

    async def wait_for_human(self, event: AuditEvent) -> Optional[str]:
        msg = (event.hitl_message or "Waiting for human…").strip()
        print(msg, file=sys.stderr)
        print("Press Enter to continue (or type an answer for form_question): ", file=sys.stderr, end="")
        loop = asyncio.get_event_loop()
        line = await loop.run_in_executor(None, sys.stdin.readline)
        text = (line or "").rstrip("\n\r")
        if event.hitl_reason == "form_question" and text:
            return text
        return None


class AsyncioHumanGate:
    """Web backends: call ``signal_done`` from an HTTP handler to resume."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._answer: Optional[str] = None

    async def signal_done(self, answer: Optional[str] = None) -> None:
        self._answer = answer
        self._event.set()

    async def wait_for_human(self, event: AuditEvent) -> Optional[str]:
        self._event.clear()
        self._answer = None
        try:
            await self._event.wait()
            return self._answer
        finally:
            self._event.clear()
