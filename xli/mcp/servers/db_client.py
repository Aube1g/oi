#!/usr/bin/env python3
"""
MCP DB Client — SQL queries, schema introspection
"""

import json
import sys
from urllib.parse import urlparse
from xli.mcp.serverkit import filter_arguments, tool_descriptors

def query_sql(connection_string, query, params=None):
    """Execute SQL query safely (read-only by default)"""
    try:
        parsed = urlparse(connection_string)
        scheme = parsed.scheme

        if scheme in ["sqlite", ""]:
            import sqlite3
            conn = sqlite3.connect(parsed.path or connection_string)
            cursor = conn.cursor()

            # Safety: only SELECT for now
            if not query.strip().upper().startswith("SELECT"):
                return "Error: Only SELECT queries allowed"

            cursor.execute(query, params or [])
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            conn.close()

            result = {
                "columns": columns,
                "rows": [list(row) for row in rows[:100]],  # Limit results
                "count": len(rows)
            }
            return json.dumps(result, indent=2, default=str)

        elif scheme in ["postgresql", "postgres"]:
            try:
                import psycopg2
                conn = psycopg2.connect(connection_string)
                cursor = conn.cursor()

                if not query.strip().upper().startswith("SELECT"):
                    return "Error: Only SELECT queries allowed"

                cursor.execute(query, params or [])
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                conn.close()

                result = {
                    "columns": columns,
                    "rows": [list(row) for row in rows[:100]],
                    "count": len(rows)
                }
                return json.dumps(result, indent=2, default=str)

            except ImportError:
                return "psycopg2 not installed. pip install psycopg2-binary"

        else:
            return f"Unsupported database: {scheme}"

    except Exception as e:
        return f"Error: {e}"

def get_schema(connection_string, table):
    """Get table schema"""
    try:
        parsed = urlparse(connection_string)
        scheme = parsed.scheme

        if scheme in ["sqlite", ""]:
            import sqlite3
            conn = sqlite3.connect(parsed.path or connection_string)
            cursor = conn.cursor()

            cursor.execute(f"PRAGMA table_info({table})")
            columns = cursor.fetchall()
            conn.close()

            schema = []
            for col in columns:
                schema.append({
                    "name": col[1],
                    "type": col[2],
                    "nullable": not col[3],
                    "default": col[4],
                    "pk": bool(col[5])
                })

            return json.dumps(schema, indent=2)

        else:
            return f"Schema introspection for {scheme} not yet implemented"

    except Exception as e:
        return f"Error: {e}"

def list_tables(connection_string):
    """List all tables"""
    try:
        parsed = urlparse(connection_string)
        scheme = parsed.scheme

        if scheme in ["sqlite", ""]:
            import sqlite3
            conn = sqlite3.connect(parsed.path or connection_string)
            cursor = conn.cursor()

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            conn.close()

            return json.dumps(tables, indent=2)

        else:
            return f"Table listing for {scheme} not yet implemented"

    except Exception as e:
        return f"Error: {e}"

TOOLS = {
    "query_sql": query_sql,
    "get_schema": get_schema,
    "list_tables": list_tables,
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

