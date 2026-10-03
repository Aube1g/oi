#!/usr/bin/env python3
"""
MCP Git Server — status, diff, commit, branch, stash
"""

import json
import sys
import subprocess
from xli.mcp.serverkit import filter_arguments, tool_descriptors

def run_git(args, cwd="."):
    """Run git command safely"""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return -1, "", str(e)

def git_status(cwd="."):
    code, stdout, stderr = run_git(["status", "--porcelain", "-b"], cwd)
    if code != 0:
        return f"Error: {stderr}"

    lines = stdout.strip().split("\n")
    branch_line = lines[0] if lines else ""
    files = []

    for line in lines[1:]:
        if line:
            status = line[:2]
            filename = line[3:].strip()
            files.append({
                "status": status,
                "file": filename
            })

    return json.dumps({
        "branch": branch_line.replace("## ", "").split("...")[0] if branch_line else "unknown",
        "files": files
    }, indent=2)

def git_diff(cwd=".", staged=False):
    args = ["diff"]
    if staged:
        args.append("--staged")

    code, stdout, stderr = run_git(args, cwd)
    if code != 0:
        return f"Error: {stderr}"
    return stdout or "No changes"

def git_commit(message, cwd=".", add_all=True):
    if add_all:
        run_git(["add", "-A"], cwd)

    code, stdout, stderr = run_git(["commit", "-m", message], cwd)
    if code == 0:
        return f"Committed: {message}\n{stdout}"
    return f"Error: {stderr}"

def git_branch(name, checkout=True, cwd="."):
    code, stdout, stderr = run_git(["branch", name], cwd)
    if code != 0:
        return f"Error creating branch: {stderr}"

    if checkout:
        code, stdout, stderr = run_git(["checkout", name], cwd)
        if code != 0:
            return f"Branch created but checkout failed: {stderr}"
        return f"Created and checked out: {name}"

    return f"Created branch: {name}"

def git_stash(cwd=".", message=None, pop=False):
    if pop:
        code, stdout, stderr = run_git(["stash", "pop"], cwd)
        if code == 0:
            return f"Stash popped:\n{stdout}"
        return f"Error: {stderr}"

    args = ["stash", "push"]
    if message:
        args.extend(["-m", message])

    code, stdout, stderr = run_git(args, cwd)
    if code == 0:
        return f"Stashed: {stdout}"
    return f"Error: {stderr}"

def git_log(cwd=".", limit=10, format="oneline"):
    args = ["log", f"-{limit}"]
    if format == "oneline":
        args.append("--oneline")
    elif format == "stat":
        args.append("--stat")

    code, stdout, stderr = run_git(args, cwd)
    if code == 0:
        return stdout
    return f"Error: {stderr}"

TOOLS = {
    "git_status": git_status,
    "git_diff": git_diff,
    "git_commit": git_commit,
    "git_branch": git_branch,
    "git_stash": git_stash,
    "git_log": git_log,
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

