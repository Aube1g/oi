#!/usr/bin/env python3
"""
OpenRouter provider.

OpenRouter is OpenAI-compatible, so this is the shared client with a different
base URL and the extra headers OpenRouter asks for to identify the calling app.
"""

from __future__ import annotations

import os

from xli.core.logger import StructuredLogger
from xli.providers._http import OpenAICompatibleProvider
from xli.providers.base import AbstractProvider

logger = StructuredLogger("xli.providers.openrouter")


class OpenRouterProvider(OpenAICompatibleProvider, AbstractProvider):
    """OpenRouter chat completions."""

    base_url = "https://openrouter.ai/api/v1"
    default_model = "openai/gpt-4o-mini"

    def __init__(self, api_key: str | None = None, model: str | None = None, **kwargs):
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        AbstractProvider.__init__(self, api_key=api_key, model=model)
        OpenAICompatibleProvider.__init__(self, api_key=api_key, model=model, **kwargs)

    @classmethod
    def key_env_var(cls) -> str:
        return "OPENROUTER_API_KEY"

    def auth_headers(self) -> dict[str, str]:
        headers = super().auth_headers()
        # Optional attribution; harmless if the site is unset.
        headers.setdefault("HTTP-Referer", "https://github.com/ty4chka/xli")
        headers.setdefault("X-Title", "xli")
        return headers

    async def embed(self, text: str) -> list[float]:
        # OpenRouter routes chat models; embeddings go to the underlying
        # provider instead, so there is nothing sensible to return here.
        logger.log_structured("WARN", "openrouter", "embeddings are not supported")
        return []
