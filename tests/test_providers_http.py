#!/usr/bin/env python3
"""Tests for the provider HTTP layer.

Every test uses httpx.MockTransport, so nothing here touches the network. That
matters because the previous provider code could not be tested at all: it
imported aiohttp, then fell back to requests, and neither is installed — so the
default provider raised ModuleNotFoundError on every call.
"""

import asyncio
import json

import httpx
import pytest

from xli.providers._http import ProviderError
from xli.providers.anthropic import AnthropicProvider
from xli.providers.mistral import MistralProvider
from xli.providers.openai import OpenAIProvider
from xli.providers.openrouter import OpenRouterProvider


# --------------------------------------------------------------------- helpers
def completion(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def transport_for(handler):
    return httpx.MockTransport(handler)


def ok_handler(text="hi there", capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(200, json=completion(text))

    return handler


# ------------------------------------------------------- no undeclared imports
class TestDependencies:
    def test_no_provider_imports_aiohttp_or_requests(self):
        """The bug that made the default provider unusable."""
        import inspect

        import xli.providers._http as http_mod
        import xli.providers.anthropic as anth
        import xli.providers.gemini as gem
        import xli.providers.mistral as mistral
        import xli.providers.ollama as oll
        import xli.providers.openai as oai
        import xli.providers.openrouter as ort

        def _imports_of(module):
            import ast

            tree = ast.parse(inspect.getsource(module))
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module.split(".")[0])
            return found

        # Defined at class scope so the assertions below can reuse it.
        globals()["_imports_of"] = _imports_of

        for module in (http_mod, mistral, oai, ort, anth, gem, oll):
            imported = _imports_of(module)
            # A docstring may mention these when explaining why they are gone;
            # only an actual import is the bug.
            assert "aiohttp" not in imported, f"{module.__name__} imports aiohttp"
            assert "requests" not in imported, f"{module.__name__} imports requests"

        # Only the transport layer talks HTTP directly. Providers that build on
        # OpenAICompatibleProvider get httpx through it, so requiring the import
        # in each of them would be wrong.
        assert "httpx" in _imports_of(http_mod), "the transport layer must use httpx"
        for module in (anth, gem, oll):
            # These three speak a non-OpenAI shape and do their own requests.
            assert "httpx" in _imports_of(module), f"{module.__name__} must use httpx"

    def test_httpx_is_the_declared_dependency(self):
        import tomllib
        from pathlib import Path

        deps = tomllib.loads(
            Path("pyproject.toml").read_text(encoding="utf-8")
        )["project"]["dependencies"]
        assert any(d.startswith("httpx") for d in deps)


# ---------------------------------------------------------------- construction
class TestConstruction:
    @pytest.mark.parametrize(
        "cls,env_var,default_model",
        [
            (MistralProvider, "MISTRAL_API_KEY", "mistral-large-latest"),
            (OpenAIProvider, "OPENAI_API_KEY", "gpt-4o-mini"),
            (OpenRouterProvider, "OPENROUTER_API_KEY", "openai/gpt-4o-mini"),
            (AnthropicProvider, "ANTHROPIC_API_KEY", "claude-3-5-sonnet-latest"),
        ],
    )
    def test_requires_a_key(self, cls, env_var, default_model, monkeypatch):
        monkeypatch.delenv(env_var, raising=False)
        with pytest.raises(ValueError, match=env_var):
            cls()

    @pytest.mark.parametrize(
        "cls,env_var",
        [
            (MistralProvider, "MISTRAL_API_KEY"),
            (OpenAIProvider, "OPENAI_API_KEY"),
            (OpenRouterProvider, "OPENROUTER_API_KEY"),
            (AnthropicProvider, "ANTHROPIC_API_KEY"),
        ],
    )
    def test_reads_key_from_environment(self, cls, env_var, monkeypatch):
        monkeypatch.setenv(env_var, "from-env")
        assert cls().api_key == "from-env"

    def test_concrete_methods_win_over_the_abstract_base(self):
        """MRO order: the implementation must shadow AbstractProvider's stubs."""
        provider = OpenAIProvider(api_key="k")
        for name in ("chat", "stream", "embed"):
            method = getattr(provider, name)
            assert not getattr(method, "__isabstractmethod__", False), name

    def test_model_override(self):
        assert OpenAIProvider(api_key="k", model="gpt-4o").model == "gpt-4o"

    def test_base_url_override(self):
        provider = OpenAIProvider(api_key="k", base_url="https://proxy.internal/v1")
        assert provider.base_url == "https://proxy.internal/v1"


# ------------------------------------------------------------------ the calls
class TestChat:
    def test_openai_posts_to_the_right_endpoint(self):
        seen = []
        provider = OpenAIProvider(api_key="k", transport=transport_for(ok_handler(capture=seen)))
        text = asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))

        assert text == "hi there"
        assert seen[0].url.path == "/v1/chat/completions"
        assert seen[0].headers["authorization"] == "Bearer k"

    def test_payload_shape(self):
        seen = []
        provider = OpenAIProvider(api_key="k", transport=transport_for(ok_handler(capture=seen)))
        asyncio.run(
            provider.chat([{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=99)
        )
        body = json.loads(seen[0].content)
        assert body["model"] == "gpt-4o-mini"
        assert body["temperature"] == 0.2
        assert body["max_tokens"] == 99
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    def test_mistral_endpoint_and_model(self):
        seen = []
        provider = MistralProvider(
            api_key="k", min_delay=0, transport=transport_for(ok_handler(capture=seen))
        )
        asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))
        assert str(seen[0].url) == "https://api.mistral.ai/v1/chat/completions"
        assert json.loads(seen[0].content)["model"] == "mistral-large-latest"

    def test_openrouter_endpoint_and_headers(self):
        seen = []
        provider = OpenRouterProvider(
            api_key="k", transport=transport_for(ok_handler(capture=seen))
        )
        asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))
        assert seen[0].url.host == "openrouter.ai"
        assert seen[0].headers["x-title"] == "xli"

    def test_content_parts_are_concatenated(self):
        payload = {
            "choices": [
                {"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}
            ]
        }
        provider = OpenAIProvider(
            api_key="k", transport=transport_for(lambda r: httpx.Response(200, json=payload))
        )
        assert asyncio.run(provider.chat([])) == "ab"

    def test_tool_only_turn_returns_empty_not_error(self):
        payload = {"choices": [{"message": {"content": None}}]}
        provider = OpenAIProvider(
            api_key="k", transport=transport_for(lambda r: httpx.Response(200, json=payload))
        )
        assert asyncio.run(provider.chat([])) == ""


# --------------------------------------------------------------------- errors
class TestErrors:
    @pytest.mark.parametrize(
        "status,retryable,fragment",
        [
            (401, False, "unauthorized"),
            (403, False, "forbidden"),
            (404, False, "not found"),
            (429, True, "rate limited"),
            (500, True, "HTTP 500"),
            (503, True, "HTTP 503"),
        ],
    )
    def test_status_classification(self, status, retryable, fragment):
        provider = OpenAIProvider(
            api_key="k", transport=transport_for(lambda r: httpx.Response(status, text="nope"))
        )
        with pytest.raises(ProviderError) as info:
            asyncio.run(provider.chat([]))
        assert info.value.retryable is retryable
        assert info.value.status == status
        assert fragment in str(info.value)

    def test_malformed_response_names_the_problem(self):
        provider = OpenAIProvider(
            api_key="k", transport=transport_for(lambda r: httpx.Response(200, json={"unexpected": 1}))
        )
        with pytest.raises(ProviderError, match="unexpected response shape"):
            asyncio.run(provider.chat([]))

    def test_invalid_json_body(self):
        provider = OpenAIProvider(
            api_key="k",
            transport=transport_for(
                lambda r: httpx.Response(200, text="<html>not json</html>")
            ),
        )
        with pytest.raises(ProviderError, match="invalid JSON"):
            asyncio.run(provider.chat([]))

    def test_timeout_is_retryable(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        provider = OpenAIProvider(api_key="k", transport=transport_for(handler))
        with pytest.raises(ProviderError) as info:
            asyncio.run(provider.chat([]))
        assert info.value.retryable is True

    def test_network_error_is_retryable(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        provider = OpenAIProvider(api_key="k", transport=transport_for(handler))
        with pytest.raises(ProviderError) as info:
            asyncio.run(provider.chat([]))
        assert info.value.retryable is True


# ---------------------------------------------------------------- mistral pace
class TestMistralResilience:
    def _provider(self, handler, **kwargs):
        kwargs.setdefault("min_delay", 0)
        kwargs.setdefault("sleeper", lambda d: asyncio.sleep(0))
        return MistralProvider(api_key="k", transport=transport_for(handler), **kwargs)

    def test_retries_a_429_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, text="slow down")
            return httpx.Response(200, json=completion("finally"))

        provider = self._provider(handler, max_retries=3)
        assert asyncio.run(provider.chat([])) == "finally"
        assert calls["n"] == 3

    def test_gives_up_after_max_retries(self):
        def handler(request):
            return httpx.Response(429, text="slow down")

        provider = self._provider(handler, max_retries=2)
        with pytest.raises(ProviderError) as info:
            asyncio.run(provider.chat([]))
        assert info.value.status == 429

    def test_does_not_retry_a_fatal_error(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401, text="bad key")

        provider = self._provider(handler, max_retries=3)
        with pytest.raises(ProviderError):
            asyncio.run(provider.chat([]))
        assert calls["n"] == 1, "an auth failure must not be retried"

    def test_never_returns_an_error_string_as_content(self):
        """The old behaviour let the agent parse an outage as a reply."""
        def handler(request):
            return httpx.Response(500, text="boom")

        provider = self._provider(handler, max_retries=2)
        with pytest.raises(ProviderError):
            asyncio.run(provider.chat([]))

    def test_backoff_actually_sleeps(self):
        slept = []

        async def record(delay):
            slept.append(delay)

        def handler(request):
            return httpx.Response(429, text="slow down")

        provider = MistralProvider(
            api_key="k",
            min_delay=0,
            max_retries=3,
            sleeper=record,
            transport=transport_for(handler),
        )
        with pytest.raises(ProviderError):
            asyncio.run(provider.chat([]))
        # Three attempts produce two backoffs (8s, 16s) interleaved with
        # pacing waits, whose delay grows with consecutive_errors.
        backoffs = [d for d in slept if d >= 8]
        assert backoffs == [8, 16], f"expected exponential backoff, got {slept}"

    def test_pacing_spaces_requests(self):
        slept = []

        async def record(delay):
            slept.append(delay)

        provider = MistralProvider(
            api_key="k",
            min_delay=4.0,
            sleeper=record,
            transport=transport_for(ok_handler()),
        )

        async def two_calls():
            await provider.chat([])
            await provider.chat([])

        asyncio.run(two_calls())
        assert provider.total_requests == 2
        assert slept, "the second call should have waited"


# ----------------------------------------------------------------- embeddings
class TestEmbed:
    def test_mistral_embed(self):
        payload = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
        provider = MistralProvider(
            api_key="k",
            min_delay=0,
            transport=transport_for(lambda r: httpx.Response(200, json=payload)),
        )
        assert asyncio.run(provider.embed("text")) == [0.1, 0.2, 0.3]

    def test_mistral_embed_failure_is_empty_list(self):
        provider = MistralProvider(
            api_key="k",
            min_delay=0,
            transport=transport_for(lambda r: httpx.Response(500, text="nope")),
        )
        assert asyncio.run(provider.embed("text")) == []

    def test_openai_embed(self):
        payload = {"data": [{"embedding": [1.0, 2.0]}]}
        provider = OpenAIProvider(
            api_key="k", transport=transport_for(lambda r: httpx.Response(200, json=payload))
        )
        assert asyncio.run(provider.embed("text")) == [1.0, 2.0]

    def test_unsupported_embed_returns_empty(self):
        assert asyncio.run(AnthropicProvider(api_key="k").embed("x")) == []
        assert asyncio.run(OpenRouterProvider(api_key="k").embed("x")) == []


# ------------------------------------------------------------------ anthropic
class TestAnthropic:
    def _provider(self, handler):
        return AnthropicProvider(api_key="k", transport=transport_for(handler))

    def test_uses_the_messages_endpoint(self):
        seen = []
        provider = self._provider(ok_handler(capture=seen))
        provider.transport = None
        seen.clear()

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}]})

        provider = AnthropicProvider(api_key="k", transport=transport_for(handler))
        asyncio.run(provider.chat([{"role": "user", "content": "hello"}]))
        assert seen[0].url.path == "/v1/messages"

    def test_auth_uses_x_api_key_not_bearer(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"content": []})

        provider = AnthropicProvider(api_key="secret", transport=transport_for(handler))
        asyncio.run(provider.chat([]))
        assert seen[0].headers["x-api-key"] == "secret"
        assert "authorization" not in seen[0].headers
        assert "anthropic-version" in seen[0].headers

    def test_system_message_moves_to_top_level(self):
        """Anthropic 400s on role=system inside messages."""
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"content": []})

        provider = AnthropicProvider(api_key="k", transport=transport_for(handler))
        asyncio.run(
            provider.chat(
                [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ]
            )
        )
        body = json.loads(seen[0].content)
        assert body["system"] == "be terse"
        assert all(m["role"] != "system" for m in body["messages"])

    def test_multiple_system_messages_are_joined(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"content": []})

        provider = AnthropicProvider(api_key="k", transport=transport_for(handler))
        asyncio.run(
            provider.chat(
                [
                    {"role": "system", "content": "one"},
                    {"role": "system", "content": "two"},
                    {"role": "user", "content": "hi"},
                ]
            )
        )
        assert json.loads(seen[0].content)["system"] == "one\n\ntwo"

    def test_joins_text_blocks(self):
        payload = {
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "tool_use", "id": "x"},
                {"type": "text", "text": "world"},
            ]
        }
        provider = self._provider(lambda r: httpx.Response(200, json=payload))
        assert asyncio.run(provider.chat([])) == "hello world"

    def test_tool_only_response_is_empty(self):
        payload = {"content": [{"type": "tool_use", "id": "x"}]}
        provider = self._provider(lambda r: httpx.Response(200, json=payload))
        assert asyncio.run(provider.chat([])) == ""

    def test_error_classification(self):
        provider = self._provider(lambda r: httpx.Response(401, text="bad key"))
        with pytest.raises(ProviderError) as info:
            asyncio.run(provider.chat([]))
        assert info.value.retryable is False


