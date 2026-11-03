#!/usr/bin/env python3
# ABOUTME: Captures a remote MCP server's tool list as {"tools": [...]} for a ccwb gateway connector.
# ABOUTME: Usage: fetch-mcp-tools.py <mcp-url> <out.json> [--token <bearer> | --oauth ...] (see --help).
"""Capture the tool schema for an AgentCore Gateway connector target.

Gateway connectors use per-user OAuth, so the gateway cannot list a vendor's
tools by itself; ccwb uploads this file as the target's mcpToolSchema. Some
servers (Google Drive/Docs/Sheets) list tools without a token. Others need a
user access token for the vendor: pass one with --token (for example a Slack
user token from the Slack app's "OAuth & Permissions" page), or let the script
sign you in with --oauth (authorization code + PKCE against a local callback,
http://127.0.0.1:<port>/callback by default; add that redirect URI to the
vendor's OAuth app temporarily). The token is kept in memory only.

Some servers personalise tool descriptions for the signed-in user (Slack embeds
"the current logged in user's user_id is U..."). The schema is shared by every
user, so pass --redact <value> (e.g. your Slack user ID) to drop each sentence
that mentions it; otherwise every user's Claude would believe it is you.
"""

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


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
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ccwb", "version": "1"},
            },
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


def redact(obj, values):
    """Drop every sentence that mentions one of ``values`` from all strings in ``obj``."""
    import re

    if not values:
        return obj
    if isinstance(obj, dict):
        return {k: redact(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, values) for v in obj]
    if isinstance(obj, str) and any(v in obj for v in values):
        # A sentence ends at ". ", "\n" or end of string; keep the separators of kept sentences.
        parts = re.split(r"(?<=[.!?])[ \t]+|(\n)", obj)
        kept, out = [p for p in parts if p is not None], []
        for p in kept:
            if p == "\n" or not any(v in p for v in values):
                out.append(p)
        text = ""
        for p in out:
            text += p if p == "\n" or not text or text.endswith("\n") else " " + p
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    return obj


def oauth_token(authorize_url, token_url, client_id, client_secret, scope, redirect_uri, timeout=300):
    """Sign the user in (authorization code + PKCE) and return an access token."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [None])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in q:
                result["error"] = q["error"][0] + ": " + q.get("error_description", [""])[0]
            else:
                result["code"] = q.get("code", [None])[0]
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and return to the terminal.")

        def log_message(self, *a):
            pass

    parsed = urllib.parse.urlparse(redirect_uri)
    server = http.server.HTTPServer((parsed.hostname, parsed.port), Handler)
    server.timeout = timeout
    url = authorize_url + ("&" if "?" in authorize_url else "?") + urllib.parse.urlencode(params)
    print(f"Opening the browser to sign in. If it does not open, visit:\n  {url}", file=sys.stderr)
    webbrowser.open(url)
    server.handle_request()
    server.server_close()
    if not result.get("code"):
        raise RuntimeError(f"sign-in failed: {result.get('error', 'timed out')}")

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
        }
    ).encode()
    req = urllib.request.Request(token_url, data=body, headers={"content-type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        tok = json.loads(r.read().decode())
    # Standard OAuth responses carry access_token; Slack user flows nest it under authed_user.
    access = tok.get("access_token") or (tok.get("authed_user") or {}).get("access_token")
    if not access:
        raise RuntimeError(f"token response had no access_token (keys: {sorted(tok)})")
    return access


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", help="MCP server URL")
    ap.add_argument("out", help='output file ({"tools": [...]})')
    ap.add_argument("--token", help="bearer token for the MCP server")
    ap.add_argument("--oauth", action="store_true", help="sign in via the browser to obtain a token")
    ap.add_argument("--authorize-url")
    ap.add_argument("--token-url")
    ap.add_argument("--client-id")
    ap.add_argument("--client-secret-file", help="file containing only the client secret")
    ap.add_argument("--scope", default="", help="space-separated scopes")
    ap.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
    ap.add_argument(
        "--redact",
        action="append",
        default=[],
        metavar="VALUE",
        help="drop sentences mentioning VALUE (e.g. your Slack user ID); repeatable",
    )
    a = ap.parse_args()
    token = a.token
    try:
        if a.oauth:
            missing = [
                f for f in ("authorize_url", "token_url", "client_id", "client_secret_file") if not getattr(a, f)
            ]
            if missing:
                ap.error("--oauth needs " + ", ".join("--" + m.replace("_", "-") for m in missing))
            with open(a.client_secret_file, encoding="utf-8") as f:
                secret = f.read().strip()
            token = oauth_token(a.authorize_url, a.token_url, a.client_id, secret, a.scope, a.redirect_uri)
        tools = redact(fetch_tools(a.url, token), a.redact)
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
