#!/usr/bin/env python3
"""
XLI Self Healing v4 — Auto-retry, error analysis, new approach
"""

import asyncio
import random
from typing import Any
from collections.abc import Callable
from dataclasses import dataclass

from xli.core.logger import StructuredLogger
from xli.providers.base import get_provider

logger = StructuredLogger("xli.heal")


@dataclass
class ErrorAnalysis:
    """Analyzed error information"""
    error_type: str
    severity: str  # low, medium, high, critical
    retryable: bool
    suggested_approach: str
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "severity": self.severity,
            "retryable": self.retryable,
            "suggested_approach": self.suggested_approach,
            "context": self.context,
        }


class SelfHealingEngine:
    """Auto-retry and error recovery"""

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0, sleeper=None):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.error_history: list[dict] = []
        # Injectable so an embedder (or a test) is not forced to wait out the
        # real backoff. base_delay=0 keeps the shape without the wall time.
        self._sleep = sleeper or asyncio.sleep

    async def heal(self, task: str, error: Exception,
                   attempt_func: Callable, context: str = "") -> Any:
        """Attempt to heal from error"""
        analysis = await self.analyze_error(error, context)

        logger.log_structured("INFO", "heal",
                             f"Healing attempt: {analysis.error_type}",
                             {"severity": analysis.severity, "retryable": analysis.retryable})

        if not analysis.retryable:
            logger.log_structured("WARN", "heal", "Error not retryable")
            raise error

        # Try with new approach
        for attempt in range(self.max_retries):
            delay = self.base_delay * (2 ** attempt) + random.uniform(0, 1)

            logger.log_structured("DEBUG", "heal",
                                 f"Retry {attempt + 1}/{self.max_retries}",
                                 {"delay": delay})

            await self._sleep(delay)

            try:
                # Modify approach based on analysis
                modified_task = f"{task}\n\n[PREVIOUS ERROR]: {str(error)}\n[APPROACH]: {analysis.suggested_approach}"
                result = await attempt_func(modified_task)

                logger.log_structured("INFO", "heal",
                                     f"Healed on attempt {attempt + 1}")
                return result

            except Exception as e:
                logger.log_structured("WARN", "heal",
                                     f"Attempt {attempt + 1} failed: {e}")
                self.error_history.append({
                    "attempt": attempt + 1,
                    "error": str(e),
                    "analysis": analysis.to_dict(),
                })

        logger.log_structured("ERROR", "heal", "All healing attempts exhausted")
        raise error

    async def analyze_error(self, error: Exception, context: str = "") -> ErrorAnalysis:
        """Analyze error and classify"""
        error_str = str(error)
        error_type = type(error).__name__
        haystack = f"{error_type}: {error_str}".lower()

        # Classify on the *message* first. Providers raise generic exception
        # types and put the real signal in the text, so matching on the class
        # name alone gets it backwards: ValueError("429 rate limited") used to
        # be reported critical and non-retryable, and RuntimeError("HTTP 503")
        # was never retried at all.
        retryable_signals = (
            "429", "rate limit", "rate_limit", "too many requests",
            "502", "503", "504", "bad gateway", "service unavailable",
            "gateway timeout", "timeout", "timed out", "temporary",
            "connection reset", "connection refused", "connection aborted",
            "eof occurred", "temporarily", "try again", "overloaded",
        )
        fatal_signals = (
            "syntaxerror", "indentationerror", "unauthorized", "401",
            "forbidden", "403", "invalid api key", "authentication",
            "no such file", "permission denied", "not implemented", "501",
        )
        retryable_types = (
            "timeout", "connection", "ratelimit", "temporary",
            "serviceunavailable", "network",
        )
        fatal_types = (
            "syntaxerror", "indentationerror", "importerror",
            "modulenotfounderror", "attributeerror", "keyerror",
        )

        signal_retryable = any(token in haystack for token in retryable_signals)
        signal_fatal = any(token in haystack for token in fatal_signals)
        type_retryable = any(token in error_type.lower() for token in retryable_types)
        type_fatal = any(token in error_type.lower() for token in fatal_types)

        # A message signal beats a class-name guess, because the message is
        # specific to this failure while the class is often just generic.
        if signal_fatal and not signal_retryable:
            retryable, fatal = False, True
        elif signal_retryable:
            retryable, fatal = True, False
        elif type_fatal:
            retryable, fatal = False, True
        elif type_retryable:
            retryable, fatal = True, False
        else:
            # No signal either way. Retrying with a *modified* approach is the
            # whole point of this engine, so an unrecognised failure gets one
            # more try rather than being declared hopeless.
            retryable, fatal = True, False

        severity = "critical" if fatal else ("high" if not retryable else "medium")

        # Get LLM suggestion
        try:
            provider = get_provider()
            prompt = f"""Analyze this error and suggest a fix approach:

Error: {error_type}: {error_str}
Context: {context[:500]}

Return JSON:
{{
    "suggested_approach": "specific approach to fix",
    "retryable": true/false
}}"""

            response = await provider.chat([
                {"role": "system", "content": "Error analysis assistant"},
                {"role": "user", "content": prompt}
            ], temperature=0.3)

            # Parse JSON
            import json
            import re
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return ErrorAnalysis(
                    error_type=error_type,
                    severity=severity,
                    retryable=data.get("retryable", retryable),
                    suggested_approach=data.get("suggested_approach", "Try alternative approach"),
                    context={"original_error": error_str}
                )

        except Exception as e:
            logger.log_error("heal", "LLM analysis failed", exc=e)

        # Fallback
        return ErrorAnalysis(
            error_type=error_type,
            severity=severity,
            retryable=retryable,
            suggested_approach=(
                "Retry with a modified approach: simplify the request, reduce scope, "
                "or use a different tool"
                if retryable
                else "Manual fix required: the failure is not transient"
            ),
            context={"original_error": error_str}
        )

    async def retry_with_backoff(self, func: Callable, *args,
                                  max_retries: int = None, **kwargs) -> Any:
        """Retry function with exponential backoff"""
        max_retries = max_retries or self.max_retries

        for attempt in range(max_retries):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    return func(*args, **kwargs)

            except Exception:
                if attempt == max_retries - 1:
                    raise

                delay = self.base_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.log_structured("DEBUG", "heal",
                                     f"Backoff retry {attempt + 1}, delay {delay:.1f}s")
                await self._sleep(delay)


_ENGINE: SelfHealingEngine | None = None


def get_healing_engine(**kwargs: Any) -> SelfHealingEngine:
    """Process-wide SelfHealingEngine. Pass kwargs only on first call."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = SelfHealingEngine(**kwargs)
    return _ENGINE


def reset_healing_engine() -> None:
    """Drop the singleton (used by tests and `xli doctor`)."""
    global _ENGINE
    _ENGINE = None

