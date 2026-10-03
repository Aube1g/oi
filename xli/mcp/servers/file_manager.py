#!/usr/bin/env python3
"""
MCP File Manager — read, write, list, grep, find
"""

import json
import sys
from pathlib import Path
import fnmatch
from xli.mcp.serverkit import filter_arguments, tool_descriptors

def read_file(path, offset=0, limit=None):
    try:
        content = Path(path).read_text(encoding="utf-8")
        lines = content.splitlines()
        if limit:
            lines = lines[offset:offset+limit]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def write_file(path, content, append=False):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(p, mode, encoding="utf-8") as f:
            f.write(content)
        return f"Written: {path}"
    except Exception as e:
        return f"Error: {e}"

def list_dir(path=".", pattern="*"):
    try:
        p = Path(path)
        files = []
        for item in p.iterdir():
            if fnmatch.fnmatch(item.name, pattern):
                files.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0
                })
        return json.dumps(files, indent=2)
    except Exception as e:
        return f"Error: {e}"

def grep(path, pattern, recursive=False):
    try:
        p = Path(path)
        matches = []

        if recursive:
            files = p.rglob("*")
        else:
            files = p.iterdir()

        for file in files:
            if file.is_file():
                try:
                    content = file.read_text(encoding="utf-8", errors="ignore")
                    for i, line in enumerate(content.splitlines(), 1):
                        if pattern in line:
                            matches.append(f"{file}:{i}:{line[:100]}")
                except Exception:
                    pass

        return "\n".join(matches[:50]) or "No matches"
    except Exception as e:
        return f"Error: {e}"

def find(root, name_pattern="*", type="any"):
    try:
        p = Path(root)
        matches = []

        for item in p.rglob(name_pattern):
            if type == "file" and not item.is_file():
                continue
            if type == "dir" and not item.is_dir():
                continue
            matches.append(str(item))

        return "\n".join(matches[:100])
    except Exception as e:
        return f"Error: {e}"

TOOLS = {
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "grep": grep,
    "find": find,
}

# Standard handler
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

