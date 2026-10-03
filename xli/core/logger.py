#!/usr/bin/env python3
"""
XLI Logger v4 - Structured logging: plain + JSONL + errors + nvim_errors
Now with COLORS and TIMESTAMPS for terminal output
"""

import json
import logging
import sys
import os
import traceback
from datetime import datetime
from xli.paths import xli_path


# ANSI color codes
COLORS = {
    "RESET": "\033[0m",
    "BOLD": "\033[1m",
    "DIM": "\033[2m",
    "RED": "\033[91m",
    "GREEN": "\033[92m",
    "YELLOW": "\033[93m",
    "BLUE": "\033[94m",
    "MAGENTA": "\033[95m",
    "CYAN": "\033[96m",
    "WHITE": "\033[97m",
    "BG_RED": "\033[41m",
    "BG_GREEN": "\033[42m",
    "BG_YELLOW": "\033[43m",
    "BG_BLUE": "\033[44m",
}

# Component color mapping
COMPONENT_COLORS = {
    "main": COLORS["CYAN"],
    "config": COLORS["BLUE"],
    "env": COLORS["GREEN"],
    "skills": COLORS["MAGENTA"],
    "memory": COLORS["MAGENTA"],
    "cache": COLORS["MAGENTA"],
    "agent": COLORS["YELLOW"],
    "chain": COLORS["CYAN"],
    "planner": COLORS["BLUE"],
    "coder": COLORS["GREEN"],
    "debugger": COLORS["RED"],
    "tester": COLORS["MAGENTA"],
    "optimizer": COLORS["YELLOW"],
    "reviewer": COLORS["BLUE"],
    "mcp": COLORS["CYAN"],
    "mcp_bridge": COLORS["CYAN"],
    "mcp.client": COLORS["CYAN"],
    "mcp.registry": COLORS["CYAN"],
    "mcp_recommender": COLORS["CYAN"],
    "diff": COLORS["BLUE"],
    "sandbox": COLORS["YELLOW"],
    "git": COLORS["GREEN"],
    "heal": COLORS["RED"],
    "prompt_lab": COLORS["MAGENTA"],
    "queue": COLORS["BLUE"],
    "progressive": COLORS["GREEN"],
    "debug_interactive": COLORS["RED"],
    "deps": COLORS["BLUE"],
    "shell": COLORS["YELLOW"],
    "file": COLORS["GREEN"],
    "history": COLORS["DIM"],
    "notify": COLORS["CYAN"],
    "buffer": COLORS["BLUE"],
    "selection": COLORS["MAGENTA"],
    "tui": COLORS["CYAN"],
    "headless": COLORS["BLUE"],
    "nvim": COLORS["GREEN"],
    "providers": COLORS["YELLOW"],
    "mistral": COLORS["GREEN"],
    "uncaught": COLORS["BG_RED"] + COLORS["WHITE"],
}

LEVEL_COLORS = {
    "DEBUG": COLORS["DIM"],
    "INFO": COLORS["GREEN"],
    "WARNING": COLORS["YELLOW"],
    "WARN": COLORS["YELLOW"],
    "ERROR": COLORS["RED"],
    "CRITICAL": COLORS["BG_RED"] + COLORS["WHITE"],
}


def colorize(text: str, color: str) -> str:
    """Wrap text in color codes"""
    return f"{color}{text}{COLORS['RESET']}"


def format_log_line(timestamp: str, level: str, component: str, message: str,
                     use_colors: bool = True) -> str:
    """Format a single log line with colors"""
    if not use_colors:
        return f"[{timestamp}] [{level}] [{component}] {message}"

    level_color = LEVEL_COLORS.get(level, COLORS["WHITE"])
    comp_color = COMPONENT_COLORS.get(component, COLORS["WHITE"])

    ts = colorize(timestamp, COLORS["DIM"])
    lvl = colorize(f"{level:8}", level_color)
    comp = colorize(f"{component:20}", comp_color)

    # Special formatting for different message types
    msg_lower = message.lower()
    if any(w in msg_lower for w in ["complete", "done", "success", "✅"]):
        msg = colorize(message, COLORS["GREEN"])
    elif any(w in msg_lower for w in ["error", "failed", "❌", "block"]):
        msg = colorize(message, COLORS["RED"])
    elif any(w in msg_lower for w in ["warning", "skip", "⏭", "caution"]):
        msg = colorize(message, COLORS["YELLOW"])
    elif any(w in msg_lower for w in ["thinking", "calling", "executing", "⚡"]):
        msg = colorize(message, COLORS["CYAN"])
    elif "step" in msg_lower:
        msg = colorize(message, COLORS["BOLD"] + COLORS["CYAN"])
    else:
        msg = message

    return f"{ts} {lvl} {comp} {msg}"


