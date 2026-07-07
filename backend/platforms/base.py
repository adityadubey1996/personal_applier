from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PlatformAdapter(Protocol):
    """Protocol for platform-specific job application adapters."""

    name: str
    typeahead_selector: str
    field_hints: dict[str, str]

    async def entry(self, agent) -> None:
        """Open the apply flow for the platform."""
        ...

    async def is_submitted(self, agent) -> bool:
        """Detect the real 'sent' signal for the platform."""
        ...

    async def discover_jobs(self, ctx, n: int) -> list[str]:
        """Search or scrape job IDs from the platform."""
        ...
