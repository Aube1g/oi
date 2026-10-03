#!/usr/bin/env python3
"""
XLI MCP Recommender v4 — Smart MCP + skills selection for tasks
"""


from xli.core.logger import StructuredLogger
from xli.core.skills import get_skills_manager
from xli.mcp.registry import MCPRegistry

logger = StructuredLogger("xli.mcp_recommender")


class MCPRecommender:
    """Intelligent MCP and skill recommendation"""

    def __init__(self):
        self.registry = MCPRegistry()
        self.skills = get_skills_manager()
        logger.log_structured("INFO", "mcp_recommender", "Initialized")

    def recommend_for_task(self, task: str) -> dict[str, list[str]]:
        """Get recommendations for task"""
        # MCP recommendations
        mcp_scores = {}
        for server_name in self.registry.list_enabled():
            score = self._score_mcp_relevance(task, server_name)
            if score > 0.3:
                mcp_scores[server_name] = score

        top_mcp = sorted(mcp_scores.items(), key=lambda x: x[1], reverse=True)[:5]

        # Skill recommendations
        skill_results = self.skills.search_skills(task, limit=5)
        top_skills = [name for name, _, _ in skill_results]

        logger.log_structured("INFO", "mcp_recommender",
                             f"Recommendations for: {task[:50]}",
                             {"mcp": len(top_mcp), "skills": len(top_skills)})

        return {
            "mcp_servers": [name for name, _ in top_mcp],
            "skills": top_skills,
            "mcp_scores": {k: round(v, 2) for k, v in top_mcp}
        }

    def _score_mcp_relevance(self, task: str, server_name: str) -> float:
        """Score MCP server relevance to task"""
        server = self.registry.get_server(server_name)
        if not server:
            return 0.0

        description = server.get("description", "").lower()
        tools = [t.lower() for t in server.get("tools", [])]
        task_lower = task.lower()

        score = 0.0

        # Description match
        desc_words = set(description.split())
        task_words = set(task_lower.split())
        desc_overlap = len(desc_words & task_words)
        score += desc_overlap * 0.1

        # Tool name match
        for tool in tools:
            if tool in task_lower:
                score += 0.3

        # Keyword match
        keywords = {
            "archaeologist": ["git", "blame", "history", "commit"],
            "refactor": ["refactor", "complexity", "clean"],
            "architecture": ["architecture", "dependency", "graph", "module"],
            "prompt": ["prompt", "template"],
            "auto_tester": ["test", "pytest", "unittest"],
            "debugger": ["debug", "error", "traceback", "exception"],
            "knowledge": ["search", "find", "code"],
            "package_monitor": ["package", "dependency", "pip", "vulnerability"],
            "shell_helper": ["shell", "command", "bash", "terminal"],
            "file_manager": ["file", "read", "write", "list", "grep"],
            "code_formatter": ["format", "black", "ruff", "style"],
            "git_mcp": ["git", "branch", "commit", "diff"],
            "env_manager": ["env", "config", "variable"],
            "db_client": ["database", "sql", "query", "schema"],
            "http_client": ["http", "api", "request", "curl"],
            "doc_generator": ["doc", "documentation", "sphinx"],
            "security_scanner": ["security", "vulnerability", "bandit"],
        }

        server_keywords = keywords.get(server_name, [])
        for kw in server_keywords:
            if kw in task_lower:
                score += 0.2

        return min(score, 1.0)

    def get_skill_recommendations(self, task: str, agent_name: str) -> str:
        """Get skill context for task"""
        return self.skills.get_skills_context(agent_name)

    def build_full_context(self, task: str, agent_name: str) -> str:
        """Build complete context with MCP + skills"""
        rec = self.recommend_for_task(task)

        context_parts = []

        # Add MCP context from recommended servers
        for server_name in rec["mcp_servers"][:3]:
            server = self.registry.get_server(server_name)
            if server:
                context_parts.append(
                    f"[MCP:{server_name}] {server.get('description', '')}"
                )

        # Add skills context
        skills_ctx = self.get_skill_recommendations(task, agent_name)
        if skills_ctx:
            context_parts.append(skills_ctx)

        return "\n\n".join(context_parts)


_INSTANCE: MCPRecommender | None = None


def get_recommender() -> MCPRecommender:
    """Process-wide MCPRecommender.

    This returned a fresh instance on every call despite the docstring, which
    rebuilt the MCP registry and re-opened the skills index each time.
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MCPRecommender()
    return _INSTANCE


def reset_recommender() -> None:
    """Drop the singleton — used by tests and after enabling/disabling servers."""
    global _INSTANCE
    _INSTANCE = None

