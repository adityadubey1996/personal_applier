from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class HitlCheckpoint:
    """URL-regex checkpoint that triggers a human-in-the-loop pause."""

    patterns: list[str]
    reason: str
    message: str


def default_hitl_checkpoints() -> list[HitlCheckpoint]:
    """LinkedIn-focused defaults (same semantics as legacy workbench HITL_CHECKPOINTS)."""
    return [
        HitlCheckpoint(
            patterns=[
                r"linkedin\.com/login",
                r"linkedin\.com/authwall",
                r"linkedin\.com/checkpoint",
                r"linkedin\.com/uas/",
            ],
            reason="login_required",
            message=(
                "LinkedIn is asking you to log in. "
                "Complete the login in the browser window, then click Continue."
            ),
        ),
        HitlCheckpoint(
            patterns=[
                r"linkedin\.com/jobs/easy-apply/\d+/review",
                r"linkedin\.com/jobs/easy-apply/.+/review",
            ],
            reason="pre_submit",
            message=(
                "The application is ready to submit. "
                "Review the form in the browser window, then click Continue to submit."
            ),
        ),
    ]


@dataclass
class SteelConfig:
    steel_http_url: str = "http://127.0.0.1:3000"
    steel_ws_url: str = "ws://127.0.0.1:3000"
    api_key: Optional[str] = None
    block_ads: bool = True
    dimensions: dict[str, int] = field(default_factory=lambda: {"width": 1280, "height": 800})
    release_on_exit: bool = True


@dataclass
class RunConfig:
    task: str
    llm_api_key: str
    llm_model: str = "openai/gpt-oss-120b"
    llm_base_url: str = "https://api.groq.com"
    llm_provider: str = "groq"  # "groq" | "google"
    max_steps: int = 50
    use_vision: bool = False
    enable_judge: bool = False
    step_approval: bool = False
    hitl_checkpoints: list[HitlCheckpoint] = field(default_factory=default_hitl_checkpoints)
    file_paths: list[str] = field(default_factory=list)
    agent_fs_dir: Optional[str] = None
