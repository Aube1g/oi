#!/usr/bin/env python3
"""
XLI Chain v4.2 — RESILIENT: skip errors, skills injection, MCP routing, LSP
"""

import re
import json
from dataclasses import dataclass, field
from pathlib import Path

from xli.core.logger import StructuredLogger, COLORS, print_step_header
from xli.providers.base import get_provider
from xli.core.skills import get_skills_manager
from xli.core.memory import get_memory
from xli.mcp.bridge import run_mcp_pre_step, run_mcp_post_step
from xli.mcp.client import MCPClient
from xli.core.shell_safety import is_shell_command_safe
from xli.mcp.registry import get_registry

logger = StructuredLogger("xli.chain")


class AutonomyMode:
    """Single lever for "how much should XliCore do on its own", instead of
    enable_self_correction and IdleReconciler being two independent things a
    caller has to remember to wire up separately.

    - MANUAL:   old default behavior. DEBUGGER runs once, blind. Idle
                reconciliation never runs unless a caller drives
                XliCore.reconciler themselves. Nothing changes for existing
                callers that don't pass `mode=`.
    - ASSISTED: self-correction loop is on (real pytest-driven DEBUGGER<->TESTER
                retries), but idle reconciliation still needs to be driven
                explicitly — useful when something else already owns the
                idle/cron loop (e.g. a UI event loop) and just wants better
                in-chain correction.
    - AUTONOMOUS: self-correction on, AND idle reconciliation is driven
                automatically: every run_chain() call checks maybe_run() at
                the start (catching a real gap since the previous call) and
                notes activity at the end, so a sequence of run_chain()
                calls with gaps between them (e.g. a human pausing between
                tasks) gets reconciled without any extra wiring. Still
                bounded by idle_threshold_seconds — a single run_chain()
                call doesn't itself count as "idle."
    """
    MANUAL = "manual"
    ASSISTED = "assisted"
    AUTONOMOUS = "autonomous"

    _ALL = (MANUAL, ASSISTED, AUTONOMOUS)

    @classmethod
    def validate(cls, mode: str) -> str:
        if mode not in cls._ALL:
            raise ValueError(f"Unknown AutonomyMode: {mode!r}. Expected one of {cls._ALL}")
        return mode

    @classmethod
    def from_env(cls, env_var: str = "XLI_AUTONOMY_MODE") -> str:
        """Read the mode from an env var, falling back to MANUAL — used by
        UIs (headless.py, tui.py) that don't have their own --mode CLI flag
        wired up yet. An invalid value logs a warning and falls back to
        MANUAL rather than raising, since this runs at UI startup where a
        crash over a typo'd env var would be a bad experience."""
        import os
        raw = os.environ.get(env_var)
        if not raw:
            return cls.MANUAL
        try:
            return cls.validate(raw.strip().lower())
        except ValueError:
            logger.log_structured("WARN", "chain", f"Invalid {env_var}={raw!r}, falling back to MANUAL", {})
            return cls.MANUAL


