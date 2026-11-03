# ABOUTME: Remote MCP connectors (Stripe, Taxy Workspace, ...) distributed to Claude Code and Claude Cowork.
# ABOUTME: Each user authenticates to the vendor with their own account (per-user OAuth); no secrets ship.

"""Remote MCP connectors.

Two kinds, both authenticated **per user** (each user signs in to the vendor
with their own account and the vendor enforces that user's permissions):

* **direct** — a vendor-hosted MCP server with OAuth Dynamic Client
  Registration (Stripe, Taxy Workspace). The MCP client registers itself, runs
  the OAuth flow in the browser and keeps the user's token on the device. No
  infrastructure, no secrets. Works with every ccwb auth mode.
* **gateway** — a vendor MCP server that needs a pre-registered OAuth app with a
  client secret (Slack, HubSpot, Google Drive/Docs/Sheets). These sit behind an
  Amazon Bedrock AgentCore Gateway (``connectors-gateway.yaml`` +
  ``connectors-target.yaml``): client secrets stay in AWS Secrets Manager, user
  tokens live in the AgentCore Identity token vault, and users reach the
  gateway with their ccwb OIDC id_token (``credential-process
  --get-mcp-auth-header`` via the ``websearch-headers`` helper). All gateway
  connectors appear to clients as ONE MCP server (default name
  ``connectors``). Requires OIDC auth.

Profile ``connectors`` items: a direct preset name (``"stripe"``), a direct
dict ``{"name", "url", "surfaces"}``, or a gateway dict
``{"via": "gateway", "preset": "slack", "client_id": ..., "client_secret_arn": ...}``.
Gateway settings live in ``profile.connectors_gateway`` (see
``GATEWAY_SETTINGS_DOC``).

Rendering targets:

* **Cowork** (Claude Desktop): ``managedMcpServers`` entries — direct:
  ``{"name", "transport": "http", "url", "oauth": true}``; gateway:
  ``{"name", "transport": "http", "url", "headersHelper", "headersHelperTtlSec"}``.
* **Claude Code**: installer snippets that add each server at *user* scope
  (``claude mcp add`` / ``claude mcp add-json``; when ``claude`` is not on PATH
  the installer prints the commands instead of editing ``.claude.json``).
  Side-by-side installs target ``~/.claude-bedrock``. The installer records
  what it added in ``~/claude-code-with-bedrock/connectors-managed.txt``
  (``name=url`` lines): unchanged connectors are left alone (users keep their
  sign-in), connectors removed from the profile are removed on the next
  install, and servers the user configured themselves are never overwritten.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from claude_code_with_bedrock.cli.utils.coexistence import (
    BEDROCK_CONFIG_DIR_NAME,
    INSTALL_DIR_MARKER,
    coexistence_enabled,
)

SURFACE_CLAUDE_CODE = "claude-code"
SURFACE_COWORK = "cowork"
ALL_SURFACES = (SURFACE_CLAUDE_CODE, SURFACE_COWORK)

# Vendor-hosted MCP servers with OAuth Dynamic Client Registration (verified
# via their /.well-known/oauth-authorization-server metadata).
PRESETS: dict[str, str] = {
    "stripe": "https://mcp.stripe.com",
    "taxy-workspace": "https://mcp.workspace.taxy.io/mcp",
    # Jira + Confluence (Atlassian Rovo MCP server). v2 has its own authorization server
    # (auth.atlassian.com, still DCR); v1 endpoints are removed in March 2027.
    "atlassian": "https://mcp.atlassian.com/v2/mcp",
    "amplitude": "https://mcp.amplitude.com/mcp",  # Amplitude US data center
    "amplitude-eu": "https://mcp.eu.amplitude.com/mcp",  # Amplitude EU data center
}

# Claude Desktop rejects the OAuth loopback callback unless the browser was sent
# back by the authorization server's host. Vendors whose sign-in redirects from a
# different host need it allowed via oauth.additionalRedirectReferrerHosts
# (verified in Desktop logs: Amplitude returns from its web app, not mcp.*).
PRESET_REFERRER_HOSTS: dict[str, tuple[str, ...]] = {
    "amplitude": ("app.amplitude.com",),
    "amplitude-eu": ("app.eu.amplitude.com",),
}

STATE_FILE_NAME = "connectors-managed.txt"

VIA_DIRECT = "direct"
VIA_GATEWAY = "gateway"

# Vendor-hosted MCP servers that need a pre-registered OAuth app (no DCR); served
# through the AgentCore Gateway. Endpoints verified against each vendor's
# /.well-known/oauth-authorization-server (or OIDC discovery) metadata.
# "tool_schema" names a bundled file in deployment/infrastructure/connector-tool-schemas/.
GATEWAY_PRESETS: dict[str, dict] = {
    "slack": {
        "description": "Slack",
        "url": "https://mcp.slack.com/mcp",
        "issuer": "https://mcp.slack.com",
        "authorization_endpoint": "https://slack.com/oauth/v2_user/authorize",
        "token_endpoint": "https://slack.com/api/oauth.v2.user.access",
        # Every user scope Slack's MCP server supports (its 27 tools, read and write).
        # The Slack app must allow all of them as User Token Scopes. For read-only,
        # override "scopes" and supply a tool_schema without the write tools.
        "scopes": [
            "search:read.public",
            "search:read.private",
            "search:read.im",
            "search:read.mpim",
            "search:read.files",
            "search:read.users",
            "channels:history",
            "channels:read",
            "channels:write",
            "groups:history",
            "groups:read",
            "groups:write",
            "im:history",
            "im:read",
            "im:write",
            "mpim:history",
            "mpim:read",
            "mpim:write",
            "users:read",
            "users:read.email",
            "chat:write",
            "canvases:read",
            "canvases:write",
            "files:read",
            "files:write",
            "lists:read",
            "lists:write",
            "reactions:read",
            "reactions:write",
            "emoji:read",
        ],
        "tool_schema": "slack.json",
    },
    "hubspot": {
        "description": "HubSpot",
        "url": "https://mcp.hubspot.com",
        "issuer": "https://mcp.hubspot.com",
        "authorization_endpoint": "https://mcp.hubspot.com/oauth/authorize/user",
        "token_endpoint": "https://mcp.hubspot.com/oauth/v3/token",
        "scopes": [],  # configured on the HubSpot MCP auth app
        "tool_schema": "hubspot.json",
    },
    "google-drive": {
        "description": "Google Drive",
        "url": "https://drivemcp.googleapis.com/mcp/v1",
        "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
        "scopes": ["https://www.googleapis.com/auth/drive"],
        "auth_params": {"access_type": "offline", "prompt": "consent"},
        "tool_schema": "google-drive.json",
    },
    "google-docs": {
        "description": "Google Docs",
        "url": "https://docsmcp.googleapis.com/mcp/v1",
        "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
        "scopes": ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive.readonly"],
        "auth_params": {"access_type": "offline", "prompt": "consent"},
        "tool_schema": "google-docs.json",
    },
    "google-sheets": {
        "description": "Google Sheets",
        "url": "https://sheetsmcp.googleapis.com/mcp/v1",
        "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
        "scopes": ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.readonly"],
        "auth_params": {"access_type": "offline", "prompt": "consent"},
        "tool_schema": "google-sheets.json",
    },
    "taxy-workspace": {
        "description": "Taxy Workspace",
        "url": "https://mcp.workspace.taxy.io/mcp",
        "discovery_url": "https://login.taxy.io/.well-known/openid-configuration",
        "scopes": ["openid", "profile", "email", "offline_access"],
        "auth_params": {"resource": "https://mcp.workspace.taxy.io/mcp"},
        "tool_schema": "taxy-workspace.json",
    },
}

# Extra authorization-request parameters connectors-target.yaml can pass.
AUTH_PARAM_KEYS = {
    "resource": "ResourceParam",
    "audience": "AudienceParam",
    "access_type": "AccessTypeParam",
    "prompt": "PromptParam",
}

DEFAULT_GATEWAY_SERVER_NAME = "connectors"
GATEWAY_SETTINGS_DOC = (
    "profile.connectors_gateway keys: portal_client_secret_arn (required), portal_scopes, region, "
    "server_name; gateway_url / portal_url / deployed are written by 'ccwb deploy connectors'."
)

# Strict: names and URLs are spliced into bash/batch/PowerShell installers.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
# Gateway target names become tool prefixes (<name>___tool); keep them short.
_GATEWAY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SECRET_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:ccwb-connectors/[A-Za-z0-9/_+=.@-]+$"
)
_SCOPE_RE = re.compile(r"^[!#-\[\]-~]{1,64}$")  # RFC 6749 scope-token chars, AgentCore max 64


@dataclass(frozen=True)
class Connector:
    """A server as clients see it (a direct connector, or the gateway itself)."""

    name: str
    url: str
    surfaces: tuple[str, ...] = ALL_SURFACES
    headers_helper: bool = False  # authenticate with the ccwb id_token helper (gateway)
    referrer_hosts: tuple[str, ...] = ()  # extra OAuth callback referrer hosts (Claude Desktop)


@dataclass(frozen=True)
class GatewayConnector:
    """One vendor MCP server behind the AgentCore Gateway (one target stack)."""

    name: str
    url: str
    client_id: str
    client_secret_arn: str
    description: str = ""
    discovery_url: str = ""
    issuer: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    scopes: tuple[str, ...] = ()
    auth_params: tuple[tuple[str, str], ...] = ()
    tool_schema: str = ""  # bundled file name or absolute/relative path
    client_auth_method: str = "CLIENT_SECRET_POST"


class ConnectorConfigError(ValueError):
    """Raised when the profile's ``connectors`` list is invalid."""


