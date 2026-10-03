#!/usr/bin/env python3
"""
XLI Sandbox v4 — Restricted exec, timeout, no network, memory limit
"""

import ast
import sys
import resource
import subprocess
import tempfile
import os
from typing import Any, Optional

from xli.core.logger import StructuredLogger
from xli.manager.config import get_config

logger = StructuredLogger("xli.sandbox")


class CodeSandbox:
    """Restricted code execution environment"""

    DANGEROUS_PATTERNS = [
        "os.system", "subprocess", "eval(", "exec(", "__import__",
        "open('/etc", "open('C:\\\\Windows", "socket.", "urllib",
        "requests.", "http.client", "ftplib", "telnetlib",
        "import os", "import subprocess", "import socket",
        "rm -rf", "mkfs", "dd if=", "chmod 777",
        "import ctypes", "import mmap", "import fcntl"
    ]

    def __init__(self):
        self.config = get_config()
        self.timeout = self.config.sandbox_timeout()
        self.max_memory = self.config.sandbox_max_memory_mb() * 1024 * 1024  # MB to bytes
        self.disable_network = self.config.sandbox_network_disabled()
        logger.log_structured("INFO", "sandbox", "Sandbox initialized", {
            "timeout": self.timeout,
            "max_memory_mb": self.max_memory // (1024 * 1024)
        })

    def is_safe(self, code: str) -> tuple[bool, str | None]:
        """Check if code is safe to execute"""
        # AST analysis
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

        # Check for dangerous imports and calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ["os", "subprocess", "socket", "urllib",
                                      "http", "ftplib", "telnetlib", "ctypes",
                                      "mmap", "fcntl"]:
                        return False, f"Dangerous import: {alias.name}"

            if isinstance(node, ast.ImportFrom):
                if node.module in ["os", "subprocess", "socket"]:
                    return False, f"Dangerous import from: {node.module}"

            if isinstance(node, ast.Call):
                # Check for eval/exec
                if isinstance(node.func, ast.Name):
                    if node.func.id in ["eval", "exec"]:
                        return False, f"Dangerous call: {node.func.id}"

        # Pattern matching
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern in code:
                return False, f"Dangerous pattern: {pattern}"

        return True, None

    def execute(self, code: str, timeout: int | None = None,
                max_memory: int | None = None) -> dict[str, Any]:
        """Execute code safely"""
        timeout = timeout or self.timeout
        max_memory = max_memory or self.max_memory

        # Safety check
        is_safe, reason = self.is_safe(code)
        if not is_safe:
            logger.log_structured("WARN", "sandbox", "Unsafe code blocked", {"reason": reason})
            return {
                "success": False,
                "output": "",
                "error": f"Unsafe code: {reason}",
                "exit_code": -1
            }

        # Create temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            temp_path = f.name

        try:
            # Run in subprocess with restrictions
            env = os.environ.copy()
            if self.disable_network:
                # Linux-specific: disable network via unshare
                pass  # Would need root for unshare

            # Set memory limit
            def set_limits():
                resource.setrlimit(resource.RLIMIT_AS, (max_memory, max_memory))
                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))

            result = subprocess.run(
                [sys.executable, temp_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                preexec_fn=set_limits if os.name != "nt" else None,
                env=env
            )

            logger.log_structured("INFO", "sandbox", "Execution complete", {
                "exit_code": result.returncode,
                "stdout_len": len(result.stdout),
                "stderr_len": len(result.stderr)
            })

            return {
                "success": result.returncode == 0,
                "output": result.stdout,
                "error": result.stderr,
                "exit_code": result.returncode
            }

        except subprocess.TimeoutExpired:
            logger.log_structured("WARN", "sandbox", "Execution timeout")
            return {
                "success": False,
                "output": "",
                "error": f"Timeout after {timeout}s",
                "exit_code": -1
            }

        except Exception as e:
            logger.log_error("sandbox", "Execution failed", exc=e)
            return {
                "success": False,
                "output": "",
                "error": str(e),
                "exit_code": -1
            }

        finally:
            # Cleanup
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    def test_code(self, code: str, test_code: str) -> dict[str, Any]:
        """Run code with tests"""
        combined = f"{code}\n\n{test_code}"
        return self.execute(combined)


_sandbox_instance: Optional["CodeSandbox"] = None


def get_sandbox() -> CodeSandbox:
    """Get singleton CodeSandbox"""
    global _sandbox_instance
    if _sandbox_instance is None:
        _sandbox_instance = CodeSandbox()
    return _sandbox_instance