@dataclass
class ChainResult:
    """Result of agent chain execution"""
    plan: str = ""
    coder: str = ""
    debugger: str = ""
    tester: str = ""
    optimizer: str = ""
    reviewer: str = ""
    final: str = ""
    success: bool = False
    files_created: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class XliAgent:
    """Single agent with skills + MCP context"""

    def __init__(self, name: str, agent_id: str, system_prompt: str, provider=None, layer_store=None):
        self.name = name
        self.agent_id = agent_id
        self.system_prompt = system_prompt
        self.logger = StructuredLogger(f"agent.{name.upper()}")
        # Accept an injected provider (used by tests with FakeProvider) so this
        # class doesn't force a real API key just to be constructed.
        self.provider = provider if provider is not None else get_provider()
        self.memory = get_memory()
        # layer_store is a DIFFERENT kind of context than self.memory, kept
        # separate on purpose rather than folded into one system:
        #   - self.memory (ConversationMemory, memory.py) is cross-session,
        #     keyword-searched, sqlite-backed — "have we seen a similar task
        #     before, possibly in a totally different run/project?"
        #   - layer_store (LayerStore, layers.py) is this run's own recent
        #     history — structured, hash-verified, parent-chained — "what did
        #     THIS chain actually just do, in order?"
        # think() below queries both and merges them into the prompt; neither
        # replaces the other.
        self.layer_store = layer_store
        self.skills = get_skills_manager()
        self.mcp_client = MCPClient()
        self.mcp_registry = get_registry()

    def _get_skills_prompt(self) -> str:
        """Inject relevant skills into prompt"""
        skills_ctx = self.skills.get_skills_context(self.name, max_skills=6)
        if skills_ctx:
            return f"\n📚 **RELEVANT SKILLS FOR YOU:**\n{skills_ctx}\n"
        return ""

    def _get_mcp_tools_prompt(self) -> str:
        """Show available MCP tools"""
        lines = ["\n🔧 **AVAILABLE MCP TOOLS (use via bash):**"]

        # Shell helper
        if self.mcp_registry.is_enabled("shell_helper"):
            lines.append("  - shell_helper: safe shell commands (use: bash with command)")

        # Code formatter
        if self.mcp_registry.is_enabled("code_formatter"):
            lines.append("  - code_formatter: format code with black/ruff")

        # File manager
        if self.mcp_registry.is_enabled("file_manager"):
            lines.append("  - file_manager: advanced file operations")

        # LSP
        if self.mcp_registry.is_enabled("lsp"):
            lines.append("  - lsp: language server protocol (diagnostics, hover, goto)")

        # Auto tester
        if self.mcp_registry.is_enabled("auto_tester"):
            lines.append("  - auto_tester: run pytest, coverage")

        # Security scanner
        if self.mcp_registry.is_enabled("security_scanner"):
            lines.append("  - security_scanner: bandit security check")

        if len(lines) > 1:
            return "\n".join(lines) + "\n"
        return ""

    def _get_layer_prompt(self) -> str:
        """Compact notes from this run's own LayerStore history — not the
        cross-session ConversationMemory, see __init__ comment. Cheap: reads
        the index only, never full layer content."""
        if self.layer_store is None:
            return ""
        window = self.layer_store.context_window(limit=6)
        if not window or window == "(no history yet)":
            return ""
        return f"\n🧱 **RECENT CHAIN HISTORY (this run):**\n{window}\n"

    async def think(self, task: str, context: str = "", error_context: str = "") -> str:
        """Agent thinks with skills and MCP context"""
        self.logger.log_structured("INFO", f"agent.{self.name.upper()}", "Thinking")

        skills_ctx = self._get_skills_prompt()
        mcp_ctx = self._get_mcp_tools_prompt()
        memory_ctx = self.memory.get_context_for_task(task)
        layer_ctx = self._get_layer_prompt()

        # Build enhanced prompt
        prompt = f"""{self.system_prompt}

{skills_ctx}
{mcp_ctx}
{memory_ctx}
{layer_ctx}
{context}

{error_context}

TASK: {task}

**FILE TOOLS (use these EXACT formats):**
To CREATE file: <tool>{{"name": "write", "args": {{"path": "/full/path", "content": "..."}}}}</tool>
To EDIT file: <tool>{{"name": "edit", "args": {{"path": "/full/path", "old_string": "...", "new_string": "..."}}}}</tool>
To RUN command: <tool>{{"name": "bash", "args": {{"command": "..."}}}}</tool>

**IMPORTANT RULES:**
1. Use write/edit tools to create/modify files — NEVER use <file> tags
2. For mkdir with multiple dirs, use: bash "mkdir -p dir1 && mkdir -p dir2"
3. Always use <done>your answer</done> when finished
4. If you see [ERROR: ...] from previous agent, FIX it
"""

        try:
            response = await self.provider.chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": task}
            ], temperature=0.4)

            self.logger.log_structured("DEBUG", f"agent.{self.name.upper()}",
                                      f"Response: {len(response)} chars")
            return response

        except Exception as e:
            self.logger.log_error(f"agent.{self.name.upper()}", "Think failed", exc=e)
            return f"[ERROR: {e}]"