def _parse_surfaces(name: str, surfaces) -> tuple[str, ...]:
    if isinstance(surfaces, str):
        surfaces = [surfaces]
    bad = [x for x in surfaces if x not in ALL_SURFACES]
    if bad or not surfaces:
        raise ConnectorConfigError(
            f"Invalid surfaces for connector '{name}': {surfaces} (allowed: {', '.join(ALL_SURFACES)})"
        )
    return tuple(dict.fromkeys(surfaces))


def _check_url(name: str, url: str, what: str = "URL") -> str:
    if not _URL_RE.match(url):
        raise ConnectorConfigError(f"Invalid {what} for connector '{name}': '{url}' (must be a plain https:// URL)")
    return url


def _parse_direct(item) -> Connector:
    if isinstance(item, str):
        key = item.strip().lower()
        if key not in PRESETS:
            hint = " (gateway presets need an object with via, client_id and client_secret_arn)"
            raise ConnectorConfigError(
                f"Unknown connector preset '{item}'. Known presets: {', '.join(sorted(PRESETS))}"
                + (hint if key in GATEWAY_PRESETS else "")
            )
        return Connector(name=key, url=PRESETS[key], referrer_hosts=PRESET_REFERRER_HOSTS.get(key, ()))

    name = str(item.get("name", "")).strip()
    url = str(item.get("url", "") or PRESETS.get(name, "")).strip()
    if not _NAME_RE.match(name):
        raise ConnectorConfigError(
            f"Invalid connector name '{name}': use lowercase letters, digits and '-' (max 63 chars)"
        )
    _check_url(name, url)
    hosts = item.get("redirect_referrer_hosts", PRESET_REFERRER_HOSTS.get(name, ()))
    if isinstance(hosts, str):
        hosts = hosts.split()
    if not isinstance(hosts, list | tuple) or any(not _HOST_RE.match(str(h)) for h in hosts):
        raise ConnectorConfigError(
            f"Invalid redirect_referrer_hosts for connector '{name}': {hosts!r} (host names only)"
        )
    return Connector(
        name=name,
        url=url,
        surfaces=_parse_surfaces(name, item.get("surfaces", list(ALL_SURFACES))),
        referrer_hosts=tuple(hosts),
    )


