#!/usr/bin/env python3
"""
XLI Kernel Build — optional Cython acceleration for xli.core.

`xli kernel build` compiles the core modules into C extensions under
`xli/_ckernel/`. Nothing about the rest of the system depends on that having
happened: the import hook in `xli.accel` prefers a compiled module when one is
present *and* still matches its source, and otherwise transparently uses the
pure-Python module. A stale or half-built kernel can therefore never make the
agent misbehave — it just runs slower.

Why not just always compile?
  * a checkout must be runnable with no toolchain at all
  * Cython's pure-Python mode rejects some valid Python, so a module that
    fails to compile must not take the others down with it

Preflight
---------
`preflight()` checks the toolchain before any work starts and returns a
structured verdict with a copy-pasteable fix. Compiling without headers is the
single most common failure (`Python.h: No such file or directory`) and its
error message is useless unless you already know what it means, so we detect it
up front and say exactly which package to install.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import sysconfig
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from collections.abc import Sequence

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = PACKAGE_ROOT / "core"
CKERNEL_DIR = PACKAGE_ROOT / "_ckernel"
MANIFEST = CKERNEL_DIR / "manifest.json"
BUILD_DIR = CKERNEL_DIR / "build"

#: What to pass setuptools as --build-lib.
#:
#: Extensions are named `xli._ckernel.<stem>`, and setuptools reproduces the
#: whole dotted package path *under* --build-lib. Pointing --build-lib at
#: CKERNEL_DIR therefore wrote to `_ckernel/xli/_ckernel/<stem>.so`, while
#: is_usable() and the accel loader both look for `_ckernel/<stem>.so`. The
#: compile succeeded and the artefact simply landed somewhere nothing reads, so
#: every module was reported as "compiler finished but no .so was produced".
#: Anchoring at the repository root makes the two agree.
BUILD_LIB_ROOT = PACKAGE_ROOT.parent

#: Modules that must never be compiled: they are imported during interpreter
#: startup or by the build itself, and a bad .so there is unrecoverable.
NEVER_COMPILE = frozenset({"__init__"})


# ------------------------------------------------------------------ diagnostics
@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "fix": self.fix}


@dataclass(slots=True)
class Preflight:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def missing(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def report(self) -> str:
        lines = []
        for check in self.checks:
            mark = "ok" if check.ok else "MISSING"
            lines.append(f"  [{mark:>7}] {check.name}: {check.detail}")
            if not check.ok and check.fix:
                lines.append(f"            fix: {check.fix}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [c.to_dict() for c in self.checks]}


def _package_manager_hint() -> str:
    """Best guess at the command that installs CPython headers on this box."""
    if shutil.which("apt-get"):
        return f"sudo apt-get install -y python{sys.version_info.major}.{sys.version_info.minor}-dev"
    if shutil.which("dnf"):
        return "sudo dnf install -y python3-devel"
    if shutil.which("yum"):
        return "sudo yum install -y python3-devel"
    if shutil.which("pacman"):
        return "sudo pacman -S --needed python"
    if shutil.which("brew"):
        return "brew install python"
    if shutil.which("zypper"):
        return "sudo zypper install -y python3-devel"
    return "install the python3 development headers for your platform"


def preflight() -> Preflight:
    """Verify the toolchain can actually produce an extension module."""
    result = Preflight()

    try:
        import Cython

        result.checks.append(
            Check("cython", True, f"Cython {Cython.__version__}", "pip install Cython")
        )
    except ImportError:
        result.checks.append(
            Check("cython", False, "Cython is not installed", "pip install Cython")
        )

    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc:
        result.checks.append(Check("compiler", True, cc, _package_manager_hint()))
    else:
        result.checks.append(
            Check("compiler", False, "no C compiler on PATH", _package_manager_hint())
        )

    include = Path(sysconfig.get_paths()["include"])
    header = include / "Python.h"
    if header.exists():
        result.checks.append(Check("python-headers", True, str(header)))
    else:
        result.checks.append(
            Check(
                "python-headers",
                False,
                f"Python.h not found in {include}",
                _package_manager_hint(),
            )
        )

    try:
        import setuptools  # noqa: F401

        result.checks.append(Check("setuptools", True, f"setuptools {setuptools.__version__}"))
    except ImportError:
        result.checks.append(
            Check("setuptools", False, "setuptools is not installed", "pip install setuptools")
        )

    if not CKERNEL_DIR.parent.is_dir():
        result.checks.append(
            Check("package", False, f"{PACKAGE_ROOT} is not a directory", "reinstall xli")
        )
    else:
        result.checks.append(Check("package", True, str(PACKAGE_ROOT)))

    return result


# ------------------------------------------------------------------- discovery
def source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_targets(only: Sequence[str] | None = None) -> list[Path]:
    """Every core module eligible for compilation, newest source first."""
    found = [
        p
        for p in sorted(CORE_DIR.glob("*.py"))
        if p.stem not in NEVER_COMPILE
    ]
    if only:
        wanted = {name.replace(".py", "") for name in only}
        found = [p for p in found if p.stem in wanted]
    return found


# -------------------------------------------------------------------- manifest
@dataclass
class Manifest:
    built_at: str = ""
    python: str = ""
    cython: str = ""
    modules: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Manifest:
        if not MANIFEST.exists():
            return cls()
        try:
            raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls(
            built_at=raw.get("built_at", ""),
            python=raw.get("python", ""),
            cython=raw.get("cython", ""),
            modules=raw.get("modules", {}),
        )

    def save(self) -> None:
        CKERNEL_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "built_at": self.built_at,
            "python": self.python,
            "cython": self.cython,
            "modules": self.modules,
        }


def _python_tag() -> str:
    """ABI tag an extension must match to be loadable by this interpreter."""
    return f"cp{sys.version_info.major}{sys.version_info.minor}-{sys.platform}"


def find_compiled(name: str) -> list[Path]:
    """Every compiled artefact for `name`, wherever under CKERNEL_DIR it landed.

    Shared by is_usable(), the build report and the accel loader so the three
    cannot disagree about where an artefact is allowed to be. Searching
    recursively is deliberate: setuptools reproduces the extension's dotted
    package path under --build-lib, so the location depends on how the
    extension was named, and a flat glob silently found nothing when that
    nesting changed.
    """
    if not CKERNEL_DIR.is_dir():
        return []
    found = [
        p
        for p in sorted(CKERNEL_DIR.rglob(f"{name}.*.so"))
        if BUILD_DIR not in p.parents
    ]
    if not found:
        found = [
            p
            for p in sorted(CKERNEL_DIR.rglob(f"{name}.pyd"))
            if BUILD_DIR not in p.parents
        ]
    return found


def is_usable(name: str, manifest: Manifest | None = None) -> bool:
    """True if a compiled module exists and still matches its Python source."""
    manifest = manifest or Manifest.load()
    entry = manifest.modules.get(name)
    if not entry:
        return False
    if entry.get("python") != _python_tag():
        return False  # built against a different interpreter — must not load

    source = CORE_DIR / f"{name}.py"
    if not source.exists():
        return False
    if entry.get("sha256") != source_hash(source):
        return False  # source changed since the build; the .so is stale

    return bool(find_compiled(name))


# ------------------------------------------------------------------------ build
@dataclass
class BuildReport:
    ok: bool
    built: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    seconds: float = 0.0
    preflight: Preflight | None = None
    log: str = ""
    #: Per-module wall time, so the slow one is visible instead of inferred.
    durations: dict[str, float] = field(default_factory=dict)
    #: Per-module artefact size in bytes, for the "what came out" column.
    artifacts: dict[str, int] = field(default_factory=dict)

    def slowest(self, limit: int = 5) -> list[tuple[str, float]]:
        return sorted(self.durations.items(), key=lambda item: -item[1])[:limit]

    @property
    def bytes_built(self) -> int:
        return sum(self.artifacts.values())

    def summary(self) -> str:
        from xli.ui.locale import plural, seconds_word, t

        if self.ok:
            head = t(
                "kernel_built",
                count=len(self.built),
                modules=plural(len(self.built), "модуль", "модуля", "модулей"),
                seconds=seconds_word(self.seconds),
            )
        else:
            head = t("kernel_failed", count=len(self.failed))
        parts = [head]
        if self.built:
            parts.append("  " + t("kernel_built_list") + " " + ", ".join(self.built))
        if self.skipped:
            parts.append("  " + t("kernel_skipped_list") + " " + ", ".join(self.skipped))
        for failure in self.failed:
            parts.append(
                "  " + t("kernel_failed_one", module=failure["module"], error=failure["error"])
            )
        if self.durations:
            slow = ", ".join(
                f"{name} {seconds_word(seconds)}" for name, seconds in self.slowest(3)
            )
            parts.append("  " + t("kernel_slowest") + " " + slow)
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "built": self.built,
            "failed": self.failed,
            "skipped": self.skipped,
            "seconds": round(self.seconds, 3),
            "durations": {name: round(value, 3) for name, value in self.durations.items()},
            "artifacts": dict(self.artifacts),
            "preflight": self.preflight.to_dict() if self.preflight else None,
        }


def build(
    targets: Sequence[str] | None = None,
    *,
    force: bool = False,
    jobs: int | None = None,
    verbose: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> BuildReport:
    """Compile the requested core modules into xli/_ckernel/.

    A module that fails to compile is reported and skipped; the rest still
    build. That matters because Cython's pure-Python mode is stricter than
    CPython, and one awkward module should not block the other thirty.
    """
    started = time.perf_counter()
    report = BuildReport(ok=True)
    report.preflight = preflight()

    if on_progress:
        on_progress({"phase": "preflight"})

    if not report.preflight.ok:
        report.ok = False
        report.failed.append(
            {
                "module": "*",
                "error": "toolchain incomplete: "
                + "; ".join(f"{c.name} ({c.detail})" for c in report.preflight.missing),
            }
        )
        report.seconds = time.perf_counter() - started
        return report

    sources = discover_targets(targets)
    if not sources:
        report.ok = False
        report.failed.append({"module": "*", "error": "no matching modules found"})
        report.seconds = time.perf_counter() - started
        return report

    manifest = Manifest.load()
    todo: list[Path] = []
    for source in sources:
        if not force and is_usable(source.stem, manifest):
            report.skipped.append(source.stem)
            continue
        todo.append(source)

    if not todo:
        report.seconds = time.perf_counter() - started
        return report

    CKERNEL_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    import Cython
    from setuptools import Extension, setup
    from Cython.Build import cythonize

    extensions = [
        Extension(f"xli._ckernel.{source.stem}", [str(source)]) for source in todo
    ]

    if on_progress:
        on_progress({"phase": "cythonize", "module_names": [s.stem for s in todo]})

    argv_backup = sys.argv[:]
    log_chunks: list[str] = []
    per_module_errors: dict[str, str] = {}

    def run_setup(exts: list[Any]) -> None:
        """Invoke build_ext for these extensions, with setuptools' noise muted.

        setup() re-reads pyproject.toml even though all we want is build_ext, so
        setuptools' packaging deprecations (the project.license table form,
        licence classifiers) get printed over the build output. They say nothing
        about whether the kernel compiled, and they made a successful build look
        like a wall of errors.
        """
        sys.argv = [
            "xli-kernel-build",
            "build_ext",
            "--build-lib", str(BUILD_LIB_ROOT),
            "--build-temp", str(BUILD_DIR / "temp"),
        ]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            setup(name="xli-ckernel", ext_modules=exts, script_args=sys.argv[1:])

    # The whole batch first, so a clean build stays parallel. Only if that
    # raises do we retry one module at a time to find out which one broke.
    #
    # This is what the docstring above has always promised: one awkward module
    # should not block the other thirty. It did not hold, because setup() is
    # called once for the batch and raises on the first compile error, and the
    # handler then marked every module in `todo` as failed — so a single bad
    # module reported all of them broken and hid which one was at fault.
    def usable_now(stem: str) -> bool:
        """Is there a compiled artefact for this module right now?

        The batch build writes every module at once, so the per-module loop
        below is the only place that can time them; this predicate is shared
        with the reporting pass so the two cannot disagree.
        """
        if is_usable(stem, Manifest.load()):
            return True
        return bool(find_compiled(stem))

    try:
        if on_progress:
            on_progress({"phase": "compile"})
        batch_started = time.perf_counter()
        cythonized = cythonize(
            extensions,
            compiler_directives={"language_level": "3"},
            build_dir=str(BUILD_DIR),
            quiet=not verbose,
            nthreads=jobs or os.cpu_count() or 1,
        )
        run_setup(cythonized)
        # The batch ran without telling us where the time went; attribute the
        # whole batch to the modules it produced, so the summary still has a
        # slowest list rather than an empty one.
        for source in todo:
            if usable_now(source.stem):
                report.durations.setdefault(source.stem, 0.0)
        if report.durations and not any(report.durations.values()):
            share = (time.perf_counter() - batch_started) / max(1, len(todo))
            for name in report.durations:
                report.durations[name] = share
    except BaseException as exc:  # noqa: BLE001 - setup() may raise SystemExit
        log_chunks.append(
            f"batch build failed ({type(exc).__name__}: {exc}); retrying per module"
        )
        for source in todo:
            if on_progress:
                on_progress({"module_start": source.stem})
            module_started = time.perf_counter()
            try:
                single = cythonize(
                    [Extension(f"xli._ckernel.{source.stem}", [str(source)])],
                    compiler_directives={"language_level": "3"},
                    build_dir=str(BUILD_DIR),
                    quiet=not verbose,
                    nthreads=1,
                )
                run_setup(single)
                report.durations[source.stem] = time.perf_counter() - module_started
                if on_progress:
                    on_progress({"module_done": source.stem})
            except BaseException as one:  # noqa: BLE001
                # Keep the compiler's own words. Without this the report would
                # fall through to "no .so was produced", which blames the wrong
                # thing and hides which module actually broke.
                per_module_errors[source.stem] = f"{type(one).__name__}: {one}"
                log_chunks.append(f"{source.stem}: {type(one).__name__}: {one}")
                if on_progress:
                    on_progress({"module_fail": source.stem, "error": str(one)})
    finally:
        sys.argv = argv_backup

    if log_chunks:
        report.log = "\n".join(log_chunks)

    if on_progress:
        on_progress({"phase": "link"})

    manifest.built_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    manifest.python = _python_tag()
    manifest.cython = Cython.__version__

    for source in todo:
        artifacts = find_compiled(source.stem)
        if artifacts:
            try:
                report.artifacts[source.stem] = max(path.stat().st_size for path in artifacts)
            except OSError:
                pass
        if is_usable(source.stem, Manifest.load()):
            report.built.append(source.stem)
            continue
        # The artefact exists but the manifest does not know about it yet.
        if artifacts:
            manifest.modules[source.stem] = {
                "sha256": source_hash(source),
                "python": _python_tag(),
                "built_at": manifest.built_at,
            }
            report.built.append(source.stem)
        else:
            report.failed.append(
                {
                    "module": source.stem,
                    "error": per_module_errors.get(
                        source.stem, "compiler finished but no .so was produced"
                    ),
                }
            )
            report.ok = False

    manifest.save()
    report.seconds = time.perf_counter() - started
    return report


def clean() -> dict[str, Any]:
    """Remove every build artefact, returning what was deleted."""
    removed: list[str] = []
    if CKERNEL_DIR.exists():
        for path in sorted(CKERNEL_DIR.rglob("*")):
            if path.is_file():
                removed.append(str(path.relative_to(PACKAGE_ROOT)))
        shutil.rmtree(CKERNEL_DIR, ignore_errors=True)
    # Cython leaves generated .c files next to the sources.
    for stray in CORE_DIR.glob("*.c"):
        stray.unlink(missing_ok=True)
        removed.append(str(stray.relative_to(PACKAGE_ROOT)))
    return {"removed": len(removed), "files": removed[:50]}


def status() -> dict[str, Any]:
    """What is compiled, what is stale, and what would happen on the next build."""
    manifest = Manifest.load()
    sources = discover_targets()
    modules: list[dict[str, Any]] = []

    for source in sources:
        entry = manifest.modules.get(source.stem, {})
        if is_usable(source.stem, manifest):
            state = "compiled"
        elif entry:
            state = "stale" if entry.get("python") == _python_tag() else "wrong-interpreter"
        else:
            state = "source-only"
        modules.append({"module": source.stem, "state": state, "sha256": source_hash(source)[:12]})

    compiled = sum(1 for m in modules if m["state"] == "compiled")
    return {
        "kernel_dir": str(CKERNEL_DIR),
        "python_tag": _python_tag(),
        "built_at": manifest.built_at,
        "cython": manifest.cython,
        "total": len(modules),
        "compiled": compiled,
        "stale": sum(1 for m in modules if m["state"] == "stale"),
        "source_only": sum(1 for m in modules if m["state"] == "source-only"),
        "modules": modules,
        "preflight_ok": preflight().ok,
    }
