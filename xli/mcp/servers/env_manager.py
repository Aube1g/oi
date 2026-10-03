#!/usr/bin/env python3
"""
MCP Env Manager — read, write, validate .env files
"""

import json
import sys
from pathlib import Path
from xli.mcp.serverkit import filter_arguments, tool_descriptors

def read_env(path=".env"):
    try:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"

        lines = p.read_text(encoding="utf-8").splitlines()
        env = {}

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("\"'")

        return json.dumps(env, indent=2)
    except Exception as e:
        return f"Error: {e}"

def write_env(path, key, value, create=True):
    try:
        p = Path(path)

        if not p.exists() and not create:
            return f"File not found: {path}"

        lines = []
        if p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()

        # Update or add key
        updated = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                updated = True
                break

        if not updated:
            lines.append(f"{key}={value}")

        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"Updated: {key}={value[:20]}..."
    except Exception as e:
        return f"Error: {e}"

def validate_env(path, required_keys):
    try:
        env_data = json.loads(read_env(path))
        missing = [k for k in required_keys if k not in env_data or not env_data[k]]

        if missing:
            return f"❌ Missing keys: {', '.join(missing)}"
        return "✅ All required keys present"
    except Exception as e:
        return f"Error: {e}"

def list_env(path=".env"):
    return read_env(path)

TOOLS = {
    "read_env": read_env,
    "write_env": write_env,
    "validate_env": validate_env,
    "list_env": list_env,
}

def handle_request(request):
    method = request.get("method")
    req_id = request.get("id")

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "result": {"tools": tool_descriptors(TOOLS)},
            "id": req_id
        }
    elif method == "tools/call":
        tool = request.get("params", {}).get("name")
        args = request.get("params", {}).get("arguments", {})

        if tool in TOOLS:
            try:
                result = TOOLS[tool](**filter_arguments(TOOLS[tool], args))
                return {
                    "jsonrpc": "2.0",
                    "result": {"content": [{"type": "text", "text": result}]},
                    "id": req_id
                }
            except Exception as e:
                return {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(e)}, "id": req_id}

        return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown tool: {tool}"}, "id": req_id}

    return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown method: {method}"}, "id": req_id}

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