# -------------------------------------------------------------------- factory
class TestFactory:
    def test_selectable_providers_are_not_stubs(self):
        """The config lets you pick these, so they must actually work."""
        import inspect

        for module_name, class_name in [
            ("xli.providers.mistral", "MistralProvider"),
            ("xli.providers.openai", "OpenAIProvider"),
            ("xli.providers.openrouter", "OpenRouterProvider"),
            ("xli.providers.anthropic", "AnthropicProvider"),
        ]:
            module = __import__(module_name, fromlist=[class_name])
            cls = getattr(module, class_name)
            source = inspect.getsource(cls)
            assert "not yet implemented" not in source.lower(), class_name
            assert "NotImplementedError" not in source, class_name

    def test_get_provider_builds_the_configured_one(self, monkeypatch):
        from xli.manager.config import reset_config
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
        monkeypatch.setenv("XLI_CONFIG_DIR", "/nonexistent-config-dir")
        monkeypatch.delenv("XLI_PROVIDER", raising=False)
        reset_provider()
        reset_config()
        try:
            provider = get_provider()
            assert isinstance(provider, MistralProvider)
        finally:
            reset_provider()
            reset_config()

    def test_unknown_provider_is_rejected_clearly(self, monkeypatch):
        """The config singleton must be dropped or it outlives the monkeypatch."""
        from xli.manager.config import reset_config
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("XLI_CONFIG_DIR", "/nonexistent-config-dir")
        monkeypatch.setenv("XLI_PROVIDER", "not-a-real-provider")
        reset_provider()
        reset_config()
        try:
            with pytest.raises(ValueError, match="Unknown provider"):
                get_provider()
        finally:
            reset_provider()
            reset_config()

    def test_env_selects_the_provider(self, monkeypatch):
        from xli.manager.config import reset_config
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("XLI_CONFIG_DIR", "/nonexistent-config-dir")
        monkeypatch.setenv("XLI_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        reset_provider()
        reset_config()
        try:
            assert isinstance(get_provider(), OpenAIProvider)
        finally:
            reset_provider()
            reset_config()

    def test_ollama_needs_no_key(self, monkeypatch):
        from xli.manager.config import reset_config
        from xli.providers.base import get_provider, reset_provider
        from xli.providers.ollama import OllamaProvider

        monkeypatch.setenv("XLI_CONFIG_DIR", "/nonexistent-config-dir")
        monkeypatch.setenv("XLI_PROVIDER", "ollama")
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        reset_provider()
        reset_config()
        try:
            assert isinstance(get_provider(), OllamaProvider)
        finally:
            reset_provider()
            reset_config()


# ------------------------------------------------------------ agent end to end
class TestAgentWithRealProvider:
    def test_agent_runs_against_a_mocked_openai_backend(self):
        """The default path, end to end, with no network and no API key."""
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy

        responses = iter(["<done>all finished</done>"])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=completion(next(responses)))

        provider = OpenAIProvider(api_key="k", transport=transport_for(handler))
        agent = Agent(provider, policy=Policy(mode=Mode.AUTO))
        result = asyncio.run(agent.run("do the thing"))

        assert result.ok is True
        assert result.stopped_reason == "done"
        assert "all finished" in result.summary

    def test_agent_surfaces_a_provider_outage(self):
        from xli.agent import Agent
        from xli.permissions.policy import Mode, Policy

        provider = OpenAIProvider(
            api_key="k", transport=transport_for(lambda r: httpx.Response(500, text="down"))
        )
        result = asyncio.run(Agent(provider, policy=Policy(mode=Mode.AUTO)).run("go"))
        assert result.stopped_reason == "provider_error"
        assert result.ok is False