def _parse_gateway(item: dict) -> GatewayConnector:
    preset_key = str(item.get("preset", "") or "").strip().lower()
    if preset_key and preset_key not in GATEWAY_PRESETS:
        raise ConnectorConfigError(
            f"Unknown gateway connector preset '{preset_key}'. Known presets: {', '.join(sorted(GATEWAY_PRESETS))}"
        )
    merged = {**GATEWAY_PRESETS.get(preset_key, {}), **{k: v for k, v in item.items() if v not in (None, "")}}
    name = str(merged.get("name") or preset_key).strip()
    if not _GATEWAY_NAME_RE.match(name):
        raise ConnectorConfigError(
            f"Invalid gateway connector name '{name}': use lowercase letters, digits and '-' (max 40 chars)"
        )
    if "surfaces" in item:
        raise ConnectorConfigError(f"Gateway connector '{name}': 'surfaces' is not supported (set it per gateway)")
    url = _check_url(name, str(merged.get("url", "")).strip())

    client_id = str(merged.get("client_id", "")).strip()
    if not client_id or not re.match(r"^[A-Za-z0-9._:@/-]{1,256}$", client_id):
        raise ConnectorConfigError(f"Gateway connector '{name}' needs the vendor OAuth app's 'client_id'")
    secret_arn = str(merged.get("client_secret_arn", "")).strip()
    if not _SECRET_ARN_RE.match(secret_arn):
        raise ConnectorConfigError(
            f"Gateway connector '{name}' needs 'client_secret_arn': a Secrets Manager secret ARN under the "
            "'ccwb-connectors/' prefix (JSON key 'client_secret')"
        )

    discovery = str(merged.get("discovery_url", "")).strip()
    issuer = str(merged.get("issuer", "")).strip()
    auth_ep = str(merged.get("authorization_endpoint", "")).strip()
    token_ep = str(merged.get("token_endpoint", "")).strip()
    if discovery:
        if not discovery.endswith("/.well-known/openid-configuration"):
            raise ConnectorConfigError(
                f"Gateway connector '{name}': discovery_url must end with /.well-known/openid-configuration"
            )
        _check_url(name, discovery, "discovery_url")
    elif issuer and auth_ep and token_ep:
        for what, v in (("issuer", issuer), ("authorization_endpoint", auth_ep), ("token_endpoint", token_ep)):
            _check_url(name, v, what)
    else:
        raise ConnectorConfigError(
            f"Gateway connector '{name}' needs 'discovery_url' or 'issuer' + 'authorization_endpoint' + 'token_endpoint'"
        )

    scopes = merged.get("scopes", [])
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, list) or any(not isinstance(x, str) or not _SCOPE_RE.match(x) for x in scopes):
        raise ConnectorConfigError(f"Gateway connector '{name}': invalid scopes {scopes!r}")

    auth_params = merged.get("auth_params", {}) or {}
    if not isinstance(auth_params, dict) or any(k not in AUTH_PARAM_KEYS for k in auth_params):
        raise ConnectorConfigError(
            f"Gateway connector '{name}': auth_params keys must be among {', '.join(sorted(AUTH_PARAM_KEYS))}"
        )

    method = str(merged.get("client_auth_method", "CLIENT_SECRET_POST")).upper()
    if method not in ("CLIENT_SECRET_POST", "CLIENT_SECRET_BASIC"):
        raise ConnectorConfigError(
            f"Gateway connector '{name}': client_auth_method must be CLIENT_SECRET_POST or _BASIC"
        )

    tool_schema = str(merged.get("tool_schema", "")).strip()
    if not tool_schema:
        raise ConnectorConfigError(
            f"Gateway connector '{name}' needs 'tool_schema': a JSON file {{\"tools\": [...]}} "
            "(capture it with scripts/fetch-mcp-tools.py)"
        )

    return GatewayConnector(
        name=name,
        url=url,
        client_id=client_id,
        client_secret_arn=secret_arn,
        description=str(merged.get("description", "")).strip(),
        discovery_url=discovery,
        issuer="" if discovery else issuer,
        authorization_endpoint="" if discovery else auth_ep,
        token_endpoint="" if discovery else token_ep,
        scopes=tuple(scopes),
        auth_params=tuple(sorted((str(k), str(v)) for k, v in auth_params.items())),
        tool_schema=tool_schema,
        client_auth_method=method,
    )


