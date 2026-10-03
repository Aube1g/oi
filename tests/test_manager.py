#!/usr/bin/env python3
"""Tests for the manager: typed config, Cython kernel build, accel hook."""

import json
import sys
from pathlib import Path

import pytest

from xli import accel
from xli.manager import (
    CKERNEL_DIR,
    Config,
    ConfigError,
    DEFAULTS,
    Manifest,
    build,
    clean,
    discover_targets,
    is_usable,
    preflight,
    reset_config,
    source_hash,
    status,
)
from xli.manager.config import _flatten, _nest
from xli.manager.kernel_build import _python_tag


@pytest.fixture(autouse=True)
def _isolate_config():
    reset_config()
    yield
    reset_config()


# ---------------------------------------------------------------------- config
class TestConfigDefaults:
    def test_defaults_present(self):
        config = Config()
        assert config.get("provider") == "mistral"
        assert config.get("permissions.mode") == "confirm"
        assert config.get("kernel.enabled") is True

    def test_missing_key_returns_default(self):
        assert Config().get("nope.nope", "fallback") == "fallback"

    def test_every_default_is_typed(self):
        from xli.manager.config import COERCERS

        for key in DEFAULTS:
            assert key in COERCERS, f"{key} has no coercer"


class TestConfigCoercion:
    def test_bool_from_strings(self):
        config = Config()
        assert config.set("kernel.enabled", "yes") is True
        assert config.set("kernel.enabled", "off") is False
        assert config.set("kernel.enabled", 1) is True

    def test_bool_rejects_nonsense(self):
        with pytest.raises(ConfigError):
            Config().set("kernel.enabled", "maybe")

    def test_int_from_string(self):
        assert Config().set("agent.max_steps", "12") == 12

    def test_float_from_string(self):
        assert Config().set("provider.temperature", "0.7") == 0.7

    def test_list_from_comma_string(self):
        assert Config().set("permissions.deny", "/etc/*, *.env") == ["/etc/*", "*.env"]

    def test_list_from_json_array(self):
        assert Config().set("permissions.deny", ["a", "b"]) == ["a", "b"]

    def test_empty_list_from_empty_string(self):
        assert Config().set("permissions.deny", "") == []

    def test_choice_rejects_typo(self):
        with pytest.raises(ConfigError) as exc:
            Config().set("permissions.mode", "autos")
        assert "not one of" in str(exc.value)

    def test_choice_accepts_valid(self):
        assert Config().set("permissions.mode", "readonly") == "readonly"

    def test_range_rejected(self):
        with pytest.raises(ConfigError):
            Config().set("provider.temperature", 5.0)
        with pytest.raises(ConfigError):
            Config().set("agent.max_steps", 0)

    def test_range_accepted(self):
        assert Config().set("provider.temperature", 1.0) == 1.0


class TestConfigLayers:
    def test_project_file_overrides_user_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path / "user"))
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        (user_dir / "config.json").write_text(json.dumps({"provider": "openai"}))

        project = tmp_path / "proj"
        (project / ".xli").mkdir(parents=True)
        (project / ".xli" / "config.json").write_text(json.dumps({"provider": "anthropic"}))

        config = Config.load(project_root=project, use_env=False)
        assert config.get("provider") == "anthropic"

    def test_env_overrides_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path / "user"))
        (tmp_path / "user").mkdir()
        (tmp_path / "user" / "config.json").write_text(json.dumps({"provider": "openai"}))
        monkeypatch.setenv("XLI_PROVIDER", "mistral")

        config = Config.load(project_root=tmp_path)
        assert config.get("provider") == "mistral"

    def test_nested_json_flattens_to_dotted_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path / "user"))
        (tmp_path / "user").mkdir()
        (tmp_path / "user" / "config.json").write_text(
            json.dumps({"permissions": {"mode": "auto", "deny": ["x"]}})
        )
        config = Config.load(project_root=tmp_path, use_env=False)
        assert config.get("permissions.mode") == "auto"
        assert config.get("permissions.deny") == ["x"]

    def test_corrupt_json_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path / "user"))
        (tmp_path / "user").mkdir()
        (tmp_path / "user" / "config.json").write_text("{not json")

        config = Config.load(project_root=tmp_path, use_env=False)
        assert config.get("provider") == "mistral"  # defaults still work
        assert any("unreadable" in note for note in config.unknown_keys)

    def test_unknown_keys_preserved_and_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path / "user"))
        (tmp_path / "user").mkdir()
        (tmp_path / "user" / "config.json").write_text(json.dumps({"future.thing": 1}))

        config = Config.load(project_root=tmp_path, use_env=False)
        assert config.get("future.thing") == 1
        assert "future.thing" in config.unknown_keys

    def test_env_alias_mode_maps_to_permissions_mode(self, monkeypatch):
        monkeypatch.setenv("XLI_MODE", "readonly")
        config = Config.load(project_root=Path("/nonexistent"))
        assert config.get("permissions.mode") == "readonly"


