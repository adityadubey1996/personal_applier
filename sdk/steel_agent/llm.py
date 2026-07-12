"""LLM factory (Groq / OpenAI-compatible) for browser-use."""
from __future__ import annotations

import logging
from typing import Any

from .config import RunConfig
from .errors import LLMError

logger = logging.getLogger(__name__)


def build_llm(config: RunConfig) -> Any:
    """Construct a browser-use / LangChain chat model from ``RunConfig``."""
    if not (config.llm_api_key or "").strip():
        raise LLMError("llm_api_key is required")

    if (getattr(config, "llm_provider", "groq") or "groq").lower() == "google":
        try:
            from browser_use import ChatGoogle

            llm = ChatGoogle(model=config.llm_model, api_key=config.llm_api_key)
            logger.info("LLM: ChatGoogle %s", config.llm_model)
            return llm
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"ChatGoogle init failed: {e}") from e

    try:
        from browser_use import ChatGroq

        llm = ChatGroq(
            model=config.llm_model,
            api_key=config.llm_api_key,
            base_url=config.llm_base_url.strip() or None,
        )
        logger.info("LLM: ChatGroq %s", config.llm_model)
        return llm
    except ImportError:
        pass
    except Exception as e:
        raise LLMError(f"ChatGroq init failed: {e}") from e

    try:
        from browser_use import ChatOpenAI

        root = config.llm_base_url.rstrip("/")
        openai_base = root if root.endswith("/openai/v1") else f"{root}/openai/v1"
        llm = ChatOpenAI(
            api_key=config.llm_api_key,
            base_url=openai_base,
            model=config.llm_model,
        )
        logger.info("LLM: browser_use ChatOpenAI %s", config.llm_model)
        return llm
    except ImportError:
        pass
    except Exception as e:
        raise LLMError(f"ChatOpenAI init failed: {e}") from e

    try:
        from langchain_openai import ChatOpenAI as LangChainChatOpenAI

        root = config.llm_base_url.rstrip("/")
        openai_base = root if root.endswith("/openai/v1") else f"{root}/openai/v1"
        llm = LangChainChatOpenAI(
            api_key=config.llm_api_key,
            base_url=openai_base,
            model=config.llm_model,
        )
        logger.info("LLM: langchain_openai ChatOpenAI %s", config.llm_model)
        return llm
    except ImportError as e:
        raise LLMError(
            "No LLM backend: install browser-use with Groq support or langchain-openai"
        ) from e