class XliCore:
    """Core orchestrator — RESILIENT with error routing"""

    def __init__(self, env, provider=None, layer_store=None,
                 mode: str = AutonomyMode.MANUAL, idle_threshold_seconds: int = 300):
        self.env = env
        self.logger = StructuredLogger("xli.chain")
        self.mode = AutonomyMode.validate(mode)
        # Every agent step gets committed as a layer (see xli/core/layers.py) so
        # the chain's history is queryable as compact notes, with full responses
        # available on disk and self-verified on read — not just held in
        # self.result attributes that vanish when the process exits.
        from xli.core.layers import get_layer_store
        self.layers = layer_store or get_layer_store()
        self._last_layer: str | None = None
        # Was missing entirely — _run_agent() calls self.memory.save_conversation()
        # on every step, which raised AttributeError before this was ever run
        # end-to-end. get_memory() is a cheap singleton (see memory.py).
        self.memory = get_memory()
        # One reconciler per XliCore, always constructed (cheap — no threads,
        # no I/O until maybe_run() actually decides to act) so `mode` can be
        # changed later on an existing instance without reconstructing it.
        # AUTONOMOUS is the only mode that actually calls maybe_run()
        # automatically; other modes leave self.reconciler available for a
        # caller to drive by hand (e.g. from a UI's own idle/cron loop).
        from xli.core.reconciler import IdleReconciler
        self.reconciler = IdleReconciler(self.layers, idle_threshold_seconds=idle_threshold_seconds)
        self.agents = {
            "PLANNER": XliAgent("PLANNER", "ag_planner",
                "You are PLANNER. Create detailed plans. Use write/bash tools.", provider=provider, layer_store=self.layers),
            "CODER": XliAgent("CODER", "ag_coder",
                "You are CODER. Write production code. Use write tool. NEVER use <file> tags.", provider=provider, layer_store=self.layers),
            "DEBUGGER": XliAgent("DEBUGGER", "ag_debugger",
                "You are DEBUGGER. Fix errors. Use bash to diagnose, write/edit to fix.", provider=provider, layer_store=self.layers),
            "TESTER": XliAgent("TESTER", "ag_tester",
                "You are TESTER. Write pytest tests. Use write tool.", provider=provider, layer_store=self.layers),
            "OPTIMIZER": XliAgent("OPTIMIZER", "ag_optimizer",
                "You are OPTIMIZER. Improve code. Use edit tool.", provider=provider, layer_store=self.layers),
            "REVIEWER": XliAgent("REVIEWER", "ag_reviewer",
                "You are REVIEWER. Review code quality. Output review.", provider=provider, layer_store=self.layers),
        }
        self.result = ChainResult()
        self.files_created = []
        self.errors = []

    async def run_chain(self, task: str, skip_questions: bool = False,
                       stream: bool = False, enable_self_correction: bool | None = None,
                       max_correction_iterations: int = 3) -> ChainResult:
        """Run chain with error routing.

        KNOWN GAP: `stream` is accepted but currently unused inside this
        method — every agent step runs to completion and only then updates
        self.result, so callers (tui.py, headless.py) that pass stream=True
        expecting incremental output currently get none. TuiStreamingHandler
        exists in xli.core.streaming but nothing here calls it per-chunk.
        Wiring real streaming would mean threading a per-step callback (or
        an async generator) through _run_agent -> agent.think() -> the
        provider's own `stream()` method. Left as a documented gap rather
        than silently faked, since picking the callback/generator shape is
        an interface decision, not a bugfix.

        enable_self_correction=True upgrades the DEBUGGER step from "runs once,
        blind, before any test even exists" to a real loop: after TESTER writes
        a test file, actually run pytest (via exec_guard) and only keep
        looping DEBUGGER<->TESTER while it's genuinely still failing, bounded
        by max_correction_iterations. See self_correcting_chain.py.

        If left as None (the default), self-correction is instead decided by
        `self.mode` (see AutonomyMode): on for ASSISTED/AUTONOMOUS, off for
        MANUAL. Passing True/False explicitly always overrides the mode for
        this one call, same as before — existing callers that pass a bool
        keep exactly their old behavior.

        In AUTONOMOUS mode, this also drives IdleReconciler automatically
        across calls: maybe_run() is checked at the START of this call
        (before anything else), then activity is (re)noted at the end. That
        order matters — it's what lets a real gap *between* two run_chain()
        calls (a human pausing, a queue going idle) actually get caught: the
        idle clock starts ticking when the previous call finished, and the
        next call's opening maybe_run() check is what notices the gap before
        resetting the clock for its own duration. Checking at the end of the
        same call that just noted its own start would almost never fire,
        since a chain run itself typically takes far less time than
        idle_threshold_seconds — it'd just be measuring the chain's own
        runtime, not real idle time. Other modes leave self.reconciler
        untouched; any caller can still call note_activity()/maybe_run() on
        it directly (e.g. from a UI's own event loop) for finer control.
        """
        if self.mode == AutonomyMode.AUTONOMOUS:
            reconcile_report = self.reconciler.maybe_run()
            if reconcile_report:
                self.logger.log_structured("INFO", "chain", "Idle reconciliation ran before chain start", {
                    "verify": reconcile_report["verify"].summary(),
                })
            self.reconciler.note_activity()

        if enable_self_correction is None:
            enable_self_correction = self.mode in (AutonomyMode.ASSISTED, AutonomyMode.AUTONOMOUS)

        self.logger.log_structured("INFO", "chain", f"Starting chain: {task[:100]}")

        # Step 1: PLAN
        print_step_header(1, 6, "PLAN", "START")
        plan_response = await self._run_agent("PLANNER", task)
        self.result.plan = plan_response
        await self._process_tools(plan_response, layer_id=self._last_layer)
        print_step_header(1, 6, "PLAN", "DONE")

        # Step 2: CODER
        print_step_header(2, 6, "CODER", "START")
        coder_task = f"Implement: {task}\n\nPlan: {plan_response[:500]}"
        coder_response = await self._run_agent("CODER", coder_task)
        self.result.coder = coder_response
        await self._process_tools(coder_response, layer_id=self._last_layer)

        # Check for errors — route to DEBUGGER
        if self._has_errors(coder_response):
            print_step_header(2, 6, "CODER", "ERRORS DETECTED → DEBUGGER")
            self.errors.append(f"CODER: {self._extract_errors(coder_response)}")
        print_step_header(2, 6, "CODER", "DONE")

        # Step 3: DEBUGGER (always runs, but with error context if needed)
        print_step_header(3, 6, "DEBUGGER", "START")
        error_ctx = ""
        if self.errors:
            error_ctx = "\n\n**ERRORS FROM PREVIOUS AGENTS:**\n" + "\n".join(self.errors)
        debug_task = f"Debug this code:\n{coder_response[:1000]}{error_ctx}"
        debug_response = await self._run_agent("DEBUGGER", debug_task)
        self.result.debugger = debug_response
        await self._process_tools(debug_response, layer_id=self._last_layer)
        print_step_header(3, 6, "DEBUGGER", "DONE")

        # Step 4: TESTER
        print_step_header(4, 6, "TESTER", "START")
        test_task = f"Write tests for:\n{coder_response[:1000]}"
        test_response = await self._run_agent("TESTER", test_task)
        self.result.tester = test_response
        await self._process_tools(test_response, layer_id=self._last_layer)
        print_step_header(4, 6, "TESTER", "DONE")

        if enable_self_correction:
            test_files = [f for f in self.files_created if "test_" in Path(f).name or "_test.py" in f]
            if test_files:
                print_step_header(4, 6, "TESTER→DEBUGGER", "SELF-CORRECTION LOOP")
                from xli.core.self_correcting_chain import SelfCorrectingChain
                sc = SelfCorrectingChain(
                    coder=self.agents["CODER"],
                    debugger=self.agents["DEBUGGER"],
                    layer_store=self.layers,
                    max_iterations=max_correction_iterations,
                )
                sc_result = await sc.run(task=debug_task, target_file=coder_task, test_file=test_files[0])
                self.result.debugger += f"\n\n[self-correction: {sc_result.goal.summary()}]"
                if not sc_result.passed:
                    self.errors.append(f"DEBUGGER: tests still failing after {max_correction_iterations} attempts")
            else:
                logger.log_structured("DEBUG", "chain", "Self-correction enabled but no test file found to run")

        # Step 5: OPTIMIZER
        print_step_header(5, 6, "OPTIMIZER", "START")
        opt_task = f"Optimize:\n{coder_response[:1000]}\n\nTests:\n{test_response[:500]}"
        opt_response = await self._run_agent("OPTIMIZER", opt_task)
        self.result.optimizer = opt_response
        await self._process_tools(opt_response, layer_id=self._last_layer)
        print_step_header(5, 6, "OPTIMIZER", "DONE")

        # Step 6: REVIEWER
        print_step_header(6, 6, "REVIEWER", "START")
        review_task = f"Review this project:\nPlan: {plan_response[:300]}\nCode: {coder_response[:500]}"
        review_response = await self._run_agent("REVIEWER", review_task)
        self.result.reviewer = review_response
        print_step_header(6, 6, "REVIEWER", "DONE")

        # Final
        self.result.final = f"Project: {self.env.project_dir}\nFiles: {', '.join(self.files_created)}\nErrors: {len(self.errors)}"
        self.result.success = len(self.files_created) > 0
        self.result.files_created = self.files_created
        self.result.errors = self.errors

        self.logger.log_structured("INFO", "chain", "CHAIN COMPLETE")

        if self._last_layer:
            self.layers.set_ref(
                "last_good" if self.result.success else "last_chain_run",
                self._last_layer,
            )

        if self.mode == AutonomyMode.AUTONOMOUS:
            # Mark "chain finished" as the moment idle time starts counting
            # from — the next run_chain() call's opening maybe_run() check
            # (see top of this method) is what actually decides whether
            # enough real idle time passed to reconcile.
            self.reconciler.note_activity()

        return self.result

    async def _run_agent(self, agent_name: str, task: str) -> str:
        """Run agent with error recovery"""
        agent = self.agents[agent_name]

        # MCP pre-step
        mcp_context = ""
        try:
            mcp_context = await run_mcp_pre_step(agent_name.lower(), task)
        except Exception as e:
            logger.log_structured("DEBUG", "chain", f"MCP pre-step skipped: {e}")

        context = f"{mcp_context}\n\nProject dir: {self.env.project_dir}"

        response = await agent.think(task, context)

        # If response is error stub, log it
        if response.startswith("[ERROR:"):
            self.errors.append(f"{agent_name}: {response}")
            logger.log_structured("WARN", "chain", f"{agent_name} returned error: {response[:100]}")

        # Commit this step as a layer: note is a short summary (cheap, goes into
        # context_window()), full response lives on disk and is self-verified
        # on read — see LayerStore.get_full().
        note = f"{agent_name}: {response[:150]!r}" if not response.startswith("[ERROR:") else f"{agent_name} ERROR: {response[:150]}"
        self._last_layer = self.layers.commit(
            content=response,
            note=note,
            kind="agent_step",
            agent=agent_name,
            parent_id=self._last_layer,
            tags=["error"] if response.startswith("[ERROR:") else [],
        )

        # MCP post-step
        try:
            await run_mcp_post_step(agent_name.lower(), response)
        except Exception:
            pass

        self.memory.save_conversation(task, response, agent_name)
        return response

    def _has_errors(self, response: str) -> bool:
        """Check if response contains errors"""
        error_indicators = ["error", "exception", "failed", "traceback", "[ERROR:"]
        return any(ind in response.lower() for ind in error_indicators)

    def _extract_errors(self, response: str) -> str:
        """Extract error lines from response"""
        lines = response.split("\n")
        errors = [l for l in lines if any(ind in l.lower() for ind in ["error", "exception", "failed"])]
        return "\n".join(errors[:5])

    async def _process_tools(self, response: str, layer_id: str | None = None):
        """Parse and execute tool calls.

        If `layer_id` is given, every real file write/edit this call
        performs is recorded onto that layer (see LayerStore.attach_file_writes)
        so LayerStore.checkout() can later replay the actual project state
        that resulted from this step, not just re-show the raw agent text.
        """

        tool_pattern = r'<tool>\s*(\{.*?\})\s*</tool>'
        matches = re.findall(tool_pattern, response, re.DOTALL)

        step_writes: dict[str, str] = {}

        for match in matches:
            try:
                tool_call = json.loads(match)
                tool_name = tool_call.get("name")
                args = tool_call.get("args", {})

                if tool_name == "write":
                    written = await self._do_write(args.get("path"), args.get("content"))
                    if written:
                        step_writes[written[0]] = written[1]
                elif tool_name == "bash":
                    await self._do_bash(args.get("command"))
                elif tool_name == "edit":
                    edited = await self._do_edit(args.get("path"), args.get("old_string"), args.get("new_string"))
                    if edited:
                        step_writes[edited[0]] = edited[1]

            except json.JSONDecodeError:
                logger.log_structured("WARN", "chain", f"Invalid tool JSON: {match[:100]}")
            except Exception as e:
                logger.log_error("chain", "Tool failed", exc=e)

        if layer_id and step_writes:
            try:
                self.layers.attach_file_writes(layer_id, step_writes)
            except Exception as e:
                logger.log_structured("WARN", "chain", f"Could not attach file_writes to layer {layer_id}: {e}")

    async def _do_write(self, path: str, content: str):
        """Returns (resolved_path, content) on success so callers can record
        it against a layer for checkout() — None on any failure/no-op."""
        if not path or content is None:
            return None
        try:
            p = Path(path)
            # Handle relative paths
            if not p.is_absolute():
                p = Path(self.env.project_dir) / p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding='utf-8')
            self.files_created.append(str(p))
            print(f"{COLORS['GREEN']}✅ Created: {p}{COLORS['RESET']}")
            logger.log_structured("INFO", "chain", f"File: {p}")
            return (str(p), content)
        except Exception as e:
            logger.log_error("chain", f"Write failed: {path}", exc=e)
            return None

    async def _do_bash(self, command: str):
        if not command:
            return

        safe, reason = is_shell_command_safe(command)
        if not safe:
            logger.log_structured("WARN", "chain", "Blocked unsafe command", {"command": command, "reason": reason})
            return f"ERROR: Blocked ({reason}): {command}"

        # Fix brace expansion
        if '{' in command and '}' in command and 'mkdir' in command:
            command = self._fix_brace_expansion(command)

        try:
            from xli.core.exec_guard import run_guarded_shell
            result = run_guarded_shell(command, timeout=30)
            output = result.stdout[:500] if result.stdout else ""
            if result.returncode != 0:
                output += f" [err: {result.returncode}]"
            print(f"{COLORS['YELLOW']}$ {command}{COLORS['RESET']}")
            if output:
                print(output[:300])
        except Exception as e:
            logger.log_error("chain", f"Bash failed: {command}", exc=e)

    def _fix_brace_expansion(self, command: str) -> str:
        """Fix bash brace expansion for Python subprocess"""
        import re
        # mkdir -p dir/{a,b,c} → mkdir -p dir/a && mkdir -p dir/b && mkdir -p dir/c
        match = re.search(r'(mkdir\s+-p\s+)(.+)/(.+)\{([^}]+)\}(.*)', command)
        if match:
            prefix = match.group(2) + "/" + match.group(3)
            items = match.group(4).split(',')
            suffix = match.group(5)
            cmds = [f"mkdir -p {prefix}{item.strip()}{suffix}" for item in items]
            return " && ".join(cmds)
        return command

    async def _do_edit(self, path: str, old: str, new: str):
        """Returns (resolved_path, new_full_content) on a real edit — None
        otherwise (missing file, no match, error)."""
        if not path or not old:
            return None
        try:
            p = Path(path)
            if not p.exists():
                # Try relative to project dir
                p = Path(self.env.project_dir) / p
            if p.exists() and old in p.read_text():
                content = p.read_text()
                new_content = content.replace(old, new, 1)
                p.write_text(new_content)
                print(f"{COLORS['GREEN']}✏️  Edited: {p}{COLORS['RESET']}")
                return (str(p), new_content)
        except Exception as e:
            logger.log_error("chain", f"Edit failed: {path}", exc=e)
        return None
