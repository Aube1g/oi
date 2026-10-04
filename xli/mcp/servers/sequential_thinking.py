#!/usr/bin/env python3
"""MCP server: sequential thinking.

Provides a structured thinking tool that helps the agent break down complex
problems into numbered steps, track its reasoning chain, and revise earlier
thoughts when new information arrives. This is the same concept as the popular
`@modelcontextprotocol/server-sequential-thinking` npm package, implemented
as a pure-Python stdio MCP server so it works without Node.js.
"""
import json
import sys
from xli.mcp.serverkit import filter_arguments, shake_hands, tool_descriptors

# In-memory thought chains keyed by a caller-chosen context id so multiple
# agents or tasks can think independently within the same process.
_chains: dict[str, list[dict]] = {}


def sequential_thinking(
    thought: str,
    step: int,
    total_steps: int,
    context_id: str = "default",
    revise_step: int = 0,
) -> str:
    """Record one step of a structured reasoning chain.

    Args:
        thought: The reasoning content for this step.
        step: Current step number (1-based).
        total_steps: Estimated total steps in this chain.
        context_id: Identifier to separate concurrent chains.
        revise_step: If > 0, replace that earlier step instead of appending.

    Returns:
        A summary of the chain so far.
    """
    if context_id not in _chains:
        _chains[context_id] = []

    chain = _chains[context_id]
    entry = {"step": step, "total_steps": total_steps, "thought": thought}

    if revise_step and 1 <= revise_step <= len(chain):
        chain[revise_step - 1] = entry
        action = f"revised step {revise_step}"
    else:
        chain.append(entry)
        action = f"recorded step {step}/{total_steps}"

    # Build a readable summary of the chain so far
    lines = [f"[{action}] context={context_id}, steps recorded: {len(chain)}"]
    for e in chain:
        prefix = f"  Step {e['step']}/{e['total_steps']}:"
        # Truncate long thoughts for the summary
        t = e["thought"]
        if len(t) > 200:
            t = t[:197] + "..."
        lines.append(f"{prefix} {t}")

    return "\n".join(lines)


def get_chain(context_id: str = "default") -> str:
    """Return the full reasoning chain for a context."""
    chain = _chains.get(context_id, [])
    if not chain:
        return f"No thoughts recorded for context '{context_id}'."
    lines = [f"Chain '{context_id}' ({len(chain)} steps):"]
    for e in chain:
        lines.append(f"  [{e['step']}/{e['total_steps']}] {e['thought']}")
    return "\n".join(lines)


def clear_chain(context_id: str = "default") -> str:
    """Clear a reasoning chain."""
    removed = len(_chains.pop(context_id, []))
    return f"Cleared {removed} steps from context '{context_id}'."


TOOLS = {
    "sequential_thinking": sequential_thinking,
    "get_chain": get_chain,
    "clear_chain": clear_chain,
}


# --- standard request handler ---
def handle_request(req):
    # initialize / ping / notifications, per the protocol. See
    # xli.mcp.serverkit.shake_hands for why this is not the server's job.
    early = shake_hands(req)
    if early is not None:
        return early

    method = req.get("method")
    rid = req.get("id")
    if method == "tools/list":
        return {"jsonrpc": "2.0", "result": {"tools": tool_descriptors(TOOLS)}, "id": rid}
    elif method == "tools/call":
        tool = req.get("params", {}).get("name")
        args = req.get("params", {}).get("arguments", {})
        if tool in TOOLS:
            try:
                res = TOOLS[tool](**filter_arguments(TOOLS[tool], args))
                return {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": res}]}, "id": rid}
            except Exception as e:
                return {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(e)}, "id": rid}
        return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown {tool}"}, "id": rid}
    return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown method {method}"}, "id": rid}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            print(json.dumps(resp), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": str(e)}}), flush=True)


if __name__ == "__main__":
    main()
