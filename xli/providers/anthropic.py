#!/usr/bin/env python3
"""
Anthropic provider.

Was a stub raising NotImplementedError while remaining selectable, so
`XLI_PROVIDER=anthropic` blew up inside the agent loop.

Anthropic is not OpenAI-compatible, so it does not reuse
`xli.providers._http`'s request/response handling wholesale. It does reuse the
error classification and the injectable transport, because those are the parts
worth sharing: three differences matter here —

  * the endpoint is /v1/messages, not /chat/completions
  * auth is an `x-api-key` header plus `anthropic-version`, not a Bearer token
  * the system prompt is a top-level `system` field, not a message with
    role="system", and the reply is a list of content blocks, not a string
"""

from __future__ import annotations

import json
import os
from typing import Any

from xli.core.logger import StructuredLogger
from xli.providers._http import ProviderError, _classify
from xli.providers.base import AbstractProvider

logger = StructuredLogger("xli.providers.anthropic")

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(AbstractProvider):
    """Anthropic Messages API."""

    base_url = "https://api.anthropic.com"
    default_model = "claude-3-5-sonnet-latest"
    request_timeout = 120.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: Any = None,
    ):
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("AnthropicProvider requires an API key (set ANTHROPIC_API_KEY)")
        super().__init__(api_key=api_key, model=model or self.default_model)
        if base_url:
            self.base_url = base_url
        if timeout:
            self.request_timeout = timeout
        self._transport = transport

    def auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _client(self):
        import httpx

        kwargs: dict[str, Any] = {"timeout": self.request_timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
        """Move system messages into Anthropic's top-level `system` field.

        Anthropic rejects role="system" in the message list, so sending the
        agent's system prompt through unchanged would be a 400 on every call.
        """
        system_parts: list[str] = []
        rest: list[dict] = []
        for message in messages:
            if message.get("role") == "system":
                content = message.get("content", "")
                if content:
                    system_parts.append(str(content))
            else:
                rest.append(
                    {"role": message.get("role", "user"), "content": message.get("content", "")}
                )
        return "\n\n".join(system_parts), rest

    async def chat(
        self, messages: list[dict], temperature: float = 0.4, max_tokens: int = 4000
    ) -> str:
        import httpx

        system, rest = self._split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": rest,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system

        url = f"{self.base_url.rstrip('/')}/v1/messages"
        try:
            async with self._client() as client:
                response = await client.post(url, headers=self.auth_headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error: {exc}", retryable=True) from exc

        if response.status_code != 200:
            raise _classify(response.status_code, response.text)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid JSON from Anthropic: {exc}") from exc

        return self._extract_text(data)

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Join the text blocks of an Anthropic response.

        The content field is a list of typed blocks, and a tool_use turn may
        contain no text block at all — that is an empty reply, not an error.
        """
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(f"unexpected Anthropic response: {json.dumps(data)[:300]}")
        return "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

    async def stream(self, messages: list[dict], temperature: float = 0.4):
        """Yield text deltas from Anthropic's streaming endpoint."""
        import httpx

        system, rest = self._split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": rest,
            "temperature": temperature,
            "max_tokens": 4000,
            "stream": True,
        }
        if system:
            payload["system"] = system

        url = f"{self.base_url.rstrip('/')}/v1/messages"
        try:
            async with (
                self._client() as client,
                client.stream("POST", url, headers=self.auth_headers(), json=payload) as response,
            ):
                if response.status_code != 200:
                    body = await response.aread()
                    raise _classify(response.status_code, body.decode("utf-8", "replace"))

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:].strip())
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield text
        except httpx.TimeoutException as exc:
            raise ProviderError(f"stream timed out: {exc}", retryable=True) from exc

    async def embed(self, text: str) -> list[float]:
        # Anthropic does not offer an embeddings endpoint.
        logger.log_structured("WARN", "anthropic", "embeddings are not supported")
        return []
