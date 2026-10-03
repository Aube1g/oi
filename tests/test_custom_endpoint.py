#!/usr/bin/env python3
"""Tests for pointing xli at a custom OpenAI-compatible endpoint.

Two things are pinned here.

1. `provider.base_url`. The transport already accepted a base_url, but nothing
   could set one: the config had no key for it and get_provider() only ever
   passed api_key. So a proxy or self-hosted gateway was unreachable.

2. A bug in config saving that this exposed. `provider` is both a setting in
   its own right and the namespace parent of `provider.model`, and _nest could
   not represent both. It walked into the scalar, hit its defensive break, and
   silently dropped the value — so `xli config set provider.model gpt-4o`
   printed "set ... wrote ..." and persisted nothing. Any key under a scalar
   prefix was affected, not just the new one.
"""

import json

import httpx
import pytest

from xli.manager.config import Config, _flatten, _nest


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
    return Config.load()


class TestBaseUrlConfig:
    def test_the_default_is_empty_meaning_use_the_providers_own(self, cfg):
        assert cfg.base_url() == ""

    def test_setting_it_is_read_back(self, cfg):
        cfg.set("provider.base_url", "https://api.xkiro.com/v1")
        assert cfg.base_url() == "https://api.xkiro.com/v1"

    def test_none_becomes_empty_not_the_string_none(self, cfg):
        cfg.set("provider.base_url", None)
        assert cfg.base_url() == ""


class TestBaseUrlReachesTheProvider:
    """The point of the setting: the request must actually go there."""

    @pytest.fixture
    def provider(self, cfg, monkeypatch):
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        cfg.set("provider", "openai")
        cfg.set("provider.model", "some-model")
        cfg.set("provider.base_url", "https://api.xkiro.com/v1")
        reset_provider()
        try:
            yield get_provider(cfg, force=True)
        finally:
            reset_provider()

    def test_the_provider_uses_the_configured_url(self, provider):
        assert provider.base_url == "https://api.xkiro.com/v1"

    def test_the_request_is_sent_to_the_configured_url(self, provider):
        import asyncio

        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}}]}
            )

        provider._transport = httpx.MockTransport(handler)
        asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))

        assert seen["url"] == "https://api.xkiro.com/v1/chat/completions"
        assert seen["auth"] == "Bearer test-key"

    def test_the_configured_model_is_used(self, provider):
        assert provider.model == "some-model"

    def test_an_empty_base_url_leaves_the_provider_default(self, cfg, monkeypatch):
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        cfg.set("provider", "openai")
        cfg.set("provider.base_url", "")
        reset_provider()
        try:
            assert get_provider(cfg, force=True).base_url == "https://api.openai.com/v1"
        finally:
            reset_provider()


class TestBaseUrlFromTheEnvironment:
    @pytest.mark.parametrize(
        "var",
        ["XLI_BASE_URL", "XLI_PROVIDER_BASE_URL", "XLI_PROVIDER__BASE_URL"],
    )
    def test_every_spelling_is_honoured(self, tmp_path, monkeypatch, var):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv(var, "https://api.xkiro.com/v1")
        assert Config.load().base_url() == "https://api.xkiro.com/v1"

    def test_the_environment_wins_over_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        config = Config.load()
        config.set("provider.base_url", "https://file.example/v1")
        config.save()

        monkeypatch.setenv("XLI_BASE_URL", "https://env.example/v1")
        assert Config.load().base_url() == "https://env.example/v1"


class TestNestingDoesNotDropKeys:
    """A key that is both a scalar and a prefix used to vanish on save."""

    def test_a_scalar_and_its_children_all_survive(self):
        flat = {
            "provider": "openai",
            "provider.model": "gpt-4o",
            "provider.base_url": "https://api.xkiro.com/v1",
        }
        assert _nest(flat) == flat, "the scalar and its children collide"

    def test_the_nesting_round_trips(self):
        flat = {
            "provider": "openai",
            "provider.model": "gpt-4o",
            "provider.base_url": "https://api.xkiro.com/v1",
        }
        assert _flatten(_nest(flat)) == flat

    def test_ordinary_sections_still_nest(self):
        assert _nest({"permissions.mode": "auto", "permissions.deny": []}) == {
            "permissions": {"mode": "auto", "deny": []}
        }

    def test_saving_then_loading_keeps_every_value(self, cfg):
        cfg.set("provider", "openai")
        cfg.set("provider.model", "gpt-4o")
        cfg.set("provider.base_url", "https://api.xkiro.com/v1")
        path = cfg.save()

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["provider"] == "openai"
        assert on_disk["provider.model"] == "gpt-4o"
        assert on_disk["provider.base_url"] == "https://api.xkiro.com/v1"

        reloaded = Config.load()
        assert reloaded.default_provider() == "openai"
        assert reloaded.model() == "gpt-4o"
        assert reloaded.base_url() == "https://api.xkiro.com/v1"

    def test_no_key_is_lost_for_any_scalar_prefix_in_defaults(self):
        """Every DEFAULTS key that is also a prefix must survive nesting."""
        from xli.manager.config import DEFAULTS

        prefixes = {
            key
            for key in DEFAULTS
            if any(other.startswith(key + ".") for other in DEFAULTS)
        }
        assert prefixes, "expected at least one scalar/prefix collision"

        for prefix in prefixes:
            child = next(k for k in DEFAULTS if k.startswith(prefix + "."))
            flat = {prefix: "scalar", child: "child"}
            assert _nest(flat) == flat, f"{prefix} / {child} collided"
