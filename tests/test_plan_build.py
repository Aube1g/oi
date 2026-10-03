#!/usr/bin/env python3
"""Tests for plan/build mode and for the provider-selection fix.

Covers:

  * xli/core/plan_build.py was unwired. Its allowed_tools listed "browse" and
    "task", neither of which exists in xli.tools.registry, so can_use_tool()
    denied ls, glob, grep, git and todo in plan mode and ls, glob, grep, git and
    todo in build mode.
  * Mode now drives both the agent's system message and its permission posture,
    from one place, so the two cannot disagree.
  * get_provider() ignored its caller's config and read the module-level
    singleton, so `xli run --provider openai` silently built the default
    provider. The cached instance was also returned before any config was
    consulted, so a second call with a different provider returned the first.
"""

import pytest

from xli.agent import Agent
from xli.core.plan_build import (
    BUILD_TOOLS,
    MODE_CONFIGS,
    PLAN_TOOLS,
    Mode,
    ModeSwitcher,
    get_mode_switcher,
    permission_mode,
    policy_for,
    reset_mode_switcher,
)
from xli.permissions.policy import Mode as PolicyMode
from xli.permissions.policy import Policy
from xli.providers.fake import FakeProvider
from xli.tools.registry import default_registry


@pytest.fixture(autouse=True)
def _reset_mode():
    reset_mode_switcher()
    yield
    reset_mode_switcher()


# ------------------------------------------------------------------ tool names
class TestAllowedToolsAreReal:
    def _real_tools(self):
        return set(default_registry().names(enabled_only=False))

    def test_every_plan_tool_exists(self):
        assert set(PLAN_TOOLS) <= self._real_tools()

    def test_every_build_tool_exists(self):
        assert set(BUILD_TOOLS) <= self._real_tools()

    def test_build_covers_the_whole_registry(self):
        """Build mode must not silently deny a tool that exists.

        BUILD_TOOLS is derived from the registry now (see plan_build), so this
        is the regression test for the derivation itself: if a future edit
        pins the list back to a literal, new tools stop being callable in
        build mode and this fails.
        """
        assert set(BUILD_TOOLS) == self._real_tools()

    def test_build_is_a_superset_of_plan(self):
        assert set(PLAN_TOOLS) <= set(BUILD_TOOLS)

    def test_mutating_tools_are_usable_in_build_mode(self):
        from xli.permissions.policy import MUTATING_TOOLS

        assert MUTATING_TOOLS <= set(BUILD_TOOLS)

    def test_plan_is_read_only(self):
        """No mutating tool may appear in plan mode."""
        mutating = {"write", "edit", "bash", "git"}
        assert not set(PLAN_TOOLS) & mutating

    def test_the_removed_ghost_tools_are_gone(self):
        for ghost in ("browse", "task"):
            assert ghost not in PLAN_TOOLS
            assert ghost not in BUILD_TOOLS

    def test_both_modes_declare_something(self):
        assert MODE_CONFIGS[Mode.PLAN].allowed_tools
        assert MODE_CONFIGS[Mode.BUILD].allowed_tools


