#!/usr/bin/env python3
"""
Mistral provider.

Transport, response parsing and error classification live in
`xli.providers._http`. What stays here is Mistral-specific: the free tier is
aggressively rate limited, so this provider paces itself and backs off on 429
rather than hammering the endpoint.

One behaviour deliberately changed: the old version returned the string
"[ERROR: ...]" as if it were the model's reply. The agent then parsed that as
a normal response and kept going, so a rate limit looked like a confused
model instead of an outage. Failures now raise, which routes them to the
agent's provider_error path where the self-healing engine can retry them.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from xli.core.logger import StructuredLogger
from xli.providers._http import OpenAICompatibleProvider, ProviderError
from xli.providers.base import AbstractProvider

logger = StructuredLogger("xli.providers.mistral")


class MistralProvider(OpenAICompatibleProvider, AbstractProvider):
    """Mistral AI, with pacing for the free tier."""

    base_url = "https://api.mistral.ai/v1"
    default_model = "mistral-large-latest"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        min_delay: float = 4.0,
        max_retries: int = 3,
        sleeper: Any = None,
        **kwargs: Any,
    ):
        api_key = api_key or os.environ.get("MISTRAL_API_KEY")
        AbstractProvider.__init__(self, api_key=api_key, model=model)
        OpenAICompatibleProvider.__init__(self, api_key=api_key, model=model, **kwargs)

        self.min_delay = min_delay
        self.max_retries = max_retries
        self.last_request_time = 0.0
        self.consecutive_errors = 0
        self.total_requests = 0
        # Injectable so tests are not forced to wait out real backoff.
        self._sleep = sleeper or asyncio.sleep

    @classmethod
    def key_env_var(cls) -> str:
        return "MISTRAL_API_KEY"

    async def _pace(self) -> None:
        """Space requests out; the free tier rejects bursts."""
        now = time.monotonic()
        delay = self.min_delay + (self.consecutive_errors * 3)
        elapsed = now - self.last_request_time
        if self.last_request_time and elapsed < delay:
            await self._sleep(delay - elapsed)
        self.last_request_time = time.monotonic()
        self.total_requests += 1

    async def chat(
        self, messages: list[dict], temperature: float = 0.4, max_tokens: int = 4000
    ) -> str:
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            await self._pace()
            try:
                text = await OpenAICompatibleProvider.chat(
                    self, messages, temperature, max_tokens
                )
                self.consecutive_errors = 0
                return text
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt == self.max_retries - 1:
                    logger.log_error("mistral", f"giving up: {exc}")
                    raise
                self.consecutive_errors += 1
                wait = 8 * (2**attempt)
                logger.log_structured(
                    "WARN",
                    "mistral",
                    f"{exc.status} on attempt {attempt + 1}, retrying in {wait}s",
                )
                await self._sleep(wait)

        # Unreachable in practice, but a bare `return None` here would be worse.
        raise last_error or ProviderError("mistral: no attempts made")

    async def embed(self, text: str) -> list[float]:
        """Mistral embeddings. Returns [] if the account has no access."""
        try:
            data = await self._post(
                "/embeddings", {"model": "mistral-embed", "input": [text]}
            )
        except ProviderError as exc:
            logger.log_error("mistral", "embed failed", exc=exc)
            return []

        try:
            return list(data["data"][0]["embedding"])
        except (KeyError, IndexError, TypeError) as exc:
            logger.log_error("mistral", f"unexpected embedding shape: {exc}")
            return []
