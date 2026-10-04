#!/usr/bin/env python3
"""MCP Notion server — pages, databases and their text.

Notion is where a project's decisions live: the spec, the meeting notes, the
"why did we do it this way" page. An agent that cannot read them will happily
invent a requirement instead.

The API is plain REST over HTTPS, and this server speaks it with `urllib`
(dependency-free by policy for bundled servers). The token comes from the
environment, which is how the Claude Code and Codex Notion servers are
configured too:

    NOTION_TOKEN=secret_...        (or NOTION_API_KEY / NOTION_KEY)

Only the handful of operations an agent actually needs are exposed, and the
text of a page is flattened into reading order, because a model given raw
Notion blocks spends its context on JSON brackets.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from xli.mcp.serverkit import filter_arguments, shake_hands, tool_descriptors

API = "https://api.notion.com/v1"
API_VERSION = "2022-06-28"
TIMEOUT = 30.0

TOKEN_ENV = ("NOTION_TOKEN", "NOTION_API_KEY", "NOTION_KEY")


def _token() -> str:
    for name in TOKEN_ENV:
        value = os.environ.get(name)
        if value:
            return value.strip()
    raise RuntimeError(
        "Не задан токен Notion. Set NOTION_TOKEN (or NOTION_API_KEY) in the "
        "environment — an integration token from https://www.notion.so/my-integrations. "
        "The integration must also be shared with the pages it should read."
    )


def _call(method: str, path: str, payload: dict | None = None) -> Any:
    """One REST call, with Notion's error body kept intact.

    Notion explains failures well ("object_not_found: make sure the
    integration is added to the page"); throwing that away and reporting a
    bare 404 is what makes people give up on the integration.
    """
    url = f"{API}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Notion-Version": API_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            error = json.loads(detail)
            message = error.get("message") or detail[:300]
            code = error.get("code", "")
        except json.JSONDecodeError:
            message, code = detail[:300], ""
        raise RuntimeError(f"Notion API {exc.code} {code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Нет связи с Notion: {exc.reason}") from exc


# ------------------------------------------------------------------ helpers
def _plain_text(rich: list[dict] | None) -> str:
    return "".join(part.get("plain_text", "") for part in (rich or []))


def _title_of(obj: dict) -> str:
    """A page's or database's title, wherever Notion decided to put it."""
    for value in (obj.get("properties") or {}).values():
        if isinstance(value, dict) and value.get("type") == "title":
            text = _plain_text(value.get("title"))
            if text:
                return text
    if isinstance(obj.get("title"), list):
        return _plain_text(obj["title"])
    return "(без названия)"


def _blocks_to_text(blocks: list[dict], depth: int = 0) -> list[str]:
    """Flatten Notion blocks into indented lines a model can read."""
    out: list[str] = []
    for block in blocks:
        kind = block.get("type", "?")
        payload = block.get(kind) or {}
        indent = "  " * depth

        if kind == "child_page":
            out.append(f"{indent}# {payload.get('title', '')}")
        elif kind in ("paragraph", "quote", "callout"):
            text = _plain_text(payload.get("rich_text"))
            if text:
                out.append(f"{indent}{text}")
        elif kind.startswith("heading_"):
            level = kind.rsplit("_", 1)[-1]
            out.append(f"{indent}{'#' * int(level)} {_plain_text(payload.get('rich_text'))}")
        elif kind in ("bulleted_list_item", "numbered_list_item", "to_do"):
            marker = "-" if kind != "to_do" else ("[x]" if payload.get("checked") else "[ ]")
            out.append(f"{indent}{marker} {_plain_text(payload.get('rich_text'))}")
        elif kind == "code":
            out.append(f"{indent}```{payload.get('language', '')}")
            out.extend(f"{indent}{line}" for line in _plain_text(payload.get("rich_text")).splitlines())
            out.append(f"{indent}```")
        elif kind == "divider":
            out.append(f"{indent}---")
        elif kind == "table_row":
            cells = [_plain_text(cell) for cell in payload.get("cells", [])]
            out.append(f"{indent}| " + " | ".join(cells) + " |")
        elif kind == "bookmark":
            out.append(f"{indent}{payload.get('url', '')}")
        elif kind == "unsupported":
            out.append(f"{indent}(недоступный блок)")

        if block.get("has_children") and kind not in ("child_page", "code"):
            children = _call("GET", f"/blocks/{block['id']}/children?page_size=100").get("results", [])
            out.extend(_blocks_to_text(children, depth + 1))
    return out


def _page_text(page_id: str, limit: int = 200) -> str:
    blocks = _call("GET", f"/blocks/{page_id}/children?page_size=100").get("results", [])
    lines = _blocks_to_text(blocks)
    if len(lines) > limit:
        lines = lines[:limit] + [f"... (обрезано, всего строк: {len(lines)})"]
    return "\n".join(lines) if lines else "(страница пуста)"


# -------------------------------------------------------------------- tools
def search(query: str = "", filter_type: str = "", page_size: int = 10) -> str:
    """Search pages and databases by title. `filter_type` is `page` or `database`."""
    payload: dict[str, Any] = {"page_size": max(1, min(int(page_size), 25))}
    if query:
        payload["query"] = query
    if filter_type in ("page", "database"):
        payload["filter"] = {"property": "object", "value": filter_type}

    result = _call("POST", "/search", payload)
    items = result.get("results", [])
    if not items:
        return f"Ничего не найдено по запросу {query!r}."

    lines = [f"Найдено: {len(items)} (есть ещё: {'да' if result.get('has_more') else 'нет'})"]
    for item in items:
        kind = "база" if item.get("object") == "database" else "страница"
        lines.append(f"- [{kind}] {_title_of(item)}  id={item.get('id')}")
        url = item.get("url")
        if url:
            lines.append(f"    {url}")
    return "\n".join(lines)


def read_page(page_id: str, limit: int = 200) -> str:
    """Read a page's full text, in reading order. Accepts an id or a Notion URL."""
    page_id = _normalise_id(page_id)
    page = _call("GET", f"/pages/{page_id}")
    header = f"# {_title_of(page)}\n{page.get('url', '')}\n"
    return header + _page_text(page_id, limit=int(limit))


def get_database(database_id: str) -> str:
    """Show a database's schema: its properties and what they mean."""
    database_id = _normalise_id(database_id)
    database = _call("GET", f"/databases/{database_id}")
    lines = [f"# {_title_of(database)}", f"id={database.get('id')}"]
    for name, prop in (database.get("properties") or {}).items():
        kind = prop.get("type", "?")
        extra = ""
        if kind in ("select", "multi_select", "status"):
            options = (prop.get(kind) or {}).get("options", [])
            extra = " [" + ", ".join(option.get("name", "") for option in options) + "]"
        elif kind == "relation":
            extra = f" -> {(prop.get('relation') or {}).get('database_id', '')}"
        lines.append(f"- {name}: {kind}{extra}")
    return "\n".join(lines)


def query_database(database_id: str, filter_json: str = "", page_size: int = 10) -> str:
    """Query a database's rows. `filter_json` is Notion's filter object as JSON."""
    database_id = _normalise_id(database_id)
    payload: dict[str, Any] = {"page_size": max(1, min(int(page_size), 25))}
    if filter_json.strip():
        try:
            payload["filter"] = json.loads(filter_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"filter_json не JSON: {exc}") from exc

    result = _call("POST", f"/databases/{database_id}/query", payload)
    rows = result.get("results", [])
    if not rows:
        return "В базе нет строк, подходящих под фильтр."

    out = [f"Строк: {len(rows)}"]
    for row in rows:
        cells = []
        for name, prop in (row.get("properties") or {}).items():
            kind = prop.get("type")
            value: Any = ""
            if kind == "title":
                value = _plain_text(prop.get("title"))
            elif kind == "rich_text":
                value = _plain_text(prop.get("rich_text"))
            elif kind == "number":
                value = prop.get("number")
            elif kind in ("select", "status"):
                value = (prop.get(kind) or {}).get("name", "")
            elif kind == "multi_select":
                value = ", ".join(option.get("name", "") for option in prop.get(kind) or [])
            elif kind == "checkbox":
                value = prop.get("checkbox")
            elif kind == "date":
                value = (prop.get("date") or {}).get("start", "")
            if value not in ("", None):
                cells.append(f"{name}={value}")
        out.append(f"- id={row.get('id')}: " + "; ".join(cells))
    return "\n".join(out)


def create_page(parent_id: str, title: str, content: str = "") -> str:
    """Create a page under a page or database, with `content` as paragraphs.

    This writes to the user's workspace, so it is the one tool here with that
    side effect; the agent should ask before using it.
    """
    parent_id = _normalise_id(parent_id)
    if not title.strip():
        raise RuntimeError("Нужен заголовок новой страницы (title).")
    parent = {"database_id": parent_id} if _looks_like_database(parent_id) else {"page_id": parent_id}

    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
        }
        for line in content.split("\n")
        if line.strip()
    ][:100]
    payload: dict[str, Any] = {
        "parent": parent,
        "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
    }
    if children:
        payload["children"] = children
    page = _call("POST", "/pages", payload)
    return f"Страница создана: {_title_of(page)}\n{page.get('url', '')}\nid={page.get('id')}"


