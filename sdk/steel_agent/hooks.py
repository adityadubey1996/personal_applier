from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from .events import AuditEvent


@dataclass
class Hooks:
    on_audit_event: Optional[Callable[["AuditEvent"], Awaitable[None]]] = None
    on_step_start: Optional[Callable[[Any], Awaitable[None]]] = None
    on_step_end: Optional[Callable[[Any], Awaitable[None]]] = None