def _resolve_all(profile) -> tuple[list[Connector], list[GatewayConnector]]:
    raw = getattr(profile, "connectors", None) or []
    if not isinstance(raw, list):
        raise ConnectorConfigError("'connectors' must be a list")
    direct: list[Connector] = []
    gateway: list[GatewayConnector] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict) and str(item.get("via", VIA_DIRECT)).lower() == VIA_GATEWAY:
            c = _parse_gateway(item)
            gateway.append(c)
        elif isinstance(item, str) or (
            isinstance(item, dict) and str(item.get("via", VIA_DIRECT)).lower() == VIA_DIRECT
        ):
            c = _parse_direct(item)
            direct.append(c)
        elif isinstance(item, dict):
            raise ConnectorConfigError(
                f"Connector 'via' must be '{VIA_DIRECT}' or '{VIA_GATEWAY}', got {item.get('via')!r}"
            )
        else:
            raise ConnectorConfigError(
                f"Connector entries must be a preset name or an object, got {type(item).__name__}"
            )
        if c.name in seen:
            raise ConnectorConfigError(f"Duplicate connector name '{c.name}'")
        seen.add(c.name)
    if gateway:
        server = gateway_server_name(profile)
        if server in seen:
            raise ConnectorConfigError(
                f"Connector name '{server}' is reserved for the connectors gateway (change connectors_gateway.server_name)"
            )
    return direct, gateway


def resolve_connectors(profile) -> list[Connector]:
    """Direct connectors from ``profile.connectors``. Raises ConnectorConfigError."""
    return _resolve_all(profile)[0]


def resolve_gateway_connectors(profile) -> list[GatewayConnector]:
    """Gateway connectors from ``profile.connectors``. Raises ConnectorConfigError."""
    return _resolve_all(profile)[1]


