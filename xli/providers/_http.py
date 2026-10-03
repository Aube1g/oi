#!/usr/bin/env python3
"""
Shared HTTP transport for OpenAI-compatible providers.

Why this file exists: `mistral.py` reached for `aiohttp`, then fell back to
`requests` — and neither is a declared dependency. `httpx` is declared and
installed, but nothing used it. The result was that the *default* provider
raised ModuleNotFoundError on every call, so `xli run` could never work.

There is now one HTTP path, on httpx, shared by every OpenAI-compatible
provider. Mistral, OpenAI and OpenRouter differ only in base URL, auth header
and default model, so those are the only things a subclass sets.
"""

from __future__ import annotations

import json
from typing import Any

from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.providers.http")


#: Raised for anything the caller should not retry (bad key, bad request).
class ProviderError(RuntimeError):
    """A provider-side failure, classified so callers can react."""

    def __init__(self, message: str, *, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _classify(status: int, body: str) -> ProviderError:
    """Turn an HTTP status into an error the retry layer can reason about."""
    snippet = body[:300].replace("\n", " ")
    retryable = status == 429 or status >= 500
    label = {
        400: "bad request",
        401: "unauthorized — check the API key",
        403: "forbidden",
        404: "not found — is the model name correct?",
        422: "unprocessable request",
        429: "rate limited",
    }.get(status, f"HTTP {status}")
    return ProviderError(f"{label}: {snippet}", status=status, retryable=retryable)


class OpenAICompatibleProvider:
    """Minimal client for the OpenAI chat-completions shape.

    Subclasses set `base_url`, `default_model` and optionally override
    `auth_headers()`. Nothing here caches a session across calls: providers are
    used for a handful of requests per run, and a shared AsyncClient would have
    to be closed by someone.
    """

    base_url: str = ""
    default_model: str = ""
    request_timeout: float = 120.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: Any = None,
    ):
        if not api_key:
            raise ValueError(
                f"{type(self).__name__} requires an API key (set {self.key_env_var()})"
            )
        self.api_key = api_key
        self.model = model or self.default_model
        if base_url:
            self.base_url = base_url
        if timeout:
            self.request_timeout = timeout
        # Injectable so tests never touch the network.
        self._transport = transport

    @classmethod
    def key_env_var(cls) -> str:
        """The environment variable this provider reads its key from."""
        name = cls.__name__.replace("Provider", "").upper()
        return f"{name}_API_KEY"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _client(self):
        import httpx

        kwargs: dict[str, Any] = {"timeout": self.request_timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    # ------------------------------------------------------------------- chat
    async def chat(
        self, messages: list[dict], temperature: float = 0.4, max_tokens: int = 4000
    ) -> str:
        """One chat completion, returning the assistant text."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = await self._post("/chat/completions", payload)
        return self._extract_text(data)

    async def _post(self, path: str, payload: dict) -> dict:
        import httpx

        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            async with self._client() as client:
                response = await client.post(url, headers=self.auth_headers(), json=payload)
        except httpx.TimeoutException as exc:
            # Timeouts are transient by definition.
            raise ProviderError(
                f"timed out after {self.request_timeout}s: {exc}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error: {exc}", retryable=True) from exc

        if response.status_code != 200:
            raise _classify(response.status_code, response.text)

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"provider returned invalid JSON: {exc}") from exc

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Pull the assistant text out of a chat-completions response.

        Errors here are reported with the shape that was actually received,
        because a bare KeyError tells you nothing about which provider changed.
        """
        try:
            choices = data["choices"]
            if not choices:
                raise KeyError("choices is empty")
            message = choices[0]["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"unexpected response shape ({exc}): {json.dumps(data)[:300]}"
            ) from exc

        if content is None:
            # Tool-call-only turns legitimately have no text content.
            return ""
        if isinstance(content, list):
            # Some providers return a list of typed parts.
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)

    # ----------------------------------------------------------------- stream
    async def stream(self, messages: list[dict], temperature: float = 0.4):
        """Yield text deltas from a streaming completion."""
        import httpx

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        url = f"{self.base_url.rstrip('/')}/chat/completions"
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
                    chunk = line[6:].strip()
                    if chunk == "[DONE]":
                        return
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    text = self._extract_text(data)
                    if text:
                        yield text
        except httpx.TimeoutException as exc:
            raise ProviderError(f"stream timed out: {exc}", retryable=True) from exc