class StructuredLogger:
    """Structured logging with multiple outputs"""

    _instances: dict[str, "StructuredLogger"] = {}

    def __new__(cls, name: str):
        if name not in cls._instances:
            cls._instances[name] = super().__new__(cls)
        return cls._instances[name]

    def __init__(self, name: str):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self.name = name
        self.log_dir = xli_path("logs")
        # Never let logging take the process down. If the log directory cannot
        # be created — a read-only filesystem, an XLI_CONFIG_DIR pointing
        # somewhere unwritable, a permissions problem — fall back to console
        # only instead of raising out of __init__. This used to be an
        # unconditional mkdir, which was only safe because ~/.xli is nearly
        # always writable.
        self.file_logging = True
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.file_logging = False

        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()

        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
            datefmt="%H:%M:%S"
        )

        if self.file_logging:
            # All logs
            fh = logging.FileHandler(self.log_dir / "xli.log", encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)

            # Errors only
            eh = logging.FileHandler(self.log_dir / "errors.log", encoding="utf-8")
            eh.setLevel(logging.ERROR)
            eh.setFormatter(formatter)
            self.logger.addHandler(eh)

            # Structured JSONL
            self.structured_path = self.log_dir / "structured.log"
        else:
            self.structured_path = None

        # Console with colors
        self.use_colors = sys.stderr.isatty() and "NO_COLOR" not in os.environ
        # Diagnostics go to stderr. Anything on stdout is program output that a
        # caller may pipe or parse (`xli tools schema --json`), and a stray log
        # line there turns valid JSON into garbage.
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(ch)

        self._error_count = 0
        self._setup_global_hook()

    def _setup_global_hook(self):
        """Catch uncaught exceptions"""
        original = sys.excepthook
        def hook(exc_type, exc_val, exc_tb):
            self.log_structured("CRITICAL", "uncaught",
                f"Uncaught: {exc_type.__name__}: {exc_val}",
                exc_info="".join(traceback.format_exception(exc_type, exc_val, exc_tb)))
            original(exc_type, exc_val, exc_tb)
        sys.excepthook = hook

    def log_structured(self, level: str, component: str, message: str,
                       details: dict | None = None, exc_info: str | None = None):
        """Write structured JSONL log + colored console output"""
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        entry = {
            "timestamp": timestamp,
            "level": level,
            "component": component,
            "message": message,
        }
        if details:
            entry["details"] = details
        if exc_info:
            entry["trace"] = exc_info

        if self.structured_path is not None:
            try:
                with open(self.structured_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            except OSError as e:
                # The directory went away or became unwritable. Say so once
                # through the console handler rather than raising, and stop
                # trying: a log write must never break the caller.
                self.structured_path = None
                self.logger.warning(f"structured logging disabled: {e}")

        # Colored console output
        colored_line = format_log_line(timestamp, level, component, message, self.use_colors)

        # Also to standard logger (file only, console gets colored).
        # logging.Logger.warn() is a deprecated alias — route WARN -> warning.
        _LEVEL_METHODS = {"WARN": "warning", "FATAL": "critical"}
        method_name = _LEVEL_METHODS.get(level.upper(), level.lower())
        method = getattr(self.logger, method_name, self.logger.info)
        method(colored_line)

    def log_error(self, component: str, message: str, exc: Exception | None = None,
                  details: dict | None = None):
        """Unified error logging"""
        self._error_count += 1
        exc_str = traceback.format_exc() if exc else None
        self.log_structured("ERROR", component, message, details, exc_str)
        self.logger.error(f"[{component}] {message}", exc_info=exc is not None)

    def log_nvim_error(self, source: str, message: str, details: dict | None = None):
        """Log nvim-side errors to shared file"""
        self.log_structured("ERROR", f"nvim.{source}", message, details)
        nvim_err = self.log_dir / "nvim_errors.log"
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{source}] {message}"
        if details:
            line += f" | {json.dumps(details, ensure_ascii=False, default=str)}"
        try:
            with open(nvim_err, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            self.logger.error(f"Failed to write nvim error: {e}")

    def get_error_count(self) -> int:
        """Return total error count"""
        return self._error_count


# Banner printer
def print_banner():
    """Print XLI startup banner"""
    banner = f"""
{COLORS["CYAN"]}{COLORS["BOLD"]}
    ██╗  ██╗██╗     ██╗
    ╚██╗██╔╝██║     ██║
     ╚███╔╝ ██║     ██║
     ██╔██╗ ██║     ██║
    ██╔╝ ██╗███████╗██║
    ╚═╝  ╚═╝╚══════╝╚═╝
{COLORS["RESET"]}
{COLORS["GREEN"]}    FIRE XLI PRO v4{COLORS["RESET"]}
{COLORS["DIM"]}    Multi-Agent AI Coding Assistant{COLORS["RESET"]}
"""
    print(banner)


def print_step_header(step_num: int, total: int, agent_name: str, status: str = "START"):
    """Print a colorful step header"""
    if status == "START":
        color = COLORS["CYAN"]
        icon = "▶"
    elif status == "DONE":
        color = COLORS["GREEN"]
        icon = "✓"
    else:
        color = COLORS["YELLOW"]
        icon = "⏭"
    print(f"\n{color}{COLORS['BOLD']}{'═' * 60}{COLORS['RESET']}")
    print(f"{color}{COLORS['BOLD']} {icon} STEP {step_num}/{total}: {agent_name.upper()} {status}{COLORS['RESET']}")
    print(f"{color}{COLORS['BOLD']}{'═' * 60}{COLORS['RESET']}")


def print_thinking(agent_name: str, thought: str):
    """Print agent thinking/reasoning"""
    comp_color = COMPONENT_COLORS.get(agent_name.lower(), COLORS["WHITE"])
    print(f"\n{comp_color}{COLORS['BOLD']}🧠 {agent_name.upper()} THINKING:{COLORS['RESET']}")
    print(f"{COLORS['DIM']}{'─' * 50}{COLORS['RESET']}")
    lines = thought.split("\n")
    for line in lines[:6]:  # Max 6 lines
        line = line.strip()
        if line and not line.startswith("📚") and "skill" not in line.lower():
            print(f"{COLORS['DIM']}  {line}{COLORS['RESET']}")
    print(f"{COLORS['DIM']}{'─' * 50}{COLORS['RESET']}")


def print_agent_output(agent_name: str, output: str, max_lines: int = 20):
    """Print agent output with color coding"""
    comp_color = COMPONENT_COLORS.get(agent_name.lower(), COLORS["WHITE"])
    print(f"\n{comp_color}{COLORS['BOLD']}📄 {agent_name.upper()} OUTPUT:{COLORS['RESET']}")
    print(f"{comp_color}{'─' * 50}{COLORS['RESET']}")
    lines = output.split("\n")
    shown = 0
    for line in lines:
        if shown >= max_lines:
            break
        # Skip empty MCP/skill noise
        if line.strip() and not line.startswith("<MCP") and "skill" not in line.lower()[:30]:
            print(f"  {line}")
            shown += 1
    if len(lines) > max_lines:
        print(f"{COLORS['DIM']}  ... ({len(lines) - max_lines} more lines){COLORS['RESET']}")
    print(f"{comp_color}{'─' * 50}{COLORS['RESET']}")


def get_logger(name: str) -> StructuredLogger:
    """Get or create StructuredLogger instance"""
    return StructuredLogger(name)
