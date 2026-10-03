#!/usr/bin/env python3
# XLI Web Search Enhanced v10
# Defensive hardening: no CAPTCHA solving/stealth bypass; challenge detection, SSRF policy, bounded search, and evidence metadata.
"""
XLI web_search tool — ENHANCED v10

Single-file search subsystem for XLI.

Highlights:
- SearXNG JSON API + HTML fallback
- Automatic SearXNG instance discovery
- Persistent instance health, latency and circuit-breaker state
- Result cache with TTL
- Query intent detection and search profiles: fast/balanced/deep
- Native time_range, pagination, engines, categories, safesearch
- Domain scoping
- Query presets: files, admin, sensitive, tech, osint, recon, github, docs
- URL canonicalization and tracking-parameter removal
- Result deduplication, relevance scoring and source diversification
- Search metadata and diagnostics
- Page extraction: title, description, author, dates, main text and links
- fetch_page and fetch_pages
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import socket
import threading
import time
from collections import Counter
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import (
    parse_qs,
    urlencode,
    unquote,
    urljoin,
    urlparse,
    urlunparse,
)

from xli.tools.base import Param, Tool, ToolError, ToolResult, tool

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


# ==========================================================================
# CONFIG
# ==========================================================================

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
    "https://search.sapti.me",
    "https://searx.foobar.vip",
    "https://search.projectsegfau.lt",
    "https://searxng.world",
    "https://search.im-in.space",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0",
]

# Paths come from xli.paths so $XLI_CONFIG_DIR moves them with everything
# else. Hardcoding ~/.xli here meant a user with a relocated config directory
# still had their search cache and instance list written to the real home —
# two conflicting truths about where xli keeps its state.
from xli.paths import xli_path  # noqa: E402

STATE_PATH = xli_path("web_search_instances.json")
CACHE_PATH = xli_path("web_search_cache")
STATE_LOCK_PATH = xli_path("web_search_instances.lock")
_STATE_LOCK = threading.RLock()

RETRYABLE_STATUS = {429, 502, 503, 504}
REFRESH_INTERVAL = 6 * 3600
COOLDOWN_SECONDS = 900
MAX_PAGES = 5
MAX_RESULTS = 100
PARALLEL_FANOUT = 5
REQUEST_TIMEOUT = 8.0
FETCH_TIMEOUT = 12.0

PROFILE_CONFIG = {
    "fast": {
        "pages": 1,
        "variants": 1,
        "fanout": 3,
    },
    "balanced": {
        "pages": 2,
        "variants": 3,
        "fanout": 5,
    },
    "deep": {
        "pages": 3,
        "variants": 4,
        "fanout": 7,
    },
}

CACHE_TTLS = {
    "fast": 300,
    "balanced": 1800,
    "deep": 900,
}

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
}

SKIP_RESULT_HOSTS = {
    "searx.space",
}

LAST_SUCCESS: dict = {}
INSTANCE_ROTATION_CURSOR = 0
INSTANCE_PROBE_TTL = 15 * 60
INSTANCE_BAD_RESULT_COOLDOWN = 30 * 60
CHALLENGE_COOLDOWN = 60 * 60
MIN_SEARCH_QUALITY = 0.24
MAX_BACKEND_RESULT_RATIO = 0.40
CHALLENGE_MARKERS = (
    "captcha", "cf-chl", "challenge-platform", "turnstile",
    "hcaptcha", "recaptcha", "datadome", "verify you are human",
    "checking your browser", "robot check", "attention required",
)

# Pages that belong to the search infrastructure itself are never useful
# answers to a user query. They are treated as provider contamination.
BACKEND_HOST_MARKERS = {
    "searx.space",
}
BACKEND_PATH_MARKERS = (
    "/preferences",
    "/stats",
    "/about",
    "/settings",
)
BACKEND_TITLE_MARKERS = (
    "public instances",
    "instance statistics",
)
BACKEND_KNOWN_PATHS = (
    "/searxng/searxng",
    "/searxng/searxng/issues",
    "/searxng/searxng/wiki",
)


# ========================= XLI v10 HARDENING =========================
CACHE_SCHEMA_VERSION = 4
RANKING_VERSION = 5
MAX_SEARCH_REQUESTS = {"fast": 8, "balanced": 16, "deep": 30}
MAX_REDIRECTS_SAFE = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS_SAFE = 250_000

_PRIVATE_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
}

def _is_private_or_local_ip(host: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )

def _safe_url_policy(url: str) -> tuple[bool, str]:
    from urllib.parse import urlsplit
    try:
        p = urlsplit(url)
    except Exception:
        return False, "invalid_url"
    if p.scheme.lower() not in {"http", "https"}:
        return False, "unsupported_scheme"
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return False, "missing_host"
    if host in _PRIVATE_HOSTNAMES or _is_private_or_local_ip(host):
        return False, "local_or_private_host"
    return True, "ok"

def _challenge_confidence(status_code: int, headers: dict, body: str) -> tuple[bool, float, str]:
    text = (body or "")[:120_000].lower()
    hdr = " ".join(f"{k}:{v}" for k, v in (headers or {}).items()).lower()
    strong = (
        "cf-chl-" in text or "challenge-platform" in text
        or "turnstile" in text or "hcaptcha" in text
        or "recaptcha" in text or "datadome" in text
    )
    medium = (
        "verify you are human" in text
        or "checking your browser" in text
        or "robot check" in text
        or "attention required" in text
    )
    captcha_word = "captcha" in text
    cf_header = "cf-mitigated:challenge" in hdr
    if strong or cf_header:
        return True, 0.98, "challenge"
    if medium and status_code in {403, 429}:
        return True, 0.92, "challenge"
    if captcha_word and status_code in {403, 429}:
        return True, 0.84, "captcha"
    return False, 0.0, ""

def _content_type_kind(content_type: str) -> str:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in {"text/html", "application/xhtml+xml"}:
        return "html"
    if ct in {"application/json", "application/ld+json"}:
        return "json"
    if ct.startswith("text/"):
        return "text"
    if ct == "application/pdf":
        return "pdf"
    if ct.startswith("image/"):
        return "image"
    return "binary"

def _query_requires_freshness(query: str) -> bool:
    q = (query or "").lower()
    return bool(re.search(
        r"\b(latest|current|today|yesterday|recent|new|updated|update|202[5-9]|20[3-9]\d)\b",
        q
    ))

def _normalise_backend_score(value, values=None) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return 0.0
    if values:
        nums = []
        for v in values:
            try:
                nums.append(float(v))
            except (TypeError, ValueError):
                pass
        if len(nums) >= 2:
            lo, hi = min(nums), max(nums)
            if hi > lo:
                return max(0.0, min(1.0, (x - lo) / (hi - lo)))
    if 0.0 <= x <= 1.0:
        return x
    return max(0.0, min(1.0, x / (1.0 + abs(x))))

def _cache_key_v10(query: str, profile: str, language: str = "", time_range: str = "") -> str:
    import hashlib
    raw = "\x1f".join([
        str(CACHE_SCHEMA_VERSION), str(RANKING_VERSION),
        str(profile or ""), str(language or ""), str(time_range or ""),
        str(query or "").strip().lower(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

async def _validate_external_url(url: str) -> tuple[bool, str]:
    """Validate URL and resolve its host before making an outbound request."""
    ok, reason = _safe_url_policy(url)
    if not ok:
        return False, reason
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        for info in infos:
            sockaddr = info[4]
            address = sockaddr[0]
            if _is_private_or_local_ip(address):
                return False, "dns_resolves_to_local_or_private_host"
    except socket.gaierror:
        return False, "dns_resolution_failed"
    except Exception as exc:
        return False, f"url_validation_failed:{type(exc).__name__}"
    return True, "ok"

def _safe_result_url(url: str) -> bool:
    return _safe_url_policy(url)[0]

def _evidence_source_type(url: str, title: str = "") -> str:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    title_l = (title or "").lower()
    if host in {"docs.python.org", "developer.mozilla.org"}:
        return "official_docs"
    if host.endswith("github.com"):
        return "github"
    if any(x in host for x in ("pypi.org", "npmjs.com", "crates.io", "rubygems.org")):
        return "package_registry"
    if any(x in title_l for x in ("documentation", "reference", "api docs")):
        return "docs"
    if any(x in host for x in ("reddit.com", "stackoverflow.com", "stackexchange.com")):
        return "community"
    if any(x in host for x in ("medium.com", "dev.to", "hashnode.dev")):
        return "blog"
    return "unknown"

def _independence_key(url: str, title: str = "") -> str:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    normalized_title = re.sub(r"\W+", " ", (title or "").lower()).strip()
    return f"{host}|{' '.join(normalized_title.split()[:12])}"

# ====================================================================

def _is_backend_result(result: dict, instance: str | None = None) -> bool:
    if not isinstance(result, dict):
        return True
    raw_url = str(result.get("url") or "")
    try:
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except Exception:
        return True
    instance_host = ""
    if instance:
        try:
            instance_host = (urlparse(instance).hostname or "").lower()
        except Exception:
            pass
    title = str(result.get("title") or "").lower()
    source = str(result.get("source") or "").lower()
    # The provider's own pages are contamination even if they look like
    # ordinary GitHub/source-code results.
    if host in BACKEND_HOST_MARKERS:
        return True
    if instance_host and host == instance_host:
        return True
    if any(marker in path for marker in BACKEND_PATH_MARKERS):
        return True
    if host == "github.com" and any(path.startswith(marker) for marker in BACKEND_KNOWN_PATHS):
        return True
    if any(marker in title for marker in BACKEND_TITLE_MARKERS):
        return True
    if source in {"searx", "searxng"}:
        return True
    return False

def _filter_provider_results(results: list[dict], instance: str | None = None) -> tuple[list[dict], int]:
    clean = []
    rejected = 0
    for item in results or []:
        if _is_backend_result(item, instance):
            rejected += 1
            continue
        clean.append(item)
    return clean, rejected

def _instance_result_quality(results: list[dict], query: str, instance: str | None = None) -> float:
    if not results:
        return 0.0
    clean, rejected = _filter_provider_results(results, instance)
    if not clean:
        return 0.0
    contamination = rejected / max(1, len(results))
    ranked = sorted(
        (_query_similarity(query, item) for item in clean),
        reverse=True,
    )
    relevance = sum(ranked[:5]) / max(1, min(5, len(ranked)))
    return max(0.0, relevance * (1.0 - contamination))



# ==========================================================================
# ACCESS-CHALLENGE DETECTION
# ==========================================================================

def _detect_access_challenge(text: str = "", status: int | None = None, headers=None) -> dict:
    """Detect common access challenges without attempting to defeat them."""
    body = (text or "").lower()
    header_text = " ".join(f"{k}: {v}" for k, v in (headers or {}).items()).lower()
    haystack = body + "\n" + header_text
    for marker in CHALLENGE_MARKERS:
        if marker in haystack:
            return {"detected": True, "kind": marker, "reason": "access challenge detected"}
    if status in (403, 429) and any(x in body for x in ("challenge", "blocked", "automated", "security")):
        return {"detected": True, "kind": "http_access_challenge", "reason": f"HTTP {status} access challenge"}
    return {"detected": False, "kind": None, "reason": None}


def _quality_report(query: str, results: list[dict]) -> dict:
    clean, rejected = _filter_provider_results(results)
    if not clean:
        return {"quality": 0.0, "usable_results": 0, "rejected_provider_results": rejected, "unique_hosts": 0, "reason": "no usable public results"}
    sims = sorted((_query_similarity(query, r) for r in clean), reverse=True)
    relevance = sum(sims[:5]) / max(1, min(5, len(sims)))
    hosts = {urlparse(str(r.get("url") or "")).hostname for r in clean}
    hosts.discard(None)
    diversity = min(1.0, len(hosts) / 3.0)
    authoritative = sum(
        {"official_docs":1.0,"package_registry":0.95,"github":0.85,"docs":0.8,"community":0.65,"blog":0.55,"unknown":0.45}.get(
            r.get("source_type") or _evidence_source_type(r.get("url", ""), r.get("title", "")), 0.45
        ) for r in clean[:5]
    ) / min(5, len(clean))
    quality = relevance * 0.65 + diversity * 0.15 + authoritative * 0.20
    return {
        "quality": round(max(0.0, min(1.0, quality)), 4),
        "usable_results": len(clean),
        "rejected_provider_results": rejected,
        "unique_hosts": len(hosts),
        "reason": "ok" if quality >= MIN_SEARCH_QUALITY else "low relevance/authority/diversity",
    }


# ==========================================================================
# HTTP
# ==========================================================================



# --- User-facing implementation-name sanitization ---
import re as _re

_BACKEND_BRAND_RE = _re.compile(
    r"\b(?:" + "sear" + "xng|sear" + "x)\b",
    _re.IGNORECASE,
)

def sanitize_user_text(value):
    """Hide implementation-specific backend names from user-facing text."""
    if not isinstance(value, str):
        return value
    return _BACKEND_BRAND_RE.sub("search backend", value)

def sanitize_user_result(result):
    """Remove implementation branding from visible result fields."""
    if not isinstance(result, dict):
        return result
    result = dict(result)
    for key in (
        "title", "content", "snippet", "description",
        "message", "text", "engine", "source", "backend"
    ):
        if isinstance(result.get(key), str):
            result[key] = sanitize_user_text(result[key])
    return result

def sanitize_user_results(results):
    """Sanitize a list of result dictionaries before presentation."""
    return [sanitize_user_result(item) for item in (results or [])]
# --- End user-facing implementation-name sanitization ---

def _headers(referer: str | None = None) -> dict:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _normalise_base_url(url: str) -> str:
    return url.strip().rstrip("/")


# ==========================================================================
# INSTANCE STATE
# ==========================================================================

def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            raise ValueError("invalid state")

        state.setdefault("instances", {})
        state.setdefault("last_refresh", 0)
        return state
    except Exception:
        return {
            "instances": {},
            "last_refresh": 0,
        }


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(f".{os.getpid()}.tmp")
        with _STATE_LOCK:
            lock_file = open(STATE_LOCK_PATH, "a+", encoding="utf-8")
            try:
                try:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, STATE_PATH)
            finally:
                try:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                lock_file.close()
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _ensure_seeded(state: dict) -> dict:
    for url in BUILTIN_INSTANCES:
        state["instances"].setdefault(
            url,
            {
                "source": "builtin",
                "dead_until": 0,
                "last_ok": 0,
                "last_mode": None,
                "last_error": None,
                "success_count": 0,
                "failure_count": 0,
                "last_latency_ms": None,
                "avg_latency_ms": None,
            },
        )
    return state


def _refresh_from_searx_space(state: dict) -> dict:
    if not HAS_HTTPX:
        return state

    if os.environ.get("SEARXNG_REFRESH") == "0":
        return state

    now = time.time()

    if now - state.get("last_refresh", 0) < REFRESH_INTERVAL:
        return state

    try:
        with httpx.Client(timeout=8, follow_redirects=True) as client:
            response = client.get(
                "https://searx.space/data/instances.json",
                headers=_headers(),
            )
            response.raise_for_status()
            data = response.json()

        added = 0

        for raw_url, info in data.get("instances", {}).items():
            url = _normalise_base_url(raw_url)

            if not url.startswith(("http://", "https://")):
                continue

            http_info = info.get("http", {}) or {}
            grade = str(http_info.get("grade", "F")).upper()
            status_code = http_info.get("status_code")

            healthy = (
                status_code == 200
                and grade in {"A+", "A", "B"}
                and not info.get("comments")
            )

            if healthy and url not in state["instances"]:
                state["instances"][url] = {
                    "source": "discovered",
                    "dead_until": 0,
                    "last_ok": 0,
                    "last_mode": None,
                    "last_error": None,
                    "success_count": 0,
                    "failure_count": 0,
                    "last_latency_ms": None,
                    "avg_latency_ms": None,
                }
                added += 1

        state["last_refresh"] = now
        state["_last_refresh_added"] = added
        state.pop("_last_refresh_error", None)

    except Exception as exc:
        state["_last_refresh_error"] = str(exc)

    return state


def _mark_instance(
    state: dict,
    url: str,
    *,
    ok: bool,
    mode: str | None = None,
    latency_ms: float | None = None,
    error: str | None = None,
    hard_failure: bool = False,
) -> None:
    entry = state["instances"].setdefault(
        url,
        {
            "source": "discovered",
            "dead_until": 0,
            "last_ok": 0,
            "last_mode": None,
            "last_error": None,
            "success_count": 0,
            "failure_count": 0,
            "last_latency_ms": None,
            "avg_latency_ms": None,
        },
    )

    if ok:
        entry["dead_until"] = 0
        entry["last_ok"] = time.time()
        entry["last_error"] = None
        entry["success_count"] = int(
            entry.get("success_count", 0)
        ) + 1

        if mode:
            entry["last_mode"] = mode

        if latency_ms is not None:
            entry["last_latency_ms"] = round(latency_ms, 2)
            old_avg = entry.get("avg_latency_ms")

            if old_avg is None:
                entry["avg_latency_ms"] = round(
                    latency_ms,
                    2,
                )
            else:
                entry["avg_latency_ms"] = round(
                    old_avg * 0.8 + latency_ms * 0.2,
                    2,
                )
    else:
        entry["failure_count"] = int(
            entry.get("failure_count", 0)
        ) + 1
        entry["last_error"] = error

        if hard_failure:
            entry["dead_until"] = (
                time.time() + COOLDOWN_SECONDS
            )


def _instance_score(state: dict, url: str) -> float:
    info = state["instances"][url]

    successes = int(info.get("success_count", 0))
    failures = int(info.get("failure_count", 0))
    total = successes + failures

    reliability = (
        successes / total
        if total
        else 0.5
    )

    latency = info.get("avg_latency_ms")

    latency_bonus = (
        max(0.0, 1.0 - min(float(latency), 5000) / 5000)
        if latency is not None
        else 0.5
    )

    recency = float(info.get("last_ok", 0))

    return (
        reliability * 100
        + latency_bonus * 20
        + min(recency / 1_000_000, 10)
    )


def _instance_pool(
    state: dict,
    limit: int | None = None,
) -> list[str]:
    global INSTANCE_ROTATION_CURSOR
    now = time.time()

    pinned = _normalise_base_url(
        os.environ.get("SEARXNG_URL", "").strip()
    )

    alive = [
        url
        for url, info in state["instances"].items()
        if info.get("dead_until", 0) <= now
    ]

    if not alive:
        alive = list(BUILTIN_INSTANCES)

    # Keep healthy instances near the front, but rotate the starting point so
    # one fast instance cannot monopolise every query forever.
    alive.sort(
        key=lambda url: _instance_score(state, url),
        reverse=True,
    )

    if alive:
        offset = INSTANCE_ROTATION_CURSOR % len(alive)
        INSTANCE_ROTATION_CURSOR += 1
        alive = alive[offset:] + alive[:offset]

    pool = []
    if pinned:
        pool.append(pinned)
    pool.extend(url for url in alive if url != pinned)

    return pool[:limit] if limit else pool


# ==========================================================================
# QUERY PRESETS
# ==========================================================================

DORK_TEMPLATES = {
    "files": [
        'filetype:pdf "{query}"', 'filetype:docx "{query}"',
        'filetype:xlsx "{query}"', 'filetype:csv "{query}"',
        'filetype:pptx "{query}"', 'filetype:txt "{query}"',
        'filetype:md "{query}"',
    ],
    "tech": [
        '"{query}" "stack trace"', '"{query}" "exception"',
        '"{query}" "changelog"', '"{query}" "release notes"',
        '"{query}" "migration guide"', '"{query}" "API reference"',
        '"{query}" "SDK"', '"{query}" "CLI"',
        '"{query}" "webhook"', '"{query}" "REST API"',
        '"{query}" "GraphQL"', '"{query}" "Docker"',
        '"{query}" "Kubernetes"',
    ],
    "github": [
        'site:github.com "{query}"',
        'site:github.com "{query}" "README"',
        'site:github.com "{query}" "issues"',
        'site:github.com "{query}" "releases"',
        'site:github.com "{query}" language:python',
        'site:github.com "{query}" language:go',
        'site:github.com "{query}" language:javascript',
    ],
    "docs": [
        '"{query}" documentation', '"{query}" API reference',
        '"{query}" developer documentation', '"{query}" changelog',
        '"{query}" release notes', '"{query}" migration guide',
    ],
}



# --- Safe/public dork expansion (v4) ---
ADDITIONAL_DORK_TEMPLATES = {
    "github": [
        'site:github.com "{query}"',
        'site:github.com "{query}" "README"',
        'site:github.com "{query}" "releases"',
        'site:github.com "{query}" "issues"',
        'site:github.com "{query}" "pull request"',
        'site:github.com "{query}" "go.mod"',
        'site:github.com "{query}" "pyproject.toml"',
        'site:github.com "{query}" "package.json"',
        'site:github.com "{query}" "Cargo.toml"',
        'site:github.com "{query}" "Dockerfile"',
        'site:github.com "{query}" language:python',
        'site:github.com "{query}" language:go',
        'site:github.com "{query}" language:javascript',
        'site:github.com "{query}" language:typescript',
    ],
    "docs": [
        'site:readthedocs.io "{query}"',
        'site:docs.github.com "{query}"',
        'site:docs.python.org "{query}"',
        'site:go.dev "{query}"',
        'site:pkg.go.dev "{query}"',
        'site:docs.rs "{query}"',
        'site:learn.microsoft.com "{query}"',
        'site:developer.mozilla.org "{query}"',
        'intitle:documentation "{query}"',
        'intitle:reference "{query}"',
        'intitle:API "{query}"',
    ],
    "tech": [
        '"{query}" "stack trace"',
        '"{query}" "exception"',
        '"{query}" "error log"',
        '"{query}" "changelog"',
        '"{query}" "release notes"',
        '"{query}" "migration guide"',
        '"{query}" "configuration"',
        '"{query}" "API reference"',
        '"{query}" "SDK"',
        '"{query}" "CLI"',
        '"{query}" "webhook"',
        '"{query}" "REST API"',
        '"{query}" "GraphQL"',
        '"{query}" "Docker"',
        '"{query}" "Kubernetes"',
    ],
    "files": [
        'filetype:pdf "{query}"',
        'filetype:docx "{query}"',
        'filetype:xlsx "{query}"',
        'filetype:csv "{query}"',
        'filetype:pptx "{query}"',
        'filetype:txt "{query}"',
        'filetype:md "{query}"',
        'filetype:json "{query}"',
        'filetype:xml "{query}"',
        'filetype:yaml "{query}"',
        'filetype:yml "{query}"',
    ],
    "web": [
        '"{query}" "login"',
        '"{query}" "dashboard"',
        '"{query}" "portal"',
        '"{query}" "web app"',
        '"{query}" "REST"',
        '"{query}" "GraphQL"',
        '"{query}" "OpenAPI"',
        '"{query}" "Swagger UI"',
        '"{query}" "API documentation"',
        '"{query}" "webhook"',
    ],
    "legal": [
        '"{query}" "terms of service"',
        '"{query}" "privacy policy"',
        '"{query}" "acceptable use policy"',
        '"{query}" "data processing agreement"',
        '"{query}" "security policy"',
        '"{query}" "cookie policy"',
        '"{query}" "subprocessors"',
    ],
    "research": [
        '"{query}" paper',
        '"{query}" benchmark',
        '"{query}" dataset',
        '"{query}" "technical report"',
        '"{query}" "white paper"',
        '"{query}" "case study"',
        '"{query}" "research report"',
        '"{query}" "conference"',
    ],
    "jobs": [
        '"{query}" "job description"',
        '"{query}" "engineering team"',
        '"{query}" "developer"',
        '"{query}" "software engineer"',
        '"{query}" "technical stack"',
        '"{query}" "engineering blog"',
    ],
    "archives": [
        '"{query}" archive',
        '"{query}" changelog',
        '"{query}" "release history"',
        '"{query}" "version history"',
        '"{query}" "migration notes"',
        '"{query}" "deprecated"',
    ],
    "standards": [
        '"{query}" RFC',
        '"{query}" ISO',
        '"{query}" IETF',
        '"{query}" W3C',
        '"{query}" OWASP',
        '"{query}" NIST',
        '"{query}" specification',
        '"{query}" standard',
    ],
    "monitoring": [
        '"{query}" "status"',
        '"{query}" "status page"',
        '"{query}" "service health"',
        '"{query}" uptime',
        '"{query}" incident',
        '"{query}" maintenance',
        '"{query}" outage',
    ],
    "mobile": [
        '"{query}" Android',
        '"{query}" iOS',
        '"{query}" APK',
        '"{query}" application',
        '"{query}" mobile SDK',
        '"{query}" mobile API',
    ],
    "devops": [
        '"{query}" CI/CD',
        '"{query}" GitHub Actions',
        '"{query}" GitLab CI',
        '"{query}" Jenkins',
        '"{query}" Argo CD',
        '"{query}" Prometheus',
        '"{query}" Grafana',
        '"{query}" observability',
    ],
}



# Query variants let the planner obtain different result sets without blindly
# multiplying requests. The caller can cap the number of variants.
QUERY_VARIANT_SUFFIXES = (
    "",
    " documentation",
    " examples",
    " tutorial",
    " reference",
    " GitHub",
    " API",
    " implementation",
    " changelog",
)

def build_safe_query_variants(query, limit=5):
    """Build deterministic, low-noise query variants for public research."""
    q = " ".join(str(query).split()).strip()
    if not q:
        return []
    result = []
    seen = set()
    for suffix in QUERY_VARIANT_SUFFIXES:
        candidate = (q + suffix).strip()
        key = candidate.casefold()
        if key not in seen:
            seen.add(key)
            result.append(candidate)
        if len(result) >= max(1, int(limit)):
            break
    return result


def classify_result_source(url):
    """Return a lightweight public-source category from a URL."""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "web"
    if host.endswith("github.com") or host.endswith("githubusercontent.com"):
        return "github"
    if any(x in host for x in ("readthedocs.", "docs.", "developer.", "devdocs.")):
        return "docs"
    if any(x in host for x in ("pypi.org", "npmjs.com", "crates.io", "pkg.go.dev")):
        return "package"
    if any(x in host for x in ("arxiv.org", "doi.org")):
        return "research"
    if any(x in host for x in ("owasp.org", "nist.gov", "cve.org", "mitre.org")):
        return "security"
    return "web"


def result_quality_score(result):
    """Small deterministic quality boost used by callers that support scoring."""
    if not isinstance(result, dict):
        return 0.0
    score = 0.0
    title = str(result.get("title") or "")
    url = str(result.get("url") or "")
    content = str(result.get("content") or result.get("snippet") or "")
    if title:
        score += 0.15
    if url.startswith("https://"):
        score += 0.10
    if len(content) >= 120:
        score += 0.15
    if classify_result_source(url) in {"github", "docs", "package", "research", "security"}:
        score += 0.20
    return min(score, 1.0)


def diversify_results(results, max_per_host=4):
    """Keep useful source diversity while preserving input ranking."""
    from urllib.parse import urlparse
    counts = {}
    output = []
    for item in results or []:
        url = str(item.get("url") or "") if isinstance(item, dict) else ""
        host = (urlparse(url).hostname or "").lower()
        if host and counts.get(host, 0) >= max_per_host:
            continue
        if host:
            counts[host] = counts.get(host, 0) + 1
        output.append(item)
    return output


# --- End v5 additions ---




def detect_intent(query: str) -> str:
    q = query.lower()

    if any(
        token in q
        for token in (
            "github",
            "repository",
            "repo",
            "source code",
        )
    ):
        return "github"

    if any(
        token in q
        for token in (
            "documentation",
            "docs",
            "api reference",
            "developer guide",
            "sdk",
            "changelog",
        )
    ):
        return "documentation"

    if any(
        token in q
        for token in (
            "latest",
            "today",
            "news",
            "breaking",
            "this week",
        )
    ):
        return "news"

    if any(
        token in q
        for token in (
            "vulnerability",
            "security",
            "cve",
            "recon",
            "osint",
        )
    ):
        return "technical"

    if any(
        token in q
        for token in (
            "pdf",
            "download",
            "manual",
            "document",
        )
    ):
        return "files"

    return "general"


def _build_query_variants(
    query: str,
    *,
    dork_mode: str | None,
    domain: str | None,
    limit: int,
) -> list[str]:
    clean = query.strip()

    if domain:
        domain = re.sub(
            r"^https?://",
            "",
            domain.strip(),
            flags=re.IGNORECASE,
        )
        domain = domain.split("/", 1)[0]
        clean = f"site:{domain} {clean}"

    variants = [clean]

    if dork_mode:
        templates = DORK_TEMPLATES[dork_mode]

        for template in templates:
            if dork_mode == "recon" and domain:
                candidate = template.format(
                    query=domain
                )
            else:
                candidate = template.format(
                    query=clean
                )

            if candidate not in variants:
                variants.append(candidate)

            if len(variants) >= limit:
                break

    return variants[:limit]


# ==========================================================================
# URL NORMALIZATION
# ==========================================================================

def canonicalize_url(url: str) -> str:
    try:
        parsed = urlparse(url)

        scheme = parsed.scheme.lower()
        host = parsed.hostname.lower() if parsed.hostname else ""

        if not scheme or not host:
            return url

        if (
            (scheme == "http" and parsed.port == 80)
            or (scheme == "https" and parsed.port == 443)
        ):
            netloc = host
        else:
            netloc = host

            if parsed.port:
                netloc += f":{parsed.port}"

        filtered_query = []

        for key, values in parse_qs(
            parsed.query,
            keep_blank_values=True,
        ).items():
            if key.lower() in TRACKING_PARAMS:
                continue

            for value in values:
                filtered_query.append(
                    (key, value)
                )

        query = urlencode(
            sorted(filtered_query),
            doseq=True,
        )

        path = parsed.path or "/"

        if path != "/":
            path = path.rstrip("/")

        return urlunparse(
            (
                scheme,
                netloc,
                path,
                "",
                query,
                "",
            )
        )
    except Exception:
        return url


# ==========================================================================
# HTML / RESULT PARSING
# ==========================================================================

TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)


def clean_text(raw: str) -> str:
    if not raw:
        return ""

    text = TAG_RE.sub(" ", raw)
    text = unescape(text)
    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def _resolve_link(
    href: str,
    base_url: str,
) -> str | None:
    if not href:
        return None

    href = unescape(href.strip())

    if href.startswith(
        (
            "#",
            "javascript:",
            "data:",
            "mailto:",
            "tel:",
        )
    ):
        return None

    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)

    if parsed.scheme not in (
        "http",
        "https",
    ):
        return None

    qs = parse_qs(parsed.query)

    for key in (
        "url",
        "u",
        "redirect",
        "to",
        "link",
    ):
        if key in qs:
            candidate = unquote(qs[key][0])

            if candidate.startswith(
                (
                    "http://",
                    "https://",
                )
            ):
                return candidate

    return absolute


def _extract_date(text: str) -> str | None:
    patterns = [
        r"\b(\d{4}-\d{2}-\d{2})\b",
        r"\b(\d{2}/\d{2}/\d{4})\b",
        (
            r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{1,2},?\s+\d{4})\b"
        ),
        (
            r"\b(\d{1,2}\s+"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{4})\b"
        ),
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)

    return None


def _extract_searx_results(
    html: str,
    base_url: str,
    max_results: int,
) -> list[dict]:
    results = []
    seen = set()

    pattern = re.compile(
        r'<(article|div|li)[^>]*'
        r'class="[^"]*\bresult\b[^"]*"[^>]*>'
        r"(.*?)</\1>",
        re.DOTALL | re.IGNORECASE,
    )

    for match in pattern.finditer(html):
        block = match.group(2)

        link = re.search(
            r'<a[^>]+href="([^"]+)"[^>]*'
            r'(?:class="[^"]*(?:url_header|result|title)[^"]*"[^>]*)?>'
            r"(.*?)</a>",
            block,
            re.DOTALL | re.IGNORECASE,
        )

        if not link:
            link = re.search(
                r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                block,
                re.DOTALL | re.IGNORECASE,
            )

        if not link:
            continue

        href, title_html = link.groups()
        url = _resolve_link(href, base_url)
        title = clean_text(title_html)

        if not url or len(title) < 3:
            continue

        canonical = canonicalize_url(url)

        if canonical in seen:
            continue

        host = urlparse(canonical).netloc

        if host in SKIP_RESULT_HOSTS:
            continue

        snippet = ""

        for snippet_pattern in (
            r'<p[^>]*class="[^"]*\bcontent\b[^"]*"[^>]*>(.*?)</p>',
            r'<p[^>]*class="[^"]*\bsnippet\b[^"]*"[^>]*>(.*?)</p>',
            (
                r'<div[^>]*class="[^"]*'
                r'(?:content|snippet)[^"]*"[^>]*>(.*?)</div>'
            ),
        ):
            match_snippet = re.search(
                snippet_pattern,
                block,
                re.DOTALL | re.IGNORECASE,
            )

            if match_snippet:
                snippet = clean_text(
                    match_snippet.group(1)
                )
                if snippet:
                    break

        results.append(
            {
                "title": title,
                "url": canonical,
                "snippet": snippet[:1000],
                "date": _extract_date(block),
                "source": host,
                "engine": None,
                "category": None,
                "score": 0.0,
            }
        )

        seen.add(canonical)

        if len(results) >= max_results:
            break

    if not results:
        results = _extract_generic_links(
            html,
            base_url,
            max_results,
        )

    return sanitize_user_results(results)


def _extract_generic_links(
    html: str,
    base_url: str,
    max_results: int,
) -> list[dict]:
    results = []
    seen = set()

    links = re.findall(
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL | re.IGNORECASE,
    )

    for href, title_html in links:
        url = _resolve_link(
            href,
            base_url,
        )
        title = clean_text(title_html)

        if not url or len(title) < 10:
            continue

        canonical = canonicalize_url(url)

        if canonical in seen:
            continue

        if urlparse(canonical).netloc == urlparse(
            base_url
        ).netloc:
            continue

        results.append(
            {
                "title": title,
                "url": canonical,
                "snippet": "",
                "date": None,
                "source": urlparse(
                    canonical
                ).netloc,
                "engine": None,
                "category": None,
                "score": 0.0,
            }
        )

        seen.add(canonical)

        if len(results) >= max_results:
            break

    return sanitize_user_results(results)


# ==========================================================================
# SEARCH API
# ==========================================================================

def _csv(value: str | None) -> list[str]:
    if not value:
        return []

    return [
        x.strip()
        for x in value.split(",")
        if x.strip()
    ]


async def _search_instance(
    client: httpx.AsyncClient,
    instance: str,
    query: str,
    language: str,
    page: int,
    time_range: str | None,
    engines: str | None,
    categories: str | None,
    safesearch: int,
    max_results: int,
    request_budget: dict | None = None,
) -> dict:
    if request_budget is not None:
        if request_budget.get("remaining", 0) <= 0:
            return {"ok": False, "instance": instance, "error": "request_budget_exhausted", "hard_failure": False}
        request_budget["remaining"] -= 1
    started = time.monotonic()
    ok_instance, instance_reason = await _validate_external_url(f"{instance.rstrip('/')}/search")
    if not ok_instance:
        return {"ok": False, "instance": instance, "error": f"instance_rejected:{instance_reason}", "hard_failure": True}

    params = {
        "q": query,
        "format": "json",
        "language": language,
        "pageno": page,
        "safesearch": safesearch,
    }

    if time_range:
        params["time_range"] = time_range

    engine_values = _csv(engines)
    category_values = _csv(categories)

    if engine_values:
        params["engines"] = ",".join(
            engine_values
        )

    if category_values:
        params["categories"] = ",".join(
            category_values
        )

    try:
        response = await client.get(
            f"{instance}/search",
            params=params,
            headers=_headers(
                referer=instance
            ),
        )

        challenge = _detect_access_challenge(response.content[:20000].decode("utf-8", errors="ignore"), response.status_code, response.headers)
        if challenge["detected"]:
            return {"ok": False, "instance": instance, "error": "access_restricted", "access_restricted": True, "challenge": challenge, "hard_failure": True}

        if response.status_code == 403:
            return {"ok": False, "instance": instance, "error": "HTTP 403", "hard_failure": True}

        if response.status_code in RETRYABLE_STATUS:
            return {
                "ok": False,
                "instance": instance,
                "error": (
                    f"HTTP {response.status_code}"
                ),
                "hard_failure": True,
            }

        challenge = _detect_access_challenge(
            response.text[:20000], response.status_code, response.headers
        )
        if challenge["detected"]:
            return {
                "ok": False, "instance": instance,
                "error": "access_restricted",
                "access_restricted": True,
                "challenge": challenge,
                "hard_failure": True,
            }

        response.raise_for_status()

        try:
            data = response.json()
            results = []

            for item in data.get(
                "results",
                [],
            ):
                raw_url = item.get("url")

                if not raw_url:
                    continue

                url = canonicalize_url(
                    str(raw_url)
                )

                source_type = _evidence_source_type(url, str(item.get("title", "")))
                results.append(
                    {
                        "title": clean_text(
                            str(
                                item.get(
                                    "title",
                                    "",
                                )
                            )
                        ),
                        "url": url,
                        "snippet": clean_text(
                            str(
                                item.get(
                                    "content",
                                    "",
                                )
                            )
                        )[:1000],
                        "date": item.get(
                            "publishedDate"
                        ),
                        "source": urlparse(
                            url
                        ).netloc,
                        "engine": item.get(
                            "engine"
                        ),
                        "category": item.get(
                            "category"
                        ),
                        "source_type": source_type,
                        "independence_key": _independence_key(url, str(item.get("title", ""))),
                        "score": float(
                            item.get(
                                "score",
                                0,
                            )
                            or 0
                        ),
                    }
                )

            latency = (
                time.monotonic() - started
            ) * 1000

            clean_results, rejected = _filter_provider_results(
                results, instance
            )
            quality = _instance_result_quality(
                clean_results, query, instance
            )

            return {
                "ok": bool(clean_results),
                "instance": instance,
                "mode": "json",
                "results": clean_results[
                    :max_results
                ],
                "quality": round(quality, 4),
                "rejected_provider_results": rejected,
                "metadata": {
                    "suggestions": data.get(
                        "suggestions",
                        [],
                    ),
                    "answers": data.get(
                        "answers",
                        [],
                    ),
                    "corrections": data.get(
                        "corrections",
                        [],
                    ),
                    "unresponsive_engines": data.get(
                        "unresponsive_engines",
                        [],
                    ),
                },
                "latency_ms": latency,
                "error": (
                    None
                    if results
                    else (
                        "provider contamination"
                        if rejected and not clean_results
                        else "0 results"
                    )
                ),
            }

        except ValueError:
            # JSON unavailable/disabled: HTML fallback.
            response = await client.get(
                f"{instance}/search",
                params={
                    "q": query,
                    "pageno": page,
                    "safesearch": safesearch,
                },
                headers=_headers(
                    referer=instance
                ),
            )
            challenge = _detect_access_challenge(
                response.text[:20000], response.status_code, response.headers
            )
            if challenge["detected"]:
                return {
                    "ok": False, "instance": instance,
                    "error": "access_restricted",
                    "access_restricted": True,
                    "challenge": challenge,
                    "hard_failure": True,
                }
            response.raise_for_status()

            results = _extract_searx_results(
                response.text,
                instance,
                max_results,
            )
            for item in results:
                item["source_type"] = _evidence_source_type(item.get("url", ""), item.get("title", ""))
                item["independence_key"] = _independence_key(item.get("url", ""), item.get("title", ""))
            results, rejected = _filter_provider_results(
                results, instance
            )
            quality = _instance_result_quality(
                results, query, instance
            )

            latency = (
                time.monotonic() - started
            ) * 1000

            return {
                "ok": bool(results),
                "instance": instance,
                "mode": "html",
                "results": results,
                "quality": round(quality, 4),
                "rejected_provider_results": rejected,
                "metadata": {},
                "latency_ms": latency,
                "error": (
                    None
                    if results
                    else "0 results"
                ),
            }

    except httpx.TimeoutException:
        return {
            "ok": False,
            "instance": instance,
            "error": "timeout",
            "hard_failure": False,
        }

    except httpx.ConnectError:
        return {
            "ok": False,
            "instance": instance,
            "error": "unreachable",
            "hard_failure": True,
        }

    except Exception as exc:
        return {
            "ok": False,
            "instance": instance,
            "error": str(exc),
            "hard_failure": False,
        }


async def _refresh_state_async(state: dict) -> dict:
    return await asyncio.to_thread(_refresh_from_searx_space, state)


async def _query_page(
    client: httpx.AsyncClient,
    query: str,
    *,
    language: str,
    page: int,
    time_range: str | None,
    engines: str | None,
    categories: str | None,
    safesearch: int,
    max_results: int,
    fanout: int,
    request_budget: dict | None = None,
) -> dict:
    state = await _refresh_state_async(
        _ensure_seeded(_load_state())
    )

    instances = _instance_pool(
        state,
        limit=max(fanout, PARALLEL_FANOUT),
    )

    tried = []
    errors = []

    for offset in range(
        0,
        len(instances),
        fanout,
    ):
        batch = instances[
            offset: offset + fanout
        ]

        tried.extend(batch)

        outcomes = await asyncio.gather(
            *(
                _search_instance(
                    client,
                    instance,
                    query,
                    language,
                    page,
                    time_range,
                    engines,
                    categories,
                    safesearch,
                    max_results,
                    request_budget,
                )
                for instance in batch
            )
        )

        winner = None

        # A 200/JSON response is not enough. Prefer a result set that has
        # actual public pages and measurable query relevance.
        viable = []
        for outcome in outcomes:
            if outcome.get("ok") and outcome.get("results"):
                viable.append(outcome)
            else:
                _mark_instance(
                    state,
                    outcome["instance"],
                    ok=False,
                    error=outcome.get("error"),
                    hard_failure=outcome.get("hard_failure", False) or outcome.get("rejected_provider_results", 0) > 0,
                )
                errors.append(
                    f"{outcome['instance']}: "
                    f"{outcome.get('error', 'no usable results')}"
                )

        if viable:
            candidate = max(
                viable,
                key=lambda item: (
                    float(item.get("quality", 0.0)),
                    len(item.get("results", [])),
                    -float(item.get("latency_ms", 999999)),
                ),
            )
            # Do not call a transport-successful but irrelevant result set a
            # successful search. Continue with another live instance batch.
            if float(candidate.get("quality", 0.0)) >= MIN_SEARCH_QUALITY:
                winner = candidate

        for outcome in outcomes:
            if outcome is winner:
                continue
            if outcome.get("ok") and outcome.get("results"):
                # A non-winning but valid instance is still healthy.
                _mark_instance(
                    state,
                    outcome["instance"],
                    ok=True,
                    mode=outcome.get("mode"),
                    latency_ms=outcome.get("latency_ms"),
                )
            else:
                _mark_instance(
                    state,
                    outcome["instance"],
                    ok=False,
                    error=outcome.get("error"),
                    hard_failure=outcome.get("hard_failure", False),
                )
                errors.append(
                    f"{outcome['instance']}: "
                    f"{outcome.get('error', 'error')}"
                )

        if winner:
            _mark_instance(
                state,
                winner["instance"],
                ok=True,
                mode=winner.get("mode"),
                latency_ms=winner.get(
                    "latency_ms"
                ),
            )

            _save_state(state)

            LAST_SUCCESS.update(
                {
                    "instance": winner[
                        "instance"
                    ],
                    "mode": winner.get(
                        "mode"
                    ),
                    "latency_ms": round(
                        winner.get(
                            "latency_ms",
                            0,
                        ),
                        2,
                    ),
                    "at": time.time(),
                }
            )

            return {
                "results": winner.get(
                    "results",
                    [],
                ),
                "metadata": winner.get(
                    "metadata",
                    {},
                ),
                "instance_used": winner[
                    "instance"
                ],
                "mode": winner.get(
                    "mode"
                ),
                "tried": tried,
                "errors": errors,
            }

    _save_state(state)

    return {
        "results": [],
        "metadata": {},
        "instance_used": None,
        "mode": None,
        "tried": tried,
        "errors": errors,
    }


# ==========================================================================
# RELEVANCE / RANKING
# ==========================================================================

def _tokens(text: str) -> list[str]:
    return re.findall(
        r"[a-zA-Z0-9_а-яА-ЯёЁ-]{2,}",
        text.lower(),
    )


def _query_similarity(query: str, result: dict) -> float:
    query_tokens = set(_tokens(query))
    if not query_tokens:
        return 0.0
    title_tokens = set(_tokens(result.get("title", "")))
    snippet_tokens = set(_tokens(result.get("snippet", "")))
    url_tokens = set(_tokens(result.get("url", "")))
    title_match = len(query_tokens & title_tokens) / len(query_tokens)
    snippet_match = len(query_tokens & snippet_tokens) / len(query_tokens)
    url_match = len(query_tokens & url_tokens) / len(query_tokens)
    haystack = (result.get("title", "") + " " + result.get("snippet", "")).lower()
    exact_phrase = 1.0 if query.strip().lower() in haystack else 0.0
    # Normalized lexical relevance: title and phrase matter more than URL.
    return max(0.0, min(1.0,
        title_match * 0.45 +
        snippet_match * 0.30 +
        url_match * 0.10 +
        exact_phrase * 0.15
    ))


def _domain_quality(
    url: str,
) -> float:
    host = urlparse(url).netloc.lower()

    if host.startswith("www."):
        host = host[4:]

    if host.endswith(
        (
            ".gov",
            ".gov.uk",
            ".edu",
        )
    ):
        return 1.0

    if host in {
        "github.com",
        "docs.python.org",
        "developer.mozilla.org",
        "gitlab.com",
        "wikipedia.org",
    }:
        return 0.9

    return 0.0


def _freshness_score(date_value, *, required: bool = True) -> float:
    if not date_value:
        return 0.0
    text = str(date_value)
    parsed = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    if parsed is None:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
    if parsed is None:
        return 0.0
    age_days = max(0.0, (datetime.utcnow() - parsed).total_seconds() / 86400)
    # Freshness is deliberately weak for evergreen searches.
    half_life = 90.0 if required else 730.0
    return max(0.0, min(1.0, 0.5 ** (age_days / half_life)))

def _result_score(query: str, result: dict) -> float:
    relevance = _query_similarity(query, result)
    source_type = result.get("source_type") or _evidence_source_type(result.get("url", ""), result.get("title", ""))
    authority = {
        "official_docs": 1.00, "package_registry": 0.95, "github": 0.85,
        "docs": 0.80, "community": 0.65, "blog": 0.55, "unknown": 0.45,
    }.get(source_type, 0.45)
    freshness = _freshness_score(result.get("date"), required=_query_requires_freshness(query))
    backend = _normalise_backend_score(result.get("score", 0))
    snippet = 1.0 if result.get("snippet") else 0.0
    return (
        relevance * 0.52 +
        authority * 0.18 +
        freshness * 0.12 +
        backend * 0.10 +
        snippet * 0.08
    )


def _build_evidence_summary(query: str, results: list[dict]) -> dict:
    clusters = {}
    hosts = set()
    source_types = Counter()
    for r in results[:20]:
        url = str(r.get("url") or "")
        host = (urlparse(url).hostname or "").lower()
        if host:
            hosts.add(host)
        st = r.get("source_type") or _evidence_source_type(url, r.get("title", ""))
        source_types[st] += 1
        key = re.sub(r"\W+", " ", str(r.get("title") or "").lower()).strip()
        if key:
            clusters.setdefault(key, []).append(r)

    duplicate_clusters = [
        {"title_key": key, "sources": len(items), "hosts": sorted({(urlparse(str(x.get("url") or "")).hostname or "").lower() for x in items})}
        for key, items in clusters.items() if len(items) > 1
    ]

    # Potential version/date disagreement, not a factual conflict claim.
    version_map = {}
    for r in results[:20]:
        text = f"{r.get('title','')} {r.get('snippet','')}"
        versions = re.findall(r"\bv?\d+(?:\.\d+){1,3}\b", text, flags=re.I)
        for version in versions[:4]:
            version_map.setdefault(version.lower(), []).append(r.get("url"))
    return {
        "independent_hosts": len(hosts),
        "source_types": dict(source_types),
        "duplicate_title_clusters": duplicate_clusters[:10],
        "potential_version_claims": {k: list(dict.fromkeys(v))[:5] for k, v in version_map.items() if len(v) >= 2},
        "confidence_note": "Evidence is source-based; version groups are potential disagreements requiring source inspection.",
    }


def _merge_and_rank(
    query: str,
    results: list[dict],
    max_results: int,
) -> list[dict]:
    merged = {}

    for result in results:
        url = canonicalize_url(
            result.get(
                "url",
                "",
            )
        )

        if not url:
            continue

        if url not in merged:
            merged[url] = dict(result)
            merged[url]["url"] = url
            continue

        current = merged[url]

        for field in (
            "title",
            "snippet",
            "date",
            "source",
            "engine",
            "category",
            "source_type",
            "independence_key",
        ):
            if not current.get(field):
                current[field] = result.get(
                    field
                )

        current["score"] = max(
            float(
                current.get(
                    "score",
                    0,
                )
                or 0
            ),
            float(
                result.get(
                    "score",
                    0,
                )
                or 0
            ),
        )

    ranked = []

    for result in merged.values():
        result["_rank"] = _result_score(
            query,
            result,
        )
        ranked.append(result)

    ranked.sort(
        key=lambda item: item["_rank"],
        reverse=True,
    )

    # Source diversification:
    # don't let the first page become five copies of one domain.
    selected = []
    source_counts = Counter()

    for result in ranked:
        source = result.get(
            "source"
        ) or urlparse(
            result["url"]
        ).netloc

        if (
            source_counts[source] >= 2
            and len(selected) < max_results - 2
        ):
            continue

        selected.append(result)
        source_counts[source] += 1

        if len(selected) >= max_results:
            break

    for result in selected:
        result.pop("_rank", None)
        result.setdefault("source_type", _evidence_source_type(result.get("url", ""), result.get("title", "")))
        result.setdefault("independence_key", _independence_key(result.get("url", ""), result.get("title", "")))

    return selected


# ==========================================================================
# CACHE
# ==========================================================================

def _cache_key(payload: dict) -> str:
    payload = dict(payload)
    payload["_cache_schema"] = CACHE_SCHEMA_VERSION
    payload["_ranking_version"] = RANKING_VERSION
    raw = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_PATH / f"{key}.json"


def _cache_get(
    key: str,
    ttl: int,
):
    path = _cache_path(key)

    try:
        if (
            time.time()
            - path.stat().st_mtime
            > ttl
        ):
            return None

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)
    except Exception:
        return None


def _cache_set(
    key: str,
    data: dict,
) -> None:
    try:
        CACHE_PATH.mkdir(
            parents=True,
            exist_ok=True,
        )

        path = _cache_path(key)
        tmp = path.with_suffix(".tmp")

        with open(
            tmp,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
            )

        os.replace(
            tmp,
            path,
        )
    except Exception:
        pass


# ==========================================================================
# MAIN SEARCH
# ==========================================================================

async def _search_async(
    query: str,
    *,
    max_results: int,
    language: str,
    dork_mode: str | None,
    time_range: str | None,
    pages: int,
    engines: str | None,
    categories: str | None,
    domain: str | None,
    safesearch: int,
    profile: str,
) -> dict:
    intent = detect_intent(query)

    profile_cfg = PROFILE_CONFIG[
        profile
    ]

    pages = min(
        max(1, int(pages)),
        profile_cfg["pages"],
        MAX_PAGES,
    )

    variants = _build_query_variants(
        query,
        dork_mode=dork_mode,
        domain=domain,
        limit=profile_cfg["variants"],
    )

    cache_payload = {
        "query": query,
        "language": language,
        "dork_mode": dork_mode,
        "time_range": time_range,
        "pages": pages,
        "engines": engines,
        "categories": categories,
        "domain": domain,
        "safesearch": safesearch,
        "profile": profile,
    }

    cache_key = _cache_key(
        cache_payload
    )

    cached = _cache_get(
        cache_key,
        CACHE_TTLS[profile],
    )

    if cached and float(cached.get("quality", 0.0)) >= MIN_SEARCH_QUALITY and cached.get("results"):
        cached.setdefault(
            "cache",
            {},
        )["hit"] = True
        return cached

    started = time.monotonic()
    request_budget = {"remaining": MAX_SEARCH_REQUESTS.get(profile, 16)}

    all_results = []
    tried = []
    errors = []
    access_restricted_instances = []

    instance_used = None
    mode_used = None

    metadata = {
        "suggestions": [],
        "answers": [],
        "corrections": [],
        "unresponsive_engines": [],
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=16,
            max_keepalive_connections=8,
        ),
    ) as client:
        for current_query in variants:
            if request_budget["remaining"] <= 0:
                errors.append("request_budget_exhausted")
                break
            for page in range(
                1,
                pages + 1,
            ):
                if request_budget["remaining"] <= 0:
                    errors.append("request_budget_exhausted")
                    break
                if len(all_results) >= max_results * 3:
                    break

                outcome = await _query_page(
                    client,
                    current_query,
                    language=language,
                    page=page,
                    time_range=time_range,
                    engines=engines,
                    categories=categories,
                    safesearch=safesearch,
                    max_results=max_results,
                    fanout=profile_cfg["fanout"],
                    request_budget=request_budget,
                )

                all_results.extend(
                    outcome.get(
                        "results",
                        [],
                    )
                )

                tried.extend(
                    outcome.get(
                        "tried",
                        [],
                    )
                )

                errors.extend(
                    outcome.get(
                        "errors",
                        [],
                    )
                )
                for err in outcome.get("errors", []):
                    if "access_restricted" in str(err):
                        access_restricted_instances.append(str(err).split(":", 1)[0])

                if not instance_used:
                    instance_used = outcome.get(
                        "instance_used"
                    )
                    mode_used = outcome.get(
                        "mode"
                    )

                page_metadata = outcome.get("metadata", {})
                if len(all_results) >= max_results * 2:
                    quick_quality = _quality_report(query, all_results)
                    if quick_quality["quality"] >= 0.72:
                        break

                for key in metadata:
                    values = page_metadata.get(
                        key,
                        [],
                    )

                    if isinstance(
                        values,
                        list,
                    ):
                        metadata[key].extend(
                            values
                        )

    # Final provider-contamination filter and ranking. Never expose backend
    # pages as successful search results.
    all_results, rejected_final = _filter_provider_results(
        all_results, instance_used
    )
    final_results = _merge_and_rank(
        query,
        all_results,
        max_results,
    )
    final_results = diversify_results(
        final_results,
        max_per_host=4,
    )[:max_results]

    quality_report = _quality_report(query, final_results)
    evidence = _build_evidence_summary(query, final_results)
    usable_quality = float(quality_report["quality"])

    search_status = "ok" if final_results and usable_quality >= MIN_SEARCH_QUALITY else "degraded"
    if not final_results:
        search_status = "access_restricted" if access_restricted_instances and not all_results else ("degraded" if rejected_final or all_results else "failed")
    elif usable_quality < MIN_SEARCH_QUALITY:
        search_status = "low_quality"

    duration_ms = (
        time.monotonic()
        - started
    ) * 1000

    result = {
        "query": query,
        "intent": intent,
        "profile": profile,
        "queries_tried": variants,
        "time_range": time_range,
        "pages": pages,
        "engines": _csv(engines),
        "categories": _csv(categories),
        "domain": domain,
        "safesearch": safesearch,
        "instance_used": instance_used,
        "mode": mode_used,
        "instances_tried": list(
            dict.fromkeys(tried)
        ),
        "results": final_results,
        "metadata": metadata,
        "errors": list(
            dict.fromkeys(errors)
        )[:10],
        "search_status": search_status,
        "transport_status": "ok" if tried else "failed",
        "quality": round(usable_quality, 4),
        "rejected_provider_results": rejected_final,
        "usable_results": quality_report["usable_results"],
        "unique_result_hosts": quality_report["unique_hosts"],
        "quality_reason": quality_report["reason"],
        "evidence": evidence,
        "access_restricted_instances": list(dict.fromkeys(access_restricted_instances)),
        "performance": {
            "duration_ms": round(
                duration_ms,
                2,
            ),
            "raw_results": len(
                all_results
            ),
            "final_results": len(final_results),
            "search_request_budget_remaining": request_budget["remaining"],
        },
        "cache": {
            "hit": False,
        },
    }

    # Don't cache empty searches.
    if final_results:
        _cache_set(
            cache_key,
            result,
        )

    return result


# ==========================================================================
# PAGE EXTRACTION
# ==========================================================================

def _meta_content(
    html: str,
    name: str,
) -> str | None:
    patterns = [
        rf'<meta[^>]+name=["\']{re.escape(name)}'
        rf'["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+property=["\']{re.escape(name)}'
        rf'["\'][^>]+content=["\']([^"\']*)["\']',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            html,
            re.IGNORECASE,
        )
        if match:
            return clean_text(
                match.group(1)
            )

    return None


def _extract_main_text(
    html: str,
) -> str:
    working = html

    # Remove non-content blocks.
    working = re.sub(
        r"<(script|style|noscript|svg|canvas|template)"
        r"\b[^>]*>.*?</\1>",
        " ",
        working,
        flags=re.DOTALL | re.IGNORECASE,
    )

    working = re.sub(
        r"<(nav|footer|header|aside)\b[^>]*>.*?</\1>",
        " ",
        working,
        flags=re.DOTALL | re.IGNORECASE,
    )

    candidates = []

    for tag in (
        "article",
        "main",
        "body",
    ):
        matches = re.findall(
            rf"<{tag}\b[^>]*>(.*?)</{tag}>",
            working,
            flags=re.DOTALL | re.IGNORECASE,
        )

        for match in matches:
            text = clean_text(match)

            if len(text) > 100:
                candidates.append(text)

    if not candidates:
        return clean_text(
            working
        )

    return max(
        candidates,
        key=len,
    )


def _extract_links(
    html: str,
    base_url: str,
    limit: int = 100,
) -> list[dict]:
    links = []
    seen = set()

    for href, text_html in re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>'
        r"(.*?)</a>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        url = _resolve_link(
            href,
            base_url,
        )

        if not url:
            continue

        canonical = canonicalize_url(
            url
        )

        if canonical in seen:
            continue

        text = clean_text(
            text_html
        )

        if not text:
            continue

        links.append(
            {
                "text": text[:300],
                "url": canonical,
            }
        )

        seen.add(canonical)

        if len(links) >= limit:
            break

    return links


async def _fetch_one(
    client: httpx.AsyncClient,
    url: str,
    max_chars: int,
) -> dict:
    started = time.monotonic()

    ok_url, reason = await _validate_external_url(url)
    if not ok_url:
        return {"ok": False, "url": url, "error": f"url_rejected:{reason}"}

    current_url = url
    response = None
    try:
        for _redirect_index in range(MAX_REDIRECTS_SAFE + 1):
            ok_url, reason = await _validate_external_url(current_url)
            if not ok_url:
                return {"ok": False, "url": url, "error": f"redirect_rejected:{reason}"}
            response = await client.get(current_url, headers=_headers(), follow_redirects=False)
            challenge = _detect_access_challenge(response.content[:20000].decode("utf-8", errors="ignore"), response.status_code, response.headers)
            if challenge["detected"]:
                return {"ok": False, "url": url, "final_url": str(response.url), "status_code": response.status_code, "error": "access_restricted", "access_restricted": True, "challenge": challenge}
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    return {"ok": False, "url": url, "status_code": response.status_code, "error": "redirect_without_location"}
                current_url = urljoin(current_url, location)
                continue
            break
        else:
            return {"ok": False, "url": url, "error": "too_many_redirects"}

        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        kind = _content_type_kind(content_type)
        try:
            declared_length = int(response.headers.get("content-length", "0") or 0)
        except (TypeError, ValueError):
            declared_length = 0
        if declared_length > MAX_RESPONSE_BYTES:
            return {"ok": False, "url": url, "final_url": str(response.url), "status_code": response.status_code, "content_type": content_type, "error": "response_too_large"}
        raw = response.content
        if len(raw) > MAX_RESPONSE_BYTES:
            return {"ok": False, "url": url, "final_url": str(response.url), "status_code": response.status_code, "content_type": content_type, "error": "response_too_large"}
        if kind not in {"html", "text", "json"}:
            return {"ok": False, "url": url, "final_url": str(response.url), "status_code": response.status_code, "content_type": content_type, "error": f"unsupported_content_type:{kind}"}
        html = raw.decode(response.encoding or "utf-8", errors="replace")

        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )

        title = clean_text(title_match.group(1)) if title_match else None
        description = _meta_content(html, "description") or _meta_content(html, "og:description")
        author = _meta_content(html, "author") or _meta_content(html, "article:author")
        published = _meta_content(html, "article:published_time") or _meta_content(html, "datePublished") or _extract_date(html)
        canonical_match = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', html, flags=re.I)
        canonical_url = urljoin(str(response.url), canonical_match.group(1)) if canonical_match else str(response.url)
        text = _extract_main_text(html)[:min(max_chars, MAX_TEXT_CHARS_SAFE)]
        links = _extract_links(html, str(response.url))
        duration_ms = (time.monotonic() - started) * 1000
        return {"ok": True, "url": url, "final_url": str(response.url), "canonical_url": canonical_url, "status_code": response.status_code, "content_type": content_type, "title": title, "description": description, "author": author, "published": published, "text": text, "links": links, "duration_ms": round(duration_ms, 2)}

    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc),
        }


# ==========================================================================
# TOOLS
# ==========================================================================

@tool(
    "web_search",
    (
        "Search the public web. Supports profiles, "
        "freshness filters, pagination, engines, categories, "
        "domain scoping, query presets and result ranking."
    ),
    [
        Param(
            "query",
            "string",
            "Search query",
            required=True,
        ),
        Param(
            "max_results",
            "integer",
            "Maximum unique results",
            default=5,
        ),
        Param(
            "language",
            "string",
            "Language code or auto",
            default="auto",
        ),
        Param(
            "profile",
            "string",
            "Search profile: fast, balanced, deep",
            default="balanced",
        ),
        Param(
            "dork_mode",
            "string",
            (
                "Preset: files, tech, github, docs"
            ),
            default=None,
        ),
        Param(
            "time_range",
            "string",
            "Freshness: day, week, month, year, or null",
            default=None,
        ),
        Param(
            "pages",
            "integer",
            "Requested pages, capped by profile",
            default=2,
        ),
        Param(
            "engines",
            "string",
            "Comma-separated search engines",
            default=None,
        ),
        Param(
            "categories",
            "string",
            "Comma-separated SearXNG categories",
            default=None,
        ),
        Param(
            "domain",
            "string",
            "Optional domain scope",
            default=None,
        ),
        Param(
            "safesearch",
            "integer",
            "Safe search: 0, 1 or 2",
            default=0,
        ),
    ],
    tags=["search"],
)
async def web_search(
    query: str,
    max_results: int = 5,
    language: str = "auto",
    profile: str = "balanced",
    dork_mode: str | None = None,
    time_range: str | None = None,
    pages: int = 2,
    engines: str | None = None,
    categories: str | None = None,
    domain: str | None = None,
    safesearch: int = 0,
) -> ToolResult:
    if not HAS_HTTPX:
        raise ToolError(
            "httpx not installed. "
            "pip install httpx"
        )

    query = query.strip()

    if not query:
        raise ToolError(
            "query is required"
        )

    if profile not in PROFILE_CONFIG:
        raise ToolError(
            "profile must be fast, balanced or deep"
        )

    if dork_mode in {"admin", "sensitive", "osint", "recon"}:
        raise ToolError("unsafe dork_mode disabled; use files, tech, github or docs")

    if dork_mode is not None:
        if dork_mode not in DORK_TEMPLATES:
            raise ToolError(
                "unknown dork_mode: "
                + ", ".join(
                    sorted(
                        DORK_TEMPLATES
                    )
                )
            )

    if time_range not in {
        None,
        "day",
        "week",
        "month",
        "year",
    }:
        raise ToolError(
            "invalid time_range"
        )

    safesearch = int(safesearch)

    if safesearch not in (
        0,
        1,
        2,
    ):
        raise ToolError(
            "safesearch must be 0, 1 or 2"
        )

    max_results = max(
        1,
        min(
            int(max_results),
            MAX_RESULTS,
        ),
    )

    outcome = await _search_async(
        query,
        max_results=max_results,
        language=language,
        dork_mode=dork_mode,
        time_range=time_range,
        pages=pages,
        engines=engines,
        categories=categories,
        domain=domain,
        safesearch=safesearch,
        profile=profile,
    )

    if not outcome["results"]:
        return ToolResult.failure(
            (
                "search failed: no usable public results; tried "
                f"{len(outcome['instances_tried'])} instance(s)"
            ),
            tool="web_search",
            data=outcome,
        )

    lines = [
        (
            f"Found {len(outcome['results'])} results "
            f"(status={outcome.get('search_status', 'unknown')}, "
            f"quality={outcome.get('quality', 0):.2f}, "
            f"intent={outcome['intent']}, profile={profile}):"
        )
    ]

    for index, result in enumerate(
        outcome["results"],
        1,
    ):
        meta = []

        if result.get("date"):
            meta.append(
                str(result["date"])
            )

        if result.get("engine"):
            meta.append(
                str(result["engine"])
            )

        score = result.get("score")

        if isinstance(
            score,
            (int, float),
        ) and score:
            meta.append(
                f"score={score:.2f}"
            )

        suffix = (
            " [" + ", ".join(meta) + "]"
            if meta
            else ""
        )

        lines.append(
            f"{index}. "
            f"{result.get('title', '')}"
            f"{suffix}"
        )
        lines.append(
            f"   URL: {result['url']}"
        )

        if result.get("snippet"):
            lines.append(
                "   "
                + re.sub(
                    r"\s+",
                    " ",
                    result["snippet"],
                )[:300]
            )

        lines.append("")

    if outcome.get("cache", {}).get(
        "hit"
    ):
        lines.append(
            "Cache: hit"
        )

    return ToolResult.success(
        data=outcome,
        summary="\n".join(lines),
        tool="web_search",
    )


@tool(
    "fetch_page",
    (
        "Fetch and extract a web page without "
        "JavaScript rendering. Returns cleaned main text, "
        "metadata and links."
    ),
    [
        Param(
            "url",
            "string",
            "URL to fetch",
            required=True,
        ),
        Param(
            "max_chars",
            "integer",
            "Maximum extracted text",
            default=10000,
        ),
    ],
    tags=["search"],
)
async def fetch_page(
    url: str,
    max_chars: int = 10000,
) -> ToolResult:
    if not HAS_HTTPX:
        raise ToolError(
            "httpx not installed. "
            "pip install httpx"
        )

    max_chars = max(
        1000,
        min(
            int(max_chars),
            100_000,
        ),
    )

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=False,
    ) as client:
        result = await _fetch_one(
            client,
            url,
            max_chars,
        )

    if not result["ok"]:
        raise ToolError(
            result.get(
                "error",
                "fetch failed",
            )
        )

    return ToolResult.success(
        data=result,
        summary=(
            f"fetched {result['final_url']} "
            f"({len(result['text'])} chars)"
        ),
        tool="fetch_page",
    )


@tool(
    "fetch_pages",
    (
        "Fetch multiple web pages concurrently and "
        "extract their main text and metadata."
    ),
    [
        Param(
            "urls",
            "string",
            "Comma-separated URLs",
            required=True,
        ),
        Param(
            "max_chars",
            "integer",
            "Maximum extracted text per page",
            default=8000,
        ),
    ],
    tags=["search"],
)
async def fetch_pages(
    urls: str,
    max_chars: int = 8000,
) -> ToolResult:
    if not HAS_HTTPX:
        raise ToolError(
            "httpx not installed. "
            "pip install httpx"
        )

    url_list = [
        item.strip()
        for item in urls.split(",")
        if item.strip()
    ]

    if not url_list:
        raise ToolError(
            "urls is required"
        )

    url_list = list(
        dict.fromkeys(
            url_list[:20]
        )
    )

    max_chars = max(
        1000,
        min(
            int(max_chars),
            50_000,
        ),
    )

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=8,
            max_keepalive_connections=4,
        ),
    ) as client:
        results = await asyncio.gather(
            *(
                _fetch_one(
                    client,
                    url,
                    max_chars,
                )
                for url in url_list
            )
        )

    success_count = sum(
        1
        for result in results
        if result.get("ok")
    )

    return ToolResult.success(
        data={
            "results": results,
            "total": len(results),
            "successful": success_count,
        },
        summary=(
            f"fetched {success_count}/"
            f"{len(results)} pages"
        ),
        tool="fetch_pages",
    )


@tool(
    "search_status",
    "Show SearXNG health, cache and search subsystem status.",
    [],
    tags=["search"],
)
def search_status() -> ToolResult:
    state = _ensure_seeded(
        _load_state()
    )

    now = time.time()
    instances = []

    for url, info in sorted(
        state["instances"].items()
    ):
        dead_until = float(
            info.get(
                "dead_until",
                0,
            )
        )

        instances.append(
            {
                "url": url,
                "source": info.get(
                    "source",
                    "unknown",
                ),
                "status": (
                    "cooling_down"
                    if dead_until > now
                    else "available"
                ),
                "cooldown_remaining_s": (
                    max(
                        0,
                        int(
                            dead_until - now
                        ),
                    )
                    if dead_until > now
                    else 0
                ),
                "success_count": int(
                    info.get(
                        "success_count",
                        0,
                    )
                ),
                "failure_count": int(
                    info.get(
                        "failure_count",
                        0,
                    )
                ),
                "avg_latency_ms": info.get(
                    "avg_latency_ms"
                ),
                "last_mode": info.get(
                    "last_mode"
                ),
                "last_error": info.get(
                    "last_error"
                ),
            }
        )

    available = sum(
        item["status"] == "available"
        for item in instances
    )

    try:
        cache_files = len(
            list(
                CACHE_PATH.glob(
                    "*.json"
                )
            )
        )
    except Exception:
        cache_files = 0

    summary = {
        "total_instances": len(
            instances
        ),
        "available": available,
        "cooling_down": (
            len(instances) - available
        ),
        "cache_entries": cache_files,
        "profiles": list(
            PROFILE_CONFIG
        ),
        "dork_modes": sorted(
            DORK_TEMPLATES
        ),
        "last_success": (
            LAST_SUCCESS or None
        ),
        "last_refresh": state.get(
            "last_refresh"
        ),
        "refresh_added": state.get(
            "_last_refresh_added",
            0,
        ),
        "refresh_error": state.get(
            "_last_refresh_error"
        ),
    }

    return ToolResult.success(
        data={
            "summary": summary,
            "instances": instances,
        },
        summary=(
            f"{available}/"
            f"{len(instances)} instances available; "
            f"{cache_files} cached searches"
        ),
        tool="search_status",
    )


WEB_SEARCH_TOOLS: list[Tool] = [
    web_search,
    fetch_page,
    fetch_pages,
    search_status,
]


# ==========================================================================
# DIAGNOSTICS
# ==========================================================================

if __name__ == "__main__":
    print(
        "XLI web_search_enhanced v9"
    )
    print(
        "HTTPX:",
        "available"
        if HAS_HTTPX
        else "missing",
    )
    print(
        "Instances:",
        len(BUILTIN_INSTANCES),
    )
    print(
        "Profiles:",
        ", ".join(PROFILE_CONFIG),
    )
    print(
        "Dork modes:",
        ", ".join(
            sorted(DORK_TEMPLATES)
        ),
    )

    # Important: this diagnostic uses the variable in THIS module.
    # It does not rely on a notebook's namespace, fixing the original
    # NameError from the generator script.



# --- v7 resilient public-page fetching ---
import asyncio as _asyncio
import time as _time
from urllib.parse import urlparse as _urlparse

_FETCH_POLICY = {
    "normal":  {"retries": 2, "concurrency": 6,  "max_bytes": 2_500_000},
    "careful": {"retries": 3, "concurrency": 3,  "max_bytes": 2_000_000},
    "deep":    {"retries": 4, "concurrency": 8,  "max_bytes": 5_000_000},
}

_DOMAIN_BACKOFF = {}
_DOMAIN_LAST_REQUEST = {}
_DOMAIN_MIN_INTERVAL = 0.35
_MAX_RETRY_AFTER = 30.0


def _fetch_policy(mode="normal"):
    return _FETCH_POLICY.get(str(mode).lower(), _FETCH_POLICY["normal"])


def _domain_key(url):
    try:
        return (_urlparse(url).hostname or "").lower()
    except Exception:
        return ""


async def _respect_domain_rate_limit(url):
    host = _domain_key(url)
    if not host:
        return
    now = _time.monotonic()
    last = _DOMAIN_LAST_REQUEST.get(host, 0.0)
    wait = _DOMAIN_MIN_INTERVAL - (now - last)
    if wait > 0:
        await _asyncio.sleep(wait)
    _DOMAIN_LAST_REQUEST[host] = _time.monotonic()


def _retry_delay(attempt, retry_after=None):
    if retry_after is not None:
        try:
            return min(float(retry_after), _MAX_RETRY_AFTER)
        except (TypeError, ValueError):
            pass
    # Exponential backoff with a small deterministic jitter component.
    return min(0.6 * (2 ** attempt), _MAX_RETRY_AFTER)


def _retryable_status(status):
    return status in {408, 425, 429, 500, 502, 503, 504}


def _extract_retry_after(headers):
    if not headers:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_response_headers(response):
    """Expose only non-sensitive response metadata."""
    headers = getattr(response, "headers", {}) or {}
    result = {}
    for key in ("content-type", "content-length", "etag", "last-modified",
                "cache-control", "retry-after"):
        value = headers.get(key)
        if value is not None:
            result[key] = str(value)[:512]
    return result


def _truncate_body(text, max_bytes):
    if not isinstance(text, str):
        return ""
    raw = text.encode("utf-8", errors="ignore")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore") + "\n[content truncated]"


def _fetch_result(url, status=None, content="", headers=None, error=None):
    return {
        "url": url,
        "status": status,
        "content": content,
        "headers": _safe_response_headers(type(
            "_R", (), {"headers": headers or {}}
        )()),
        "error": error,
        "fetched_at": _time.time(),
    }


async def _resilient_request(client, method, url, *, mode="normal",
                             headers=None, params=None, **kwargs):
    """Fetch a public resource with bounded retries and rate limiting.

    This intentionally does not bypass CAPTCHAs, access controls, WAF rules,
    authentication, or other anti-automation mechanisms.
    """
    policy = _fetch_policy(mode)
    host = _domain_key(url)
    retries = policy["retries"]

    for attempt in range(retries + 1):
        await _respect_domain_rate_limit(url)
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                follow_redirects=False,
                **kwargs,
            )
            status = getattr(response, "status_code", None)

            if status == 429 or _retryable_status(status):
                if attempt < retries:
                    retry_after = _extract_retry_after(
                        getattr(response, "headers", {})
                    )
                    delay = _retry_delay(attempt, retry_after)
                    _DOMAIN_BACKOFF[host] = _time.monotonic() + delay
                    await _asyncio.sleep(delay)
                    continue

            return response

        except Exception:
            if attempt >= retries:
                raise
            await _asyncio.sleep(_retry_delay(attempt))

    raise RuntimeError("request retry budget exhausted")


async def resilient_fetch_url(client, url, *, mode="normal",
                              headers=None, max_bytes=None, timeout=None):
    """Public-page fetch helper for callers that already have an HTTP client."""
    ok_url, reason = await _validate_external_url(url)
    if not ok_url:
        return {"url": url, "status": None, "content": "", "headers": {}, "ok": False, "error": f"url_rejected:{reason}"}
    policy = _fetch_policy(mode)
    max_bytes = max_bytes or policy["max_bytes"]

    response = await _resilient_request(
        client,
        "GET",
        url,
        mode=mode,
        headers=headers,
        timeout=timeout,
    )

    challenge = _challenge_confidence(getattr(response, "status_code", None), getattr(response, "headers", {}), getattr(response, "text", "")[:120000])
    if challenge[0]:
        return {"url": str(response.url) if getattr(response, "url", None) else url, "status": getattr(response, "status_code", None), "content": "", "headers": _safe_response_headers(response), "ok": False, "error": "access_restricted", "access_restricted": True, "challenge": {"detected": True, "confidence": challenge[1], "kind": challenge[2]}}
    content = ""
    try:
        content = response.text
    except Exception:
        content = ""

    return {
        "url": str(response.url) if getattr(response, "url", None) else url,
        "status": getattr(response, "status_code", None),
        "content": _truncate_body(content, max_bytes),
        "headers": _safe_response_headers(response),
        "ok": bool(getattr(response, "is_success", False)),
    }


async def resilient_fetch_pages(client, urls, *, mode="normal",
                                headers=None, timeout=None):
    """Fetch multiple public URLs with bounded concurrency."""
    policy = _fetch_policy(mode)
    semaphore = _asyncio.Semaphore(policy["concurrency"])

    async def one(url):
        async with semaphore:
            try:
                return await resilient_fetch_url(
                    client, url, mode=mode, headers=headers, timeout=timeout
                )
            except Exception as exc:
                return {
                    "url": url,
                    "status": None,
                    "content": "",
                    "headers": {},
                    "ok": False,
                    "error": str(exc)[:500],
                }

    return await _asyncio.gather(*(one(url) for url in urls))


def classify_http_response(status):
    if status is None:
        return "network_error"
    if 200 <= status < 300:
        return "ok"
    if status in (301, 302, 303, 307, 308):
        return "redirect"
    if status == 401:
        return "authentication_required"
    if status == 403:
        return "access_restricted"
    if status == 404:
        return "not_found"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limited"
    if 500 <= status < 600:
        return "server_error"
    return "http_error"

# --- End v9 resilient public-page fetching ---

