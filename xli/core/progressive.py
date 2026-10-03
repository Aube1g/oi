#!/usr/bin/env python3
"""
XLI Progressive v4 — MVP → tests → optimize (only if needed)
"""


from xli.core.logger import StructuredLogger
from xli.core.agent import XliAgent

logger = StructuredLogger("xli.progressive")


class ProgressiveEnhancement:
    """Build code progressively: MVP first, then enhance"""

    def __init__(self):
        self.coder = XliAgent("CODER", "ag_coder",
                             "You are Coder. Write minimal working code.")
        self.tester = XliAgent("TESTER", "ag_tester",
                              "You are Tester. Write focused tests.")
        self.optimizer = XliAgent("OPTIMIZER", "ag_optimizer",
                                 "You are Optimizer. Improve only when needed.")
        logger.log_structured("INFO", "progressive", "Initialized")

    async def mvp(self, task: str) -> str:
        """Create minimum viable product"""
        logger.log_structured("INFO", "progressive", "Creating MVP")

        mvp_prompt = f"""Create MINIMAL working code for:
{task}

Requirements:
- ONLY core functionality
- NO error handling
- NO logging
- NO tests
- NO docs
- JUST the working solution"""

        result = await self.coder.think(mvp_prompt)
        logger.log_structured("INFO", "progressive", "MVP created",
                             {"len": len(result)})
        return result

    async def add_tests(self, code: str, task: str) -> str:
        """Add tests to code"""
        logger.log_structured("INFO", "progressive", "Adding tests")

        test_prompt = f"""Write tests for this code:

Task: {task}

Code:
{code[:1500]}

Requirements:
- Focus on main paths
- 3-5 test cases
- Use pytest
- Include edge cases"""

        tests = await self.tester.think(test_prompt)

        # Combine code + tests
        result = f"{code}\n\n# Tests\n{tests}"
        logger.log_structured("INFO", "progressive", "Tests added")
        return result

    async def optimize(self, code: str, metrics: dict | None = None) -> str:
        """Optimize only if metrics show need"""
        logger.log_structured("INFO", "progressive", "Checking optimization need")

        # Check if optimization needed
        needs_opt = False
        if metrics:
            if metrics.get("execution_time", 0) > 1.0:
                needs_opt = True
            if metrics.get("memory_usage", 0) > 100 * 1024 * 1024:  # 100MB
                needs_opt = True

        if not needs_opt:
            logger.log_structured("INFO", "progressive", "No optimization needed")
            return code

        opt_prompt = f"""Optimize this code (only critical improvements):

{code[:1500]}

Focus on:
- Algorithm complexity
- Memory usage
- Hot paths only
- Keep readability"""

        optimized = await self.optimizer.think(opt_prompt)
        logger.log_structured("INFO", "progressive", "Optimized")
        return optimized

    async def run_progressive(self, task: str) -> dict[str, str]:
        """Full progressive cycle"""
        logger.log_structured("INFO", "progressive", "Starting progressive cycle")

        # Step 1: MVP
        mvp_code = await self.mvp(task)

        # Step 2: Tests
        tested = await self.add_tests(mvp_code, task)

        # Step 3: Check if optimization needed (simplified)
        metrics = {"execution_time": 0.5}  # Would measure actual

        # Step 4: Optimize if needed
        final = await self.optimize(tested, metrics)

        return {
            "mvp": mvp_code,
            "tested": tested,
            "final": final,
            "optimized": final != tested
        }


def get_progressive() -> ProgressiveEnhancement:
    """Get ProgressiveEnhancement instance"""
    return ProgressiveEnhancement()

