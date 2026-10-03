#!/usr/bin/env python3
"""
XLI MCP Bridge v4 — Pre/post steps, smart server selection
"""


from xli.core.logger import StructuredLogger
from xli.mcp.client import MCPClient
from xli.mcp.registry import MCPRegistry

logger = StructuredLogger("xli.mcp_bridge")

# MCP routing: which servers to use for which agents
MCP_ROUTING = {
    "coder": {
        "pre": ["knowledge", "architecture"],
        "post": ["auto_tester", "code_formatter"],
    },
    "debugger": {
        "pre": ["debugger"],
        "post": ["auto_tester"],
    },
    "optimizer": {
        "pre": ["refactor", "architecture"],
        "post": ["auto_tester", "security_scanner"],
    },
    "tester": {
        "pre": ["knowledge"],
        "post": [],
    },
    "reviewer": {
        "pre": ["architecture"],
        "post": ["security_scanner"],
    },
    "planner": {
        "pre": ["knowledge"],
        "post": [],
    }
}


class MCPBridge:
    """Bridge between agents and MCP servers"""

    def __init__(self):
        self.client = MCPClient()
        self.registry = MCPRegistry()
        logger.log_structured("INFO", "mcp_bridge", "Initialized")

    async def run_mcp_pre_step(self, agent_name: str, task: str) -> str:
        """Run MCP pre-steps for agent"""
        routing = MCP_ROUTING.get(agent_name.lower(), {})
        pre_servers = routing.get("pre", [])

        if not pre_servers:
            return ""

        context_parts = []

        for server_name in pre_servers:
            if not self.registry.is_enabled(server_name):
                continue

            server = self.registry.get_server(server_name)
            if not server:
                continue

            tools = server.get("tools", [])
            if not tools:
                continue

            # Select first available tool
            tool = tools[0]

            try:
                result = await self.client.call_tool(
                    server_name,
                    tool,
                    {"query": task, "code": task}
                )

                if result and "error" not in str(result).lower():
                    context_parts.append(f"[{server_name}] {str(result)[:500]}")
                    logger.log_structured("DEBUG", "mcp_bridge",
                                         f"Pre-step: {server_name}/{tool}")

            except Exception as e:
                logger.log_error("mcp_bridge",
                                f"Pre-step failed: {server_name}", exc=e)

        return "\n\n".join(context_parts) if context_parts else ""

    async def run_mcp_post_step(self, agent_name: str, code: str) -> str:
        """Run MCP post-steps for agent"""
        routing = MCP_ROUTING.get(agent_name.lower(), {})
        post_servers = routing.get("post", [])

        if not post_servers:
            return ""

        results = []

        for server_name in post_servers:
            if not self.registry.is_enabled(server_name):
                continue

            server = self.registry.get_server(server_name)
            if not server:
                continue

            tools = server.get("tools", [])
            if not tools:
                continue

            tool = tools[0]

            try:
                result = await self.client.call_tool(
                    server_name,
                    tool,
                    {"code": code, "test_code": code}
                )

                if result and "error" not in str(result).lower():
                    results.append(f"[{server_name}] {str(result)[:500]}")
                    logger.log_structured("DEBUG", "mcp_bridge",
                                         f"Post-step: {server_name}/{tool}")

            except Exception as e:
                logger.log_error("mcp_bridge",
                                f"Post-step failed: {server_name}", exc=e)

        return "\n\n".join(results) if results else ""

    def get_relevant_servers(self, task: str) -> list[str]:
        """Get relevant MCP servers for task"""
        keywords = {
            "git": ["git_mcp", "archaeologist"],
            "test": ["auto_tester"],
            "debug": ["debugger"],
            "error": ["debugger"],
            "format": ["code_formatter"],
            "style": ["code_formatter"],
            "security": ["security_scanner"],
            "vulnerability": ["security_scanner", "package_monitor"],
            "dependency": ["package_monitor", "architecture"],
            "import": ["architecture"],
            "database": ["db_client"],
            "sql": ["db_client"],
            "api": ["http_client"],
            "http": ["http_client"],
            "request": ["http_client"],
            "env": ["env_manager"],
            "config": ["env_manager"],
            "document": ["doc_generator"],
            "doc": ["doc_generator"],
            "shell": ["shell_helper"],
            "command": ["shell_helper"],
            "file": ["file_manager"],
            "find": ["file_manager"],
            "grep": ["file_manager"],
        }

        task_lower = task.lower()
        relevant = set()

        for kw, servers in keywords.items():
            if kw in task_lower:
                relevant.update(servers)

        # Filter enabled
        enabled = [s for s in relevant if self.registry.is_enabled(s)]

        logger.log_structured("DEBUG", "mcp_bridge",
                             f"Relevant servers: {enabled}",
                             {"task": task[:50]})

        return enabled


# Global bridge instance
_mcp_bridge = None

async def run_mcp_pre_step(agent_name: str, task: str) -> str:
    """Global pre-step function"""
    global _mcp_bridge
    if _mcp_bridge is None:
        _mcp_bridge = MCPBridge()
    return await _mcp_bridge.run_mcp_pre_step(agent_name, task)

async def run_mcp_post_step(agent_name: str, code: str) -> str:
    """Global post-step function"""
    global _mcp_bridge
    if _mcp_bridge is None:
        _mcp_bridge = MCPBridge()
    return await _mcp_bridge.run_mcp_post_step(agent_name, code)

