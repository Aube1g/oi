#!/usr/bin/env python3
"""
Google Gemini provider.

Gemini does not speak the OpenAI shape, so it does not inherit the shared
client's request building. Two things the earlier implementation got wrong are
fixed here:

  * the API key went in the query string, which means it lands in proxy logs,
    server access logs and any traceback that prints the URL. It now goes in
    the `x-goog-api-key` header.
  * role="system" was rewritten to "user", silently demoting the agent's
    system prompt into the conversation. Gemini supports `systemInstruction`,
    so that is where it goes.
"""

from __future__ import annotations

import json
import os
from typing import Any

from xli.core.logger import StructuredLogger
from xli.providers._http import ProviderError, _classify
from xli.providers.base import AbstractProvider

logger = StructuredLogger("xli.providers.gemini")


class GeminiProvider(AbstractProvider):
    """Google Gemini generateContent."""

    base_url = "https://generativelanguage.googleapis.com/v1beta"
    default_model = "gemini-1.5-pro"
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
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GeminiProvider requires an API key (set GEMINI_API_KEY)")
        super().__init__(api_key=api_key, model=model or self.default_model)
        if base_url:
            self.base_url = base_url
        if timeout:
            self.request_timeout = timeout
        self._transport = transport

    def _client(self):
        import httpx

        kwargs: dict[str, Any] = {"timeout": self.request_timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _to_contents(messages: list[dict]) -> tuple[str, list[dict]]:
        """Convert OpenAI-style messages to Gemini contents.

        Returns (system_instruction, contents). Gemini has no "system" role in
        contents, so those messages are lifted into systemInstruction rather
        than being relabelled as user turns.
        """
        system_parts: list[str] = []
        contents: list[dict] = []
        for message in messages:
            role = message.get("role", "user")
            text = str(message.get("content", ""))
            if role == "system":
                if text:
                    system_parts.append(text)
                continue
            # Gemini uses "model" where OpenAI uses "assistant".
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": text}]})
        return "\n\n".join(system_parts), contents

    async def chat(
        self, messages: list[dict], temperature: float = 0.4, max_tokens: int = 4000
    ) -> str:
        import httpx

        system, contents = self._to_contents(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{self.base_url.rstrip('/')}/models/{self.model}:generateContent"
        try:
            async with self._client() as client:
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"network error: {exc}", retryable=True) from exc

        if response.status_code != 200:
            raise _classify(response.status_code, response.text)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"invalid JSON from Gemini: {exc}") from exc

        return self._extract_text(data)

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Join the parts of the first candidate.

        A blocked or empty generation has no parts, which is an empty reply
        rather than a crash — but a missing `candidates` key entirely means the
        response shape is not what we expect.
        """
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            reason = data.get("promptFeedback", {}).get("blockReason")
            if reason:
                return ""
            raise ProviderError(f"unexpected Gemini response: {json.dumps(data)[:300]}")

        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts if isinstance(part, dict))

    async def stream(self, messages: list[dict], temperature: float = 0.4):
        """Yield text deltas from streamGenerateContent."""
        import httpx

        system, contents = self._to_contents(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{self.base_url.rstrip('/')}/models/{self.model}:streamGenerateContent?alt=sse"
        try:
            async with (
                self._client() as client,
                client.stream(
                    "POST",
                    url,
                    headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
                    json=payload,
                ) as response,
            ):
                if response.status_code != 200:
                    body = await response.aread()
                    raise _classify(response.status_code, body.decode("utf-8", "replace"))

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        chunk = json.loads(line[6:].strip())
                    except json.JSONDecodeError:
                        continue
                    text = self._extract_text(chunk)
                    if text:
                        yield text
        except httpx.TimeoutException as exc:
            raise ProviderError(f"stream timed out: {exc}", retryable=True) from exc

    async def embed(self, text: str) -> list[float]:
        try:
            data = await self._embed_request(text)
            return list(data["embedding"]["values"])
        except Exception as exc:  # noqa: BLE001 - embeddings are optional
            logger.log_error("gemini", "embed failed", exc=exc)
            return []

    async def _embed_request(self, text: str) -> dict:

        url = f"{self.base_url.rstrip('/')}/models/text-embedding-004:embedContent"
        async with self._client() as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
                json={"content": {"parts": [{"text": text}]}},
            )
        if response.status_code != 200:
            raise _classify(response.status_code, response.text)
        return response.json()
