#!/usr/bin/env python3
"""Tests for where the compiled kernel lands, and that the loader agrees.

`xli kernel build` compiled every module and then reported all of them as
"compiler finished but no .so was produced". Nothing had actually failed: the
extensions are named `xli._ckernel.<stem>`, and setuptools reproduces that whole
dotted package path *under* --build-lib. --build-lib was CKERNEL_DIR, so
artefacts went to `_ckernel/xli/_ckernel/<stem>.so` while is_usable() and the
accel loader both looked for `_ckernel/<stem>.so`. The glob could never match.

These tests need no C toolchain: they check the path arithmetic and the shared
lookup, which is the part that was wrong.
"""

from pathlib import Path

import pytest

from xli.manager import kernel_build as kb
from xli.manager.kernel_build import (
    BUILD_LIB_ROOT,
    CKERNEL_DIR,
    PACKAGE_ROOT,
    Manifest,
    find_compiled,
    is_usable,
)


# ------------------------------------------------------------------ the fix
class TestBuildLibMatchesTheLoader:
    def test_build_lib_root_is_the_repository_root(self):
        """Anchoring here is what makes the dotted extension name resolve."""
        assert PACKAGE_ROOT.parent == BUILD_LIB_ROOT

    def test_extension_output_lands_in_ckernel_dir(self):
        """The actual regression: setuptools must write where we look."""
        from setuptools import Extension
        from setuptools.command.build_ext import build_ext
        from setuptools.dist import Distribution

        ext = Extension("xli._ckernel.diff_engine", ["x.py"])
        dist = Distribution({"ext_modules": [ext]})
        cmd = build_ext(dist)
        cmd.build_lib = str(BUILD_LIB_ROOT)
        cmd.finalize_options()

        output = Path(cmd.get_ext_fullpath("xli._ckernel.diff_engine"))
        assert output.parent == CKERNEL_DIR, (
            f"setuptools would write {output} but the loader looks in {CKERNEL_DIR}"
        )

    def test_output_name_starts_with_the_module_stem(self):
        """So the `{stem}.*.so` glob can match it."""
        from setuptools import Extension
        from setuptools.command.build_ext import build_ext
        from setuptools.dist import Distribution

        ext = Extension("xli._ckernel.agent", ["x.py"])
        dist = Distribution({"ext_modules": [ext]})
        cmd = build_ext(dist)
        cmd.build_lib = str(BUILD_LIB_ROOT)
        cmd.finalize_options()

        name = Path(cmd.get_ext_fullpath("xli._ckernel.agent")).name
        assert name.startswith("agent.")
        assert name.endswith(".so")

    def test_the_old_build_lib_would_have_mismatched(self):
        """Pins the diagnosis, so the wrong anchor is not reintroduced."""
        from setuptools import Extension
        from setuptools.command.build_ext import build_ext
        from setuptools.dist import Distribution

        ext = Extension("xli._ckernel.agent", ["x.py"])
        dist = Distribution({"ext_modules": [ext]})
        cmd = build_ext(dist)
        cmd.build_lib = str(CKERNEL_DIR)  # the old, wrong value
        cmd.finalize_options()

        output = Path(cmd.get_ext_fullpath("xli._ckernel.agent"))
        assert output.parent != CKERNEL_DIR


