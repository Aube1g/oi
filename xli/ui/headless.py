#!/usr/bin/env python3
"""
XLI Headless UI v4.1 — FIXED: субагенты через TaskTool, умная обработка ошибок
"""
import json
from argparse import Namespace
from xli.ui.base import XliUI
from xli.core.env import EnvironmentAdapter
from xli.core.logger import StructuredLogger, print_banner, COLORS
from xli.core.multistep import MultiStepAgent  # NEW

logger = StructuredLogger("xli.ui.headless")


class HeadlessUI(XliUI):
    def __init__(self):
        super().__init__()
        self.env = EnvironmentAdapter()

    async def run(self, args: Namespace):
        task = args.task or ""
        if args.debug_file:
            task = f"debug file: {args.debug_file}"
        if not task:
            print("Error: No task specified")
            return

        # NEW: Используем MultiStepAgent вместо XliCore для субагентов
        if args.multistep or "sub-agent" in task.lower() or "@" in task:
            await self.run_multistep(task)
        else:
            await self.run_chain(task, args)

    async def run_multistep(self, task: str):
        """NEW: Multi-step с субагентами"""
        agent = MultiStepAgent(
            name="headless",
            system_prompt="""You are an expert developer. Use tools to accomplish tasks.
When you need specialized help, delegate to sub-agents using the task tool.
Available sub-agents:
- @coder: writes code
- @tester: writes tests
- @debugger: fixes bugs
- @optimizer: optimizes code
- @reviewer: reviews code

Delegate complex sub-tasks to them and combine results.""",
            max_steps=15
        )

        print_banner()
        print(f"\n{COLORS['CYAN']}🤖 HEADLESS MULTI-STEP MODE{COLORS['RESET']}")
        print(f"{COLORS['DIM']}Task: {task[:80]}{COLORS['RESET']}\n")

        try:
            result = await agent.run(task)
            print(f"\n{'='*60}")
            print("RESULT:")
            print(result)
            print(f"{'='*60}")
            print(agent.get_steps_summary())
        except Exception as e:
            logger.log_error("headless", "Multi-step failed", exc=e)
            print(f"ERROR: {e}")

    async def run_chain(self, task: str, args: Namespace):
        """Обычная цепочка агентов"""
        from xli.core.chain import XliCore, AutonomyMode
        # Priority: explicit args.mode (no CLI entry point in this repo sets
        # it yet, but this is forward-compatible with one that will) > the
        # XLI_AUTONOMY_MODE env var > MANUAL (previous, unchanged default).
        mode = getattr(args, "mode", None) or AutonomyMode.from_env()
        core = XliCore(self.env, mode=mode)
        try:
            result = await core.run_chain(task, skip_questions=args.skip_questions, stream=False)
            self.display_result(result, args.output_format)
        except Exception as e:
            logger.log_error("headless", "Chain failed", exc=e)
            print(f"ERROR: {e}")

    def display_result(self, result, format: str = "text"):
        if hasattr(result, 'final'):
            plan, coder, debugger = result.plan, result.coder, result.debugger
            tester, optimizer, reviewer = result.tester, result.optimizer, result.reviewer
            final, success = result.final, result.success
        else:
            plan = result.get("plan", "")
            coder = result.get("coder", "")
            debugger = result.get("debugger", "")
            tester = result.get("tester", "")
            optimizer = result.get("optimizer", "")
            reviewer = result.get("reviewer", "")
            final = result.get("final", "")
            success = result.get("success", False)

        if format == "json":
            import dataclasses
            if hasattr(result, '__dataclass_fields__'):
                print(json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2, default=str))
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif format == "vim":
            print("XLI_RESULT_START")
            print(final)
            print("XLI_RESULT_END")
        else:
            print("\n" + "=" * 60)
            print("FIRE XLI PRO v4 - Result")
            print("=" * 60)
            if plan:
                print("\nPLAN:\n" + plan[:500])
            print("\nCODER:\n" + coder[:800])
            if debugger:
                print("\nDEBUGGER:\n" + debugger[:300])
            if tester:
                print("\nTESTER:\n" + tester[:300])
            if optimizer:
                print("\nOPTIMIZER:\n" + optimizer[:300])
            if reviewer:
                print("\nREVIEWER:\n" + reviewer[:300])
            print("\n" + "=" * 60)
            print("Done!" if success else "Failed")

    def display(self, text: str, title: str | None = None):
        if title:
            print(f"\n=== {title} ===")
        print(text)

    async def input(self, prompt: str, default: str = "") -> str:
        return input(f"{prompt}: ") or default

    def notify(self, msg: str, level: str = "info"):
        print(f"[{level.upper()}] {msg}")

    def open_buffer(self, content: list, name: str = "XLI", filetype: str = "markdown"):
        print(f"\n=== {name} ===")
        print("\n".join(content))

    def progress(self, percent: int, message: str = ""):
        print(f"\r{message} {percent}%", end="", flush=True)
        if percent >= 100:
            print()

    def clear(self):
        print("\n" * 50)

    def choice(self, question: str, options: list, default=None) -> int:
        print(f"\n{question}")
        for i, opt in enumerate(options, 1):
            print(f"  {i}. {opt}")
        try:
            return int(input("Select: ")) - 1
        except Exception:
            return default or 0

    def confirm(self, question: str) -> bool:
        return input(f"{question} (y/n): ").lower().startswith("y")


async def run_headless(args: Namespace):
    print_banner()
    ui = HeadlessUI()
    await ui.run(args)
