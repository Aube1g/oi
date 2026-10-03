"""Tests for the sub-agent subsystem.

The point of a sub-agent is that it is *narrower* than the main agent, so the
tests concentrate on the boundaries: what a spec is allowed to say, what the
registry will load, and what `verify` refuses to pass.
"""

import json

import pytest

from xli.agents import AgentRegistry, AgentSpec, get_registry, reset_registry
from xli.agents.builtin import BUILTIN_NAMES, BUILTIN_SPECS


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Every test gets its own ~/.xli and its own project directory."""
    monkeypatch.setenv("XLI_CONFIG_DIR", str(tmp_path / "home"))
    reset_registry()
    yield tmp_path
    reset_registry()


# --------------------------------------------------------------------- the spec
class TestSpecValidation:
    def test_a_good_spec_has_no_problems(self):
        spec = AgentSpec(
            name="reviewer",
            description="Reads code and reports defects.",
            role="You review code.",
            tools=["read", "grep"],
            mode="readonly",
        )
        assert spec.validate() == []

    @pytest.mark.parametrize(
        "name",
        # Each of these violates one rule: empty, single char, uppercase,
        # leading digit, a space, too long, and a leading underscore.
        ["", "R", "UPPER", "Upper", "1leading", "has space", "x" * 41, "_leading"],
    )
    def test_name_shape_is_enforced(self, name):
        spec = AgentSpec(name=name, description="d", role="r", tools=["read"])
        assert any("name" in problem for problem in spec.validate())

    @pytest.mark.parametrize("name", ["ab", "reviewer", "under_score-ok2", "a1", "x" * 40])
    def test_a_valid_name_is_accepted(self, name):
        spec = AgentSpec(name=name, description="d", role="r", tools=["read"])
        assert not any("name" in problem for problem in spec.validate())

    def test_empty_description_is_a_problem(self):
        spec = AgentSpec(name="ok-name", description="  ", role="r", tools=["read"])
        assert any("description" in p for p in spec.validate())

    def test_empty_role_is_a_problem(self):
        spec = AgentSpec(name="ok-name", description="d", role="", tools=["read"])
        assert any("role" in p for p in spec.validate())

    def test_empty_tools_is_a_problem(self):
        # A sub-agent with no tools cannot act, which is almost always a typo.
        spec = AgentSpec(name="ok-name", description="d", role="r", tools=[])
        assert any("tools" in p for p in spec.validate())

    def test_auto_mode_is_refused(self):
        # A delegated agent must not be able to widen its own authority.
        spec = AgentSpec(name="ok-name", description="d", role="r", tools=["read"], mode="auto")
        assert any("mode" in p for p in spec.validate())

    def test_max_steps_bounds(self):
        for bad in (0, -1, 101):
            spec = AgentSpec(
                name="ok-name", description="d", role="r", tools=["read"], max_steps=bad
            )
            assert any("max_steps" in p for p in spec.validate())

    def test_temperature_bounds(self):
        for bad in (-0.1, 2.1):
            spec = AgentSpec(
                name="ok-name", description="d", role="r", tools=["read"], temperature=bad
            )
            assert any("temperature" in p for p in spec.validate())

    def test_unknown_tool_is_reported_when_catalogue_given(self):
        spec = AgentSpec(
            name="ok-name", description="d", role="r", tools=["read", "teleport"]
        )
        problems = spec.validate(["read", "write"])
        assert any("teleport" in p for p in problems)

    def test_known_tools_pass_against_the_catalogue(self):
        spec = AgentSpec(name="ok-name", description="d", role="r", tools=["read"])
        assert spec.validate(["read", "write"]) == []


class TestSpecRoundTrip:
    def test_to_dict_omits_builtin(self):
        # `builtin` is derived from where a spec was loaded, not stored.
        spec = AgentSpec(name="ok-name", description="d", role="r", tools=["read"])
        spec.builtin = True
        assert "builtin" not in spec.to_dict()

    def test_round_trip_preserves_fields(self):
        spec = AgentSpec(
            name="ok-name",
            description="d",
            role="r",
            tools=["read", "grep"],
            mode="readonly",
            max_steps=7,
            temperature=0.1,
            tags=["review"],
        )
        back = AgentSpec.from_dict(spec.to_dict())
        assert back.name == spec.name
        assert back.tools == spec.tools
        assert back.mode == spec.mode
        assert back.max_steps == spec.max_steps
        assert back.tags == spec.tags

    def test_unknown_field_is_rejected(self):
        # A typo in a security-relevant field must not pass silently.
        with pytest.raises(ValueError, match="unknown field"):
            AgentSpec.from_dict({"name": "ok-name", "mood": "readonly"})

    def test_tools_may_be_a_comma_string(self):
        spec = AgentSpec.from_dict({"name": "ok-name", "tools": "read, grep"})
        assert spec.tools == ["read", "grep"]


class TestSystemPrompt:
    def test_states_the_tool_boundary(self):
        spec = AgentSpec(
            name="reviewer", description="d", role="You review.", tools=["read", "grep"]
        )
        prompt = spec.system_prompt()
        assert "read, grep" in prompt
        assert "You review." in prompt

    def test_states_the_permission_mode(self):
        spec = AgentSpec(
            name="reviewer", description="d", role="r", tools=["read"], mode="readonly"
        )
        assert "readonly" in spec.system_prompt()

    def test_states_the_step_budget(self):
        spec = AgentSpec(
            name="reviewer", description="d", role="r", tools=["read"], max_steps=5
        )
        assert "5 steps" in spec.system_prompt()

    def test_includes_project_when_given(self):
        spec = AgentSpec(name="reviewer", description="d", role="r", tools=["read"])
        assert "/srv/demo" in spec.system_prompt(project="/srv/demo")


# ------------------------------------------------------------------- built-ins
class TestBuiltins:
    def test_every_builtin_is_valid(self):
        for spec in BUILTIN_SPECS:
            assert spec.validate() == [], f"{spec.name}: {spec.validate()}"

    def test_builtin_names_are_unique(self):
        assert len(BUILTIN_NAMES) == len(set(BUILTIN_NAMES))

    def test_names_match_the_specs(self):
        assert tuple(spec.name for spec in BUILTIN_SPECS) == BUILTIN_NAMES

    def test_no_builtin_runs_in_auto_mode(self):
        for spec in BUILTIN_SPECS:
            assert spec.mode in ("readonly", "confirm"), spec.name

    def test_builtin_tools_are_a_subset_of_the_real_catalogue(self):
        from xli.tools.registry import default_registry

        available = set(default_registry().names(enabled_only=False))
        for spec in BUILTIN_SPECS:
            assert set(spec.tools) <= available, f"{spec.name} names a tool that does not exist"


# ------------------------------------------------------------------- registry
class TestRegistryLoading:
    def test_builtins_are_present(self):
        registry = AgentRegistry()
        for name in BUILTIN_NAMES:
            assert name in registry

    def test_builtin_specs_are_marked(self):
        assert all(spec.builtin for spec in AgentRegistry().builtins())

    def test_len_counts_builtins(self):
        assert len(AgentRegistry()) == len(BUILTIN_SPECS)

    def test_malformed_json_is_reported_not_fatal(self, tmp_path):
        # One bad hand-edit must not take the whole registry down.
        directory = tmp_path / "home" / "agents"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{ not json", encoding="utf-8")

        registry = AgentRegistry()
        assert "broken" in registry.problems()
        assert len(registry) == len(BUILTIN_SPECS)   # built-ins still load

    def test_non_object_json_is_reported(self, tmp_path):
        directory = tmp_path / "home" / "agents"
        directory.mkdir(parents=True)
        (directory / "list.json").write_text("[1, 2]", encoding="utf-8")
        assert "must contain a JSON object" in AgentRegistry().problems()["list"]

    def test_unknown_field_is_reported(self, tmp_path):
        directory = tmp_path / "home" / "agents"
        directory.mkdir(parents=True)
        (directory / "odd.json").write_text(
            json.dumps({"name": "odd", "mood": "readonly"}), encoding="utf-8"
        )
        assert "unknown field" in AgentRegistry().problems()["odd"]

    def test_filename_must_match_the_declared_name(self, tmp_path):
        directory = tmp_path / "home" / "agents"
        directory.mkdir(parents=True)
        (directory / "alpha.json").write_text(
            json.dumps({"name": "beta", "role": "r", "tools": ["read"]}), encoding="utf-8"
        )
        problems = AgentRegistry().problems()
        assert "alpha" in problems and "beta" in problems["alpha"]


class TestRegistryPrecedence:
    def _write(self, directory, name, **fields):
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"name": name, "description": "d", "role": "r", "tools": ["read"]}
        payload.update(fields)
        (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_user_spec_is_loaded(self, tmp_path):
        self._write(tmp_path / "home" / "agents", "mine")
        assert "mine" in AgentRegistry()

    def test_project_overrides_user(self, tmp_path):
        self._write(tmp_path / "home" / "agents", "shared", role="from user")
        project = tmp_path / "proj"
        self._write(project / ".xli" / "agents", "shared", role="from project")

        registry = AgentRegistry(project_root=project)
        assert registry.get("shared").role == "from project"

    def test_project_overrides_builtin(self, tmp_path):
        project = tmp_path / "proj"
        self._write(project / ".xli" / "agents", "reviewer", role="project reviewer")

        spec = AgentRegistry(project_root=project).get("reviewer")
        assert spec.role == "project reviewer"
        assert spec.builtin is False

    def test_a_different_project_does_not_see_the_first(self, tmp_path):
        first = tmp_path / "one"
        self._write(first / ".xli" / "agents", "only-in-one")
        second = tmp_path / "two"
        second.mkdir()

        assert "only-in-one" in AgentRegistry(project_root=first)
        assert "only-in-one" not in AgentRegistry(project_root=second)


class TestRegistryMutating:
    def _spec(self, name="custom-one", **fields):
        payload = {"description": "d", "role": "r", "tools": ["read"]}
        payload.update(fields)
        return AgentSpec(name=name, **payload)

    def test_save_then_reload(self):
        registry = AgentRegistry()
        path = registry.save(self._spec())
        assert path.is_file()

        reloaded = AgentRegistry().get("custom-one")
        assert reloaded is not None
        assert reloaded.tools == ["read"]
        assert reloaded.builtin is False

    def test_delete_removes_it(self):
        registry = AgentRegistry()
        registry.save(self._spec())
        assert registry.delete("custom-one")
        assert "custom-one" not in AgentRegistry()

    def test_delete_of_a_builtin_is_refused(self):
        assert AgentRegistry().delete("reviewer") is False

    def test_delete_of_an_unknown_name_is_false(self):
        assert AgentRegistry().delete("nope") is False

    def test_saving_over_a_builtin_requires_project_scope(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        registry = AgentRegistry(project_root=project)

        spec = self._spec("reviewer")
        spec.builtin = True
        with pytest.raises(ValueError, match="built-in"):
            registry.save(spec)

        # The same spec is fine when scoped to the project.
        assert registry.save(spec, project=True).is_file()


class TestRegistryVerify:
    def test_clean_registry_reports_nothing(self):
        from xli.tools.registry import default_registry

        available = default_registry().names(enabled_only=False)
        assert AgentRegistry().verify(available) == {}

    def test_a_bad_spec_is_reported(self, tmp_path):
        directory = tmp_path / "home" / "agents"
        directory.mkdir(parents=True)
        (directory / "bad.json").write_text(
            json.dumps({"name": "bad", "description": "", "role": "", "tools": []}),
            encoding="utf-8",
        )
        report = AgentRegistry().verify(["read"])
        assert "bad" in report
        assert len(report["bad"]) >= 3

    def test_a_load_failure_appears_in_the_report(self, tmp_path):
        directory = tmp_path / "home" / "agents"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{", encoding="utf-8")
        assert "broken" in AgentRegistry().verify()

    def test_summary_shape(self):
        row = AgentRegistry().summary()[0]
        assert set(row) == {
            "name", "description", "tools", "mode", "max_steps", "builtin", "tags",
        }


# ---------------------------------------------------------------- shared handle
class TestGetRegistry:
    def test_same_root_returns_the_same_object(self, tmp_path):
        assert get_registry(tmp_path) is get_registry(tmp_path)

    def test_a_different_root_builds_a_fresh_one(self, tmp_path):
        first = get_registry(tmp_path / "one")
        second = get_registry(tmp_path / "two")
        assert first is not second

    def test_force_rebuilds(self, tmp_path):
        first = get_registry(tmp_path)
        assert get_registry(tmp_path, force=True) is not first

    def test_reset_clears_it(self, tmp_path):
        first = get_registry(tmp_path)
        reset_registry()
        assert get_registry(tmp_path) is not first


# ------------------------------------------------------- CLI action consistency
class TestCliActionConsistency:
    """`xli tools list` worked while `xli skills list` did not.

    The same shape of command behaved two ways, which is the kind of
    inconsistency that makes a CLI feel untrustworthy even when every command
    individually works.
    """

    @pytest.mark.parametrize("command", ["tools", "skills", "mcp", "agents"])
    def test_the_bare_command_parses(self, command):
        from xli.cli import build_parser

        args = build_parser().parse_args([command] if command != "agents" else [command, "list"])
        assert args.func is not None

    @pytest.mark.parametrize("command", ["tools", "skills", "mcp"])
    def test_the_list_action_parses(self, command):
        from xli.cli import build_parser

        args = build_parser().parse_args([command, "list"])
        assert args.action == "list"

    def test_an_unknown_action_is_refused(self):
        from xli.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["skills", "frobnicate"])
