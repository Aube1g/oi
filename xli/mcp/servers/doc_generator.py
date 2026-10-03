#!/usr/bin/env python3
"""
MCP Doc Generator — pdoc, sphinx, mkdocs
"""

import json
import sys
import subprocess
from pathlib import Path
from xli.mcp.serverkit import filter_arguments, tool_descriptors

def generate_pdoc(module_path, output_dir="docs"):
    """Generate API docs via pdoc"""
    try:
        result = subprocess.run(
            ["pdoc", "-o", output_dir, module_path],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            return f"✅ Documentation generated in {output_dir}/\n{result.stdout}"
        return f"pdoc error:\n{result.stderr}"

    except FileNotFoundError:
        return "pdoc not installed. pip install pdoc"
    except Exception as e:
        return f"Error: {e}"

def generate_sphinx(source_dir=".", output_dir="_build"):
    """Generate Sphinx documentation"""
    try:
        # Check for conf.py
        conf = Path(source_dir) / "conf.py"
        if not conf.exists():
            return "No conf.py found. Run sphinx-quickstart first."

        result = subprocess.run(
            ["sphinx-build", "-b", "html", source_dir, output_dir],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            return f"✅ Sphinx docs built in {output_dir}/\n{result.stdout[:500]}"
        return f"Sphinx error:\n{result.stderr[:500]}"

    except FileNotFoundError:
        return "sphinx not installed. pip install sphinx"
    except Exception as e:
        return f"Error: {e}"

def generate_mkdocs(docs_dir="docs", serve=False):
    """Generate MkDocs documentation"""
    try:
        # Check for mkdocs.yml
        config = Path("mkdocs.yml")
        if not config.exists():
            return "No mkdocs.yml found. Run mkdocs new . first."

        if serve:
            return "Use mkdocs serve separately for preview"

        result = subprocess.run(
            ["mkdocs", "build"],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            return f"✅ MkDocs built\n{result.stdout}"
        return f"MkDocs error:\n{result.stderr}"

    except FileNotFoundError:
        return "mkdocs not installed. pip install mkdocs"
    except Exception as e:
        return f"Error: {e}"

TOOLS = {
    "generate_pdoc": generate_pdoc,
    "generate_sphinx": generate_sphinx,
    "generate_mkdocs": generate_mkdocs,
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