def gateway_settings(profile) -> dict:
    gw = getattr(profile, "connectors_gateway", None)
    return gw if isinstance(gw, dict) else {}


def gateway_server_name(profile) -> str:
    name = str(gateway_settings(profile).get("server_name") or DEFAULT_GATEWAY_SERVER_NAME)
    if not _NAME_RE.match(name):
        raise ConnectorConfigError(f"Invalid connectors_gateway.server_name '{name}'")
    return name


def has_gateway_connectors(profile) -> bool:
    try:
        return bool(resolve_gateway_connectors(profile))
    except ConnectorConfigError:
        return False


def connectors_for(profile, surface: str) -> list[Connector]:
    """Servers to configure on a client surface: direct ones + the gateway (once deployed)."""
    direct, gateway = _resolve_all(profile)
    result = [c for c in direct if surface in c.surfaces]
    url = str(gateway_settings(profile).get("gateway_url", "") or "").strip()
    if gateway and url:
        result.append(Connector(name=gateway_server_name(profile), url=_check_url("gateway", url), headers_helper=True))
    return result


def validate_connectors(profile) -> list[tuple[str, str]]:
    """Return (severity, message) problems for packaging/deploy. Never raises."""
    try:
        _, gateway = _resolve_all(profile)
    except ConnectorConfigError as e:
        return [("error", str(e))]
    problems: list[tuple[str, str]] = []
    if gateway:
        auth = getattr(profile, "effective_auth_type", getattr(profile, "auth_type", "oidc"))
        if auth != "oidc":
            problems.append(
                ("error", f"Gateway connectors require OIDC auth (this profile uses '{auth}'); use direct connectors")
            )
        if not _SECRET_ARN_RE.match(str(gateway_settings(profile).get("portal_client_secret_arn", ""))):
            problems.append(
                (
                    "error",
                    "Gateway connectors need connectors_gateway.portal_client_secret_arn: a Secrets Manager secret "
                    "under 'ccwb-connectors/' holding the OIDC app's client secret (JSON key 'client_secret')",
                )
            )
        if not gateway_settings(profile).get("gateway_url"):
            problems.append(
                ("warning", "Connectors gateway not deployed yet: run 'ccwb deploy connectors' before 'ccwb package'")
            )
    return problems


# ---------------------------------------------------------------------------
# Cowork (Claude Desktop managedMcpServers)
# ---------------------------------------------------------------------------


def cowork_entries(profile) -> list[dict]:
    from claude_code_with_bedrock.cli.utils.cowork_3p import (
        WEBSEARCH_HEADERS_HELPER_PLACEHOLDER,
        WEBSEARCH_HEADERS_TTL_SEC,
    )

    entries = []
    for c in connectors_for(profile, SURFACE_COWORK):
        if c.headers_helper:
            helper = (getattr(profile, "websearch_headers_helper_path", "") or "").strip()
            entries.append(
                {
                    "name": c.name,
                    "transport": "http",
                    "url": c.url,
                    "headersHelper": helper or WEBSEARCH_HEADERS_HELPER_PLACEHOLDER,
                    "headersHelperTtlSec": WEBSEARCH_HEADERS_TTL_SEC,
                }
            )
        else:
            # oauth: true = dynamic registration. An object with only
            # additionalRedirectReferrerHosts (space-separated) still registers dynamically.
            oauth = {"additionalRedirectReferrerHosts": " ".join(c.referrer_hosts)} if c.referrer_hosts else True
            entries.append({"name": c.name, "transport": "http", "url": c.url, "oauth": oauth})
    return entries


def add_connectors_mcp_config(mdm_config: dict, profile, console) -> None:
    """Merge direct connectors into ``managedMcpServers`` (JSON-encoded string).

    Keeps admin-defined and web search entries; replaces same-named entries so
    re-runs never duplicate. No-op when no connector targets Cowork.
    """
    entries = cowork_entries(profile)
    if not entries:
        return

    existing_raw = mdm_config.get("managedMcpServers")
    servers: list = []
    if isinstance(existing_raw, str) and existing_raw.strip():
        try:
            parsed = json.loads(existing_raw)
            if isinstance(parsed, list):
                servers = parsed
        except (ValueError, TypeError):
            servers = []
    elif isinstance(existing_raw, list):
        servers = list(existing_raw)

    names = {e["name"] for e in entries}
    servers = [s for s in servers if not (isinstance(s, dict) and s.get("name") in names)]
    servers.extend(entries)
    mdm_config["managedMcpServers"] = json.dumps(servers)
    console.print(f"[dim]Added MCP connectors to CoWork config: {', '.join(sorted(names))}[/dim]")


