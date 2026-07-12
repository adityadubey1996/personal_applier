"""Steel HTTP client, URL helpers, and ``SteelSession`` lifecycle."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

import httpx

from .config import SteelConfig
from .errors import SteelSessionError

logger = logging.getLogger(__name__)


def resolve_cdp_websocket_url(session: Dict[str, Any], steel_ws_base: str) -> str:
    """
    Build the CDP WebSocket URL for browser-use.

    Steel often returns websocketUrl with host 0.0.0.0 (unusable from the host) or a
    bare ws root without sessionId; always prefer ``{STEEL_WS_URL}/ws?sessionId=…``.
    """
    session_id = session.get("id") or ""
    base = steel_ws_base.rstrip("/")
    canonical = f"{base}/ws?sessionId={session_id}"
    raw = (session.get("websocketUrl") or "").strip()
    if not raw:
        logger.info("Steel session has no websocketUrl; using %s", canonical)
        return canonical
    if "0.0.0.0" in raw:
        logger.info("Steel websocketUrl used 0.0.0.0; using host CDP URL %s", canonical)
        return canonical
    if session_id and "sessionid=" not in raw.lower():
        logger.info("Steel websocketUrl missing sessionId; using %s", canonical)
        return canonical
    return raw


def rewrite_viewer_url_for_host(viewer_url: Optional[str], steel_http_base: str) -> str:
    """
    Steel returns sessionViewerUrl with host 0.0.0.0; browsers on the host cannot load that.
    Rewrite using the hostname/port from STEEL_BASE_URL.
    """
    if viewer_url is None:
        return ""
    u = str(viewer_url).strip()
    if not u or u.lower() in ("none", "null"):
        return ""
    base = urlparse(steel_http_base)
    host = base.hostname or "127.0.0.1"
    parsed = urlparse(u)
    if parsed.hostname in ("0.0.0.0", None):
        port = parsed.port or base.port
        if port:
            netloc = f"{host}:{port}"
        else:
            netloc = host
        parsed = parsed._replace(netloc=netloc)
        out = urlunparse(parsed)
        logger.info("Rewrote Steel viewer URL for host browser: %s", out)
        return out
    return u


def normalize_steel_session_for_host(session: Dict[str, Any], steel_http_base: str) -> Dict[str, Any]:
    """Return a copy of session with sessionViewerUrl usable from the user's machine."""
    s = dict(session)
    s["sessionViewerUrl"] = rewrite_viewer_url_for_host(
        s.get("sessionViewerUrl"), steel_http_base
    )
    return s


class SteelClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0)

    async def create_session(
        self,
        block_ads: bool = True,
        dimensions: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Create a new Steel browser session."""
        payload: dict[str, Any] = {"blockAds": block_ads}
        # Shrink the DOM/screenshots the LLM ingests (safe while use_vision is off).
        payload.setdefault("optimizeBandwidth", {"blockImages": True, "blockMedia": True})
        if dimensions:
            payload["dimensions"] = dimensions
        else:
            payload["dimensions"] = {"width": 1280, "height": 800}

        url = f"{self.base_url}/v1/sessions"
        logger.info("Connecting to Steel: POST %s", url)
        response = await self.client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        sid = data.get("id", "?")
        logger.info(
            "Steel session created: id=%s viewer=%s",
            sid,
            data.get("sessionViewerUrl"),
        )
        return data

    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """Get session details."""
        url = f"{self.base_url}/v1/sessions/{session_id}"
        logger.debug("Steel: GET %s", url)
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()

    async def release_session(self, session_id: str) -> bool:
        """Release/delete a session."""
        try:
            url = f"{self.base_url}/v1/sessions/{session_id}"
            logger.info("Steel: DELETE %s", url)
            response = await self.client.delete(url)
            return response.status_code in (200, 204, 404)
        except Exception as e:
            logger.warning("Error releasing session: %s", e)
            return False

    async def list_sessions(self) -> list:
        """List all active sessions."""
        response = await self.client.get(f"{self.base_url}/v1/sessions")
        response.raise_for_status()
        return response.json()

    async def health_check(self) -> bool:
        """Check if Steel API is healthy."""
        try:
            response = await self.client.get(f"{self.base_url}/v1/health")
            ok = response.status_code == 200
            if ok:
                logger.debug("Steel health OK: %s", self.base_url)
            else:
                logger.warning("Steel health non-200: %s", response.status_code)
            return ok
        except Exception as e:
            logger.warning("Steel health check failed: %s", e)
            return False

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()


async def _health_check_with_backoff(client: SteelClient) -> None:
    delays = (1.0, 2.0, 4.0)
    for attempt, delay in enumerate(delays, start=1):
        if await client.health_check():
            return
        if attempt < len(delays):
            await asyncio.sleep(delay)
    raise SteelSessionError("Steel health check failed after retries")


_SYSTEM_MSG_PATCH = (
    "Structured actions: use the action key `search` with {query, engine?} for "
    "web search (duckduckgo/google/bing). Use `search_page` only for in-page "
    "text grep with {pattern, regex?, case_sensitive?, ...}; never put "
    "`engine` or `query` (web search) inside `search_page`."
)


class SteelSession:
    def __init__(self, config: SteelConfig) -> None:
        self._config = config
        self._client: Optional[SteelClient] = None
        self._raw_session: Dict[str, Any] = {}
        self._session_id: str = ""
        self._cdp_url: str = ""
        self._viewer_url: str = ""

    @property
    def cdp_url(self) -> str:
        return self._cdp_url

    @property
    def viewer_url(self) -> str:
        return self._viewer_url

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def raw_session(self) -> Dict[str, Any]:
        return dict(self._raw_session)

    async def __aenter__(self) -> "SteelSession":
        self._client = SteelClient(self._config.steel_http_url.rstrip("/"))
        try:
            data = await self._client.create_session(
                block_ads=self._config.block_ads,
                dimensions=self._config.dimensions,
            )
            sid = str(data.get("id") or "")
            try:
                await _health_check_with_backoff(self._client)
            except SteelSessionError:
                if sid:
                    try:
                        await self._client.release_session(sid)
                    except Exception:
                        pass
                raise
            self._raw_session = normalize_steel_session_for_host(data, self._config.steel_http_url)
            self._session_id = str(self._raw_session.get("id") or "")
            self._cdp_url = resolve_cdp_websocket_url(self._raw_session, self._config.steel_ws_url)
            self._viewer_url = str(self._raw_session.get("sessionViewerUrl") or "")
            return self
        except SteelSessionError:
            await self._safe_close_client()
            raise
        except Exception as e:
            await self._safe_close_client()
            raise SteelSessionError(str(e)) from e

    async def _safe_close_client(self) -> None:
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._config.release_on_exit:
            try:
                await self.release()
            except Exception as e:
                logger.warning("SteelSession.__aexit__ release failed: %s", e)

    @classmethod
    async def create(cls, config: SteelConfig) -> "SteelSession":
        s = cls(config)
        await s.__aenter__()
        return s

    async def release(self) -> None:
        sid = self._session_id
        client = self._client
        self._session_id = ""
        self._cdp_url = ""
        self._viewer_url = ""
        self._raw_session = {}
        if client and sid:
            try:
                await client.release_session(sid)
            except Exception as e:
                logger.warning("Steel release_session failed for %s: %s", sid, e)
        if client:
            try:
                await client.close()
            except Exception as e:
                logger.warning("Steel client close failed: %s", e)
        self._client = None


def get_system_message_patch() -> str:
    """browser-use ``extend_system_message`` fragment shared with workbench."""
    return _SYSTEM_MSG_PATCH