class TestConfigPersistence:
    def test_save_writes_only_differences(self, tmp_path):
        config = Config()
        config.set("provider", "openai")
        path = config.save(tmp_path / "config.json")

        written = json.loads(path.read_text())
        assert written == {"provider": "openai"}

    def test_save_and_reload_roundtrip(self, tmp_path):
        config = Config()
        config.set("permissions.mode", "auto")
        config.set("permissions.deny", ["*.env"])
        path = config.save(tmp_path / "c.json")

        reloaded = Config()
        reloaded._merge_file(path)
        assert reloaded.get("permissions.mode") == "auto"
        assert reloaded.get("permissions.deny") == ["*.env"]

    def test_save_is_atomic_no_tmp_left_behind(self, tmp_path):
        config = Config()
        config.set("provider", "openai")
        config.save(tmp_path / "config.json")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_unset_restores_default(self):
        config = Config()
        config.set("provider", "openai")
        assert config.unset("provider") is True
        assert config.get("provider") == "mistral"

    def test_diff_from_defaults(self):
        config = Config()
        config.set("ui.language", "en")
        assert config.diff_from_defaults() == {"ui.language": "en"}

    def test_section(self):
        section = Config().section("sandbox")
        assert set(section) == {"sandbox.timeout", "sandbox.max_memory_mb", "sandbox.disable_network"}

    def test_describe_lists_everything(self):
        text = Config().describe()
        assert "provider =" in text and "permissions.mode =" in text

    def test_singleton(self):
        from xli.manager.config import get_config

        assert get_config() is get_config()
        reset_config()
        assert get_config() is not None


class TestConfigHelpers:
    def test_flatten_nest_roundtrip(self):
        flat = {"permissions.mode": "auto", "provider": "openai"}
        assert _flatten(_nest(flat)) == flat

    def test_flatten_keeps_known_nested_key(self):
        # `permissions.deny` is a list, not a section, even though it has a dot.
        assert _flatten({"permissions": {"deny": ["a"]}}) == {"permissions.deny": ["a"]}


# --------------------------------------------------------------------- targets
class TestKernelDiscovery:
    def test_discovers_core_modules(self):
        targets = discover_targets()
        assert len(targets) > 20
        assert all(t.suffix == ".py" for t in targets)
        assert "__init__.py" not in [t.name for t in targets]

    def test_filter_by_name(self):
        targets = discover_targets(["diff_engine", "vector_store"])
        assert {t.stem for t in targets} == {"diff_engine", "vector_store"}

    def test_filter_accepts_py_suffix(self):
        assert [t.stem for t in discover_targets(["diff_engine.py"])] == ["diff_engine"]

    def test_unknown_filter_yields_nothing(self):
        assert discover_targets(["does_not_exist"]) == []


# ------------------------------------------------------------------- preflight
class TestPreflight:
    def test_reports_each_component(self):
        result = preflight()
        names = {c.name for c in result.checks}
        assert {"cython", "compiler", "python-headers", "setuptools", "package"} <= names

    def test_report_mentions_fix_for_failures(self):
        result = preflight()
        text = result.report()
        for check in result.missing:
            assert check.name in text

    def test_ok_is_all_checks(self):
        result = preflight()
        assert result.ok is all(c.ok for c in result.checks)

    def test_to_dict_shape(self):
        payload = preflight().to_dict()
        assert isinstance(payload["ok"], bool)
        assert all("fix" in c for c in payload["checks"])

    def test_header_check_matches_sysconfig(self):
        import sysconfig

        result = preflight()
        header_check = next(c for c in result.checks if c.name == "python-headers")
        include = Path(sysconfig.get_paths()["include"])
        assert header_check.ok is (include / "Python.h").exists()


# ----------------------------------------------------------------------- build
class TestKernelBuild:
    def test_build_without_toolchain_fails_cleanly(self):
        """No crash, and the reason is actionable rather than a gcc traceback."""
        report = build(["diff_engine"])
        if report.preflight.ok:
            pytest.skip("toolchain is complete here; failure path not reachable")
        assert report.ok is False
        assert report.built == []
        joined = " ".join(f["error"] for f in report.failed)
        assert "toolchain incomplete" in joined

    def test_status_reports_source_only_when_nothing_built(self):
        info = status()
        assert info["total"] > 20
        assert info["compiled"] == 0
        assert info["source_only"] == info["total"]
        assert all(m["state"] == "source-only" for m in info["modules"])

    def test_status_shape(self):
        info = status()
        assert info["python_tag"] == _python_tag()
        assert set(info["modules"][0]) == {"module", "state", "sha256"}

    def test_clean_is_safe_when_nothing_built(self):
        result = clean()
        assert "removed" in result
        assert not CKERNEL_DIR.exists()