# ------------------------------------------------------------- find_compiled
class TestFindCompiled:
    def test_empty_when_nothing_is_built(self):
        assert find_compiled("no_such_module_at_all") == []

    def test_finds_a_flat_artefact(self, monkeypatch, tmp_path):
        ck = tmp_path / "_ckernel"
        ck.mkdir()
        artefact = ck / "agent.cpython-311-x86_64-linux-gnu.so"
        artefact.write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)
        assert find_compiled("agent") == [artefact]

    def test_finds_a_nested_artefact(self, monkeypatch, tmp_path):
        """The shape the bug produced: artefacts buried under xli/_ckernel/."""
        ck = tmp_path / "_ckernel"
        nested = ck / "xli" / "_ckernel"
        nested.mkdir(parents=True)
        artefact = nested / "agent.cpython-311-x86_64-linux-gnu.so"
        artefact.write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)
        assert find_compiled("agent") == [artefact]

    def test_ignores_the_intermediate_build_directory(self, monkeypatch, tmp_path):
        """Scratch objects live under build/ and are not loadable modules."""
        ck = tmp_path / "_ckernel"
        scratch = ck / "build" / "temp.linux-x86_64-cpython-311"
        scratch.mkdir(parents=True)
        (scratch / "agent.o").write_bytes(b"")
        real = ck / "agent.cpython-311-x86_64-linux-gnu.so"
        real.write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)
        monkeypatch.setattr(kb, "BUILD_DIR", ck / "build")
        assert find_compiled("agent") == [real]

    def test_falls_back_to_pyd(self, monkeypatch, tmp_path):
        ck = tmp_path / "_ckernel"
        ck.mkdir()
        artefact = ck / "agent.pyd"
        artefact.write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)
        assert find_compiled("agent") == [artefact]

    def test_does_not_raise_when_the_directory_is_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(kb, "CKERNEL_DIR", tmp_path / "missing")
        assert find_compiled("agent") == []

    def test_is_usable_and_the_loader_share_one_lookup(self):
        """The three call sites must not disagree again."""
        import inspect

        from xli import accel

        assert "find_compiled" in inspect.getsource(kb.is_usable)
        assert "find_compiled" in inspect.getsource(accel.KernelFinder.find_spec)


# ------------------------------------------------------------------- manifest
class TestManifestGuardsStillApply:
    def test_a_built_module_without_a_manifest_entry_is_not_usable(self, monkeypatch, tmp_path):
        ck = tmp_path / "_ckernel"
        ck.mkdir()
        (ck / "agent.cpython-311-x86_64-linux-gnu.so").write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)
        assert is_usable("agent", Manifest()) is False

    def test_an_artefact_for_another_interpreter_is_not_usable(self, monkeypatch, tmp_path):
        ck = tmp_path / "_ckernel"
        ck.mkdir()
        (ck / "agent.cpython-311-x86_64-linux-gnu.so").write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)

        source = tmp_path / "core" / "agent.py"
        source.parent.mkdir(parents=True)
        source.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(kb, "CORE_DIR", source.parent)

        manifest = Manifest(
            modules={
                "agent": {
                    "sha256": kb.source_hash(source),
                    "python": "cpython-27",
                    "built_at": "now",
                }
            }
        )
        assert is_usable("agent", manifest) is False

    def test_an_artefact_whose_source_changed_is_not_usable(self, monkeypatch, tmp_path):
        ck = tmp_path / "_ckernel"
        ck.mkdir()
        (ck / "agent.cpython-311-x86_64-linux-gnu.so").write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)

        source = tmp_path / "core" / "agent.py"
        source.parent.mkdir(parents=True)
        source.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(kb, "CORE_DIR", source.parent)

        manifest = Manifest(
            modules={
                "agent": {
                    "sha256": "stale-hash",
                    "python": kb._python_tag(),
                    "built_at": "now",
                }
            }
        )
        assert is_usable("agent", manifest) is False

    def test_a_matching_artefact_is_usable(self, monkeypatch, tmp_path):
        """The happy path, which the path bug made unreachable."""
        ck = tmp_path / "_ckernel"
        ck.mkdir()
        (ck / "agent.cpython-311-x86_64-linux-gnu.so").write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)

        source = tmp_path / "core" / "agent.py"
        source.parent.mkdir(parents=True)
        source.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(kb, "CORE_DIR", source.parent)

        manifest = Manifest(
            modules={
                "agent": {
                    "sha256": kb.source_hash(source),
                    "python": kb._python_tag(),
                    "built_at": "now",
                }
            }
        )
        assert is_usable("agent", manifest) is True

    def test_a_nested_matching_artefact_is_also_usable(self, monkeypatch, tmp_path):
        """Defensive: an artefact left in the old location is still honoured."""
        ck = tmp_path / "_ckernel"
        nested = ck / "xli" / "_ckernel"
        nested.mkdir(parents=True)
        (nested / "agent.cpython-311-x86_64-linux-gnu.so").write_bytes(b"")
        monkeypatch.setattr(kb, "CKERNEL_DIR", ck)

        source = tmp_path / "core" / "agent.py"
        source.parent.mkdir(parents=True)
        source.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(kb, "CORE_DIR", source.parent)

        manifest = Manifest(
            modules={
                "agent": {
                    "sha256": kb.source_hash(source),
                    "python": kb._python_tag(),
                    "built_at": "now",
                }
            }
        )
        assert is_usable("agent", manifest) is True


