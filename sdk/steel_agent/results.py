from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .events import AuditEvent


@dataclass
class TaskResult:
    status: Literal["completed", "cancelled", "failed"]
    output: str
    steps: int
    hitl_pauses: int
    duration_s: float
    audit_events: list["AuditEvent"] = field(default_factory=list)