class TestIsUsable:
    def test_unknown_module_not_usable(self):
        assert is_usable("diff_engine", Manifest()) is False

    def test_wrong_interpreter_rejected(self, tmp_path):
        manifest = Manifest(
            modules={
                "diff_engine": {
                    "sha256": source_hash(discover_targets(["diff_engine"])[0]),
                    "python": "cp99-doesnotexist",
                }
            }
        )
        assert is_usable("diff_engine", manifest) is False

    def test_missing_source_rejected(self):
        manifest = Manifest(
            modules={"no_such_module": {"sha256": "x", "python": _python_tag()}}
        )
        assert is_usable("no_such_module", manifest) is False

    def test_matching_manifest_but_no_so_is_not_usable(self):
        """The .so must actually exist — a manifest entry alone proves nothing."""
        source = discover_targets(["diff_engine"])[0]
        manifest = Manifest(
            modules={"diff_engine": {"sha256": source_hash(source), "python": _python_tag()}}
        )
        assert is_usable("diff_engine", manifest) is False


class TestManifest:
    def test_load_missing_returns_empty(self):
        manifest = Manifest.load()
        assert manifest.modules in ({}, manifest.modules)

    def test_roundtrip(self, tmp_path, monkeypatch):
        import xli.manager.kernel_build as kb

        monkeypatch.setattr(kb, "CKERNEL_DIR", tmp_path)
        monkeypatch.setattr(kb, "MANIFEST", tmp_path / "manifest.json")

        manifest = Manifest(built_at="now", python="cp311", cython="3.3", modules={"a": {"sha256": "x"}})
        manifest.save()
        reloaded = Manifest.load()
        assert reloaded.built_at == "now"
        assert reloaded.modules == {"a": {"sha256": "x"}}

    def test_corrupt_manifest_returns_empty(self, tmp_path, monkeypatch):
        import xli.manager.kernel_build as kb

        monkeypatch.setattr(kb, "CKERNEL_DIR", tmp_path)
        monkeypatch.setattr(kb, "MANIFEST", tmp_path / "manifest.json")
        (tmp_path / "manifest.json").write_text("{broken")

        assert Manifest.load().modules == {}


# ------------------------------------------------------------------------ accel
class TestAccelHook:
    def test_nothing_accelerated_when_kernel_unbuilt(self):
        assert accel.accelerated() == []

    def test_auto_install_noop_without_kernel(self):
        assert accel.auto_install() is False
        assert accel.is_installed() is False

    def test_install_and_uninstall_are_symmetric(self):
        before = len(sys.meta_path)
        accel.install()
        assert accel.is_installed() is True
        assert len(sys.meta_path) == before + 1
        accel.uninstall()
        assert accel.is_installed() is False
        assert len(sys.meta_path) == before

    def test_install_is_idempotent(self):
        accel.install()
        count = len(sys.meta_path)
        accel.install()
        assert len(sys.meta_path) == count
        accel.uninstall()

    def test_hook_ignores_unrelated_modules(self):
        finder = accel.KernelFinder()
        assert finder.find_spec("os.path") is None
        assert finder.find_spec("json") is None

    def test_hook_falls_back_when_module_not_compiled(self):
        """The safety property: no compiled artefact means pure Python wins."""
        finder = accel.KernelFinder()
        assert finder.find_spec("xli.core.diff_engine") is None

    def test_hook_does_not_hijack_stale_manifest(self, monkeypatch, tmp_path):
        """A manifest claiming a build must not win over a missing .so."""
        import xli.manager.kernel_build as kb

        source = discover_targets(["diff_engine"])[0]
        monkeypatch.setattr(kb, "CKERNEL_DIR", tmp_path)
        monkeypatch.setattr(
            kb,
            "MANIFEST",
            tmp_path / "manifest.json",
        )
        Manifest(
            modules={"diff_engine": {"sha256": source_hash(source), "python": _python_tag()}}
        ).save()

        finder = accel.KernelFinder()
        assert finder.find_spec("xli.core.diff_engine") is None

    def test_core_modules_still_import_without_kernel(self):
        """Regression guard: the hook must never break normal imports."""
        import importlib

        module = importlib.import_module("xli.core.diff_engine")
        assert hasattr(module, "DiffEngine")

    def test_pure_python_module_behaves(self):
        from xli.core.diff_engine import DiffEngine

        engine = DiffEngine()
        diff = engine.unified_diff("a\nb\n", "a\nc\n", "f.py")
        assert "-b" in diff and "+c" in diff
