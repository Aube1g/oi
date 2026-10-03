#!/usr/bin/env python3
"""
XLI Environment Adapter v4
Auto-detect: TUI (textual) -> Neovim (pynvim) -> Headless (fallback)
Added: structured logging, error tracking, nvim error capture
"""

import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Any

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.env")


class EnvironmentAdapter:
    """Адаптирует операции под текущую среду с полным логированием"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.name = self._detect_mode()
        self.home = str(Path.home())
        self.cwd = os.getcwd()
        # Alias used throughout xli/core/chain.py — was previously missing
        # entirely, meaning run_chain() raised AttributeError on its very
        # first agent step every time it was actually run.
        self.project_dir = self.cwd
        self.is_termux = "/data/data/com.termux" in self.home
        self.nvim = None
        self._connection_errors = []

        logger.log_structured("INFO", "env", "EnvironmentAdapter initializing", {
            "mode": self.name,
            "cwd": self.cwd,
            "termux": self.is_termux
        })

        if self.name == "neovim":
            self._connect_nvim()

        logger.log_structured("INFO", "env", "EnvironmentAdapter ready", {
            "mode": self.name,
            "nvim_connected": self.nvim is not None
        })

    def _detect_mode(self) -> str:
        """Auto-detect execution mode"""
        # Check pynvim first
        try:
            import pynvim
            nvim_listen = os.environ.get("NVIM_LISTEN_ADDRESS")
            if nvim_listen or "NVIM" in os.environ:
                logger.log_structured("DEBUG", "env", "Detected neovim mode", {"listen": nvim_listen})
                return "neovim"
        except ImportError:
            pass

        # Check textual
        try:
            import textual
            logger.log_structured("DEBUG", "env", "Detected terminal mode")
            return "terminal"
        except ImportError:
            pass

        logger.log_structured("DEBUG", "env", "Detected headless mode")
        return "headless"

    def _connect_nvim(self):
        """Connect to Neovim with detailed logging"""
        logger.log_structured("INFO", "nvim", "Connecting to Neovim...")

        try:
            import pynvim

            # Try socket
            nvim_listen = os.environ.get("NVIM_LISTEN_ADDRESS")
            if nvim_listen and Path(nvim_listen).exists():
                logger.log_structured("DEBUG", "nvim", "Trying socket", {"path": nvim_listen})
                self.nvim = pynvim.attach("socket", path=nvim_listen)
                logger.log_structured("INFO", "nvim", "Connected via socket", {"path": nvim_listen})
                return

            # Try child (inside :terminal)
            if "NVIM" in os.environ:
                logger.log_structured("DEBUG", "nvim", "Trying child connection")
                self.nvim = pynvim.attach("child", argv=sys.argv)
                logger.log_structured("INFO", "nvim", "Connected via child")
                return

        except Exception as e:
            err_msg = f"Neovim connect failed: {e}"
            logger.log_error("nvim", err_msg, exc=e)
            self._connection_errors.append(err_msg)

        # Fallback socket paths
        for sock in [
            "/tmp/nvim",
            "/data/data/com.termux/files/usr/tmp/nvim",
            str(Path.home() / ".local/share/nvim/server.pipe")
        ]:
            if Path(sock).exists():
                try:
                    logger.log_structured("DEBUG", "nvim", "Trying fallback socket", {"path": sock})
                    self.nvim = pynvim.attach("socket", path=sock)
                    logger.log_structured("INFO", "nvim", "Connected via fallback", {"path": sock})
                    return
                except Exception as e:
                    logger.log_error("nvim", f"Fallback socket failed: {sock}", exc=e)
                    self._connection_errors.append(f"{sock}: {e}")

        logger.log_structured("ERROR", "nvim", "All connection methods exhausted",
                             {"errors": self._connection_errors})

    def is_nvim(self) -> bool:
        return self.name == "neovim" and self.nvim is not None

    def is_terminal(self) -> bool:
        return self.name == "terminal"

    def is_headless(self) -> bool:
        return self.name == "headless"

    def notify(self, msg: str, level: str = "info"):
        """Adaptive notifications"""
        logger.log_structured("INFO", "notify", f"Notify [{level}]: {msg[:100]}")

        if self.is_nvim():
            try:
                level_map = {"info": 2, "warn": 3, "warning": 3, "error": 4}
                self.nvim.call("vim.notify", msg, level_map.get(level, 2), {
                    "title": "FIRE XLI", "timeout": 3000
                })
                logger.log_structured("DEBUG", "notify", "nvim.notify sent")
                return
            except Exception as e:
                logger.log_error("notify", f"nvim.notify failed: {e}", exc=e)

        print(f"[{level.upper()}] {msg}")

    def run_shell(self, cmd: str, timeout: int = 30) -> str:
        """Execute shell with dangerous command check and full logging"""
        logger.log_structured("INFO", "shell", f"Executing: {cmd[:100]}",
                             {"timeout": timeout, "cwd": self.cwd})

        from xli.core.shell_safety import is_shell_command_safe
        safe, reason = is_shell_command_safe(cmd)
        if not safe:
            logger.log_structured("WARN", "shell", "Dangerous command blocked", {"cmd": cmd[:50], "reason": reason})
            self.notify(f"WARNING Dangerous command blocked ({reason}): {cmd[:50]}", "warn")
            return f"BLOCKED ({reason}): {cmd}"

        try:
            from xli.core.exec_guard import run_guarded_shell
            result = run_guarded_shell(cmd, timeout=timeout, cwd=self.cwd)
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            logger.log_structured("DEBUG", "shell", "Command finished",
                                 {"exit_code": result.returncode,
                                  "stdout_len": len(stdout),
                                  "stderr_len": len(stderr)})

            if result.returncode == 0:
                return f"OK exit {result.returncode}\n{stdout}" if stdout else f"OK exit {result.returncode}"

            logger.log_error("shell", f"Command failed: exit {result.returncode}",
                            details={"cmd": cmd[:100], "stderr": stderr[:500]})
            return f"FAIL exit {result.returncode}\n{stderr or stdout}"

        except subprocess.TimeoutExpired:
            logger.log_error("shell", f"Command timeout after {timeout}s",
                            details={"cmd": cmd[:100]})
            return f"TIMEOUT after {timeout}s"
        except Exception as e:
            logger.log_error("shell", "Command execution failed", exc=e,
                            details={"cmd": cmd[:100]})
            return f"ERROR: {e}"

    def write_file(self, path: str, content: str) -> str:
        """Atomic file write with parent mkdir and logging"""
        logger.log_structured("INFO", "file", f"Writing: {path}",
                             {"content_len": len(content)})

        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            logger.log_structured("INFO", "file", "File written", {"path": path, "size": len(content)})
            return f"OK File: {path}"
        except Exception as e:
            logger.log_error("file", f"Write failed: {path}", exc=e)
            return f"ERROR: {e}"

    def read_file(self, path: str) -> str:
        """Read file with error logging"""
        try:
            content = Path(path).read_text(encoding="utf-8")
            logger.log_structured("DEBUG", "file", f"Read file: {path}",
                                 {"content_len": len(content)})
            return content
        except Exception as e:
            logger.log_error("file", f"Read failed: {path}", exc=e)
            return f"ERROR: {e}"

    def open_buffer(self, content: list[str], name: str = "XLI",
                    filetype: str = "markdown", float_win: bool = False):
        """Open buffer: float window or split in nvim, fallback to print"""
        logger.log_structured("INFO", "buffer", "Opening buffer", {
            "name": name,
            "lines": len(content),
            "float": float_win
        })

        if not self.is_nvim():
            logger.log_structured("DEBUG", "buffer", "Not in nvim, printing to stdout")
            print(f"\n=== {name} ===")
            print("\n".join(content))
            return

        try:
            buf = self.nvim.api.create_buf(False, True)
            buf.name = f"xli://{name}"
            buf.options["filetype"] = filetype
            buf.options["buftype"] = "nofile"
            buf.options["bufhidden"] = "hide"
            buf[:] = content

            if float_win:
                editor_w = self.nvim.options["columns"]
                editor_h = self.nvim.options["lines"]
                width = min(100, editor_w - 4)
                height = min(30, editor_h - 4)
                win = self.nvim.api.open_win(buf, True, {
                    "relative": "editor",
                    "row": (editor_h - height) // 2,
                    "col": (editor_w - width) // 2,
                    "width": width,
                    "height": height,
                    "style": "minimal",
                    "border": "rounded",
                    "title": f" {name} ",
                    "title_pos": "center",
                })
                self.nvim.api.buf_set_keymap(buf.number, "n", "q", ":q<<CR>",
                    {"noremap": True, "silent": True})
                logger.log_structured("DEBUG", "buffer", "Float window opened", {"win": win})
            else:
                self.nvim.command("vsplit")
                win = self.nvim.current.window
                win.buffer = buf
                win.options["wrap"] = True
                win.options["cursorline"] = True
                logger.log_structured("DEBUG", "buffer", "Split buffer opened")

        except Exception as e:
            logger.log_error("buffer", "Open buffer failed", exc=e)
            print(f"\n=== {name} ===")
            print("\n".join(content))

    def get_current_file(self) -> str:
        """Get current file path in nvim"""
        if self.is_nvim():
            try:
                path = self.nvim.current.buffer.name
                logger.log_structured("DEBUG", "file", f"Current file: {path}")
                return path
            except Exception as e:
                logger.log_error("file", "get_current_file failed", exc=e)
        return ""

    def get_selection(self) -> str:
        """Get visual selection from nvim"""
        if not self.is_nvim():
            return ""
        try:
            old_reg = self.nvim.call("getreg", '"')
            old_type = self.nvim.call("getregtype", '"')
            self.nvim.command('normal! gv"xy')
            sel = self.nvim.call("getreg", "x")
            self.nvim.call("setreg", '"', old_reg, old_type)
            logger.log_structured("DEBUG", "selection", "Got selection", {"len": len(sel)})
            return sel
        except Exception as e:
            logger.log_error("selection", "get_selection failed", exc=e)
            return ""

    def append_history(self, task: str, result: str = "", agent: str = ""):
        """Append to task history"""
        hist_file = xli_path("history.txt")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] [{agent}] {task}"
        if result:
            entry += f"\n  -> {result[:200]}"
        try:
            with open(hist_file, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
            logger.log_structured("DEBUG", "history", "Appended", {"task": task[:50]})
        except Exception as e:
            logger.log_error("history", "Append failed", exc=e)

    def get_history(self, lines: int = 50) -> list[str]:
        """Read task history"""
        hist_file = xli_path("history.txt")
        if not hist_file.exists():
            return []
        try:
            with open(hist_file, encoding="utf-8") as f:
                all_lines = f.readlines()
            result = [l.strip() for l in all_lines[-lines:] if l.strip()]
            logger.log_structured("DEBUG", "history", f"Read {len(result)} lines")
            return result
        except Exception as e:
            logger.log_error("history", "Read failed", exc=e)
            return []

    def get_system_context(self) -> str:
        """Build system context for agents"""
        lines = ["\nTARGET CONTEXT:"]
        lines.append(f"- Env: {self.name.upper()}")
        lines.append(f"- CWD: {self.cwd}")
        lines.append(f"- Home: {self.home}")

        if self.is_termux:
            lines.append("- Platform: Termux (Android)")
            lines.append("- Packages: pkg install")
            lines.append("- Python: python3")
            lines.append("- Storage: ~/storage/")

        if self.name == "neovim":
            lines.append("\nNEOVIM MODE:")
            lines.append("- NO textual/tui")
            lines.append("- Commands in <SHELL>tags</SHELL>")
            lines.append("- Files: <SHELL>echo '...' > /full/path</SHELL>")
            lines.append("- Result: text in response")
        elif self.name == "terminal":
            lines.append("\nTERMINAL MODE:")
            lines.append("- Full TUI available")
        else:
            lines.append("\nHEADLESS MODE:")
            lines.append("- Text output only")

        # Available MCP servers
        try:
            from xli.mcp.registry import get_available_servers
            servers = get_available_servers()
            lines.append("\nAVAILABLE MCP:")
            for name, info in servers.items():
                lines.append(f"  OK {name}: {info.get('description', '')[:40]}")
        except Exception as e:
            logger.log_error("mcp", "get_available_servers failed", exc=e)

        return "\n".join(lines)

    def get_error_summary(self) -> dict[str, Any]:
        """Error summary for diagnostics"""
        return {
            "error_count": logger.get_error_count(),
            "log_dir": str(self.log_dir),
            "structured_log": str(logger.structured_path),
            "env": self.name,
            "is_termux": self.is_termux,
        }



def get_env_adapter() -> EnvironmentAdapter:
    """Get singleton EnvironmentAdapter"""
    return EnvironmentAdapter()
