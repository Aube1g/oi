#!/usr/bin/env python3
"""
XLI Context Scout + AGENTS.md Generator
Auto-discovers project structure and coding patterns
"""

import json
import re
from pathlib import Path
from dataclasses import dataclass

from xli.core.logger import StructuredLogger

#: Directories that hold generated or third-party content. Scanning them makes
#: a "project structure" report describe build output instead of the project.
_IGNORED_DIRS = frozenset({
    "build", "dist", "out", "target", "node_modules", "__pycache__",
    "venv", "env", "site-packages", "coverage", "htmlcov", "egg-info",
})

logger = StructuredLogger("xli.context")


@dataclass
class ProjectPattern:
    """Discovered coding pattern"""
    name: str
    description: str
    files: list[str]
    example: str


@dataclass
class ProjectContext:
    """Full project context for AGENTS.md"""
    name: str
    language: str
    framework: str
    patterns: list[ProjectPattern]
    key_files: list[str]
    dependencies: list[str]
    conventions: list[str]


class ContextScout:
    """Discovers project structure and patterns"""

    def __init__(self, project_path: str = "."):
        self.project_path = Path(project_path).resolve()
        self.patterns: list[ProjectPattern] = []
        self.key_files: list[str] = []

    def scan(self) -> ProjectContext:
        """Full project scan"""
        logger.log_structured("INFO", "context", f"Scanning {self.project_path}")

        # Detect language/framework
        lang, framework = self._detect_stack()

        # Find key files
        self.key_files = self._find_key_files()

        # Discover patterns
        self.patterns = self._discover_patterns()

        # Extract conventions
        conventions = self._extract_conventions()

        # Get dependencies
        deps = self._get_dependencies()

        ctx = ProjectContext(
            name=self.project_path.name,
            language=lang,
            framework=framework,
            patterns=self.patterns,
            key_files=self.key_files,
            dependencies=deps,
            conventions=conventions
        )

        return ctx

    def _detect_stack(self) -> tuple:
        """Detect primary language and framework"""
        files = list(self.project_path.rglob("*"))

        # Language detection
        exts = {}
        for f in files:
            if f.is_file():
                ext = f.suffix.lower()
                exts[ext] = exts.get(ext, 0) + 1

        lang_map = {
            '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript',
            '.go': 'Go', '.rs': 'Rust', '.java': 'Java', '.cpp': 'C++',
            '.c': 'C', '.rb': 'Ruby', '.php': 'PHP'
        }

        primary_lang = 'Unknown'
        max_count = 0
        for ext, count in exts.items():
            if ext in lang_map and count > max_count:
                max_count = count
                primary_lang = lang_map[ext]

        # Framework detection
        framework = 'Unknown'
        if (self.project_path / "package.json").exists():
            framework = self._detect_js_framework()
        elif (self.project_path / "requirements.txt").exists() or \
             (self.project_path / "pyproject.toml").exists():
            framework = self._detect_python_framework()
        elif (self.project_path / "go.mod").exists():
            framework = 'Go Modules'
        elif (self.project_path / "Cargo.toml").exists():
            framework = 'Cargo'

        return primary_lang, framework

    def _detect_js_framework(self) -> str:
        """Detect JS/TS framework"""
        pkg = self.project_path / "package.json"
        if not pkg.exists():
            return 'Unknown'
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get('dependencies', {}), **data.get('devDependencies', {})}

            frameworks = {
                'next': 'Next.js', 'react': 'React', 'vue': 'Vue',
                'svelte': 'Svelte', 'angular': 'Angular',
                'express': 'Express', 'fastify': 'Fastify',
                'nest': 'NestJS'
            }

            for dep in deps:
                for key, name in frameworks.items():
                    if key in dep.lower():
                        return name
        except Exception:
            pass
        return 'Node.js'

    def _detect_python_framework(self) -> str:
        """Detect Python framework"""
        files = list(self.project_path.glob("*.py"))[:20]
        content = ""
        for f in files:
            try:
                content += f.read_text()[:500]
            except Exception:
                pass

        frameworks = {
            'fastapi': 'FastAPI', 'flask': 'Flask', 'django': 'Django',
            'streamlit': 'Streamlit', 'gradio': 'Gradio'
        }

        for key, name in frameworks.items():
            if key in content.lower():
                return name

        return 'Python'

    def _find_key_files(self) -> list[str]:
        """Find important project files"""
        key_patterns = [
            'README*', 'LICENSE*', 'CONTRIBUTING*', 'CHANGELOG*',
            'package.json', 'requirements.txt', 'pyproject.toml',
            'go.mod', 'Cargo.toml', 'Dockerfile', 'docker-compose*',
            'Makefile', 'justfile', '.github/workflows/*',
            'src/**/*', 'app/**/*', 'lib/**/*', 'core/**/*'
        ]

        found = []
        for pattern in key_patterns:
            for f in self.project_path.rglob(pattern):
                if not f.is_file():
                    continue
                if any(part in _IGNORED_DIRS or part.startswith('.') for part in f.parts[:-1]):
                    continue
                rel = str(f.relative_to(self.project_path))
                if rel not in found:
                    found.append(rel)

        found.sort()
        return found[:30]

    def _discover_patterns(self) -> list[ProjectPattern]:
        """Discover coding patterns from source files"""
        patterns = []

        # Look for common patterns
        src_files = list(self.project_path.rglob("*.py"))[:10] + \
                    list(self.project_path.rglob("*.js"))[:10] + \
                    list(self.project_path.rglob("*.ts"))[:10]

        for f in src_files:
            try:
                content = f.read_text()

                # Detect patterns
                if 'class ' in content and 'def ' in content:
                    patterns.append(ProjectPattern(
                        name="OOP Style",
                        description="Uses classes with methods",
                        files=[str(f.relative_to(self.project_path))],
                        example=self._extract_example(content, 'class ')
                    ))

                if 'async def ' in content:
                    patterns.append(ProjectPattern(
                        name="Async/Await",
                        description="Uses async/await patterns",
                        files=[str(f.relative_to(self.project_path))],
                        example=self._extract_example(content, 'async def ')
                    ))

                if 'typing.' in content or 'from typing import' in content:
                    patterns.append(ProjectPattern(
                        name="Type Hints",
                        description="Uses typing annotations",
                        files=[str(f.relative_to(self.project_path))],
                        example=self._extract_example(content, 'def ')
                    ))

            except Exception:
                pass

        # Deduplicate
        seen = set()
        unique = []
        for p in patterns:
            if p.name not in seen:
                seen.add(p.name)
                unique.append(p)

        return unique[:10]

    def _extract_example(self, content: str, keyword: str) -> str:
        """Extract code example around keyword"""
        idx = content.find(keyword)
        if idx == -1:
            return ""
        start = max(0, idx - 50)
        end = min(len(content), idx + 300)
        return content[start:end].strip()

    def _extract_conventions(self) -> list[str]:
        """Extract coding conventions"""
        conventions = []

        # Check for common config files
        if (self.project_path / ".editorconfig").exists():
            conventions.append("Uses EditorConfig")
        if (self.project_path / "pyproject.toml").exists():
            conventions.append("Uses pyproject.toml")
        if (self.project_path / ".pre-commit-config.yaml").exists():
            conventions.append("Uses pre-commit hooks")
        if (self.project_path / "tests").exists() or (self.project_path / "test").exists():
            conventions.append("Has test directory")
        if (self.project_path / ".github").exists():
            conventions.append("Uses GitHub Actions")

        return conventions

    def _get_dependencies(self) -> list[str]:
        """Project dependencies, from whichever manifest is present.

        pyproject.toml is checked because it is the modern Python standard;
        relying only on requirements.txt silently reports zero dependencies
        for any project that declares them in pyproject.
        """
        deps: list[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            name = name.strip()
            if name and not name.startswith("#") and name.lower() not in seen:
                seen.add(name.lower())
                deps.append(name)

        # Python: pyproject.toml (PEP 621)
        pyproject = self.project_path / "pyproject.toml"
        if pyproject.exists():
            try:
                import tomllib

                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                project = data.get("project", {})
                for requirement in project.get("dependencies", []):
                    # "httpx>=0.27.0" -> "httpx"
                    add(re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0])
                for extras in project.get("optional-dependencies", {}).values():
                    for requirement in extras:
                        add(re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0])
            except Exception as exc:
                logger.log_structured("WARN", "context", f"pyproject.toml unreadable: {exc}")

        # Python: requirements.txt
        req = self.project_path / "requirements.txt"
        if req.exists():
            try:
                for line in req.read_text(encoding="utf-8", errors="replace").splitlines()[:50]:
                    if line.strip() and not line.lstrip().startswith("#"):
                        add(re.split(r"[<>=!~\[; ]", line.strip(), maxsplit=1)[0])
            except OSError:
                pass

        # Node
        pkg = self.project_path / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                for group in ("dependencies", "devDependencies"):
                    for dep in list(data.get(group, {}).keys())[:30]:
                        add(dep)
            except (json.JSONDecodeError, OSError):
                pass

        return deps[:40]

    def generate_agents_md(self) -> str:
        """Generate AGENTS.md content"""
        ctx = self.scan()

        lines = [
            f"# {ctx.name}",
            "",
            f"**Language:** {ctx.language}",
            f"**Framework:** {ctx.framework}",
            "",
            "## Project Structure",
            "",
        ]

        for f in ctx.key_files[:15]:
            lines.append(f"- `{f}`")

        lines.extend([
            "",
            "## Coding Patterns",
            "",
        ])

        for p in ctx.patterns:
            lines.extend([
                f"### {p.name}",
                f"{p.description}",
                "",
                "```",
                p.example[:200],
                "```",
                "",
            ])

        lines.extend([
            "## Conventions",
            "",
        ])
        for c in ctx.conventions:
            lines.append(f"- {c}")

        lines.extend([
            "",
            "## Key Dependencies",
            "",
        ])
        for d in ctx.dependencies[:15]:
            lines.append(f"- {d}")

        return "\n".join(lines)

    def save_agents_md(self):
        """Save AGENTS.md to project root"""
        content = self.generate_agents_md()
        path = self.project_path / "AGENTS.md"
        path.write_text(content)
        logger.log_structured("INFO", "context", f"Saved AGENTS.md to {path}")
        return path


def init_project(path: str = ".") -> Path:
    """Initialize AGENTS.md for project"""
    scout = ContextScout(path)
    return scout.save_agents_md()
