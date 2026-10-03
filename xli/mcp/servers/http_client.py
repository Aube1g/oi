#!/usr/bin/env python3
"""
MCP HTTP Client — curl-like requests, API testing
"""

import json
import sys
from xli.mcp.serverkit import filter_arguments, tool_descriptors
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

def http_request(method="GET", url="", headers=None, body=None, timeout=30):
    """Make HTTP request"""
    if not HAS_HTTPX:
        return "httpx not installed. pip install httpx"

    try:
        method = method.upper()
        headers = headers or {}

        with httpx.Client(timeout=timeout) as client:
            if method == "GET":
                response = client.get(url, headers=headers)
            elif method == "POST":
                response = client.post(url, headers=headers, json=body)
            elif method == "PUT":
                response = client.put(url, headers=headers, json=body)
            elif method == "DELETE":
                response = client.delete(url, headers=headers)
            elif method == "PATCH":
                response = client.patch(url, headers=headers, json=body)
            else:
                return f"Unsupported method: {method}"

            result = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": response.text[:2000],  # Limit body size
                "url": str(response.url)
            }

            return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error: {e}"

def test_api(base_url, endpoints):
    """Test multiple API endpoints"""
    if not HAS_HTTPX:
        return "httpx not installed"

    results = []

    for endpoint in endpoints:
        method = endpoint.get("method", "GET")
        path = endpoint.get("path", "/")
        expected_status = endpoint.get("expected_status", 200)

        url = base_url.rstrip("/") + "/" + path.lstrip("/")

        try:
            with httpx.Client(timeout=10) as client:
                if method == "GET":
                    response = client.get(url)
                elif method == "POST":
                    response = client.post(url, json=endpoint.get("body", {}))
                else:
                    response = client.request(method, url)

                passed = response.status_code == expected_status
                results.append({
                    "endpoint": path,
                    "method": method,
                    "status": response.status_code,
                    "expected": expected_status,
                    "passed": passed,
                    "response_preview": response.text[:200]
                })

        except Exception as e:
            results.append({
                "endpoint": path,
                "method": method,
                "error": str(e),
                "passed": False
            })

    passed_count = sum(1 for r in results if r.get("passed"))

    summary = f"Passed: {passed_count}/{len(results)}\n"
    summary += json.dumps(results, indent=2)

    return summary

def curl_like(url, method="GET", headers=None, data=None):
    """curl-like request with full output"""
    return http_request(method, url, headers, data)

TOOLS = {
    "http_request": http_request,
    "test_api": test_api,
    "curl_like": curl_like,
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

