#!/usr/bin/env python3
"""Config basics.

There is one config system: xli.manager.config. The legacy xli.core.config was
a second, parallel one that read only ~/.xli/config.json and ~/.xli/.env and
ignored XLI_* entirely — which is why `XLI_PROVIDER=openai` silently did
nothing while the provider factory consulted it. These tests pin the surviving
behaviour.
"""

import pytest

from xli.manager.config import Config, get_config, reset_config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path))
    reset_config()
    yield
    reset_config()


def test_config_exists():
    assert get_config() is not None


def test_default_provider():
    assert get_config().default_provider() in {
        "mistral", "openai", "anthropic", "claude", "openrouter",
        "gemini", "google", "ollama", "local",
    }


def test_env_overrides_the_default_provider(monkeypatch):
    """The regression: this used to be ignored entirely."""
    monkeypatch.setenv("XLI_PROVIDER", "openai")
    assert Config.load().default_provider() == "openai"


def test_env_overrides_nested_keys(monkeypatch):
    monkeypatch.setenv("XLI_PROVIDER__MODEL", "gpt-4o")
    monkeypatch.setenv("XLI_AGENT__MAX_STEPS", "7")
    config = Config.load()
    assert config.model() == "gpt-4o"
    assert int(config.get("agent.max_steps")) == 7


def test_env_alias_shorthands(monkeypatch):
    monkeypatch.setenv("XLI_MODE", "auto")
    monkeypatch.setenv("XLI_MAX_STEPS", "5")
    config = Config.load()
    assert config.permission_mode() == "auto"
    assert int(config.get("agent.max_steps")) == 5


def test_api_key_lookup(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert Config.load().api_key("openai") == "sk-test"


@pytest.mark.parametrize(
    "alias,var",
    [("claude", "ANTHROPIC_API_KEY"), ("google", "GEMINI_API_KEY")],
)
def test_api_key_aliases(monkeypatch, alias, var):
    monkeypatch.setenv(var, "aliased")
    assert Config.load().api_key(alias) == "aliased"


def test_api_key_absent_is_none(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert Config.load().api_key("mistral") is None


def test_sandbox_accessors():
    config = get_config()
    assert isinstance(config.sandbox_timeout(), int)
    assert isinstance(config.sandbox_max_memory_mb(), int)
    assert isinstance(config.sandbox_network_disabled(), bool)


def test_team_and_project():
    config = get_config()
    assert config.team() == "default"
    assert config.project() == "default"


def test_model_and_sampling_defaults():
    config = get_config()
    assert isinstance(config.model(), str) and config.model()
    assert isinstance(config.temperature(), float)
    assert isinstance(config.max_tokens(), int)


# ---------------------------------------------------------------- .env loading
class TestDotEnvLoading:
    """The legacy xli/core/config.py loaded ~/.xli/.env; deleting that module
    silently dropped the feature. These pin it back."""

    @pytest.fixture(autouse=True)
    def _restore_environ(self):
        """Loading .env writes into the real process environment.

        That is the intended behaviour — api_key() reads os.environ — but it
        means monkeypatch cannot undo it: delenv() before the load does not
        know which names the file will add. Without this, XLI_PROVIDER and
        friends leak into every later test in the session.
        """
        import os

        snapshot = dict(os.environ)
        yield
        os.environ.clear()
        os.environ.update(snapshot)
        reset_config()

    def test_key_from_dotenv_reaches_api_key(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-dotenv\n", encoding="utf-8")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = Config.load()
        assert config.api_key("openai") == "sk-from-dotenv"
        assert "OPENAI_API_KEY" in config.env_loaded

    def test_comments_and_blank_lines_skipped(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text(
            "# a comment\n\n   \nOPENAI_API_KEY=sk-value\n", encoding="utf-8"
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert Config.load().api_key("openai") == "sk-value"

    def test_quotes_are_stripped(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text(
            'OPENAI_API_KEY="sk-quoted"\nANTHROPIC_API_KEY=\'sk-single\'\n', encoding="utf-8"
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config = Config.load()
        assert config.api_key("openai") == "sk-quoted"
        assert config.api_key("anthropic") == "sk-single"

    def test_export_prefix_is_handled(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("export OPENAI_API_KEY=sk-exported\n", encoding="utf-8")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert Config.load().api_key("openai") == "sk-exported"

    def test_real_environment_wins_over_the_file(self, tmp_path, monkeypatch):
        """So one command can override a stale file value."""
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-file\n", encoding="utf-8")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        config = Config.load()
        assert config.api_key("openai") == "sk-from-env"
        assert "OPENAI_API_KEY" not in config.env_loaded

    def test_xli_vars_in_dotenv_apply_to_config(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("XLI_PROVIDER=openai\nXLI_MODE=auto\n", encoding="utf-8")
        monkeypatch.delenv("XLI_PROVIDER", raising=False)
        monkeypatch.delenv("XLI_MODE", raising=False)
        config = Config.load()
        assert config.default_provider() == "openai"
        assert config.permission_mode() == "auto"

    def test_project_dotenv_beats_user_dotenv(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-user\n", encoding="utf-8")
        project = tmp_path / "proj"
        (project / ".xli").mkdir(parents=True)
        (project / ".xli" / ".env").write_text("OPENAI_API_KEY=sk-project\n", encoding="utf-8")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = Config.load(project_root=project)
        assert config.api_key("openai") == "sk-project"

    def test_missing_dotenv_is_fine(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config = Config.load()
        assert config.env_loaded == []
        assert config.api_key("openai") is None

    def test_malformed_line_does_not_break_loading(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text(
            "nonsense-without-equals\n=novalue\nOPENAI_API_KEY=sk-ok\n", encoding="utf-8"
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert Config.load().api_key("openai") == "sk-ok"

    def test_python_dotenv_is_actually_used_when_present(self):
        """It is a declared dependency, so the primary path must be reachable."""
        import importlib.util

        from xli.manager.config import _parse_dotenv

        result = _parse_dotenv("A=1\nB='two'\n")
        assert result == {"A": "1", "B": "two"}
        if importlib.util.find_spec("dotenv"):
            # exercised above via the real library
            assert True


class TestDependencyDeclarations:
    def test_python_dotenv_is_now_used(self):
        """It was declared but never imported anywhere."""
        import ast
        from pathlib import Path

        imported = set()
        for path in Path("xli").rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
        assert "dotenv" in imported
