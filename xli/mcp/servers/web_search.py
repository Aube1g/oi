#!/usr/bin/env python3
"""
MCP Web Search — free web search using public SearXNG instances.

No setup, no Docker, no API key. Ships with a list of known public
SearXNG instances and rotates through them automatically: if one
instance fails, times out, or blocks the request (403/429/captcha),
the next one is tried.

Optional: set SEARXNG_URL to pin your own instance first (tried before
the public list). Optional: set SEARXNG_REFRESH=1 to pull a fresh
instance list from searx.space on startup (falls back to the built-in
list if that fails or is unreachable).
"""

from __future__ import annotations

import json
import os
import random
import sys
import time

from xli.mcp.serverkit import filter_arguments, shake_hands, tool_descriptors

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# ---------------------------------------------------------------------------
# Known-good public SearXNG instances (JSON format enabled), Sep 2026.
# Kept deliberately diverse (different operators/hosts) so one operator
# rate-limiting us doesn't take out the whole list.
# ---------------------------------------------------------------------------
BUILTIN_INSTANCES = [
    "https://searx.be",
    "https://priv.au",
    "https://searx.tiekoetter.com",
    "https://baresearch.org",
    "https://search.inetol.net",
    "https://searx.perennialte.ch",
    "https://opnxng.com",
    "https://search.bus-hit.me",
    "https://searxng.site",
    "https://copp.gg",
    "https://search.rhscz.eu",
    "https://searx.stream",
]

# Realistic desktop UAs, rotated per-request — a static UA is itself a
# fingerprint some instances flag.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

_STATE_PATH = os.path.join(os.path.dirname(__file__), ".web_search_state.json")
_RETRYABLE_STATUS = {403, 429, 503}


def _headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _load_dead_instances() -> dict:
    """Instances that failed recently, so we skip them for a cooldown window."""
    try:
        with open(_STATE_PATH, "r") as f:
            data = json.load(f)
        now = time.time()
        return {url: until for url, until in data.items() if until > now}
    except Exception:
        return {}


def _save_dead_instances(dead: dict) -> None:
    try:
        with open(_STATE_PATH, "w") as f:
            json.dump(dead, f)
    except Exception:
        pass  # best-effort only


def _mark_dead(url: str, cooldown_seconds: int = 900) -> None:
    dead = _load_dead_instances()
    dead[url] = time.time() + cooldown_seconds
    _save_dead_instances(dead)


def _refresh_from_searx_space() -> list[str]:
    """Pull a live instance list from searx.space (best-effort, optional)."""
    if not HAS_HTTPX:
        return []
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get("https://searx.space/data/instances.json", headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        fresh = []
        for url, info in data.get("instances", {}).items():
            http_info = info.get("http", {})
            if http_info.get("status_code") == 200 and not info.get("comments"):
                fresh.append(url.rstrip("/"))
        return fresh
    except Exception:
        return []


def _instance_pool() -> list[str]:
    pool = []
    pinned = os.environ.get("SEARXNG_URL", "").strip().rstrip("/")
    if pinned:
        pool.append(pinned)

    if os.environ.get("SEARXNG_REFRESH") == "1":
        fresh = _refresh_from_searx_space()
        if fresh:
            random.shuffle(fresh)
            pool.extend(fresh[:20])

    remaining = [u for u in BUILTIN_INSTANCES if u not in pool]
    random.shuffle(remaining)
    pool.extend(remaining)

    dead = _load_dead_instances()
    return [u for u in pool if u not in dead] or pool  # never return empty


def web_search(query: str, max_results: int = 5, language: str = "auto") -> str:
    """Search the web (free, no API key) via rotating public SearXNG instances."""
    if not HAS_HTTPX:
        return "httpx not installed. pip install httpx"
    if not query:
        return "Error: query is required"

    instances = _instance_pool()
    errors = []

    for base_url in instances:
        try:
            params = {"q": query, "format": "json", "language": language}
            with httpx.Client(timeout=12, follow_redirects=True) as client:
                response = client.get(f"{base_url}/search", params=params, headers=_headers())

            if response.status_code in _RETRYABLE_STATUS:
                _mark_dead(base_url)
                errors.append(f"{base_url}: HTTP {response.status_code}")
                continue
            response.raise_for_status()

            try:
                data = response.json()
            except ValueError:
                # Instance returned HTML (JSON format disabled) — treat as dead.
                _mark_dead(base_url)
                errors.append(f"{base_url}: non-JSON response")
                continue

            results = []
            for item in data.get("results", [])[:max_results]:
                results.append({
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "snippet": item.get("content"),
                })

            if not results:
                errors.append(f"{base_url}: 0 results")
                continue

            return json.dumps(
                {"query": query, "instance": base_url, "results": results},
                indent=2,
                ensure_ascii=False,
            )

        except httpx.TimeoutException:
            errors.append(f"{base_url}: timeout")
            continue
        except httpx.ConnectError:
            _mark_dead(base_url)
            errors.append(f"{base_url}: unreachable")
            continue
        except Exception as e:  # noqa: BLE001 - keep trying other instances
            errors.append(f"{base_url}: {e}")
            continue

    return json.dumps({"query": query, "results": [], "errors": errors[:5]}, indent=2, ensure_ascii=False)


def fetch_page(url: str, max_chars: int = 3000) -> str:
    """Fetch a URL's raw text content (no JS rendering)."""
    if not HAS_HTTPX:
        return "httpx not installed. pip install httpx"

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            response = client.get(url, headers=_headers())
            response.raise_for_status()
            return response.text[:max_chars]
    except Exception as e:
        return f"Error: {e}"


TOOLS = {
    "web_search": web_search,
    "fetch_page": fetch_page,
}


def handle_request(request):
    # initialize / ping / notifications, per the protocol. See
    # xli.mcp.serverkit.shake_hands for why this is not the server's job.
    early = shake_hands(request)
    if early is not None:
        return early

    method = request.get("method")
    req_id = request.get("id")

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "result": {"tools": tool_descriptors(TOOLS)},
            "id": req_id,
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
                    "id": req_id,
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