# ------------------------------------------------------------------ switcher
class TestModeSwitcher:
    def test_defaults_to_plan(self):
        assert get_mode_switcher().is_plan()

    def test_singleton_is_stable(self):
        assert get_mode_switcher() is get_mode_switcher()

    def test_reset_gives_a_new_instance(self):
        first = get_mode_switcher()
        reset_mode_switcher()
        assert get_mode_switcher() is not first

    def test_toggle_flips_both_ways(self):
        switcher = ModeSwitcher(Mode.PLAN)
        assert switcher.toggle().mode is Mode.BUILD
        assert switcher.toggle().mode is Mode.PLAN

    def test_switch_returns_the_context(self):
        switcher = ModeSwitcher(Mode.PLAN)
        context = switcher.switch(Mode.BUILD)
        assert context.can_modify_files is True
        assert context.can_run_shell is True

    def test_history_is_recorded(self):
        switcher = ModeSwitcher(Mode.PLAN)
        switcher.switch(Mode.BUILD)
        switcher.switch(Mode.PLAN)
        assert switcher.mode_history == [Mode.PLAN, Mode.BUILD, Mode.PLAN]

    def test_plan_denies_write_allows_read(self):
        switcher = ModeSwitcher(Mode.PLAN)
        assert switcher.can_use_tool("read") is True
        assert switcher.can_use_tool("ls") is True
        assert switcher.can_use_tool("write") is False
        assert switcher.can_use_tool("bash") is False

    def test_build_allows_write(self):
        switcher = ModeSwitcher(Mode.BUILD)
        assert switcher.can_use_tool("write") is True
        assert switcher.can_use_tool("git") is True

    def test_status_line_names_the_mode(self):
        assert "PLAN" in ModeSwitcher(Mode.PLAN).get_status_line()
        assert "BUILD" in ModeSwitcher(Mode.BUILD).get_status_line()


# ------------------------------------------------------------------- bridging
class TestPermissionBridge:
    def test_plan_maps_to_readonly(self):
        assert permission_mode(Mode.PLAN) is PolicyMode.READONLY

    def test_build_maps_to_confirm(self):
        assert permission_mode(Mode.BUILD) is PolicyMode.CONFIRM

    def test_policy_for_plan_blocks_writes(self):
        policy = policy_for(Mode.PLAN)
        assert policy.check("write", {"path": "a.py"}).allowed is False

    def test_policy_for_plan_allows_reads(self):
        policy = policy_for(Mode.PLAN)
        assert policy.check("read", {"path": "a.py"}).allowed is True

    def test_policy_for_build_allows_writes(self):
        policy = policy_for(Mode.BUILD)
        assert policy.check("write", {"path": "a.py"}).allowed is True

    def test_every_plan_tool_passes_the_plan_policy(self):
        """The two gates must agree, or a call is allowed by one and not the other."""
        policy = policy_for(Mode.PLAN)
        args = {"path": "a.py"}
        for tool in PLAN_TOOLS:
            assert policy.check(tool, args).allowed, f"plan policy denied {tool}"


# ---------------------------------------------------------------------- agent
class TestAgentMode:
    def test_plan_sets_a_readonly_policy(self):
        agent = Agent(FakeProvider(responses=["ok"]), mode=Mode.PLAN)
        assert agent.policy.mode is PolicyMode.READONLY

    def test_build_sets_a_confirm_policy(self):
        agent = Agent(FakeProvider(responses=["ok"]), mode=Mode.BUILD)
        assert agent.policy.mode is PolicyMode.CONFIRM

    def test_no_mode_keeps_the_default(self):
        agent = Agent(FakeProvider(responses=["ok"]))
        assert agent.policy.mode is PolicyMode.CONFIRM
        assert agent.mode is None

    def test_plan_injects_the_plan_message(self):
        prompt = Agent(FakeProvider(responses=["ok"]), mode=Mode.PLAN).system_prompt()
        assert "PLAN mode" in prompt

    def test_build_injects_the_build_message(self):
        prompt = Agent(FakeProvider(responses=["ok"]), mode=Mode.BUILD).system_prompt()
        assert "BUILD mode" in prompt

    def test_no_mode_injects_neither(self):
        prompt = Agent(FakeProvider(responses=["ok"])).system_prompt()
        assert "PLAN mode" not in prompt
        assert "BUILD mode" not in prompt

    def test_an_explicit_policy_wins_over_the_mode(self):
        """The caller is closer to the truth about intent than a shorthand flag."""
        agent = Agent(
            FakeProvider(responses=["ok"]),
            mode=Mode.PLAN,
            policy=Policy(mode=PolicyMode.AUTO),
        )
        assert agent.policy.mode is PolicyMode.AUTO

    def test_plan_agent_cannot_write(self, tmp_path):
        """End to end: the agent's own policy refuses the call."""
        target = tmp_path / "nope.txt"
        agent = Agent(FakeProvider(responses=["ok"]), mode=Mode.PLAN)
        assert agent.policy.check("write", {"path": str(target)}).allowed is False
        assert not target.exists()

    def test_tools_are_still_listed_in_plan_mode(self):
        """Plan mode narrows permissions; it must not hide the catalogue."""
        prompt = Agent(FakeProvider(responses=["ok"]), mode=Mode.PLAN).system_prompt()
        assert "read" in prompt