# ---------------------------------------------------------------------------
# Claude Code (user-scope MCP servers via installer)
# ---------------------------------------------------------------------------


HELPER_FILE_NAME = "websearch-headers"  # installed by ccwb package next to credential-process


def _desired(profile) -> dict[str, str]:
    return {c.name: c.url for c in connectors_for(profile, SURFACE_CLAUDE_CODE)}


def _helper_names(profile) -> list[str]:
    return [c.name for c in connectors_for(profile, SURFACE_CLAUDE_CODE) if c.headers_helper]


def bash_connectors_block(profile) -> str:
    """install.sh snippet: add/remove user-scope MCP connectors for Claude Code.

    Uses $ACTUAL_HOME / $ACTUAL_USER / $SUDO_USER from the installer. Never
    fails the install (connectors are optional).
    """
    desired = _desired(profile)
    names = " ".join(desired)
    pairs = " ".join(f"{n}={u}" for n, u in desired.items())
    if coexistence_enabled(profile):
        cfg_dir = f'"$ACTUAL_HOME/{BEDROCK_CONFIG_DIR_NAME}"'
    else:
        cfg_dir = '""'
    return (
        f"""
# --- MCP connectors for Claude Code (user scope) ---
# Each user signs in to the vendor with their own account on first use.
CCWB_CONNECTORS="{names}"
CCWB_CONNECTOR_PAIRS="{pairs}"
CCWB_HELPER_CONNECTORS="{" ".join(_helper_names(profile))}"
CCWB_HEADERS_HELPER="$ACTUAL_HOME/{INSTALL_DIR_MARKER}/{HELPER_FILE_NAME}"
CCWB_CFG_DIR={cfg_dir}
CCWB_CONNECTOR_STATE="$ACTUAL_HOME/{INSTALL_DIR_MARKER}/{STATE_FILE_NAME}"
"""
        + r"""# Locate claude even when PATH lacks it (Jamf/MDM run this as root with a minimal PATH).
CCWB_SYSTEM_CLAUDE_PATHS="/opt/homebrew/bin/claude /usr/local/bin/claude"
CCWB_CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
if [ -z "$CCWB_CLAUDE_BIN" ]; then
    for _ccwb_c in "$ACTUAL_HOME/.local/bin/claude" "$ACTUAL_HOME/.claude/local/claude" $CCWB_SYSTEM_CLAUDE_PATHS; do
        if [ -x "$_ccwb_c" ]; then CCWB_CLAUDE_BIN="$_ccwb_c"; break; fi
    done
fi
_ccwb_claude() {
    # Its own dir first on PATH so npm-installed claude finds node.
    _ccwb_path="$(dirname "$CCWB_CLAUDE_BIN"):/opt/homebrew/bin:/usr/local/bin:$PATH"
    if [ -n "$SUDO_USER" ]; then
        sudo -u "$ACTUAL_USER" -H env PATH="$_ccwb_path" ${CCWB_CFG_DIR:+CLAUDE_CONFIG_DIR="$CCWB_CFG_DIR"} "$CCWB_CLAUDE_BIN" "$@"
    else
        env PATH="$_ccwb_path" ${CCWB_CFG_DIR:+CLAUDE_CONFIG_DIR="$CCWB_CFG_DIR"} "$CCWB_CLAUDE_BIN" "$@"
    fi
}
if [ -n "$CCWB_CONNECTORS" ] || [ -s "$CCWB_CONNECTOR_STATE" ]; then
    echo
    echo "Configuring MCP connectors for Claude Code..."
    if [ -n "$CCWB_CFG_DIR" ]; then mkdir -p "$CCWB_CFG_DIR"; fi
    if [ -n "$CCWB_CLAUDE_BIN" ]; then
        # State holds name=url pairs this installer added. Unchanged entries are
        # left alone so users keep their vendor sign-in; stale ones are removed.
        _ccwb_prev=""
        if [ -f "$CCWB_CONNECTOR_STATE" ]; then _ccwb_prev="$(tr '\n' ' ' < "$CCWB_CONNECTOR_STATE")"; fi
        _ccwb_now=""
        for _ccwb_old in $_ccwb_prev; do
            case " $CCWB_CONNECTOR_PAIRS " in
                *" $_ccwb_old "*) ;;
                *)
                    _ccwb_claude mcp remove -s user "${_ccwb_old%%=*}" >/dev/null 2>&1 || true
                    case " $CCWB_CONNECTORS " in *" ${_ccwb_old%%=*} "*) ;; *) echo "  OK Removed MCP connector: ${_ccwb_old%%=*}" ;; esac
                    ;;
            esac
        done
        for _ccwb_pair in $CCWB_CONNECTOR_PAIRS; do
            _ccwb_name="${_ccwb_pair%%=*}"
            _ccwb_url="${_ccwb_pair#*=}"
            case " $_ccwb_prev " in
                *" $_ccwb_pair "*)
                    echo "  OK MCP connector: $_ccwb_name (unchanged)"
                    _ccwb_now="$_ccwb_now $_ccwb_pair"
                    continue
                    ;;
            esac
            case " $CCWB_HELPER_CONNECTORS " in
                *" $_ccwb_name "*)
                    _ccwb_add() { _ccwb_claude mcp add-json -s user "$_ccwb_name" "{\"type\":\"http\",\"url\":\"$_ccwb_url\",\"headersHelper\":\"$CCWB_HEADERS_HELPER\"}"; } ;;
                *)
                    _ccwb_add() { _ccwb_claude mcp add --transport http -s user "$_ccwb_name" "$_ccwb_url"; } ;;
            esac
            if _ccwb_add >/dev/null 2>&1; then
                echo "  OK MCP connector: $_ccwb_name -> $_ccwb_url"
                _ccwb_now="$_ccwb_now $_ccwb_pair"
            else
                echo "  SKIP $_ccwb_name: could not add (a server with this name may already exist)"
            fi
        done
        printf '%s\n' $_ccwb_now > "$CCWB_CONNECTOR_STATE"
    else
        echo "  WARN 'claude' not found on PATH. Install Claude Code, then re-run this installer"
        echo "       or add the connectors manually:"
        for _ccwb_pair in $CCWB_CONNECTOR_PAIRS; do
            case " $CCWB_HELPER_CONNECTORS " in
                *" ${_ccwb_pair%%=*} "*)
                    echo "       claude mcp add-json -s user ${_ccwb_pair%%=*} '{\"type\":\"http\",\"url\":\"${_ccwb_pair#*=}\",\"headersHelper\":\"$CCWB_HEADERS_HELPER\"}'" ;;
                *)
                    echo "       claude mcp add --transport http -s user ${_ccwb_pair%%=*} ${_ccwb_pair#*=}" ;;
            esac
        done
    fi
    if [ -n "$SUDO_USER" ]; then chown "$ACTUAL_USER" "$CCWB_CONNECTOR_STATE" 2>/dev/null || true; fi
    if [ -n "$CCWB_CONNECTORS" ]; then
        echo "  Sign in to each connector from Claude Code with /mcp on first use."
    fi
fi
"""
    )


