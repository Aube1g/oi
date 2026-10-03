#!/usr/bin/env python3
"""
XLI web_search tool — free web search the LLM can actually call.

This lives in xli/tools/ (not xli/mcp/servers/) because the agent loop only
ever sees tools that are registered in xli.tools.registry via
xli.tools.builtin.BUILTIN_TOOLS. MCP servers under xli/mcp/servers/ are a
separate subsystem wired into fixed pre/post pipelines for specific
sub-agents (see xli/mcp/bridge.py's MCP_ROUTING) — they are never exposed to
the LLM as callable tools directly. A tool defined here, by contrast, shows
up in the system prompt's tool list and the LLM can call it like `read` or
`bash`.

Same free, no-API-key approach as before: rotates through public SearXNG
instances, tries JSON first and falls back to parsing the HTML results page
(most public instances disable format=json on purpose). Instance health is
cached to disk and merged with fresh discoveries from searx.space — nothing
is ever lost, only added to or temporarily cooled down.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from xli.tools.base import Param, Tool, ToolError, ToolResult, tool

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# --------------------------------------------------------------------------
# Seed list — never removed, always part of the pool regardless of what
# happens to the cache file or to searx.space.
# --------------------------------------------------------------------------
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
    "https://etsi.me",
    "https://search.hbubli.cc",
    "https://searx.oloke.xyz",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

from xli.paths import xli_path  # noqa: E402

_STATE_PATH = xli_path("web_search_instances.json")
_RETRYABLE_STATUS = {429, 503}
_REFRESH_INTERVAL = 6 * 3600
_DEAD_COOLDOWN = 900
_LAST_SUCCESS: dict = {}


def _headers(referer: str | None = None) -> dict:
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    if referer:
        h["Referer"] = referer
    return h


# --------------------------------------------------------------------------
# Persistent, merge-only cache of instance health.
# --------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        with open(_STATE_PATH, "r") as f:
            state = json.load(f)
        if not isinstance(state, dict) or "instances" not in state:
            raise ValueError("bad shape")
        return state
    except Exception:
        return {"instances": {}, "last_refresh": 0}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def _ensure_seeded(state: dict) -> dict:
    for url in BUILTIN_INSTANCES:
        if url not in state["instances"]:
            state["instances"][url] = {
                "source": "builtin", "dead_until": 0, "last_mode": None, "last_ok": 0,
            }
    return state


def _refresh_from_searx_space(state: dict) -> dict:
    if not HAS_HTTPX or os.environ.get("SEARXNG_REFRESH") == "0":
        return state
    now = time.time()
    if now - state.get("last_refresh", 0) < _REFRESH_INTERVAL:
        return state
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.get("https://searx.space/data/instances.json", headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        added = 0
        for url, info in data.get("instances", {}).items():
            url = url.rstrip("/")
            http_info = info.get("http", {}) or {}
            healthy = http_info.get("status_code") == 200 and not info.get("comments")
            if healthy and url not in state["instances"]:
                state["instances"][url] = {
                    "source": "discovered", "dead_until": 0, "last_mode": None, "last_ok": 0,
                }
                added += 1
        state["last_refresh"] = now
        state["_last_refresh_added"] = added
        state.pop("_last_refresh_error", None)
    except Exception as e:
        state["_last_refresh_error"] = str(e)
    return state


def _mark_result(state: dict, url: str, *, ok: bool, mode: str | None = None) -> None:
    entry = state["instances"].setdefault(
        url, {"source": "discovered", "dead_until": 0, "last_mode": None, "last_ok": 0}
    )
    if ok:
        entry["dead_until"] = 0
        entry["last_ok"] = time.time()
        if mode:
            entry["last_mode"] = mode
    else:
        entry["dead_until"] = time.time() + _DEAD_COOLDOWN


def _instance_pool(state: dict) -> list[str]:
    now = time.time()
    pinned = os.environ.get("SEARXNG_URL", "").strip().rstrip("/")
    alive = [u for u, info in state["instances"].items() if info.get("dead_until", 0) <= now]
    alive.sort(key=lambda u: state["instances"][u].get("last_ok", 0), reverse=True)
    pool = ([pinned] if pinned else []) + [u for u in alive if u != pinned]
    return pool or list(BUILTIN_INSTANCES)


# --------------------------------------------------------------------------
# HTML result parsing.
# --------------------------------------------------------------------------
_RESULT_BLOCK_RE = re.compile(
    r'<(?:article|div)[^>]*class="[^"]*\bresult\b[^"]*"[^>]*>(.*?)</(?:article|div)>', re.DOTALL,
)
_LINK_RE = re.compile(
    r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*(?:url_header|result[-_]?title)?[^"]*"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_ANY_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_SNIPPET_RE = re.compile(r'<p[^>]*class="[^"]*\bcontent\b[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SKIP_HOSTS = ("searx.space",)


def _clean_text(raw: str) -> str:
    return unescape(_TAG_RE.sub("", raw)).strip()


def _resolve_link(href: str, base_url: str) -> str | None:
    if href.startswith("#") or href.startswith("javascript:"):
        return None
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "url" in qs:
            return unquote(qs["url"][0])
        return None
    return href


def _parse_html_results(html: str, base_url: str, max_results: int) -> list[dict]:
    results, seen = [], set()
    for block in _RESULT_BLOCK_RE.findall(html):
        link_match = _LINK_RE.search(block) or _ANY_LINK_RE.search(block)
        if not link_match:
            continue
        href, title_html = link_match.groups()
        url = _resolve_link(href, base_url)
        if not url or url in seen or any(s in url for s in _SKIP_HOSTS):
            continue
        title = _clean_text(title_html)
        if not title:
            continue
        snippet_match = _SNIPPET_RE.search(block)
        results.append({
            "title": title, "url": url,
            "snippet": _clean_text(snippet_match.group(1)) if snippet_match else "",
        })
        seen.add(url)
        if len(results) >= max_results:
            break
    return results


def _try_json(client, base_url: str, query: str, language: str) -> list[dict] | None:
    params = {"q": query, "format": "json", "language": language}
    response = client.get(f"{base_url}/search", params=params, headers=_headers())
    if response.status_code == 403:
        return None
    if response.status_code in _RETRYABLE_STATUS:
        raise httpx.HTTPStatusError("retryable", request=response.request, response=response)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        return None
    return [
        {"title": item.get("title"), "url": item.get("url"), "snippet": item.get("content")}
        for item in data.get("results", []) if item.get("url")
    ]


def _try_html(client, base_url: str, query: str, language: str, max_results: int) -> list[dict]:
    params = {"q": query}
    if language and language != "auto":
        params["language"] = language
    response = client.get(f"{base_url}/search", params=params, headers=_headers(referer=base_url))
    if response.status_code in _RETRYABLE_STATUS:
        raise httpx.HTTPStatusError("retryable", request=response.request, response=response)
    response.raise_for_status()
    return _parse_html_results(response.text, base_url, max_results)


def _do_search(query: str, max_results: int, language: str) -> dict:
    state = _refresh_from_searx_space(_ensure_seeded(_load_state()))
    instances = _instance_pool(state)
    errors, tried = [], []

    for base_url in instances:
        tried.append(base_url)
        try:
            with httpx.Client(timeout=12, follow_redirects=True) as client:
                results = _try_json(client, base_url, query, language)
                mode = "json"
                if results is None:
                    results = _try_html(client, base_url, query, language, max_results)
                    mode = "html"

            if not results:
                errors.append(f"{base_url}: 0 results ({mode})")
                continue

            _mark_result(state, base_url, ok=True, mode=mode)
            _save_state(state)
            _LAST_SUCCESS.update({"instance": base_url, "mode": mode, "at": time.time()})
            return {
                "query": query, "instance_used": base_url, "mode": mode,
                "instances_tried": tried, "results": results[:max_results],
            }
        except httpx.HTTPStatusError as e:
            _mark_result(state, base_url, ok=False)
            errors.append(f"{base_url}: HTTP {getattr(e.response, 'status_code', '?')}")
        except httpx.TimeoutException:
            errors.append(f"{base_url}: timeout")
        except httpx.ConnectError:
            _mark_result(state, base_url, ok=False)
            errors.append(f"{base_url}: unreachable")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{base_url}: {e}")

    _save_state(state)
    return {"query": query, "results": [], "instances_tried": tried, "errors": errors}


@tool(
    "web_search",
    "Search the public web (free, no API key) via rotating public SearXNG "
    "instances. Returns titles, URLs and snippets. Use this whenever you "
    "need current information you don't already know.",
    [
        Param("query", "string", "The search query", required=True),
        Param("max_results", "integer", "Max results to return", default=5),
        Param("language", "string", "Language code, or 'auto'", default="auto"),
    ],
    tags=["search"],
)
def web_search(query: str, max_results: int = 5, language: str = "auto") -> ToolResult:
    if not HAS_HTTPX:
        raise ToolError("httpx not installed. pip install httpx")
    if not query or not query.strip():
        raise ToolError("query is required")

    outcome = _do_search(query.strip(), max(1, int(max_results)), language)
    if not outcome["results"]:
        return ToolResult.failure(
            f"no results; tried {len(outcome['instances_tried'])} instance(s): "
            f"{'; '.join(outcome['errors'][:3])}",
            tool="web_search",
            data=outcome,
        )
    return ToolResult.success(
        data=outcome,
        summary=f"{len(outcome['results'])} result(s) via {outcome['instance_used']} ({outcome['mode']})",
        tool="web_search",
    )


@tool(
    "fetch_page",
    "Fetch a URL's raw text content (no JS rendering). Use after web_search "
    "to read a specific result in more detail.",
    [
        Param("url", "string", "URL to fetch", required=True),
        Param("max_chars", "integer", "Max characters to return", default=3000),
    ],
    tags=["search"],
)
def fetch_page(url: str, max_chars: int = 3000) -> ToolResult:
    if not HAS_HTTPX:
        raise ToolError("httpx not installed. pip install httpx")
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            response = client.get(url, headers=_headers())
            response.raise_for_status()
            text = response.text[: max(1, int(max_chars))]
    except Exception as e:
        raise ToolError(str(e)) from e
    return ToolResult.success(
        data={"url": url, "content": text},
        summary=f"fetched {url} ({len(text)} chars)",
        tool="fetch_page",
    )


@tool(
    "search_status",
    "Show known SearXNG instances, their health (available/cooling down), "
    "which one/mode served the last successful search, and the last "
    "auto-refresh result. Use to check the web_search setup is working.",
    [],
    tags=["search"],
)
def search_status() -> ToolResult:
    state = _ensure_seeded(_load_state())
    now = time.time()

    instances_info = []
    for url, info in sorted(state["instances"].items()):
        dead_until = info.get("dead_until", 0)
        instances_info.append({
            "url": url,
            "source": info.get("source", "unknown"),
            "status": "cooling_down" if dead_until > now else "available",
            "cooldown_remaining_s": max(0, int(dead_until - now)) if dead_until > now else 0,
            "last_working_mode": info.get("last_mode"),
            "last_success_ago_s": int(now - info["last_ok"]) if info.get("last_ok") else None,
        })

    summary = {
        "total_known_instances": len(instances_info),
        "builtin_count": sum(1 for i in instances_info if i["source"] == "builtin"),
        "discovered_count": sum(1 for i in instances_info if i["source"] == "discovered"),
        "available_now": sum(1 for i in instances_info if i["status"] == "available"),
        "cooling_down": sum(1 for i in instances_info if i["status"] == "cooling_down"),
        "last_refresh_ago_s": int(now - state["last_refresh"]) if state.get("last_refresh") else None,
        "last_refresh_added": state.get("_last_refresh_added"),
        "last_refresh_error": state.get("_last_refresh_error"),
        "last_successful_search": _LAST_SUCCESS or None,
        "pinned_instance": os.environ.get("SEARXNG_URL") or None,
    }
    return ToolResult.success(
        data={"summary": summary, "instances": instances_info},
        summary=f"{summary['available_now']}/{summary['total_known_instances']} instances available",
        tool="search_status",
    )


WEB_SEARCH_TOOLS: list[Tool] = [web_search, fetch_page, search_status]

