# ABOUTME: Tests scripts/fetch-mcp-tools.py --oauth sign-in (PKCE, state check, token extraction).
# ABOUTME: A local fake token endpoint stands in for the vendor; webbrowser.open simulates the browser redirect.

import http.server
import importlib.util
import json
import socket
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fetch-mcp-tools.py"
spec = importlib.util.spec_from_file_location("fetch_mcp_tools", SCRIPT)
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _token_server(response: dict, seen: dict):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"])).decode()
            seen.update(urllib.parse.parse_qs(body))
            out = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv


def _browser(tamper_state=False, error=None):
    """Return a fake webbrowser.open that follows the redirect to the local callback."""

    def open_(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        cb = {"state": "wrong" if tamper_state else q["state"][0]}
        cb.update({"error": error} if error else {"code": "the-code"})
        target = q["redirect_uri"][0] + "?" + urllib.parse.urlencode(cb)
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=10).read(), daemon=True).start()
        return True

    return open_


@pytest.mark.parametrize(
    "response",
    [{"access_token": "tok-1", "token_type": "bearer"}, {"ok": True, "authed_user": {"access_token": "tok-1"}}],
)
def test_oauth_exchanges_code_with_pkce(response):
    seen = {}
    srv = _token_server(response, seen)
    redirect = f"http://127.0.0.1:{_free_port()}/callback"
    with patch.object(fetch.webbrowser, "open", _browser()):
        token = fetch.oauth_token(
            "https://vendor.example/authorize",
            f"http://127.0.0.1:{srv.server_port}/token",
            "cid",
            "csecret",
            "read write",
            redirect,
            timeout=10,
        )
    srv.server_close()
    assert token == "tok-1"
    assert seen["code"] == ["the-code"] and seen["client_secret"] == ["csecret"]
    assert seen["redirect_uri"] == [redirect] and len(seen["code_verifier"][0]) >= 43


def test_oauth_rejects_state_mismatch():
    redirect = f"http://127.0.0.1:{_free_port()}/callback"
    with patch.object(fetch.webbrowser, "open", _browser(tamper_state=True)):
        with pytest.raises(RuntimeError, match="state mismatch"):
            fetch.oauth_token("https://v/authorize", "http://127.0.0.1:1/token", "c", "s", "", redirect, timeout=10)


def test_oauth_surfaces_vendor_error():
    redirect = f"http://127.0.0.1:{_free_port()}/callback"
    with patch.object(fetch.webbrowser, "open", _browser(error="access_denied")):
        with pytest.raises(RuntimeError, match="access_denied"):
            fetch.oauth_token("https://v/authorize", "http://127.0.0.1:1/token", "c", "s", "", redirect, timeout=10)


def test_redact_drops_only_sentences_naming_the_user():
    tool = {
        "name": "slack_search_public",
        "description": "Searches public channels. Current logged in user's user_id is U09C902J8R1.\nTips: be specific.",
        "inputSchema": {"properties": {"user_id": {"description": "Slack user ID (e.g., 'U0ABC12345')."}}},
    }
    out = fetch.redact({"tools": [tool]}, ["U09C902J8R1"])["tools"][0]
    assert out["description"] == "Searches public channels.\nTips: be specific."
    assert out["inputSchema"] == tool["inputSchema"]  # untouched (example ID, not the user's)


def test_redact_is_noop_without_values():
    data = {"tools": [{"name": "t", "description": "Your user_id: U1."}]}
    assert fetch.redact(data, []) == data