def append_text(block_id: str, content: str) -> str:
    """Append paragraphs to an existing page or block."""
    block_id = _normalise_id(block_id)
    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
        }
        for line in content.split("\n")
        if line.strip()
    ]
    if not children:
        raise RuntimeError("Нечего добавлять: content пуст.")
    result = _call("POST", f"/blocks/{block_id}/children", {"children": children[:100]})
    return f"Добавлено блоков: {len(result.get('results', children))}"


def _normalise_id(value: str) -> str:
    """Accept a raw id, a dashed id, or a full Notion URL."""
    text = (value or "").strip()
    if text.startswith("http"):
        text = text.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        text = text.split("-")[-1] if text.count("-") >= 4 else text
    compact = text.replace("-", "")
    if len(compact) == 32 and all(char in "0123456789abcdefABCDEF" for char in compact):
        return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"
    return text


def _looks_like_database(_parent_id: str) -> bool:
    """A page id and a database id are the same shape, so the caller's choice
    cannot be inferred — env var `NOTION_CREATE_IN_DATABASE=1` says it."""
    return os.environ.get("NOTION_CREATE_IN_DATABASE", "").strip() not in ("", "0", "false")


TOOLS = {
    "search": search,
    "read_page": read_page,
    "get_database": get_database,
    "query_database": query_database,
    "create_page": create_page,
    "append_text": append_text,
}


def handle_request(request):
    # initialize / ping / notifications, per the protocol
    early = shake_hands(request)
    if early is not None:
        return early
    method = request.get("method")
    request_id = request.get("id")

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_descriptors(TOOLS)}}
    if method == "tools/call":
        params = request.get("params") or {}
        tool = params.get("name")
        if tool not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Нет такого инструмента: {tool}"},
            }
        try:
            result = TOOLS[tool](**filter_arguments(TOOLS[tool], params.get("arguments") or {}))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(result)}], "isError": False},
            }
        except Exception as exc:  # noqa: BLE001 - the error text is the result
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Нет такого метода: {method}"},
    }


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            reply = handle_request(request)
            if reply is not None:
                print(json.dumps(reply, ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad line must not stop the server
            print(
                json.dumps(
                    {"jsonrpc": "2.0", "error": {"code": -32700, "message": str(exc)}},
                    ensure_ascii=False,
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