def _ps_connectors_script(profile) -> str:
    desired = _desired(profile)
    pairs = "; ".join(f"'{n}' = '{u}'" for n, u in desired.items())
    helpers = ", ".join(f"'{n}'" for n in _helper_names(profile))
    cfg_dir = f"(Join-Path $env:USERPROFILE '{BEDROCK_CONFIG_DIR_NAME}')" if coexistence_enabled(profile) else "$null"
    return f"""\
# 'Continue': under 'Stop', PowerShell 5.1 turns native stderr into terminating errors.
$ErrorActionPreference = 'Continue'
try {{
    $desired = [ordered]@{{ {pairs} }}
    $cfgDir = {cfg_dir}
    $helperNames = @({helpers})
    $helperPath = Join-Path $env:USERPROFILE '{INSTALL_DIR_MARKER}\\{HELPER_FILE_NAME}.cmd'
    function Get-AddJson($u) {{
        $j = (@{{ type = 'http'; url = $u; headersHelper = $helperPath }} | ConvertTo-Json -Compress)
        # Windows PowerShell 5.1 (and pwsh < 7.3) drop embedded quotes when calling native programs.
        if ($PSVersionTable.PSVersion -lt [version]'7.3') {{ $j = $j -replace '"', '\\"' }}
        return $j
    }}
    $state = Join-Path $env:USERPROFILE '{INSTALL_DIR_MARKER}\\{STATE_FILE_NAME}'
    $prev = @()
    if (Test-Path $state) {{ $prev = @(Get-Content $state | Where-Object {{ $_.Trim() }}) }}
    if ($desired.Count -eq 0 -and $prev.Count -eq 0) {{ exit 0 }}
    Write-Host ''
    Write-Host 'Configuring MCP connectors for Claude Code...'
    if ($cfgDir) {{ New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null; $env:CLAUDE_CONFIG_DIR = $cfgDir }}
    $now = @()
    $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    if (-not $claude) {{
        $candidates = @()
        if ($env:USERPROFILE) {{ $candidates += (Join-Path $env:USERPROFILE '.local\\bin\\claude.exe') }}
        if ($env:APPDATA) {{ $candidates += (Join-Path $env:APPDATA 'npm\\claude.cmd') }}
        foreach ($c in $candidates) {{ if (Test-Path $c) {{ $claude = $c; break }} }}
    }}
    if ($claude) {{
        # State holds name=url pairs this installer added: keep unchanged ones
        # (users keep their vendor sign-in), remove stale ones.
        $pairs = @($desired.Keys | ForEach-Object {{ $_ + '=' + $desired[$_] }})
        foreach ($old in $prev) {{
            if ($pairs -notcontains $old) {{
                $oldName = $old.Split('=')[0]
                & $claude mcp remove -s user $oldName *> $null
                if (-not $desired.Contains($oldName)) {{ Write-Host ('  OK Removed MCP connector: ' + $oldName) }}
            }}
        }}
        foreach ($name in $desired.Keys) {{
            $pair = $name + '=' + $desired[$name]
            if ($prev -contains $pair) {{ Write-Host ('  OK MCP connector: ' + $name + ' (unchanged)'); $now += $pair; continue }}
            if ($helperNames -contains $name) {{ & $claude mcp add-json -s user $name (Get-AddJson $desired[$name]) *> $null }}
            else {{ & $claude mcp add --transport http -s user $name $desired[$name] *> $null }}
            if ($LASTEXITCODE -eq 0) {{ Write-Host ('  OK MCP connector: ' + $name + ' -> ' + $desired[$name]); $now += $pair }}
            else {{ Write-Host ('  SKIP ' + $name + ': could not add (a server with this name may already exist)') }}
        }}
    }} else {{
        Write-Host "  WARN 'claude' not found on PATH. Install Claude Code, then re-run install.bat"
        Write-Host '       or add the connectors manually:'
        foreach ($name in $desired.Keys) {{
            if ($helperNames -contains $name) {{ Write-Host ('       claude mcp add-json -s user ' + $name + " '" + (@{{ type = 'http'; url = $desired[$name]; headersHelper = $helperPath }} | ConvertTo-Json -Compress) + "'") }}
            else {{ Write-Host ('       claude mcp add --transport http -s user ' + $name + ' ' + $desired[$name]) }}
        }}
        exit 0
    }}
    Set-Content -Encoding UTF8 -Path $state -Value ($now -join [Environment]::NewLine)
    if ($desired.Count -gt 0) {{ Write-Host '  Sign in to each connector from Claude Code with /mcp on first use.' }}
}} catch {{
    Write-Host ('  WARN Could not configure MCP connectors: ' + $_)
    exit 0
}}
"""


