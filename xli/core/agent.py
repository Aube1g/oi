#!/usr/bin/env python3
"""
XLI Agent v4 - XliAgent + SubAgent (hierarchical), streaming, cache
Now with VISIBLE THINKING / REASONING output (single LLM call)
"""

import re
from pathlib import Path

from xli.core.logger import StructuredLogger, print_thinking, print_agent_output
from xli.core.skills import get_skills_manager
from xli.core.memory import get_memory
from xli.core.cache import get_cache
from xli.core.env import EnvironmentAdapter
from xli.core.shell_safety import is_shell_command_safe
from xli.mcp.bridge import run_mcp_pre_step, run_mcp_post_step
from xli.providers.base import get_provider

logger = StructuredLogger("xli.agent")


class XliAgent:
    """Main agent with MCP, skills, memory, cache, and visible thinking"""

    def __init__(self, name: str, agent_id: str, base_role: str,
                 use_mcp: bool = True, use_cache: bool = True):
        self.name = name
        self.agent_id = agent_id
        self.base_role = base_role
        self.use_mcp = use_mcp
        self.use_cache = use_cache
        self.env = EnvironmentAdapter()
        self.history = []
        self.debug_logs = []
        self.subagents = {}
        self.skills = get_skills_manager()
        self.memory = get_memory()
        self.cache = get_cache()

        logger.log_structured("DEBUG", "agent", "Agent initialized",
                             {"name": name, "id": agent_id})

    def add_subagent(self, name: str, specialization: str, agent_id: str):
        """Add a specialized sub-agent"""
        sub = SubAgent(name, self, specialization, agent_id)
        self.subagents[name] = sub
        return sub

    def _build_role(self) -> str:
        """Build full role prompt with context"""
        lines = [
            self.base_role,
            "",
            self.env.get_system_context(),
            "",
            "RULES:",
            "- NO triple backticks for code",
            "- Shell commands ONLY in <SHELL>command</SHELL> tags",
            "- Python code output as plain text, NO markdown",
            f"- FULL PATHS: {self.env.home}/...",
            "- Be concise but informative",
            "- When using tools: call them properly via MCP, not as shell commands",
            "- DO NOT create files with touch/echo/cat, output code as text in response",
            "- DO NOT run pip install/apt/pkg, assume all deps are available",
            "- DO NOT run chmod or execute scripts, just output the code",
        ]
        return "\n".join(lines)

    async def think(self, task: str, context: str = "", stream: bool = False) -> str:
        """Main thinking method with visible reasoning (SINGLE LLM call)"""
        logger.log_structured("INFO", f"agent.{self.name}", "Thinking",
                             {"task": task[:100]})

        # Log skills being used
        skills_ctx = self.skills.get_skills_context(self.name)
        if skills_ctx:
            logger.log_structured("INFO", f"agent.{self.name}",
                                 "Loaded skills context")

        # Check cache
        if self.use_cache:
            try:
                cache_messages = [{"role": "system", "content": self._build_role()}]
                if context:
                    cache_messages.append({"role": "user", "content": context})
                if skills_ctx:
                    cache_messages.append({"role": "user", "content": skills_ctx})
                cache_messages.append({"role": "user", "content": task})

                cached = self.cache.get(cache_messages, provider="mistral",
                                       model="mistral-large-latest",
                                       temperature=0.4)
                if cached:
                    logger.log_structured("INFO", f"agent.{self.name}", "Cache hit")
                    return cached
            except Exception:
                pass

        # MCP pre-step
        mcp_ctx = ""
        if self.use_mcp:
            try:
                mcp_ctx = await run_mcp_pre_step(self.name, task)
                if mcp_ctx:
                    logger.log_structured("INFO", f"agent.{self.name}",
                                         "MCP pre-step done")
            except Exception as e:
                logger.log_error(f"agent.{self.name}", "MCP pre-step failed", exc=e)

        # Build messages with reasoning prompt
        system_prompt = self._build_role() + "\n\nBefore your final answer, briefly explain your approach (2-3 sentences). Start with [THINKING] and end with [ANSWER]."

        messages = [{"role": "system", "content": system_prompt}]
        for msg in self.history[-10:]:
            messages.append(msg)
        if context:
            messages.append({"role": "user", "content": "CONTEXT:\n" + context})
        if mcp_ctx:
            messages.append({"role": "user", "content": "MCP CONTEXT:\n" + mcp_ctx})
        if skills_ctx:
            messages.append({"role": "user", "content": "SKILLS:\n" + skills_ctx})
        messages.append({"role": "user", "content": task})

        # === SINGLE LLM CALL with reasoning ===
        print_thinking(self.name, f"Analyzing: {task[:80]}...")

        provider = get_provider()

        print(f"  ⚡ {self.name} calling LLM...")
        raw = await provider.chat(messages, temperature=0.4)

        # Extract reasoning and answer
        reasoning = ""
        answer = raw

        if "[THINKING]" in raw and "[ANSWER]" in raw:
            parts = raw.split("[ANSWER]")
            reasoning = parts[0].replace("[THINKING]", "").strip()
            answer = parts[1].strip()
        elif "[THINKING]" in raw:
            reasoning = raw.split("[THINKING]")[1].split("\n")[0].strip()

        if reasoning:
            # Clean and show reasoning
            reasoning_clean = reasoning.replace("**", "").replace("#", "").strip()
            reasoning_lines = [l.strip() for l in reasoning_clean.split("\n") if l.strip() and not l.strip().startswith("-")]
            reasoning_clean = "\n".join(reasoning_lines[:4])
            if reasoning_clean:
                print_thinking(self.name, reasoning_clean)

        cleaned = clean_agent_response(answer)

        # Execute shell commands in response
        shell_output = self._execute_shell(cleaned)
        if shell_output:
            cleaned += f"\n\n[SHELL OUTPUT]:\n{shell_output}"

        # Cache result
        if self.use_cache:
            try:
                cache_messages = [{"role": "system", "content": self._build_role()}]
                if context:
                    cache_messages.append({"role": "user", "content": context})
                if skills_ctx:
                    cache_messages.append({"role": "user", "content": skills_ctx})
                cache_messages.append({"role": "user", "content": task})

                self.cache.set(cache_messages, "mistral",
                              "mistral-large-latest", 0.4, cleaned)
            except Exception:
                pass

        # MCP post-step
        if self.use_mcp:
            try:
                await run_mcp_post_step(self.name, cleaned)
            except Exception as e:
                logger.log_error(f"agent.{self.name}", "MCP post-step failed", exc=e)

        # Save to memory
        self.history.extend([
            {"role": "user", "content": task},
            {"role": "assistant", "content": cleaned}
        ])
        self.memory.add_turn("user", task)
        self.memory.add_turn("assistant", cleaned)

        # Show output preview (limit lines)
        print_agent_output(self.name, cleaned, max_lines=20)

        return cleaned

    def _execute_shell(self, text: str) -> str:
        """Execute shell commands found in <SHELL> tags"""
        matches = re.findall(r'<SHELL>(.*?)</SHELL>', text, re.DOTALL)
        if not matches:
            return ""

        results = []
        for cmd in matches:
            cmd = cmd.strip()
            if not cmd:
                continue
            # Validate command using the shared, centralized guard (also
            # used by chain.py, multistep.py, nvim.py) instead of a local
            # copy-pasted blocklist that could drift out of sync.
            safe, reason = is_shell_command_safe(cmd)
            if not safe:
                results.append(f"$ {cmd}\nBLOCKED: restricted command ({reason})")
            else:
                out = self.env.run_shell(cmd)
                results.append(f"$ {cmd}\n{out}")

        return "\n\n".join(results) if results else ""


class SubAgent:
    """Sub-agent with specialized role"""

    def __init__(self, name: str, parent: XliAgent, specialization: str,
                 agent_id: str = None):
        self.name = name
        self.parent = parent
        self.specialization = specialization
        self.agent_id = agent_id or f"ag_{name.lower()}"
        self.logger = StructuredLogger(f"agent.{name}")

    async def think(self, task: str, context: str = "") -> str:
        """Think with specialization"""
        full_task = f"[{self.specialization}]\n{task}"
        return await self.parent.think(full_task, context)


def clean_agent_response(raw: str) -> str:
    """Clean agent response: remove markdown, fix paths"""
    # Remove markdown code blocks
    cleaned = re.sub(r'```[a-zA-Z]*\n', '', raw)
    cleaned = re.sub(r'```', '', cleaned)

    # Fix home path references
    home = str(Path.home())
    cleaned = cleaned.replace('~/', home + '/')

    return cleaned.strip()
