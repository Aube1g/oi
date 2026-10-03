#!/usr/bin/env python3
"""
XLI Planner v4 — Task decomposition, dependencies, parallel groups
"""

import json
import re
from typing import Any

from xli.core.logger import StructuredLogger
from xli.providers.base import get_provider

logger = StructuredLogger("xli.planner")


class PlanStep:
    """Single step in execution plan"""
    def __init__(self, agent: str, task: str, depends_on: list[str] = None):
        self.agent = agent
        self.task = task
        self.depends_on = depends_on or []
        self.completed = False
        self.result = ""


class PlanGenerator:
    """Generates execution plans from tasks"""

    def __init__(self):
        logger.log_structured("INFO", "planner", "PlanGenerator initialized")

    async def generate_plan(self, task: str) -> dict[str, Any]:
        """Generate execution plan with dependencies"""
        prompt = f"""Analyze this task and create an execution plan:
        
Task: {task}

Create a JSON plan with steps. Each step has:
- agent: one of [coder, debugger, tester, optimizer, reviewer]
- task: specific sub-task
- depends_on: list of step indices this depends on (can be empty)

Example:
{{
    "steps": [
        {{"agent": "coder", "task": "Create main structure", "depends_on": []}},
        {{"agent": "tester", "task": "Write tests for main", "depends_on": [0]}},
        {{"agent": "debugger", "task": "Fix any issues", "depends_on": [0, 1]}}
    ],
    "parallel_groups": [[0], [1], [2]]
}}

Return ONLY valid JSON."""

        try:
            provider = get_provider()
            response = await provider.chat([
                {"role": "system", "content": "You are a task planner. Return only JSON."},
                {"role": "user", "content": prompt}
            ], temperature=0.3)

            # Extract JSON
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                plan = json.loads(match.group())
                logger.log_structured("INFO", "planner", "Plan generated",
                                     {"steps": len(plan.get("steps", []))})
                return plan

        except Exception as e:
            logger.log_error("planner", "Plan generation failed", exc=e)

        # Fallback: simple sequential plan
        return {
            "steps": [
                {"agent": "coder", "task": task, "depends_on": []}
            ],
            "parallel_groups": [[0]]
        }

    async def generate_questions(self, task: str) -> list[dict[str, Any]]:
        """Generate clarifying questions"""
        prompt = f"""Task: {task}

Generate 2-4 clarifying questions. Reply ONLY JSON array:
[
    {{"id": "name", "question": "text", "type": "text/choice", 
      "options": ["opt1", "opt2"], "default": "default"}}
]"""

        try:
            provider = get_provider()
            response = await provider.chat([
                {"role": "system", "content": "Reply ONLY JSON array."},
                {"role": "user", "content": prompt}
            ], temperature=0.4)

            match = re.search(r'\[.*\]', response, re.DOTALL)
            if match:
                data = json.loads(match.group())
                logger.log_structured("INFO", "planner",
                                     f"Generated {len(data)} questions")
                return data

        except Exception as e:
            logger.log_error("planner", "Question generation failed", exc=e)

        # Fallback questions
        return [
            {"id": "language", "question": "Language?", "type": "choice",
             "options": ["Python", "JavaScript", "TypeScript", "Go", "Rust"]},
            {"id": "framework", "question": "Framework?", "type": "choice",
             "options": ["None", "React", "Vue", "FastAPI", "Flask"]},
            {"id": "details", "question": "Extra requirements?", "type": "text",
             "default": "None"},
        ]

    def get_parallel_groups(self, plan: dict) -> list[list[int]]:
        """Extract parallel execution groups from plan"""
        return plan.get("parallel_groups", [[i] for i in range(len(plan.get("steps", [])))])

    def validate_plan(self, plan: dict) -> bool:
        """Validate plan structure"""
        steps = plan.get("steps", [])
        if not steps:
            return False

        valid_agents = {"coder", "debugger", "tester", "optimizer", "reviewer"}

        for i, step in enumerate(steps):
            if step.get("agent") not in valid_agents:
                logger.log_structured("WARN", "planner",
                                     f"Invalid agent in step {i}: {step.get('agent')}")
                return False

            # Check dependencies exist
            for dep in step.get("depends_on", []):
                if dep < 0 or dep >= len(steps):
                    logger.log_structured("WARN", "planner",
                                         f"Invalid dependency in step {i}: {dep}")
                    return False

        return True

