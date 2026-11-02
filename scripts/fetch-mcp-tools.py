#!/usr/bin/env python3
# ABOUTME: Captures a remote MCP server's tool list as {"tools": [...]} for a ccwb gateway connector.
# ABOUTME: Usage: fetch-mcp-tools.py <mcp-url> <out.json> [--token <bearer>] (token optional for public listings).
"""Capture the tool schema for an AgentCore Gateway connector target.

Gateway connectors use per-user OAuth, so the gateway cannot list a vendor's
tools by itself; ccwb uploads this file as the target's mcpToolSchema. Some
servers (Google Drive/Docs/Sheets) list tools without a token. Others need a
user access token for the vendor (for example a Slack user token from the
Slack app's "OAuth & Permissions" page).
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


def _post(url, body, token=None, session=None):
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        sid = r.headers.get("Mcp-Session-Id")
    msgs = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]
    if not msgs and text.strip():
        msgs = [json.loads(text)]
    return sid, msgs


def fetch_tools(url, token=None):
    sid, _ = _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ccwb", "version": "1"}},
        },
        token,
    )
    if sid:
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token, sid)
    tools, cursor = [], None
    while True:
        params = {"cursor": cursor} if cursor else {}
        _, msgs = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params}, token, sid)
        result = next((m["result"] for m in msgs if isinstance(m, dict) and "result" in m), None)
        if result is None:
            raise RuntimeError(f"tools/list failed: {msgs}")
        tools.extend(result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor:
            return tools


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--token")
    a = ap.parse_args()
    try:
        tools = fetch_tools(a.url, a.token)
    except (urllib.error.URLError, RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"tools": tools}, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {len(tools)} tools to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
