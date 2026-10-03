#!/usr/bin/env python3
"""
XLI Diff Engine v4 — Unified diff, patch apply, ai_edit
"""

import difflib
import re

from xli.core.logger import StructuredLogger
from xli.providers.base import get_provider

logger = StructuredLogger("xli.diff")


class DiffEngine:
    """Code diff and patch operations"""

    def __init__(self):
        logger.log_structured("INFO", "diff", "DiffEngine initialized")

    def unified_diff(self, old: str, new: str, filename: str = "file") -> str:
        """Generate unified diff"""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)

        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm=""
        )

        return "".join(diff)

    def apply_patch(self, original: str, patch: str) -> str:
        """Apply unified diff patch to original"""
        try:
            # Simple patch application
            lines = original.splitlines(keepends=True)
            patch_lines = patch.splitlines()

            result = []
            i = 0

            for line in patch_lines:
                if line.startswith("@@"):
                    # Parse hunk header
                    match = re.match(r"@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@", line)
                    if match:
                        old_start = int(match.group(1))
                        old_count = int(match.group(2)) if match.group(2) else 1
                        # Skip to hunk position
                        i = max(0, old_start - 1)

                elif line.startswith("-"):
                    # Remove line
                    if i < len(lines) and lines[i].rstrip() == line[1:].rstrip():
                        i += 1

                elif line.startswith("+"):
                    # Add line
                    result.append(line[1:] + "\n" if not line[1:].endswith("\n") else line[1:])

                elif line.startswith(" "):
                    # Context line
                    if i < len(lines):
                        result.append(lines[i])
                        i += 1
                # Skip "+++" and "---" lines

            # Add remaining lines
            while i < len(lines):
                result.append(lines[i])
                i += 1

            return "".join(result)

        except Exception as e:
            logger.log_error("diff", "Patch apply failed", exc=e)
            return original

    async def ai_edit(self, instruction: str, code: str) -> str:
        """Convert instruction to code edit via LLM"""
        prompt = f"""Apply this instruction to the code:

Instruction: {instruction}

Code:

{code}

Return ONLY the modified code, no explanations."""

        try:
            provider = get_provider()
            response = await provider.chat([
                {"role": "system", "content": "You are a code editor. Return only code."},
                {"role": "user", "content": prompt}
            ], temperature=0.2)

            # Clean response
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("python"):
                    cleaned = cleaned[6:]

            logger.log_structured("INFO", "diff", "AI edit applied",
                                 {"instruction": instruction[:50]})
            return cleaned.strip()

        except Exception as e:
            logger.log_error("diff", "AI edit failed", exc=e)
            return code

    def parse_diff(self, diff_text: str) -> list[tuple[str, str, str]]:
        """Parse diff into (filename, old_content, new_content) tuples"""
        files = []
        current_file = None
        old_lines = []
        new_lines = []
        in_old = False
        in_new = False

        for line in diff_text.splitlines():
            if line.startswith("---"):
                if current_file:
                    files.append((current_file, "".join(old_lines), "".join(new_lines)))
                current_file = line[4:].split("\t")[0].replace("a/", "")
                old_lines = []
                new_lines = []

            elif line.startswith("+++"):
                pass  # New file marker

            elif line.startswith("-"):
                old_lines.append(line[1:] + "\n")

            elif line.startswith("+"):
                new_lines.append(line[1:] + "\n")

            elif line.startswith(" "):
                old_lines.append(line[1:] + "\n")
                new_lines.append(line[1:] + "\n")

        if current_file:
            files.append((current_file, "".join(old_lines), "".join(new_lines)))

        return files


def get_diff_engine() -> DiffEngine:
    """Get singleton DiffEngine"""
    return DiffEngine()

