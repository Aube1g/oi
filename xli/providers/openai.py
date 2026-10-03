#!/usr/bin/env python3
"""
OpenAI provider.

Was a stub that raised NotImplementedError on every method while still being
selectable through the config, so `XLI_PROVIDER=openai` failed deep inside the
agent loop instead of at startup. It is now a real client: OpenAI is the
reference implementation of the chat-completions shape that
`xli.providers._http` speaks.
"""

from __future__ import annotations

import os

from xli.core.logger import StructuredLogger
from xli.providers._http import OpenAICompatibleProvider
from xli.providers.base import AbstractProvider

logger = StructuredLogger("xli.providers.openai")


class OpenAIProvider(OpenAICompatibleProvider, AbstractProvider):
    """OpenAI chat completions."""

    base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"

    def __init__(self, api_key: str | None = None, model: str | None = None, **kwargs):
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        AbstractProvider.__init__(self, api_key=api_key, model=model)
        OpenAICompatibleProvider.__init__(self, api_key=api_key, model=model, **kwargs)

    @classmethod
    def key_env_var(cls) -> str:
        return "OPENAI_API_KEY"

    async def embed(self, text: str) -> list[float]:
        try:
            data = await self._post(
                "/embeddings", {"model": "text-embedding-3-small", "input": [text]}
            )
            return list(data["data"][0]["embedding"])
        except Exception as exc:  # noqa: BLE001 - embeddings are optional
            logger.log_error("openai", "embed failed", exc=exc)
            return []