# --------------------------------------------------------------------- gemini
class TestGemini:
    def _provider(self, handler, **kwargs):
        from xli.providers.gemini import GeminiProvider

        return GeminiProvider(api_key="k", transport=transport_for(handler), **kwargs)

    @staticmethod
    def reply(text: str) -> dict:
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}

    def test_uses_generate_content_endpoint(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=self.reply("hi"))

        provider = self._provider(handler)
        assert asyncio.run(provider.chat([{"role": "user", "content": "hello"}])) == "hi"
        assert seen[0].url.path == "/v1beta/models/gemini-1.5-pro:generateContent"

    def test_key_goes_in_a_header_not_the_query_string(self):
        """A key in the URL lands in proxy and server access logs."""
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=self.reply("hi"))

        provider = self._provider(handler)
        asyncio.run(provider.chat([]))
        assert seen[0].headers["x-goog-api-key"] == "k"
        assert "key=" not in str(seen[0].url)
        assert "k" not in str(seen[0].url.query)

    def test_system_becomes_system_instruction(self):
        """Relabelling system as user silently demotes the agent's prompt."""
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=self.reply("hi"))

        provider = self._provider(handler)
        asyncio.run(
            provider.chat(
                [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ]
            )
        )
        body = json.loads(seen[0].content)
        assert body["systemInstruction"]["parts"][0]["text"] == "be terse"
        assert all(c["role"] != "system" for c in body["contents"])

    def test_assistant_becomes_model(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=self.reply("hi"))

        provider = self._provider(handler)
        asyncio.run(
            provider.chat(
                [
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                ]
            )
        )
        roles = [c["role"] for c in json.loads(seen[0].content)["contents"]]
        assert roles == ["user", "model"]

    def test_joins_multiple_parts(self):
        payload = {
            "candidates": [
                {"content": {"parts": [{"text": "one "}, {"text": "two"}]}}
            ]
        }
        provider = self._provider(lambda r: httpx.Response(200, json=payload))
        assert asyncio.run(provider.chat([])) == "one two"

    def test_blocked_generation_is_empty_not_an_error(self):
        payload = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        provider = self._provider(lambda r: httpx.Response(200, json=payload))
        assert asyncio.run(provider.chat([])) == ""

    def test_missing_candidates_raises(self):
        provider = self._provider(lambda r: httpx.Response(200, json={"nope": 1}))
        with pytest.raises(ProviderError, match="unexpected Gemini response"):
            asyncio.run(provider.chat([]))

    def test_error_classification(self):
        provider = self._provider(lambda r: httpx.Response(429, text="quota"))
        with pytest.raises(ProviderError) as info:
            asyncio.run(provider.chat([]))
        assert info.value.retryable is True


