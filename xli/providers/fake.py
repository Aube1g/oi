#!/usr/bin/env python3
"""
XLI Fake Provider — deterministic, offline stand-in for AbstractProvider.

Use in tests via the `fake_provider` fixture in tests/conftest.py so the test
suite doesn't need real MISTRAL_API_KEY/OPENAI_API_KEY/etc. Responses are
scripted: pass a list of strings (or a callable) and they're returned in
order on each .chat() call.
"""

from typing import Any
from collections.abc import Callable

from xli.providers.base import AbstractProvider


class FakeProvider(AbstractProvider):
    """In-memory provider. No network calls, no API key required."""

    def __init__(
        self,
        responses: list[str] | None = None,
        response_fn: Callable[[list[dict]], str] | None = None,
        model: str = "fake-model",
        api_key: str | None = None,
        **kwargs,
    ):
        # `api_key` is accepted and ignored so the provider factory can build
        # this through exactly the same path as a real one.
        del kwargs
        super().__init__(api_key=api_key or "fake-key", model=model)
        self._responses = list(responses or ["OK"])
        self._response_fn = response_fn
        self.calls: list[list[dict]] = []

    def _next_response(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        if self._response_fn is not None:
            return self._response_fn(messages)
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)

    async def chat(self, messages: list[dict], temperature: float = 0.4,
                    max_tokens: int = 4000) -> str:
        return self._next_response(messages)

    async def stream(self, messages: list[dict], temperature: float = 0.4) -> Any:
        text = self._next_response(messages)
        async def _gen():
            yield text
        return _gen()

    async def embed(self, text: str) -> list[float]:
        # Deterministic fake embedding, fine for tests that just check shape/type.
        return [float(len(text) % 7)] * 8
