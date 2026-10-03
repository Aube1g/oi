#!/usr/bin/env python3
"""
Ollama provider — local models over HTTP.

Ollama needs no API key, so this is the one provider that works with nothing
configured beyond a running `ollama serve`. That makes it the sensible default
for anyone trying xli without an account.

Its API is not OpenAI-shaped either: no auth header, `/api/chat`, and the reply
sits at `message.content` rather than `choices[0].message.content`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from xli.core.logger import StructuredLogger
from xli.providers._http import ProviderError, _classify
from xli.providers.base import AbstractProvider

logger = StructuredLogger("xli.providers.ollama")


class OllamaProvider(AbstractProvider):
    """Local Ollama /api/chat."""

    base_url = "http://localhost:11434"
    default_model = "llama3.1"
    request_timeout = 300.0  # local models on CPU can be slow

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: Any = None,
    ):
        # No key required. Accept one so the factory can pass it uniformly,
        # and so a remote Ollama behind a proxy can still send it.
        super().__init__(
            api_key=api_key or os.environ.get("OLLAMA_API_KEY") or "",
            model=model or os.environ.get("OLLAMA_MODEL") or self.default_model,
        )
        self.base_url = base_url or os.environ.get("OLLAMA_HOST") or self.base_url
        if timeout:
            self.request_timeout = timeout
        self._transport = transport

    def _client(self):
        import httpx

        kwargs: dict[str, Any] = {"timeout": self.request_timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def chat(
        self, messages: list[dict], temperature: float = 0.4, max_tokens: int = 4000
    ) -> str:
        import httpx

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        url = f"{self.base_url.rstrip('/')}/api/chat"
        try:
            async with self._client() as client:
                response = await client.post(url, headers=self._headers(), json=payload)
        except httpx.ConnectError as exc:
            # The most common Ollama failure is simply that it is not running.
            raise ProviderError(
                f"cannot reach Ollama at {self.base_url} — is `ollama serve` running? ({exc})",
                retryable=False,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"timed out after {self.request_timeout}s: {exc}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error: {exc}", retryable=True) from exc

        if response.status_code != 200:
            raise _classify(response.status_code, response.text)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid JSON from Ollama: {exc}") from exc

        return self._extract_text(data)

    @staticmethod
    def _extract_text(data: dict) -> str:
        message = data.get("message")
        if not isinstance(message, dict):
            raise ProviderError(f"unexpected Ollama response: {json.dumps(data)[:300]}")
        return str(message.get("content", "") or "")

    async def stream(self, messages: list[dict], temperature: float = 0.4):
        """Yield deltas from Ollama's newline-delimited JSON stream."""
        import httpx

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        url = f"{self.base_url.rstrip('/')}/api/chat"
        try:
            async with (
                self._client() as client,
                client.stream("POST", url, headers=self._headers(), json=payload) as response,
            ):
                if response.status_code != 200:
                    body = await response.aread()
                    raise _classify(response.status_code, body.decode("utf-8", "replace"))

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("done"):
                        return
                    text = (chunk.get("message") or {}).get("content", "")
                    if text:
                        yield text
        except httpx.ConnectError as exc:
            raise ProviderError(
                f"cannot reach Ollama at {self.base_url} — is `ollama serve` running?",
                retryable=False,
            ) from exc

    async def embed(self, text: str) -> list[float]:

        url = f"{self.base_url.rstrip('/')}/api/embeddings"
        try:
            async with self._client() as client:
                response = await client.post(
                    url,
                    headers=self._headers(),
                    json={"model": self.model, "prompt": text},
                )
            if response.status_code != 200:
                raise _classify(response.status_code, response.text)
            return list(response.json().get("embedding", []))
        except Exception as exc:  # noqa: BLE001 - embeddings are optional
            logger.log_error("ollama", "embed failed", exc=exc)
            return []