# --------------------------------------------------------------------- ollama
class TestOllama:
    def _provider(self, handler, **kwargs):
        from xli.providers.ollama import OllamaProvider

        return OllamaProvider(transport=transport_for(handler), **kwargs)

    def test_no_api_key_required(self):
        from xli.providers.ollama import OllamaProvider

        provider = OllamaProvider()
        assert provider.model == "llama3.1"
        assert provider.base_url == "http://localhost:11434"

    def test_chat_shape(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"message": {"content": "hello back"}})

        provider = self._provider(handler)
        assert asyncio.run(provider.chat([{"role": "user", "content": "hi"}])) == "hello back"
        assert seen[0].url.path == "/api/chat"
        body = json.loads(seen[0].content)
        assert body["stream"] is False
        assert body["model"] == "llama3.1"

    def test_no_auth_header_when_no_key(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"message": {"content": ""}})

        provider = self._provider(handler)
        asyncio.run(provider.chat([]))
        assert "authorization" not in seen[0].headers

    def test_connection_refused_says_what_to_do(self):
        """The common failure is that the daemon simply is not running."""
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        provider = self._provider(handler)
        with pytest.raises(ProviderError) as info:
            asyncio.run(provider.chat([]))
        assert "ollama serve" in str(info.value)
        assert info.value.retryable is False

    def test_empty_content_is_not_an_error(self):
        provider = self._provider(
            lambda r: httpx.Response(200, json={"message": {"content": ""}})
        )
        assert asyncio.run(provider.chat([])) == ""

    def test_malformed_response(self):
        provider = self._provider(lambda r: httpx.Response(200, json={"nope": 1}))
        with pytest.raises(ProviderError, match="unexpected Ollama response"):
            asyncio.run(provider.chat([]))

    def test_model_from_environment(self, monkeypatch):
        from xli.providers.ollama import OllamaProvider

        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5")
        monkeypatch.setenv("OLLAMA_HOST", "http://192.168.1.5:11434")
        provider = OllamaProvider()
        assert provider.model == "qwen2.5"
        assert provider.base_url == "http://192.168.1.5:11434"

    def test_embeddings(self):
        provider = self._provider(
            lambda r: httpx.Response(200, json={"embedding": [0.5, 0.6]})
        )
        assert asyncio.run(provider.embed("text")) == [0.5, 0.6]
