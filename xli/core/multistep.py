#!/usr/bin/env python3
"""
XLI Multi-Step Agent Loop v4.3 — FIXED: sub-agents inherit tools, create real files
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from xli.core.logger import StructuredLogger, COLORS
from xli.providers.base import get_provider
from xli.core.skills import get_skills_manager
from xli.core.shell_safety import is_shell_command_safe

logger = StructuredLogger("xli.multistep")


class StepType(Enum):
    THINK = "think"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"
    DONE = "done"
    ERROR = "error"
    IDLE = "idle"


@dataclass
class Step:
    step_num: int
    step_type: StepType
    content: str
    tool_name: str = None
    tool_args: dict = None
    tool_result: str = None
    error: str = None


class Tool:
    def __init__(self, name: str, description: str, schema: dict):
        self.name = name
        self.description = description
        self.schema = schema
        self.pre_hooks: list[Callable] = []
        self.post_hooks: list[Callable] = []

    async def execute(self, **kwargs) -> str:
        raise NotImplementedError

    def add_pre_hook(self, hook: Callable):
        self.pre_hooks.append(hook)

    def add_post_hook(self, hook: Callable):
        self.post_hooks.append(hook)

    async def run(self, **kwargs) -> str:
        for hook in self.pre_hooks:
            await hook(self.name, kwargs)
        try:
            result = await self.execute(**kwargs)
            for hook in self.post_hooks:
                await hook(self.name, kwargs, result)
            return result
        except Exception as e:
            for hook in self.post_hooks:
                await hook(self.name, kwargs, None, str(e))
            raise


class PythonExecTool(Tool):
    """Runs a Python snippet through CodeSandbox (AST-checked, resource-limited,
    separate subprocess). Use this instead of BashTool for anything that's actually
    Python — it gets real static analysis of imports/calls, not just a shell blocklist."""

    def __init__(self):
        super().__init__(
            name="python_exec",
            description="Execute a short Python snippet in a restricted sandbox (no os/subprocess/socket, memory+CPU limited). Returns stdout/stderr.",
            schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to run"},
                },
                "required": ["code"]
            }
        )

    async def execute(self, code: str) -> str:
        from xli.core.sandbox import get_sandbox
        sandbox = get_sandbox()
        result = sandbox.execute(code)
        if result["success"]:
            return result["output"] or "(no output)"
        return f"ERROR: {result['error']}"


class BashTool(Tool):
    """Execute bash — FIXED brace expansion"""

    def __init__(self):
        super().__init__(
            name="bash",
            description="Execute bash shell commands",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout", "default": 30}
                },
                "required": ["command"]
            }
        )
    async def execute(self, command: str, timeout: int = 30) -> str:
        import re
        import subprocess
        from xli.core.exec_guard import run_guarded_shell

        safe, reason = is_shell_command_safe(command)
        if not safe:
            logger.log_structured("WARN", "multistep", "Blocked unsafe command", {"command": command, "reason": reason})
            return f"ERROR: Blocked ({reason}): {command}"

        # FIX brace expansion: mkdir -p dir/{a,b,c}
        if '{' in command and '}' in command and 'mkdir' in command:
            match = re.search(r'(mkdir\s+-p\s+)(.+)/(.+)\{([^}]+)\}(.*)', command)
            if match:
                prefix = match.group(2) + "/" + match.group(3)
                items = match.group(4).split(',')
                suffix = match.group(5)
                for item in items:
                    subcmd = f"mkdir -p {prefix}{item.strip()}{suffix}"
                    try:
                        run_guarded_shell(subcmd, timeout=timeout)
                    except Exception:
                        pass
                return f"Created dirs: {prefix}{{{match.group(4)}}}{suffix}"

        try:
            result = run_guarded_shell(command, timeout=timeout)
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]: {result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit {result.returncode}]"

            print(f"\n{COLORS['YELLOW']}$ {command}{COLORS['RESET']}")
            if output:
                print(output[:1500])
            return output[:2000]
        except subprocess.TimeoutExpired:
            return f"ERROR: Timeout after {timeout}s"
        except Exception as e:
            return f"ERROR: {e}"


class FileReadTool(Tool):
    def __init__(self):
        super().__init__(
            name="read",
            description="Read file contents",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 100}
                },
                "required": ["path"]
            }
        )

    async def execute(self, path: str, offset: int = 0, limit: int = 100) -> str:
        from pathlib import Path
        try:
            p = Path(path)
            if not p.exists():
                return f"ERROR: File not found: {path}"
            lines = p.read_text().split('\n')
            selected = lines[offset:offset + limit]
            content = '\n'.join(selected)
            print(f"\n{COLORS['CYAN']}📖 Reading: {path}{COLORS['RESET']}")
            print(content[:500])
            return content
        except Exception as e:
            return f"ERROR: {e}"


class FileWriteTool(Tool):
    def __init__(self):
        super().__init__(
            name="write",
            description="Write or overwrite file — USE THIS to create files!",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full file path"},
                    "content": {"type": "string", "description": "File content"}
                },
                "required": ["path", "content"]
            }
        )

    async def execute(self, path: str, content: str) -> str:
        from pathlib import Path
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
            print(f"\n{COLORS['GREEN']}✅ Written: {path} ({len(content)} chars){COLORS['RESET']}")
            return f"File written: {path} ({len(content)} chars)"
        except Exception as e:
            return f"ERROR: {e}"


class FileEditTool(Tool):
    def __init__(self):
        super().__init__(
            name="edit",
            description="Edit file by replacing text",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"}
                },
                "required": ["path", "old_string", "new_string"]
            }
        )

    async def execute(self, path: str, old_string: str, new_string: str) -> str:
        from pathlib import Path
        try:
            p = Path(path)
            if not p.exists():
                return f"ERROR: File not found: {path}"
            content = p.read_text()
            if old_string not in content:
                return "ERROR: String not found"
            new_content = content.replace(old_string, new_string, 1)
            p.write_text(new_content)
            print(f"\n{COLORS['GREEN']}✏️  Edited: {path}{COLORS['RESET']}")
            return f"File edited: {path}"
        except Exception as e:
            return f"ERROR: {e}"


class TaskTool(Tool):
    """Delegate to sub-agent — INHERITS parent tools!"""

    def __init__(self, parent_tools: dict[str, Tool] = None, coordinator=None, provider=None):
        super().__init__(
            name="task",
            description="Delegate to sub-agent (@coder, @tester, @debugger, @optimizer, @reviewer). Sub-agents inherit ALL tools including write/bash!",
            schema={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Sub-agent: @coder, @tester, @debugger, @optimizer, @reviewer"},
                    "prompt": {"type": "string", "description": "Task. MUST include: 1) What files to create 2) Full paths"}
                },
                "required": ["agent", "prompt"]
            }
        )
        self.coordinator = coordinator
        self.parent_tools = parent_tools or {}
        self.provider = provider

    async def execute(self, agent: str, prompt: str) -> str:
        agent = agent.lstrip('@')
        print(f"\n{COLORS['MAGENTA']}🤖 Sub-agent @{agent} starting...{COLORS['RESET']}")

        try:
            # Create sub-agent with inherited tools
            sub = MultiStepAgent(
                name=agent,
                system_prompt=f"""You are {agent} specialist. CRITICAL RULES:
1. You MUST create files using the write tool: <tool>{{{{"name": "write", "args": {{{{"path": "/full/path", "content": "..."}}}}}}}}</tool>
2. You MUST run commands using bash tool: <tool>{{{{"name": "bash", "args": {{{{"command": "..."}}}}}}}}</tool>
3. NEVER just output code in text — always write to files!
4. Be concise. Use at most 3-4 tools.
5. Finish with <done>summary</done>""",
                max_steps=6,
                provider=self.provider
            )

            # INHERIT all parent tools
            for name, tool in self.parent_tools.items():
                if name not in sub.tools:
                    sub.register_tool(tool)

            result = await sub.run(prompt)

            if result.startswith("[ERROR:"):
                print(f"{COLORS['RED']}❌ @{agent} error: {result[:100]}{COLORS['RESET']}")
                return f"[SUB-AGENT @{agent} ERROR]: {result}\nDelegate to @debugger."

            # Count files created by sub-agent
            files_created = [s.tool_args.get("path") for s in sub.steps
                           if s.step_type == StepType.TOOL_RESULT and s.tool_name == "write"]
            if files_created:
                print(f"{COLORS['GREEN']}📁 @{agent} created: {', '.join(files_created)}{COLORS['RESET']}")

            return f"[Sub-agent @{agent} result]:\n{result}"
        except Exception as e:
            err_msg = f"[SUB-AGENT @{agent} FAILED]: {e}"
            print(f"{COLORS['RED']}❌ {err_msg}{COLORS['RESET']}")
            return err_msg + "\nDelegate to @debugger."


class MultiStepAgent:
    """Multi-step with tool inheritance for sub-agents"""

    def __init__(self, name: str, system_prompt: str, max_steps: int = 10, provider=None):
        self.name = name
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.tools: dict[str, Tool] = {}
        self.steps: list[Step] = []
        self.history: list[dict] = []
        self.logger = StructuredLogger(f"multistep.{name}")
        self.idle_count = 0
        self.max_idle = 2
        self.skills = get_skills_manager()
        self.error_count = 0
        # Injected provider (e.g. FakeProvider in tests) takes priority over the
        # global get_provider() singleton, so this class doesn't force a real
        # API key just to be constructed/run in tests.
        self._provider = provider

        # Register base tools
        self.register_tool(BashTool())
        self.register_tool(PythonExecTool())
        self.register_tool(FileReadTool())
        self.register_tool(FileWriteTool())
        self.register_tool(FileEditTool())
        # TaskTool gets parent_tools reference for inheritance
        self.register_tool(TaskTool(parent_tools=self.tools, provider=self._provider))

    def register_tool(self, tool: Tool):
        self.tools[tool.name] = tool
        self.logger.log_structured("DEBUG", "multistep", f"Tool: {tool.name}")

    def get_tools_schema(self) -> list[dict]:
        return [
            {"name": name, "description": tool.description, "parameters": tool.schema}
            for name, tool in self.tools.items()
        ]

    def _build_system_prompt(self) -> str:
        tools_desc = "\n\n".join([
            f"Tool: {name}\nDescription: {tool.description}\nSchema: {json.dumps(tool.schema)}"
            for name, tool in self.tools.items()
        ])

        skills_ctx = self.skills.get_skills_context(self.name, max_skills=4)

        parts = [self.system_prompt]
        if skills_ctx:
            parts.append(skills_ctx)
        parts.append(f"\nAVAILABLE TOOLS:\n\n{tools_desc}\n")
        parts.append('USE TOOLS: <tool>{"name": "TOOL_NAME", "args": {...}}</tool>')
        parts.append("FINISH: <done>answer</done>")
        parts.append("If [ERROR: ...] appears, FIX it or delegate to @debugger via task tool.")
        return "\n\n".join(parts)

    async def run(self, task: str) -> str:
        self.logger.log_structured("INFO", "multistep", f"Task: {task[:100]}")

        print(f"\n{COLORS['CYAN']}{'═'*60}{COLORS['RESET']}")
        print(f"{COLORS['CYAN']}{COLORS['BOLD']}🚀 {self.name.upper()}{COLORS['RESET']}")
        print(f"{COLORS['CYAN']}{'═'*60}{COLORS['RESET']}")
        print(f"{COLORS['DIM']}Task: {task[:80]}{COLORS['RESET']}\n")

        messages = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user", "content": task}
        ]

        final_answer = ""
        tools_used = 0

        for step_num in range(self.max_steps):
            print(f"\n{COLORS['YELLOW']}── Step {step_num + 1}/{self.max_steps} ──{COLORS['RESET']}")

            provider = self._provider if self._provider is not None else get_provider()

            try:
                response = await provider.chat(messages, temperature=0.4)
            except Exception as e:
                logger.log_error("multistep", f"LLM failed step {step_num + 1}", exc=e)
                self.error_count += 1
                error_msg = f"[ERROR: LLM failed: {e}]"
                messages.append({"role": "assistant", "content": error_msg})
                messages.append({"role": "user", "content": "LLM failed. Try simpler approach or <done>."})
                self.steps.append(Step(step_num, StepType.ERROR, error_msg))
                continue

            if response.startswith("[ERROR:"):
                self.error_count += 1
                print(f"{COLORS['RED']}❌ Provider error: {response[:100]}{COLORS['RESET']}")
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": "API error. Try simpler or <done>."})
                self.steps.append(Step(step_num, StepType.ERROR, response))
                continue

            if "<done>" in response:
                parts = response.split("<done>")
                final_answer = parts[0].strip()
                if len(parts) > 1 and parts[1].strip():
                    final_answer += "\n" + parts[1].replace("</done>", "").strip()
                self.steps.append(Step(step_num, StepType.DONE, final_answer))
                print(f"\n{COLORS['GREEN']}{COLORS['BOLD']}✅ DONE ({step_num + 1} steps, {tools_used} tools){COLORS['RESET']}")
                break

            tool_json = self._extract_tool_call(response)

            if tool_json:
                tool_name = tool_json.get("name")
                tool_args = tool_json.get("args", {})
                print(f"{COLORS['MAGENTA']}🔧 {tool_name}{COLORS['RESET']}")

                step = Step(step_num, StepType.TOOL_CALL, response[:200],
                           tool_name=tool_name, tool_args=tool_args)
                self.steps.append(step)
                tools_used += 1

                if tool_name in self.tools:
                    try:
                        result = await self.tools[tool_name].run(**tool_args)
                        step.tool_result = result
                        step.step_type = StepType.TOOL_RESULT

                        if result.startswith("ERROR:"):
                            self.error_count += 1
                            print(f"{COLORS['RED']}⚠️  Tool error: {result[:100]}{COLORS['RESET']}")

                        messages.append({"role": "assistant", "content": response})
                        messages.append({"role": "user", "content": f"Tool '{tool_name}': {result[:1500]}"})

                    except Exception as e:
                        step.error = str(e)
                        step.step_type = StepType.TOOL_ERROR
                        self.error_count += 1
                        print(f"{COLORS['RED']}❌ Tool error: {e}{COLORS['RESET']}")
                        messages.append({"role": "assistant", "content": response})
                        messages.append({"role": "user", "content": f"Tool '{tool_name}' error: {e}. Try alternative."})
                else:
                    print(f"{COLORS['RED']}❌ Unknown tool: {tool_name}{COLORS['RESET']}")
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"Tool '{tool_name}' not found. Available: {list(self.tools.keys())}"})
            else:
                self.steps.append(Step(step_num, StepType.THINK, response[:200]))

                if len(response.strip()) < 50 or "i will" in response.lower():
                    self.idle_count += 1
                    if self.idle_count >= self.max_idle:
                        print(f"{COLORS['YELLOW']}⚠️  Early exit: idle{COLORS['RESET']}")
                        final_answer = response.strip() or "[Completed]"
                        break
                else:
                    self.idle_count = 0

                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": "Continue. Use tools or <done>."})

        else:
            self.logger.log_structured("WARN", "multistep", f"Max steps ({self.max_steps})")
            if not final_answer:
                final_answer = "[MAX STEPS]"
            print(f"\n{COLORS['YELLOW']}⚠️  Max steps{COLORS['RESET']}")

        if self.error_count > 0:
            print(f"{COLORS['RED']}⚠️  {self.error_count} errors{COLORS['RESET']}")

        return final_answer

    def _extract_tool_call(self, text: str) -> dict | None:
        import re

        patterns = [
            (r'<tool>\s*(\{.*?\})\s*</tool>', re.DOTALL),
            (r'```json\s*(\{.*?\})\s*```', re.DOTALL),
        ]

        for pattern, flags in patterns:
            match = re.search(pattern, text, flags)
            if match:
                try:
                    return json.loads(match.group(1))
                except Exception:
                    pass

        match = re.search(r'(\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"args"\s*:)', text, re.DOTALL)
        if match:
            start = match.start()
            brace_count = 0
            end = start
            for i, c in enumerate(text[start:]):
                if c == '{':
                    brace_count += 1
                elif c == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end = start + i + 1
                        break
            try:
                return json.loads(text[start:end])
            except Exception:
                pass

        return None

    def get_steps_summary(self) -> str:
        lines = [f"\n{COLORS['CYAN']}Steps ({len(self.steps)}):{COLORS['RESET']}"]
        for step in self.steps:
            if step.step_type == StepType.TOOL_CALL:
                lines.append(f"  {COLORS['MAGENTA']}🔧 {step.tool_name}({step.tool_args}){COLORS['RESET']}")
            elif step.step_type == StepType.TOOL_RESULT:
                lines.append(f"  {COLORS['GREEN']}  -> {step.tool_result[:80]}{COLORS['RESET']}")
            elif step.step_type == StepType.TOOL_ERROR:
                lines.append(f"  {COLORS['RED']}  ❌ {step.error}{COLORS['RESET']}")
            elif step.step_type == StepType.ERROR:
                lines.append(f"  {COLORS['RED']}💥 {step.content[:80]}{COLORS['RESET']}")
            elif step.step_type == StepType.DONE:
                lines.append(f"  {COLORS['GREEN']}✅ DONE{COLORS['RESET']}")
            else:
                lines.append(f"  {COLORS['DIM']}💭{COLORS['RESET']}")
        return "\n".join(lines)
