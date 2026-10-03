#!/usr/bin/env python3
"""
MCP Security Scanner — bandit, safety, semgrep
"""

import json
import sys
import subprocess
from xli.mcp.serverkit import filter_arguments, tool_descriptors

def scan_bandit(path=".", severity="low", confidence="low"):
    """Scan Python code with bandit"""
    try:
        result = subprocess.run(
            ["bandit", "-r", path, "-f", "json",
             "-ll" if severity == "medium" else "-lll" if severity == "high" else "",
             "-ii" if confidence == "medium" else "-iii" if confidence == "high" else ""],
            capture_output=True,
            text=True,
            timeout=60
        )

        # Parse JSON output if possible
        try:
            data = json.loads(result.stdout)
            issues = data.get("results", [])

            if not issues:
                return "✅ No security issues found by bandit"

            summary = f"⚠️ Found {len(issues)} issues:\n"
            for issue in issues[:10]:
                summary += f"- {issue.get('test_name', 'Unknown')}: {issue.get('issue_text', '')}\n"
                summary += f"  File: {issue.get('filename', '')}:{issue.get('line_number', 0)}\n"

            return summary

        except json.JSONDecodeError:
            return result.stdout or result.stderr

    except FileNotFoundError:
        return "bandit not installed. pip install bandit"
    except Exception as e:
        return f"Error: {e}"

def scan_safety():
    """Check package vulnerabilities with safety"""
    try:
        result = subprocess.run(
            ["safety", "check", "--json"],
            capture_output=True,
            text=True,
            timeout=60
        )

        try:
            data = json.loads(result.stdout)
            vulnerabilities = data.get("vulnerabilities", [])

            if not vulnerabilities:
                return "✅ No known vulnerabilities found"

            summary = f"⚠️ Found {len(vulnerabilities)} vulnerabilities:\n"
            for vuln in vulnerabilities[:10]:
                summary += f"- {vuln.get('package_name', '')}: {vuln.get('vulnerability_id', '')}\n"
                summary += f"  {vuln.get('advisory', '')[:100]}...\n"

            return summary

        except json.JSONDecodeError:
            if result.returncode == 0:
                return "✅ No vulnerabilities found"
            return result.stdout or result.stderr

    except FileNotFoundError:
        return "safety not installed. pip install safety"
    except Exception as e:
        return f"Error: {e}"

def scan_semgrep(path=".", config="auto"):
    """Scan with semgrep"""
    try:
        args = ["semgrep", "--config", config, path, "--json"]

        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=120
        )

        try:
            data = json.loads(result.stdout)
            results = data.get("results", [])

            if not results:
                return "✅ No issues found by semgrep"

            summary = f"⚠️ Found {len(results)} issues:\n"
            for r in results[:10]:
                summary += f"- {r.get('check_id', 'Unknown')}: {r.get('extra', {}).get('message', '')}\n"
                summary += f"  File: {r.get('path', '')}:{r.get('start', {}).get('line', 0)}\n"

            return summary

        except json.JSONDecodeError:
            return result.stdout[:1000] or result.stderr[:500]

    except FileNotFoundError:
        return "semgrep not installed. pip install semgrep"
    except Exception as e:
        return f"Error: {e}"

TOOLS = {
    "scan_bandit": scan_bandit,
    "scan_safety": scan_safety,
    "scan_semgrep": scan_semgrep,
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