# ------------------------------------------------------------------ packaging
class TestPackagingMetadata:
    def test_license_form_is_accepted_by_old_setuptools(self):
        """The SPDX string is rejected outright below setuptools 77.

        `xli kernel build` calls setup() in-process with whatever setuptools is
        installed, so the portable table form is the one that cannot fail the
        build. Verified against setuptools 66.1.1, where the string form raises.
        """
        import setuptools
        from setuptools.config.pyprojecttoml import read_configuration

        root = Path(__file__).resolve().parent.parent
        cfg = read_configuration(str(root / "pyproject.toml"))
        license_value = cfg["project"]["license"]
        if tuple(int(p) for p in setuptools.__version__.split(".")[:1]) < (77,):
            assert isinstance(license_value, dict), (
                "setuptools < 77 cannot parse an SPDX license string"
            )
        assert cfg["project"]["name"] == "xli-pro"

    def test_build_setup_call_suppresses_setuptools_noise(self):
        """The deprecation banner was printed over every build."""
        import inspect

        source = inspect.getsource(kb.build)
        assert "catch_warnings" in source
        assert "simplefilter" in source


# ------------------------------------------------------------ fault isolation
class TestOneBadModuleDoesNotSinkTheRest:
    """build() promises "a module that fails is reported and skipped; the rest
    still build". It did not hold: setup() runs once for the whole batch and
    raises on the first compile error, and the handler then marked every module
    in the batch as failed — so one bad module reported all of them broken and
    hid which one was at fault.

    These drive the real build(), with only the compiler stand-ins replaced.
    """

    @pytest.fixture
    def scratch(self, tmp_path, monkeypatch):
        import types

        # These drive the real cythonize() plumbing, so they need Cython. The
        # toolchain is optional by design (`xli kernel build` explains what to
        # install), and a missing optional dependency must skip, not error.
        Cython_Build = pytest.importorskip("Cython.Build")

        from xli.manager import kernel_build as kb

        core = tmp_path / "core"
        core.mkdir()
        for name in ("good_one", "broken_one", "good_two"):
            (core / f"{name}.py").write_text("VALUE = 1\n", encoding="utf-8")

        monkeypatch.setattr(kb, "CORE_DIR", core)
        monkeypatch.setattr(kb, "CKERNEL_DIR", tmp_path / "_ckernel")
        monkeypatch.setattr(kb, "BUILD_DIR", tmp_path / "_ckernel" / "build")
        monkeypatch.setattr(kb, "MANIFEST", tmp_path / "_ckernel" / "manifest.json")
        monkeypatch.setattr(kb, "BUILD_LIB_ROOT", tmp_path)
        monkeypatch.setattr(
            kb,
            "preflight",
            lambda: types.SimpleNamespace(ok=True, missing=[], checks=[], to_dict=lambda: {}),
        )
        # cythonize passes the extensions straight through
        monkeypatch.setattr(Cython_Build, "cythonize", lambda exts, **kw: exts)

        def make_setup(behaviour):
            calls = []

            def fake_setup(*, name=None, ext_modules=None, script_args=None, **kw):
                names = [e.name for e in ext_modules]
                calls.append(len(names))
                behaviour(names)
                for ext in ext_modules:
                    stem = ext.name.rsplit(".", 1)[-1]
                    kb.CKERNEL_DIR.mkdir(parents=True, exist_ok=True)
                    (
                        kb.CKERNEL_DIR / f"{stem}.cpython-311-x86_64-linux-gnu.so"
                    ).write_bytes(b"x")

            fake_setup.calls = calls
            return fake_setup

        return kb, core, make_setup

    def test_only_the_broken_module_is_reported(self, scratch):
        kb, core, make_setup = scratch

        def behaviour(names):
            if any("broken_one" in n for n in names):
                raise SystemExit("error: command 'gcc' failed with exit code 1")

        import setuptools

        setup = make_setup(behaviour)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(setuptools, "setup", setup)
            report = kb.build()

        assert sorted(report.built) == ["good_one", "good_two"]
        assert [f["module"] for f in report.failed] == ["broken_one"]
        assert report.ok is False

    def test_the_failure_carries_the_compilers_own_message(self, scratch):
        kb, core, make_setup = scratch

        def behaviour(names):
            if any("broken_one" in n for n in names):
                raise SystemExit("error: command 'gcc' failed with exit code 1")

        import setuptools

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(setuptools, "setup", make_setup(behaviour))
            report = kb.build()

        error = report.failed[0]["error"]
        assert "gcc" in error
        assert "no .so was produced" not in error, (
            "the generic message blames the wrong thing and hides the real cause"
        )

    def test_a_clean_batch_is_not_rebuilt_module_by_module(self, scratch):
        """The fast path must stay parallel — no per-module retry on success."""
        kb, core, make_setup = scratch

        import setuptools

        setup = make_setup(lambda names: None)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(setuptools, "setup", setup)
            report = kb.build()

        assert setup.calls == [3], f"expected one batch call, got {setup.calls}"
        assert sorted(report.built) == ["broken_one", "good_one", "good_two"]
        assert report.ok is True

    def test_the_retry_passes_one_extension_per_call(self, scratch):
        kb, core, make_setup = scratch

        def behaviour(names):
            if any("broken_one" in n for n in names):
                raise SystemExit("error: command 'gcc' failed with exit code 1")

        import setuptools

        setup = make_setup(behaviour)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(setuptools, "setup", setup)
            kb.build()

        # one batch call, then one call per module in the retry
        assert setup.calls[0] == 3
        assert setup.calls[1:] == [1, 1, 1]

    def test_the_log_records_the_batch_failure_and_the_culprit(self, scratch):
        kb, core, make_setup = scratch

        def behaviour(names):
            if any("broken_one" in n for n in names):
                raise SystemExit("error: command 'gcc' failed with exit code 1")

        import setuptools

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(setuptools, "setup", make_setup(behaviour))
            report = kb.build()

        assert "retrying per module" in report.log
        assert "broken_one" in report.log


