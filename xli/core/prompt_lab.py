#!/usr/bin/env python3
"""
XLI Prompt Lab v4 — A/B testing prompts, evaluation, winner selection
"""

import json
import hashlib
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass
from datetime import datetime

from xli.paths import xli_path
from xli.core.logger import StructuredLogger
from xli.providers.base import get_provider

logger = StructuredLogger("xli.prompt_lab")

PROMPT_DIR = xli_path("prompts")


@dataclass
class PromptVariant:
    """Single prompt variant"""
    name: str
    template: str
    score: float = 0.0
    runs: int = 0


class PromptExperiment:
    """A/B experiment for prompts"""

    def __init__(self, name: str):
        self.name = name
        self.variants: list[PromptVariant] = []
        self.results: list[dict] = []
        self.created = datetime.now().isoformat()


class PromptLab:
    """A/B testing and prompt optimization"""

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

        PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        self.experiments: dict[str, PromptExperiment] = {}
        self.prompt_library: dict[str, dict] = {}
        self._load_library()

        logger.log_structured("INFO", "prompt_lab", "PromptLab initialized")

    def _load_library(self):
        """Load saved prompts"""
        lib_file = PROMPT_DIR / "library.json"
        if lib_file.exists():
            try:
                with open(lib_file) as f:
                    self.prompt_library = json.load(f)
            except Exception as e:
                logger.log_error("prompt_lab", "Load library failed", exc=e)

    def _save_library(self):
        """Save prompt library"""
        try:
            with open(PROMPT_DIR / "library.json", "w") as f:
                json.dump(self.prompt_library, f, indent=2)
        except Exception as e:
            logger.log_error("prompt_lab", "Save library failed", exc=e)

    def create_experiment(self, name: str, variants: list[tuple[str, str]]) -> str:
        """Create new A/B experiment"""
        exp_id = f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        exp = PromptExperiment(name)
        for var_name, template in variants:
            exp.variants.append(PromptVariant(name=var_name, template=template))

        self.experiments[exp_id] = exp

        logger.log_structured("INFO", "prompt_lab",
                             f"Experiment created: {exp_id}",
                             {"variants": len(variants)})

        return exp_id

    async def run_experiment(self, task: str, exp_id: str,
                           metric_func: Callable[[Any], float] | None = None) -> dict:
        """Run experiment with all variants"""
        if exp_id not in self.experiments:
            return {"error": "Experiment not found"}

        exp = self.experiments[exp_id]
        provider = get_provider()

        results = []

        for variant in exp.variants:
            logger.log_structured("DEBUG", "prompt_lab",
                                 f"Running variant: {variant.name}")

            # Build prompt from template
            prompt = variant.template.replace("{{task}}", task)

            start = datetime.now()



            try:
                response = await provider.chat([
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": prompt}
                ], temperature=0.4)

                elapsed = (datetime.now() - start).total_seconds()

                # Evaluate if metric function provided
                score = 0.5
                if metric_func:
                    try:
                        score = metric_func(response)
                    except Exception as e:
                        logger.log_error("prompt_lab", "Metric failed", exc=e)

                result = {
                    "variant": variant.name,
                    "response": response[:500],
                    "time": elapsed,
                    "score": score,
                    "length": len(response)
                }

                variant.runs += 1
                variant.score = (variant.score * (variant.runs - 1) + score) / variant.runs

            except Exception as e:
                logger.log_error("prompt_lab", f"Variant {variant.name} failed", exc=e)
                result = {
                    "variant": variant.name,
                    "error": str(e),
                    "score": 0.0
                }

            results.append(result)
            exp.results.append(result)

        logger.log_structured("INFO", "prompt_lab",
                             f"Experiment {exp_id} complete",
                             {"results": len(results)})

        return {
            "experiment_id": exp_id,
            "results": results,
            "winner": self.get_winner(exp_id)
        }

    def evaluate_results(self, exp_id: str) -> dict:
        """Evaluate experiment results"""
        if exp_id not in self.experiments:
            return {"error": "Experiment not found"}

        exp = self.experiments[exp_id]

        # Calculate statistics
        stats = {}
        for variant in exp.variants:
            variant_results = [r for r in exp.results if r.get("variant") == variant.name]
            if variant_results:
                scores = [r.get("score", 0) for r in variant_results]
                times = [r.get("time", 0) for r in variant_results]

                stats[variant.name] = {
                    "avg_score": sum(scores) / len(scores),
                    "avg_time": sum(times) / len(times),
                    "runs": len(variant_results),
                    "final_score": variant.score
                }

        return {
            "experiment": exp.name,
            "variants": stats,
            "total_runs": len(exp.results)
        }

    def get_winner(self, exp_id: str) -> str | None:
        """Get winning variant"""
        if exp_id not in self.experiments:
            return None

        exp = self.experiments[exp_id]

        if not exp.variants:
            return None

        winner = max(exp.variants, key=lambda v: v.score)
        logger.log_structured("INFO", "prompt_lab",
                             f"Winner: {winner.name} (score: {winner.score:.2f})")

        return winner.name

    def save_prompt(self, name: str, template: str, score: float = 0.0,
                    tags: list[str] = None):
        """Save prompt to library"""
        version = hashlib.sha256(template.encode()).hexdigest()[:8]

        self.prompt_library[name] = {
            "template": template,
            "version": version,
            "score": score,
            "tags": tags or [],
            "created": datetime.now().isoformat(),
            "uses": 0
        }

        self._save_library()
        logger.log_structured("INFO", "prompt_lab",
                             f"Prompt saved: {name} (v{version})")

    def load_prompt(self, name: str) -> str | None:
        """Load prompt template from library"""
        if name in self.prompt_library:
            self.prompt_library[name]["uses"] += 1
            self._save_library()
            return self.prompt_library[name]["template"]
        return None

    def list_prompts(self, tag: str | None = None) -> list[dict]:
        """List prompts in library"""
        prompts = []
        for name, info in self.prompt_library.items():
            if tag and tag not in info.get("tags", []):
                continue
            prompts.append({
                "name": name,
                "version": info["version"],
                "score": info["score"],
                "uses": info.get("uses", 0),
                "tags": info.get("tags", [])
            })
        return sorted(prompts, key=lambda x: x["score"], reverse=True)

    def compare_prompts(self, name1: str, name2: str, task: str) -> dict:
        """Compare two prompts on same task"""
        p1 = self.load_prompt(name1)
        p2 = self.load_prompt(name2)

        if not p1 or not p2:
            return {"error": "Prompt not found"}

        # Create experiment
        exp_id = self.create_experiment(f"compare_{name1}_vs_{name2}", [
            (name1, p1),
            (name2, p2)
        ])

        return {"experiment_id": exp_id, "status": "created"}


def get_prompt_lab() -> PromptLab:
    """Get singleton PromptLab"""
    return PromptLab()

