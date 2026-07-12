"""Detect markers and heuristics for browser-only human-in-the-loop pauses.

Ported from workbench ``hitl_markers`` (see SDK_PLAN).
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

_MAX_MESSAGE_LEN = 2000

_TEXT_FIELDS = ("thinking", "evaluation_previous_goal", "memory", "next_goal")

_DEFAULT_BROWSER_MSG = (
    "The agent needs you to enter information or sign in using the browser on the right. "
    "When you are finished, click Continue."
)

_AUTH_OR_SENSITIVE_RE = re.compile(
    r"(?i)(sign\s*[- ]?in|log\s*[- ]?in|password|credential|authenticat|"
    r"\bgmail\b|\boutlook\b|otp\b|2fa\b|mfa\b|verify\s+it'?s\s+you|"
    r"email\s+or\s+phone|obtain\s+the\s+user|need\s+the\s+user|"
    r"user'?s\s+(email|phone|password)|enter\s+your\s+(email|phone|password))",
)

_ASKS_USER_FOR_SECRET_RE = re.compile(
    r"(?i)(obtain|need|ask|require|get).{0,48}(user|your).{0,40}"
    r"(email|phone|password|credential|sign\s*[- ]?in|log\s*[- ]?in)",
)


def collect_model_text_blob(model_output: Any) -> str:
    parts: list[str] = []
    for attr in _TEXT_FIELDS:
        try:
            v = getattr(model_output, attr, None)
            if v is not None and str(v).strip():
                parts.append(str(v))
        except Exception:
            continue
    return "\n".join(parts)


def _extract_lines_after_marker(blob: str, marker: re.Pattern[str]) -> Optional[str]:
    m = marker.search(blob)
    if not m:
        return None
    rest = blob[m.end() :]
    lines: list[str] = []
    for line in rest.splitlines():
        s = line.strip()
        if not s:
            if lines:
                break
            continue
        if lines and re.match(r"^[A-Z][A-Z0-9_]{2,}:\s*", s):
            break
        lines.append(s)
    if not lines:
        return None
    text = "\n".join(lines) if len(lines) > 1 else lines[0]
    text = text.strip()
    if not text:
        return None
    return text[:_MAX_MESSAGE_LEN]


def _planned_action_includes_text_into_field(model_output: Any) -> bool:
    """True if the model plans to type into a DOM field (input_text-style)."""
    try:
        for a in model_output.action or []:
            d = a.model_dump(exclude_none=True)
            for key, val in d.items():
                if key in ("done", "wait", "screenshot", "scroll"):
                    continue
                if isinstance(val, dict) and "text" in val and "index" in val:
                    return True
    except Exception:
        pass
    return False


def extract_browser_action_message(model_output: Any) -> Optional[str]:
    """Message for a browser-only HITL pause, or None if no trigger."""
    blob = collect_model_text_blob(model_output)
    if not blob:
        return None

    explicit = _extract_lines_after_marker(blob, re.compile(r"COMPLETE_IN_BROWSER:\s*", re.IGNORECASE))
    if explicit:
        return explicit

    legacy = _extract_lines_after_marker(blob, re.compile(r"MISSING_USER_INPUT:\s*", re.IGNORECASE))
    if legacy:
        return legacy

    if _AUTH_OR_SENSITIVE_RE.search(blob) or _ASKS_USER_FOR_SECRET_RE.search(blob):
        if _planned_action_includes_text_into_field(model_output):
            return _DEFAULT_BROWSER_MSG
        if _ASKS_USER_FOR_SECRET_RE.search(blob):
            return _DEFAULT_BROWSER_MSG

    return None


_FORM_QUESTION_RE = re.compile(r"HITL_QUESTION:\s*", re.IGNORECASE)

_APP_MARKER_RE = re.compile(
    r"(APPLICATION_START|APPLICATION_SUBMITTED|APPLICATION_FAILED|APPLICATION_SKIPPED):\s*(.+)",
    re.IGNORECASE | re.MULTILINE,
)


def _clean_form_question(raw: str) -> Optional[str]:
    """Keep only the question itself.

    The agent often appends reasoning ("Success: ...", memory lines) after the
    question on following lines; _extract_lines_after_marker captures them
    because they are not ALL-CAPS markers. Take the first line and, if it
    contains a '?', cut at the first '?' (inclusive).
    """
    if not raw:
        return None
    first = raw.splitlines()[0].strip()
    if not first:
        return None
    qpos = first.find("?")
    if qpos != -1:
        return first[: qpos + 1].strip()
    # No '?': drop a trailing reasoning clause if present.
    first = re.split(r"\s+(?:Success|Note|Memory|Eval)\s*:", first, maxsplit=1)[0].strip()
    return first or None


def extract_form_question(model_output: Any) -> Optional[str]:
    """Detect HITL_QUESTION: <text> — agent needs a user-supplied form answer."""
    blob = collect_model_text_blob(model_output)
    if not blob:
        return None
    raw = _extract_lines_after_marker(blob, _FORM_QUESTION_RE)
    return _clean_form_question(raw) if raw else None


def extract_application_event(model_output: Any) -> Optional[Tuple[str, str]]:
    """Return (EVENT_TYPE, payload) from APPLICATION_* markers, or None."""
    blob = collect_model_text_blob(model_output)
    if not blob:
        return None
    m = _APP_MARKER_RE.search(blob)
    if not m:
        return None
    return m.group(1).upper(), m.group(2).strip()


def extract_missing_user_input_question(model_output: Any) -> Optional[str]:
    """Backward-compatible alias for ``extract_browser_action_message``."""
    return extract_browser_action_message(model_output)
