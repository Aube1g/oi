#!/usr/bin/env python3
"""
MCP Code Formatter — black, ruff, mypy, isort
"""

import json
import sys
import subprocess
from xli.mcp.serverkit import filter_arguments, tool_descriptors

def format_black(code, line_length=100):
    try:
        result = subprocess.run(
            ["black", "-", "--line-length", str(line_length)],
            input=code,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return result.stdout
        return f"Black error:\n{result.stderr}"
    except FileNotFoundError:
        return "black not installed. pip install black"
    except Exception as e:
        return f"Error: {e}"

def lint_ruff(code, select="E,W,F,I"):
    try:
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            temp_path = f.name

        result = subprocess.run(
            ["ruff", "check", temp_path, "--select", select],
            capture_output=True,
            text=True,
            timeout=30
        )

        os.unlink(temp_path)

        if result.returncode == 0:
            return "✅ No issues found"
        return f"Ruff issues:\n{result.stdout or result.stderr}"

    except FileNotFoundError:
        return "ruff not installed. pip install ruff"
    except Exception as e:
        return f"Error: {e}"

def type_check_mypy(code, strict=False):
    try:
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            temp_path = f.name

        args = ["mypy", temp_path]
        if strict:
            args.append("--strict")

        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30
        )

        os.unlink(temp_path)

        return result.stdout or result.stderr or "✅ Type check passed"

    except FileNotFoundError:
        return "mypy not installed. pip install mypy"
    except Exception as e:
        return f"Error: {e}"

def sort_imports(code):
    try:
        result = subprocess.run(
            ["isort", "-"],
            input=code,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return result.stdout
        return f"isort error:\n{result.stderr}"
    except FileNotFoundError:
        return "isort not installed. pip install isort"
    except Exception as e:
        return f"Error: {e}"

TOOLS = {
    "format_black": format_black,
    "lint_ruff": lint_ruff,
    "type_check_mypy": type_check_mypy,
    "sort_imports": sort_imports,
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
