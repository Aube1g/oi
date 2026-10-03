#!/usr/bin/env python3
"""
XLI Config — typed, validated, layered settings.

Resolution order, lowest to highest precedence:

    built-in defaults  <  ~/.xli/config.json  <  ./.xli/config.json  <  XLI_* env

Everything is a dotted path (`provider.model`, `permissions.mode`,
`kernel.enabled`), so `xli config set permissions.mode auto` and the JSON file
describe exactly the same tree. Unknown keys are kept but flagged, which is how
a config written by a newer version survives a downgrade.

Values are coerced and validated on write, not on read: `set("temperature",
"banana")` fails immediately with a useful message instead of exploding three
calls later inside the provider.
"""

from __future__ import annotations

import json
import io
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterator

CONFIG_DIRNAME = ".xli"
CONFIG_FILENAME = "config.json"
ENV_PREFIX = "XLI_"
ENV_FILENAME = ".env"


DEFAULTS: dict[str, Any] = {
    # --- model
    "provider": "mistral",
    "provider.model": "mistral-large-latest",
    #: Point the provider at another OpenAI-compatible endpoint. Empty means
    #: "use the provider's own URL". Set this for a proxy or a self-hosted
    #: gateway, e.g. https://api.xkiro.com/v1
    "provider.base_url": "",
    "provider.temperature": 0.4,
    "provider.max_tokens": 4000,
    "provider.timeout": 120,
    "provider.stream": True,
    # --- agent
    "agent.max_steps": 24,
    "agent.team": "default",
    "agent.project": "default",
    # --- permissions
    "permissions.mode": "confirm",
    "permissions.deny": [],
    "permissions.allow": [],
    # --- tools
    "tools.disabled": [],
    "tools.bash_timeout": 60,
    # --- kernel (Cython acceleration)
    "kernel.enabled": True,
    "kernel.auto_build": False,
    # --- mcp
    "mcp.enabled": True,
    "mcp.servers": [],
    #: Server names turned off, overriding the registry default. Mirrors
    #: tools.disabled so the two subsystems behave the same way.
    "mcp.disabled": [],
    #: Server names explicitly turned on (needed because four ship disabled).
    "mcp.enabled_servers": [],
    # --- ui
    "ui.theme": "dark",
    "ui.language": "ru",
    "ui.mouse": True,
    # --- sandbox
    "sandbox.timeout": 10,
    "sandbox.max_memory_mb": 256,
    "sandbox.disable_network": True,
}


