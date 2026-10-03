#!/usr/bin/env python3
"""
XLI Skills Manager v4 — SQLite FTS, lazy load, 200+ skills
"""

import re
import sqlite3
from pathlib import Path
from datetime import datetime

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.skills")

# Skills ship inside the package; the user directory can add to them or
# override a bundled skill of the same name. Scanning only the user directory
# (the original behaviour) left the index permanently empty.
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
SKILLS_DIR = xli_path("skills")
SKILLS_DB = xli_path("skills.db")


class SkillsManager:
    """Manages skills with SQLite FTS indexing"""

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

        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._scan_skills()

        logger.log_structured("INFO", "skills", "SkillsManager initialized")

    def _init_db(self):
        """Initialize SQLite with FTS4"""
        conn = sqlite3.connect(str(SKILLS_DB))
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                path TEXT NOT NULL,
                category TEXT,
                content TEXT,
                size INTEGER,
                modified TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts4(
                name, content, category
            )
        """)

        conn.commit()
        conn.close()

    def _skill_roots(self) -> list[Path]:
        """Bundled skills first, then the user directory.

        Later roots win, so a file in ~/.xli/skills overrides a bundled skill
        with the same name.
        """
        roots = []
        if BUNDLED_SKILLS_DIR.is_dir():
            roots.append(BUNDLED_SKILLS_DIR)
        if SKILLS_DIR.is_dir():
            roots.append(SKILLS_DIR)
        return roots

    def _scan_skills(self) -> None:
        """Index every skill under the bundled and user roots."""
        conn = sqlite3.connect(str(SKILLS_DB))
        cursor = conn.cursor()

        cursor.execute("SELECT name, path, modified FROM skills")
        existing = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

        current_skills: set[str] = set()
        indexed = 0

        for root in self._skill_roots():
            for skill_file in root.rglob("*.md"):
                if skill_file.name.startswith((".", "_")):
                    continue

                rel_path = str(skill_file.relative_to(root))
                name = rel_path.replace("/", "_").replace("\\", "_").removesuffix(".md")
                current_skills.add(name)

                # Store the mtime as text and compare text to text. The original
                # code stored a datetime but compared against .isoformat(), so
                # the check never matched and every run re-read every file.
                mtime = datetime.fromtimestamp(skill_file.stat().st_mtime).isoformat()

                if name in existing:
                    old_path, old_mtime = existing[name]
                    if old_path == str(skill_file) and old_mtime == mtime:
                        continue

                try:
                    content = skill_file.read_text(encoding="utf-8", errors="replace")
                    category = (
                        str(skill_file.parent.relative_to(root))
                        if skill_file.parent != root
                        else "core"
                    )

                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO skills (name, path, category, content, size, modified)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (name, str(skill_file), category, content, len(content), mtime),
                    )

                    # FTS4 has no REPLACE semantics — inserting over an existing
                    # row leaves both, and search then returns duplicates.
                    cursor.execute("DELETE FROM skills_fts WHERE name = ?", (name,))
                    cursor.execute(
                        "INSERT INTO skills_fts (name, content, category) VALUES (?, ?, ?)",
                        (name, content, category),
                    )
                    indexed += 1

                except OSError as exc:
                    logger.log_error("skills", f"Failed to index {skill_file}", exc=exc)

        removed = 0
        for name in existing:
            if name not in current_skills:
                cursor.execute("DELETE FROM skills WHERE name = ?", (name,))
                cursor.execute("DELETE FROM skills_fts WHERE name = ?", (name,))
                removed += 1

        conn.commit()
        conn.close()

        if indexed or removed:
            logger.log_structured(
                "INFO", "skills", f"indexed {indexed}, removed {removed}, total {len(current_skills)}"
            )

    def load_skill(self, name: str) -> str | None:
        """Lazy load skill content by name"""
        conn = sqlite3.connect(str(SKILLS_DB))
        cursor = conn.cursor()
        cursor.execute("SELECT content FROM skills WHERE name = ?", (name,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return row[0]
        return None

    def search_skills(self, query: str, limit: int = 10) -> list[tuple[str, str, float]]:
        """Full-text search, ranked.

        FTS4 gives no usable score on its own (`rank` is FTS5-only), and the
        original query hardcoded `0.0`, so callers could not tell a strong
        match from noise. FTS narrows the candidates; the actual score is a
        term frequency over the name and body, computed here. With a few
        hundred skills that is instantaneous.
        """
        terms = [t for t in re.split(r"\W+", query.lower()) if t]
        if not terms:
            return []

        conn = sqlite3.connect(str(SKILLS_DB))
        cursor = conn.cursor()
        rows: list[tuple[str, str, str]] = []

        # FTS first. The query is quoted term by term so user input can never
        # be read as FTS syntax.
        fts_query = " ".join(f'"{t}"' for t in terms)
        try:
            cursor.execute(
                """
                SELECT DISTINCT s.name, s.category, s.content
                FROM skills_fts sf
                JOIN skills s ON s.name = sf.name
                WHERE skills_fts MATCH ?
                LIMIT ?
                """,
                (fts_query, max(limit * 20, 50)),
            )
            rows = list(cursor.fetchall())
        except sqlite3.OperationalError:
            rows = []

        if not rows:
            like = f"%{terms[0]}%"
            cursor.execute(
                "SELECT name, category, content FROM skills WHERE name LIKE ? OR content LIKE ? LIMIT ?",
                (like, like, max(limit * 20, 50)),
            )
            rows = list(cursor.fetchall())

        conn.close()

        scored: list[tuple[str, str, float]] = []
        for name, category, content in rows:
            haystack = f"{name} {category}".lower()
            body = (content or "").lower()
            score = 0.0
            for term in terms:
                # A hit in the name or category is worth much more than one in
                # the body, which is where long skill files swamp the signal.
                score += 5.0 * haystack.count(term)
                score += min(body.count(term), 20) * 0.25
            if score > 0:
                scored.append((name, category, round(score, 3)))

        scored.sort(key=lambda item: (-item[2], item[0]))
        results = scored[:limit]
        logger.log_structured(
            "DEBUG", "skills", f"Search '{query[:30]}': {len(results)} results"
        )
        return results

    def get_skills_context(self, agent_name: str, max_skills: int = 6) -> str:
        """Get relevant skills context for agent"""
        keywords = {
            "CODER": ["python", "code", "file", "script", "function", "class", "import", "pip", "git", "bash"],
            "DEBUGGER": ["error", "debug", "bug", "fix", "test", "trace", "exception", "logging", "pytest"],
            "OPTIMIZER": ["optimize", "performance", "speed", "memory", "fast", "profile", "refactor", "efficient"],
            "PLANNER": ["plan", "architecture", "design", "structure", "module", "component"],
            "REVIEWER": ["review", "quality", "standard", "pattern", "clean", "solid"]
        }

        agent_keywords = keywords.get(agent_name.upper(), ["code"])

        all_results = []
        for kw in agent_keywords[:3]:
            results = self.search_skills(kw, limit=5)
            for name, category, score in results:
                all_results.append((abs(score) if score else 0, name, category))

        seen = set()
        unique_results = []
        for score, name, category in sorted(all_results, reverse=True):
            if name not in seen:
                seen.add(name)
                unique_results.append((score, name, category))

        selected = unique_results[:max_skills]

        if not selected:
            return ""

        result = "\n\n📚 **Available Skills:**\n"
        for score, name, category in selected:
            content = self.load_skill(name)
            if content:
                short = content[:600] + "..." if len(content) > 600 else content
                result += f"\n### {name} ({category})\n{short}\n"

        return result

    def add_skill(self, path: Path) -> bool:
        """Add new skill file and index it"""
        if not path.exists() or not path.suffix == ".md":
            return False

        try:
            dest = SKILLS_DIR / path.name
            dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            self._scan_skills()
            logger.log_structured("INFO", "skills", f"Added skill: {path.name}")
            return True
        except Exception as e:
            logger.log_error("skills", f"Failed to add skill {path}", exc=e)
            return False

    def list_skills(self) -> list[tuple[str, str, int]]:
        """List all skills with metadata"""
        conn = sqlite3.connect(str(SKILLS_DB))
        cursor = conn.cursor()
        cursor.execute("SELECT name, category, size FROM skills ORDER BY category, name")
        results = [(row[0], row[1], row[2]) for row in cursor.fetchall()]
        conn.close()
        return results

    def rebuild_index(self):
        """Force rebuild FTS index"""
        logger.log_structured("INFO", "skills", "Rebuilding index")
        conn = sqlite3.connect(str(SKILLS_DB))
        cursor = conn.cursor()
        cursor.execute("DELETE FROM skills_fts")
        cursor.execute("SELECT name, content, category FROM skills")
        for row in cursor.fetchall():
            cursor.execute("INSERT INTO skills_fts (name, content, category) VALUES (?, ?, ?)", row)
        conn.commit()
        conn.close()
        logger.log_structured("INFO", "skills", "Index rebuilt")


def get_skills_manager() -> SkillsManager:
    """Get singleton SkillsManager"""
    return SkillsManager()
