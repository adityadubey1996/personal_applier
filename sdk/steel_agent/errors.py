"""Error taxonomy for Steel agent runs."""


class SteelAgentError(Exception):
    """Base class for SDK errors."""


class SteelSessionError(SteelAgentError):
    """Steel HTTP API failure or health check exhaustion."""


class CDPConnectionError(SteelAgentError):
    """browser-use / CDP WebSocket failure."""


class LLMError(SteelAgentError):
    """LLM API or structured-output failure."""


class MaxStepsError(SteelAgentError):
    """Agent exhausted max_steps without completing."""


class UserCancelError(SteelAgentError):
    """Run cancelled by caller or user stop."""