# key -> (coercer, validator-error-message)
def _as_str(value: Any) -> str:
    # None means "unset", not the four-character string "None". Without this a
    # cleared value came back as a literal URL/model named None.
    if value is None:
        return ""
    return str(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n", ""):
        return False
    raise ValueError(f"cannot read {value!r} as a boolean")


def _as_int(value: Any) -> int:
    return int(str(value).strip())


def _as_float(value: Any) -> float:
    return float(str(value).strip())


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    raise ValueError(f"cannot read {value!r} as a list")


COERCERS: dict[str, Callable[[Any], Any]] = {
    "provider": _as_str,
    "provider.model": _as_str,
    "provider.base_url": _as_str,
    "provider.temperature": _as_float,
    "provider.max_tokens": _as_int,
    "provider.timeout": _as_int,
    "provider.stream": _as_bool,
    "agent.max_steps": _as_int,
    "agent.team": _as_str,
    "agent.project": _as_str,
    "permissions.mode": _as_str,
    "permissions.deny": _as_list,
    "permissions.allow": _as_list,
    "tools.disabled": _as_list,
    "mcp.disabled": _as_list,
    "mcp.enabled_servers": _as_list,
    "tools.bash_timeout": _as_int,
    "kernel.enabled": _as_bool,
    "kernel.auto_build": _as_bool,
    "mcp.enabled": _as_bool,
    "mcp.servers": _as_list,
    "ui.theme": _as_str,
    "ui.language": _as_str,
    "ui.mouse": _as_bool,
    "sandbox.timeout": _as_int,
    "sandbox.max_memory_mb": _as_int,
    "sandbox.disable_network": _as_bool,
}

#: Allowed values where a typo would otherwise only surface at runtime.
CHOICES: dict[str, tuple[str, ...]] = {
    "permissions.mode": ("auto", "confirm", "readonly"),
    "ui.language": ("ru", "en"),
    "ui.theme": ("dark", "light", "mono"),
}

#: Ranges checked for numeric keys: key -> (min, max).
RANGES: dict[str, tuple[float, float]] = {
    "provider.temperature": (0.0, 2.0),
    "provider.max_tokens": (1, 1_000_000),
    "provider.timeout": (1, 3600),
    "agent.max_steps": (1, 1000),
    "tools.bash_timeout": (1, 3600),
    "sandbox.timeout": (1, 3600),
    "sandbox.max_memory_mb": (16, 65536),
}


class ConfigError(ValueError):
    """Raised when a value cannot be coerced or is out of range."""


@dataclass
class Config:
    """Layered settings with typed access."""

    data: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))
    user_file: Path | None = None
    project_file: Path | None = None
    unknown_keys: list[str] = field(default_factory=list)
    #: .env files consulted, and the variable names taken from them.
    env_files: list[Path] = field(default_factory=list)
    env_loaded: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ paths
    @classmethod
    def user_path(cls) -> Path:
        override = os.environ.get("XLI_CONFIG_DIR")
        base = Path(override) if override else Path.home() / CONFIG_DIRNAME
        return base / CONFIG_FILENAME

    @classmethod
    def project_path(cls, root: Path | None = None) -> Path:
        return Path(root or Path.cwd()) / CONFIG_DIRNAME / CONFIG_FILENAME

    # ----------------------------------------------------------------- loading
    @classmethod
    def load(
        cls,
        *,
        project_root: Path | None = None,
        use_env: bool = True,
    ) -> Config:
        config = cls()
        config.user_file = cls.user_path()
        config.project_file = cls.project_path(project_root)

        for path in (config.user_file, config.project_file):
            config._merge_file(path)

        if use_env:
            # .env first, so keys declared there are visible to api_key() and
            # to _merge_env() below.
            #
            # Project before user, because load_dotenv_files keeps the first
            # value for a name: that makes project beat user, matching how the
            # two config.json files layer, while a variable already exported in
            # the real environment still beats both.
            config.env_files = [
                config.project_file.parent / ENV_FILENAME,
                config.user_file.parent / ENV_FILENAME,
            ]
            config.env_loaded = load_dotenv_files(config.env_files)
            config._merge_env()

        return config

    def _merge_file(self, path: Path) -> None:
        if not path or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt config must not stop the agent; defaults are safer than
            # a crash, and `xli doctor` reports the damage.
            self.unknown_keys.append(f"unreadable config: {path}")
            return
        if not isinstance(raw, dict):
            self.unknown_keys.append(f"config is not an object: {path}")
            return
        for key, value in _flatten(raw).items():
            self._set_raw(key, value)

    def _merge_env(self) -> None:
        """XLI_PERMISSIONS_MODE -> permissions.mode, and so on."""
        for env_key, env_value in os.environ.items():
            if not env_key.startswith(ENV_PREFIX):
                continue
            tail = env_key[len(ENV_PREFIX) :]
            key = _env_to_key(tail)
            if key in DEFAULTS:
                self._set_raw(key, env_value)

    def _set_raw(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            self.unknown_keys.append(key)
            self.data[key] = value
            return
        self.data[key] = _coerce(key, value)

    # ----------------------------------------------------------------- access
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> Any:
        """Validate, store and return the coerced value."""
        coerced = _coerce(key, value)
        self.data[key] = coerced
        return coerced

    def unset(self, key: str) -> bool:
        if key in self.data and key in DEFAULTS:
            self.data[key] = DEFAULTS[key]
            return True
        return self.data.pop(key, None) is not None

    def keys(self) -> list[str]:
        return sorted(self.data)

    def items(self) -> Iterator[tuple[str, Any]]:
        yield from sorted(self.data.items())

    def section(self, prefix: str) -> dict[str, Any]:
        return {k: v for k, v in self.data.items() if k.startswith(prefix + ".") or k == prefix}

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self.data

    # ------------------------------------------------------------- persistence
    def save(self, path: Path | None = None) -> Path:
        """Write the non-default keys to disk.

        Defaults to the *user* config, because user_path() is what honours
        XLI_CONFIG_DIR. This used to prefer project_file, so XLI_CONFIG_DIR was
        respected when reading and silently ignored when writing — a config dir
        override that only half worked, and one that wrote into the current
        repository instead of the directory the caller asked for.

        Pass path= explicitly to write a project-level config.
        """
        target = Path(path) if path else (self.user_file or self.project_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write only what differs from the defaults, so the file stays readable
        # and a future default change still applies.
        diff = {k: v for k, v in self.data.items() if DEFAULTS.get(k, object()) != v}
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(_nest(diff), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(target)
        return target

    def diff_from_defaults(self) -> dict[str, Any]:
        return {k: v for k, v in self.data.items() if DEFAULTS.get(k, object()) != v}

    # ------------------------------------------------------------- convenience
    # ------------------------------------------------------ typed accessors
    #: Provider name -> the environment variable holding its API key. The
    #: aliases exist because "claude" and "google" are how people refer to
    #: Anthropic and Gemini, and `XLI_PROVIDER=claude` should just work.
    API_KEY_VARS = {
        "mistral": "MISTRAL_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "google": "GEMINI_API_KEY",
    }

    def api_key(self, provider: str | None = None) -> str | None:
        """The API key for a provider, or None if it is not in the environment."""
        name = (provider or self.default_provider()).lower()
        env_var = self.API_KEY_VARS.get(name, f"{name.upper()}_API_KEY")
        return os.environ.get(env_var)

    def default_provider(self) -> str:
        return str(self.get("provider", "mistral"))

    def model(self) -> str:
        return str(self.get("provider.model", "mistral-large-latest"))

    def base_url(self) -> str:
        """A custom endpoint, or "" to use the provider's own."""
        return str(self.get("provider.base_url", "") or "")

    def temperature(self) -> float:
        return float(self.get("provider.temperature", 0.4))

    def max_tokens(self) -> int:
        return int(self.get("provider.max_tokens", 4000))

    def team(self) -> str:
        return str(self.get("agent.team", "default"))

    def project(self) -> str:
        return str(self.get("agent.project", "default"))

    def sandbox_timeout(self) -> int:
        return int(self.get("sandbox.timeout", 10))

    def sandbox_max_memory_mb(self) -> int:
        return int(self.get("sandbox.max_memory_mb", 256))

    def sandbox_network_disabled(self) -> bool:
        return bool(self.get("sandbox.disable_network", True))

    def permission_mode(self) -> str:
        return str(self.get("permissions.mode", "confirm"))

    def kernel_enabled(self) -> bool:
        return bool(self.get("kernel.enabled", True))

    def describe(self) -> str:
        lines = [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in self.items()]
        return "\n".join(lines)


# ------------------------------------------------------------------- helpers
def _coerce(key: str, value: Any) -> Any:
    coercer = COERCERS.get(key)
    if coercer is None:
        return value
    try:
        coerced = coercer(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}: {exc}") from exc

    allowed = CHOICES.get(key)
    if allowed and coerced not in allowed:
        raise ConfigError(f"{key}: {coerced!r} is not one of {', '.join(allowed)}")

    bounds = RANGES.get(key)
    if bounds is not None:
        low, high = bounds
        if not low <= coerced <= high:
            raise ConfigError(f"{key}: {coerced} is outside {low}..{high}")

    return coerced


def _flatten(raw: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Turn a nested dict into dotted keys; known scalars stay as they are."""
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and dotted not in DEFAULTS:
            flat.update(_flatten(value, dotted + "."))
        else:
            flat[dotted] = value
    return flat


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    """Inverse of _flatten, for a human-readable config file.

    Some keys are both a scalar and a namespace prefix: `provider` is a setting
    in its own right *and* the parent of `provider.model` and
    `provider.base_url`. Those two cannot share a slot — one is a string, the
    other a dict.

    The old code walked into the string, hit its defensive break, and dropped
    the value on the floor. Because `setdefault` returns the existing string and
    `break` skips the for/else, the assignment never ran, so
    `xli config set provider.model gpt-4o` printed "set ... wrote ..." and
    persisted nothing. Any key under a scalar prefix was affected.

    Such keys are now written flat, which _flatten reads back unchanged.
    """
    nested: dict[str, Any] = {}
    for key, value in sorted(flat.items()):
        parts = key.split(".")
        if len(parts) == 1:
            nested[key] = value
            continue

        cursor = nested
        blocked = False
        for part in parts[:-1]:
            existing = cursor.get(part)
            if existing is None:
                cursor[part] = {}
                cursor = cursor[part]
            elif isinstance(existing, dict):
                cursor = existing
            else:
                blocked = True   # a scalar already owns this slot
                break

        if blocked or isinstance(cursor.get(parts[-1]), dict):
            nested[key] = value  # cannot nest it; keep the dotted form
        else:
            cursor[parts[-1]] = value
    return nested


#: Shorthand env names, so `XLI_MODE=auto` works as well as the full spelling.
_ENV_OVERRIDES = {
    "MODE": "permissions.mode",
    "PERMISSION_MODE": "permissions.mode",
    "MODEL": "provider.model",
    "BASE_URL": "provider.base_url",
    "PROVIDER_BASE_URL": "provider.base_url",
    "MAX_STEPS": "agent.max_steps",
    "LANGUAGE": "ui.language",
    "LANG": "ui.language",
    "KERNEL": "kernel.enabled",
}


def _env_to_key(tail: str) -> str:
    """Map the tail of an XLI_* variable onto a dotted config key.

    A double underscore is the section separator: XLI_PERMISSIONS__MODE ->
    permissions.mode. Single underscores are kept as-is, because otherwise
    `XLI_MAX_TOKENS` could not be told apart from `max.tokens`.
    """
    if tail in _ENV_OVERRIDES:
        return _ENV_OVERRIDES[tail]
    return tail.lower().replace("__", ".")


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse a .env file. Handles comments, blank lines, quotes and `export`.

    python-dotenv is a declared dependency and is used when present, but the
    fallback keeps a bare checkout working if it is missing.
    """
    try:
        from dotenv import dotenv_values

        return {k: v for k, v in dotenv_values(stream=io.StringIO(text)).items() if v is not None}
    except ImportError:
        pass

    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export").strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv_files(paths: Iterable[Path]) -> list[str]:
    """Read .env files into the process environment.

    Files are read in order and the first value for a name wins, so pass the
    higher-precedence file first.

    A variable already set in the real environment always wins, so
    `XLI_...=y xli run` and a key exported in the shell beat any file. The
    previous implementation overwrote os.environ unconditionally, which made it
    impossible to override a stale file value for a single command.

    Returns the names that were actually set, for diagnostics.
    """
    set_names: list[str] = []
    for path in paths:
        if not path or not path.is_file():
            continue
        try:
            values = _parse_dotenv(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for key, value in values.items():
            if key in os.environ:
                continue
            os.environ[key] = value
            set_names.append(key)
    return set_names


_instance: Config | None = None


def get_config(**kwargs: Any) -> Config:
    """Process-wide config singleton."""
    global _instance
    if _instance is None:
        _instance = Config.load(**kwargs)
    return _instance


def reset_config() -> None:
    """Drop the singleton — used by tests and by `xli config reload`."""
    global _instance
    _instance = None