# -------------------------------------------------------------------- cli flags
class TestCliModeFlags:
    def test_plan_and_build_are_parsed(self):
        from xli.cli import build_parser

        parser = build_parser()
        assert parser.parse_args(["run", "--plan", "task"]).work_mode == "plan"
        assert parser.parse_args(["run", "--build", "task"]).work_mode == "build"

    def test_neither_flag_means_no_mode(self):
        from xli.cli import build_parser

        assert build_parser().parse_args(["run", "task"]).work_mode is None

    def test_the_flags_are_mutually_exclusive(self):
        from xli.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "--plan", "--build", "task"])

    def test_resolver_maps_flag_to_mode(self):
        from xli.cli import _work_mode, build_parser

        args = build_parser().parse_args(["run", "--plan", "task"])
        assert _work_mode(args) is Mode.PLAN
        args = build_parser().parse_args(["run", "--build", "task"])
        assert _work_mode(args) is Mode.BUILD
        args = build_parser().parse_args(["run", "task"])
        assert _work_mode(args) is None

    def test_resolver_survives_a_namespace_without_the_attr(self):
        """Subparsers added before the flag existed must not break."""
        import argparse

        from xli.cli import _work_mode

        assert _work_mode(argparse.Namespace()) is None


# ------------------------------------------------------------- provider choice
class TestProviderSelection:
    def test_get_provider_uses_the_config_it_is_given(self, monkeypatch, tmp_path):
        """`xli run --provider X` used to build the default provider instead."""
        from xli.manager.config import Config, reset_config
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        reset_config()
        reset_provider()

        config = Config.load()
        config.set("provider", "openai")
        provider = get_provider(config)
        assert type(provider).__name__ == "OpenAIProvider"
        reset_provider()

    def test_get_provider_without_a_config_uses_the_singleton(self, monkeypatch, tmp_path):
        from xli.manager.config import get_config, reset_config
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
        reset_config()
        reset_provider()

        get_config().set("provider", "mistral")
        assert type(get_provider()).__name__ == "MistralProvider"
        reset_provider()

    def test_force_rebuilds_even_when_cached(self, monkeypatch, tmp_path):
        """A cached instance used to be returned before the config was read."""
        from xli.manager.config import Config, reset_config
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("OPENAI_API_KEY", "k1")
        monkeypatch.setenv("MISTRAL_API_KEY", "k2")
        reset_config()
        reset_provider()

        first = Config.load()
        first.set("provider", "openai")
        assert type(get_provider(first)).__name__ == "OpenAIProvider"

        second = Config.load()
        second.set("provider", "mistral")
        # without force the cached OpenAI provider comes back
        assert type(get_provider(second)).__name__ == "OpenAIProvider"
        assert type(get_provider(second, force=True)).__name__ == "MistralProvider"
        reset_provider()

    def test_cli_passes_its_config_through(self, monkeypatch, tmp_path):
        """_build_provider must hand the CLI's config to the factory."""
        from xli.cli import _build_provider
        from xli.manager.config import Config, reset_config
        from xli.providers.base import reset_provider

        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        reset_config()
        reset_provider()

        config = Config.load()
        config.set("provider", "openai")
        assert type(_build_provider(config)).__name__ == "OpenAIProvider"
        reset_provider()

    def test_aliases_resolve(self, monkeypatch, tmp_path):
        from xli.manager.config import Config, reset_config
        from xli.providers.base import get_provider, reset_provider

        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        reset_config()
        reset_provider()

        config = Config.load()
        config.set("provider", "claude")
        assert type(get_provider(config)).__name__ == "AnthropicProvider"
        reset_provider()