# cmd.exe rejects command lines longer than 8191 characters.
_CMD_LINE_LIMIT = 8191


def _ps_gzip_command(script: str) -> str:
    """``powershell -Command`` line that gunzips and runs ``script``.

    Keeps install.bat lines far below cmd.exe's 8191-char limit (UTF-16
    ``-EncodedCommand`` nearly triples the size). Base64 contains no characters
    that cmd treats specially (``%``, ``!``, ``&``, ``"``).
    """
    import base64
    import gzip

    payload = base64.b64encode(gzip.compress(script.encode("utf-8"), mtime=0)).decode("ascii")
    stub = (
        "$s=New-Object IO.StreamReader((New-Object IO.Compression.GZipStream("
        f"[IO.MemoryStream][Convert]::FromBase64String('{payload}'),"
        "[IO.Compression.CompressionMode]::Decompress)),[Text.Encoding]::UTF8);"
        "Invoke-Expression $s.ReadToEnd()"
    )
    return f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{stub}"'


def windows_connectors_block(profile) -> str:
    """install.bat snippet (gzip+base64 PowerShell; avoids batch quoting and length limits)."""
    line = _ps_gzip_command(_ps_connectors_script(profile))
    if len(line) > _CMD_LINE_LIMIT:
        raise ConnectorConfigError(
            f"Too many Claude Code connectors for the Windows installer ({len(line)} > {_CMD_LINE_LIMIT} chars)"
        )
    return f"""
REM --- MCP connectors for Claude Code [user scope] ---
{line}
"""