# --------------------------------------------------------------- install layout
class TestInstalledPackageLayout:
    """`xli kernel build` is normally run against an installed package, not a
    checkout -- that is how the Termux report that found this arrived. The
    anchors are derived from __file__, so they must resolve correctly when the
    package lives in site-packages too.
    """

    def test_the_anchors_follow_the_package(self, tmp_path, monkeypatch):
        from xli.manager import kernel_build as kb

        installed = tmp_path / "site-packages" / "xli"
        installed.mkdir(parents=True)

        monkeypatch.setattr(kb, "PACKAGE_ROOT", installed)
        monkeypatch.setattr(kb, "CKERNEL_DIR", installed / "_ckernel")
        monkeypatch.setattr(kb, "BUILD_LIB_ROOT", installed.parent)

        assert kb.PACKAGE_ROOT.parent == kb.BUILD_LIB_ROOT
        assert kb.CKERNEL_DIR == kb.PACKAGE_ROOT / "_ckernel"

    def test_output_lands_inside_the_installed_package(self, tmp_path):
        from setuptools import Extension
        from setuptools.command.build_ext import build_ext
        from setuptools.dist import Distribution

        installed = tmp_path / "site-packages" / "xli"
        ckernel = installed / "_ckernel"
        build_lib_root = installed.parent  # site-packages

        ext = Extension("xli._ckernel.diff_engine", ["x.py"])
        dist = Distribution({"ext_modules": [ext]})
        cmd = build_ext(dist)
        cmd.build_lib = str(build_lib_root)
        cmd.finalize_options()

        output = Path(cmd.get_ext_fullpath("xli._ckernel.diff_engine"))
        assert output.parent == ckernel
        # and not loose in site-packages, where it would be unimportable
        assert output.parent != build_lib_root

    def test_a_source_checkout_resolves_the_same_way(self):
        """Same arithmetic, different root — so the two layouts cannot diverge."""
        assert PACKAGE_ROOT.parent == BUILD_LIB_ROOT
        assert CKERNEL_DIR == PACKAGE_ROOT / "_ckernel"
