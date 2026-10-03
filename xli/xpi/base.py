#!/usr/bin/env python3
"""
XPI Plugin Base — what an internal plugin looks like.

Subclass `XpiPlugin`, implement whichever hooks you care about, drop the
package into `~/.xli/xpi/<name>/` with a `manifest.json`, and the manager picks
it up. Every hook takes a single `context` dict and returns either `None` or
something JSON-able; the manager collects the returns and reports them.

Two rules worth keeping in mind when writing a plugin:

  * Hooks run **in-process**, on the agent's thread. Do not block. If you need
    to do real work, hand it to a thread and return immediately.
  * A hook that raises is caught, logged and reported — it will not take the
    agent down, but it will not be retried either.
"""

from __future__ import annotations

from typing import Any

from xli.core.logger import StructuredLogger


class XpiPlugin:
    """Base class for XPI plugins.

    Not an ABC: every hook is optional, so there is nothing a subclass is
    required to implement. Subclass it and override only what you need.
    """

    def __init__(self, name: str = "", version: str = "1.0.0"):
        self.name = name or type(self).__name__
        self.version = version
        #: "all", or one of "tui" / "nvim" / "headless" to restrict the plugin.
        self.platform = "all"
        self.logger = StructuredLogger(f"xli.{self.name}")

    # ------------------------------------------------------------- lifecycle
    def on_load(self, context: dict[str, Any]) -> Any:
        """Called once, right after the plugin is constructed."""

    def on_unload(self, context: dict[str, Any]) -> Any:
        """Called before the plugin is dropped (disable, reload, shutdown)."""

    # ----------------------------------------------------------------- agent
    def on_agent_start(self, context: dict[str, Any]) -> Any:
        """A task is about to run. context: {task, max_steps, mode}."""

    def on_tool_call(self, context: dict[str, Any]) -> Any:
        """A tool call is about to execute. context: {name, args}."""

    def on_tool_result(self, context: dict[str, Any]) -> Any:
        """A tool call finished. context: {name, ok, summary}."""

    def on_agent_end(self, context: dict[str, Any]) -> Any:
        """The task finished. context: {ok, summary, steps, stopped_reason}."""

    # ------------------------------------------------------------ platforms
    def on_tui_mount(self, context: dict[str, Any]) -> Any:
        """The full-screen UI opened."""

    def on_nvim_attach(self, context: dict[str, Any]) -> Any:
        """The Neovim frontend connected to the kernel."""

    def on_headless_start(self, context: dict[str, Any]) -> Any:
        """A headless run started."""

    # -------------------------------------------------------------- dispatch
    #: Hook name -> the platform it belongs to, for `initialize()`.
    PLATFORM_HOOKS = {
        "tui": "on_tui_mount",
        "nvim": "on_nvim_attach",
        "headless": "on_headless_start",
    }

    def get_platform_hook(self, platform: str):
        """The bound hook for a platform, or None."""
        name = self.PLATFORM_HOOKS.get(platform)
        return getattr(self, name, None) if name else None

    def initialize(self, platform: str, **kwargs: Any) -> bool:
        """Run the platform hook. Returns True if a hook existed and ran.

        Kept for callers that drive a single platform directly; the manager
        normally goes through `dispatch()` instead.
        """
        hook = self.get_platform_hook(platform)
        if hook is None:
            return False
        try:
            hook({"platform": platform, **kwargs})
            self.logger.log_structured(
                "INFO", f"xpi.{self.name}", f"initialized for {platform}"
            )
            return True
        except Exception as exc:  # noqa: BLE001 - a plugin must not break startup
            self.logger.log_error(f"xpi.{self.name}", f"init failed for {platform}", exc=exc)
            return False
