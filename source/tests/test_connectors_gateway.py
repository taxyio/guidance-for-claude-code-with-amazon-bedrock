# ABOUTME: Tests gateway MCP connectors (Slack, HubSpot, Google, Taxy via AgentCore Gateway + consent portal).
# ABOUTME: Covers parsing/validation, client rendering, installer snippets, deploy/destroy orchestration, templates.

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from claude_code_with_bedrock.cli.commands.package import PackageCommand
from claude_code_with_bedrock.cli.utils.connectors import (
    ConnectorConfigError,
    add_connectors_mcp_config,
    bash_connectors_block,
    connectors_for,
    resolve_connectors,
    resolve_gateway_connectors,
    validate_connectors,
    windows_connectors_block,
)
from claude_code_with_bedrock.cli.utils.connectors_deploy import (
    build_gateway_params,
    build_target_params,
    deploy_connectors,
    destroy_connectors,
    load_tool_schema,
    portal_scopes,
    schema_key,
)
from claude_code_with_bedrock.cli.utils.cowork_3p import WEBSEARCH_HEADERS_HELPER_PLACEHOLDER
from claude_code_with_bedrock.config import Profile
from tests.cfn_yaml import load_intrinsics

REPO = Path(__file__).resolve().parents[2]
SECRET = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:ccwb-connectors/stack/slack-AbCdEf"
PORTAL_SECRET = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:ccwb-connectors/stack/portal-idp-AbCdEf"
TENANT = "11111111-2222-3333-4444-555555555555"
GW_URL = "https://gw-abc.gateway.bedrock-agentcore.eu-central-1.amazonaws.com/mcp"


def _gw(preset="google-drive", **kw):
    return {"via": "gateway", "preset": preset, "client_id": "cid-123", "client_secret_arn": SECRET, **kw}


def _profile(connectors, gateway=None, **kw) -> Profile:
    base = {
        "name": "p",
        "provider_domain": f"login.microsoftonline.com/{TENANT}/v2.0",
        "provider_type": "azure",
        "client_id": "app-client",
        "credential_storage": "session",
        "aws_region": "eu-central-1",
        "identity_pool_name": "pool",
        "allowed_bedrock_regions": ["eu-central-1"],
        "connectors": connectors,
        "connectors_gateway": gateway if gateway is not None else {"portal_client_secret_arn": PORTAL_SECRET},
    }
    base.update(kw)
    return Profile(**base)


def _quiet() -> Console:
    return Console(file=open(os.devnull, "w"))


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


class TestParsing:
    def test_old_profile_has_empty_gateway_settings(self):
        p = Profile.from_dict(
            {
                "name": "o",
                "provider_domain": "x.okta.com",
                "client_id": "c",
                "aws_region": "eu-central-1",
                "identity_pool_name": "i",
            }
        )
        assert p.connectors_gateway == {}
        assert resolve_gateway_connectors(p) == []

    def test_preset_fills_vendor_details(self):
        (c,) = resolve_gateway_connectors(_profile([_gw("google-sheets")]))
        assert c.name == "google-sheets"
        assert c.url == "https://sheetsmcp.googleapis.com/mcp/v1"
        assert c.discovery_url.endswith("/.well-known/openid-configuration")
        assert dict(c.auth_params) == {"access_type": "offline", "prompt": "consent"}
        assert c.tool_schema == "google-sheets.json"

    def test_slack_preset_uses_user_token_endpoints(self):
        (c,) = resolve_gateway_connectors(_profile([_gw("slack", tool_schema="/tmp/slack.json")]))
        assert c.authorization_endpoint == "https://slack.com/oauth/v2_user/authorize"
        assert c.token_endpoint == "https://slack.com/api/oauth.v2.user.access"
        assert c.discovery_url == ""

    def test_overrides_and_custom_name(self):
        (c,) = resolve_gateway_connectors(_profile([_gw("taxy-workspace", name="taxy", scopes="openid email")]))
        assert c.name == "taxy" and c.scopes == ("openid", "email")

    def test_direct_and_gateway_coexist(self):
        p = _profile(["stripe", _gw()])
        assert [c.name for c in resolve_connectors(p)] == ["stripe"]
        assert [c.name for c in resolve_gateway_connectors(p)] == ["google-drive"]

    @pytest.mark.parametrize(
        "override, match",
        [
            ({"preset": "nope"}, "Unknown gateway connector preset"),
            ({"client_id": ""}, "client_id"),
            (
                {"client_secret_arn": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:other/x"},
                "ccwb-connectors/",
            ),
            ({"name": "x" * 41}, "max 40"),
            ({"auth_params": {"redirect_uri": "https://evil"}}, "auth_params"),
            ({"scopes": ["has space"]}, "invalid scopes"),
            ({"surfaces": ["cowork"]}, "surfaces"),
            ({"client_auth_method": "PRIVATE_KEY_JWT"}, "client_auth_method"),
            ({"discovery_url": "https://x.example/oauth"}, "openid-configuration"),
        ],
    )
    def test_invalid_gateway_entries(self, override, match):
        with pytest.raises(ConnectorConfigError, match=match):
            resolve_gateway_connectors(_profile([_gw(**override)]))

    def test_custom_connector_requires_tool_schema(self):
        item = {
            "via": "gateway",
            "name": "acme",
            "url": "https://mcp.acme.example/mcp",
            "client_id": "c",
            "client_secret_arn": SECRET,
            "discovery_url": "https://login.acme.example/.well-known/openid-configuration",
        }
        with pytest.raises(ConnectorConfigError, match="tool_schema"):
            resolve_gateway_connectors(_profile([item]))

    def test_slack_preset_scopes_cover_bundled_tools(self):
        (c,) = resolve_gateway_connectors(_profile([_gw("slack")]))
        assert c.tool_schema == "slack.json"
        assert {"chat:write", "canvases:write", "lists:write", "files:write", "reactions:write"} <= set(c.scopes)

    def test_custom_gateway_connector_needs_auth_server(self):
        item = {
            "via": "gateway",
            "name": "acme",
            "url": "https://mcp.acme.example/mcp",
            "client_id": "c",
            "client_secret_arn": SECRET,
            "tool_schema": "/tmp/acme.json",
        }
        with pytest.raises(ConnectorConfigError, match="discovery_url"):
            resolve_gateway_connectors(_profile([item]))

    def test_gateway_preset_as_string_gives_hint(self):
        with pytest.raises(ConnectorConfigError, match="gateway presets need an object"):
            resolve_connectors(_profile(["slack"]))

    def test_server_name_is_reserved(self):
        with pytest.raises(ConnectorConfigError, match="reserved"):
            resolve_gateway_connectors(_profile([{"name": "connectors", "url": "https://x.example"}, _gw()]))

    def test_invalid_via(self):
        with pytest.raises(ConnectorConfigError, match="via"):
            resolve_connectors(_profile([{"via": "proxy", "name": "x", "url": "https://x.example"}]))


class TestValidation:
    def test_requires_oidc(self):
        problems = validate_connectors(_profile([_gw()], auth_type="idc"))
        assert any(sev == "error" and "OIDC" in m for sev, m in problems)

    def test_requires_portal_secret(self):
        problems = validate_connectors(_profile([_gw()], gateway={}))
        assert any(sev == "error" and "portal_client_secret_arn" in m for sev, m in problems)

    def test_warns_until_deployed(self):
        problems = validate_connectors(_profile([_gw()]))
        assert problems == [("warning", problems[0][1])] and "ccwb deploy connectors" in problems[0][1]

    def test_clean_when_deployed(self):
        assert (
            validate_connectors(
                _profile([_gw()], gateway={"portal_client_secret_arn": PORTAL_SECRET, "gateway_url": GW_URL})
            )
            == []
        )

    def test_direct_only_profiles_unaffected(self):
        assert validate_connectors(_profile(["stripe"], auth_type="idc", gateway={})) == []


# ---------------------------------------------------------------------------
# Client rendering
# ---------------------------------------------------------------------------


def _deployed(connectors, **kw):
    return _profile(connectors, gateway={"portal_client_secret_arn": PORTAL_SECRET, "gateway_url": GW_URL}, **kw)


class TestClients:
    def test_gateway_entry_only_after_deploy(self):
        assert [c.name for c in connectors_for(_profile([_gw()]), "cowork")] == []
        (c,) = connectors_for(_deployed([_gw()]), "cowork")
        assert (c.name, c.url, c.headers_helper) == ("connectors", GW_URL, True)

    def test_custom_server_name(self):
        p = _profile(
            [_gw()],
            gateway={"gateway_url": GW_URL, "server_name": "company-apps", "portal_client_secret_arn": PORTAL_SECRET},
        )
        assert [c.name for c in connectors_for(p, "claude-code")] == ["company-apps"]

    def test_cowork_gateway_entry_uses_id_token_helper(self):
        cfg = {}
        add_connectors_mcp_config(cfg, _deployed(["stripe", _gw()]), _quiet())
        servers = {s["name"]: s for s in json.loads(cfg["managedMcpServers"])}
        assert servers["stripe"]["oauth"] is True
        gw = servers["connectors"]
        assert gw["url"] == GW_URL and gw["headersHelper"] == WEBSEARCH_HEADERS_HELPER_PLACEHOLDER
        assert gw["headersHelperTtlSec"] > 0 and "oauth" not in gw

    def test_cowork_helper_override(self):
        cfg = {}
        add_connectors_mcp_config(cfg, _deployed([_gw()], websearch_headers_helper_path="/opt/h"), _quiet())
        assert json.loads(cfg["managedMcpServers"])[0]["headersHelper"] == "/opt/h"

    def test_installers_ship_helper_for_gateway_without_web_search(self, tmp_path):
        p = _deployed([_gw()], web_search_enabled=False)
        (tmp_path / "sh").mkdir()
        (tmp_path / "bat").mkdir()
        sh = PackageCommand()._create_installer(tmp_path / "sh", p, [("linux", Path("/tmp/x"))])
        assert "Installing web search headersHelper" in sh.read_text(encoding="utf-8")
        bat = PackageCommand()._create_windows_installer(tmp_path / "bat", p).read_text(encoding="utf-8")
        assert "websearch-headers.cmd" in bat

    def test_no_helper_without_gateway_or_web_search(self, tmp_path):
        sh = PackageCommand()._create_installer(tmp_path, _profile(["stripe"]), [("linux", Path("/tmp/x"))])
        assert "Installing web search headersHelper" not in sh.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Installer snippets (fake claude)
# ---------------------------------------------------------------------------

FAKE_CLAUDE_PY = """import json, os, sys
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
"""


def _env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    (home / "claude-code-with-bedrock").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "fake_claude.py").write_text(FAKE_CLAUDE_PY)
    sh = bindir / "claude"
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bindir / "fake_claude.py"}" "$@"\n')
    sh.chmod(0o755)
    (bindir / "claude.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{bindir / "fake_claude.py"}" %*\r\n')
    env = {k: v for k, v in os.environ.items() if k not in ("SUDO_USER", "CLAUDE_CONFIG_DIR")}
    env["PATH"] = os.pathsep.join(
        [str(bindir)] + [d for d in env.get("PATH", "").split(os.pathsep) if not Path(d, "claude").exists()]
    )
    env["FAKE_LOG"] = str(tmp_path / "calls.log")
    env["HOME"] = env["USERPROFILE"] = str(home)
    return env


def _calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()]


def _gateway_json(call: list[str]) -> dict:
    # Windows PowerShell 5.1 escapes embedded quotes for native programs; a .cmd shim sees them raw.
    return json.loads(call[-1].replace('\\"', '"'))


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="bash installer runs on macOS/Linux")
def test_bash_adds_gateway_with_headers_helper(tmp_path):
    env = _env(tmp_path)
    script = 'ACTUAL_HOME="$HOME"; ACTUAL_USER="$USER"\n' + bash_connectors_block(_deployed(["stripe", _gw()]))
    r = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    calls = _calls(tmp_path)
    assert calls[0] == ["mcp", "add", "--transport", "http", "-s", "user", "stripe", "https://mcp.stripe.com"]
    assert calls[1][:5] == ["mcp", "add-json", "-s", "user", "connectors"]
    assert _gateway_json(calls[1]) == {
        "type": "http",
        "url": GW_URL,
        "headersHelper": str(tmp_path / "home" / "claude-code-with-bedrock" / "websearch-headers"),
    }


def _powershell():
    return shutil.which("powershell") if sys.platform == "win32" else shutil.which("pwsh")


@pytest.mark.skipif(_powershell() is None, reason="PowerShell not available")
def test_powershell_adds_gateway_with_headers_helper(tmp_path):
    import re

    env = _env(tmp_path)
    line = next(x for x in windows_connectors_block(_deployed([_gw()])).splitlines() if " -Command " in x)
    command = re.search(r'-Command "(.*)"$', line).group(1)
    r = subprocess.run(
        [_powershell(), "-NoProfile", "-Command", command], env=env, capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stdout + r.stderr
    (call,) = _calls(tmp_path)
    assert call[:5] == ["mcp", "add-json", "-s", "user", "connectors"]
    cfg = _gateway_json(call)
    assert cfg["url"] == GW_URL and cfg["headersHelper"].endswith("websearch-headers.cmd")


# ---------------------------------------------------------------------------
# Deploy orchestration
# ---------------------------------------------------------------------------


class TestDeployHelpers:
    def test_portal_scopes_entra_includes_api_scope(self):
        assert portal_scopes(_profile([])) == ["openid", "profile", "email", "api://app-client/connectors.access"]
        assert portal_scopes(_profile([], provider_type="okta", provider_domain="x.okta.com")) == [
            "openid",
            "profile",
            "email",
        ]
        assert portal_scopes(_profile([], gateway={"portal_scopes": ["openid"]})) == ["openid"]

    def test_gateway_params(self):
        params = {p["ParameterKey"]: p["ParameterValue"] for p in build_gateway_params(_profile([_gw()]))}
        assert (
            params["DiscoveryUrl"]
            == f"https://login.microsoftonline.com/{TENANT}/v2.0/.well-known/openid-configuration"
        )
        assert params["ClientId"] == "app-client"
        assert params["PortalClientSecretArn"] == PORTAL_SECRET
        assert params["PortalScopes"].endswith("api://app-client/connectors.access")

    def test_target_params_google(self):
        (c,) = resolve_gateway_connectors(_profile([_gw("google-drive")]))
        params = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in build_target_params(c, {"GatewayId": "gw", "PortalUrl": "https://portal/"}, "s3://b/k.json")
        }
        assert params["PortalUrl"] == "https://portal"
        assert params["AccessTypeParam"] == "offline" and params["PromptParam"] == "consent"
        assert params["ResourceParam"] == "" and params["Issuer"] == ""
        assert params["Scopes"] == "https://www.googleapis.com/auth/drive"

    def test_target_params_match_template(self):
        template = load_intrinsics(REPO / "deployment/infrastructure/connectors-target.yaml")
        (c,) = resolve_gateway_connectors(_profile([_gw("taxy-workspace")]))
        keys = {
            p["ParameterKey"]
            for p in build_target_params(c, {"GatewayId": "g", "PortalUrl": "https://p"}, "s3://b/k.json")
        }
        assert keys == set(template["Parameters"])

    def test_gateway_params_match_template(self):
        template = load_intrinsics(REPO / "deployment/infrastructure/connectors-gateway.yaml")
        keys = {p["ParameterKey"] for p in build_gateway_params(_profile([_gw()]))}
        required = {k for k, v in template["Parameters"].items() if "Default" not in v}
        assert required <= keys <= set(template["Parameters"])

    @pytest.mark.parametrize(
        "preset", ["google-drive", "google-docs", "google-sheets", "taxy-workspace", "slack", "hubspot"]
    )
    def test_bundled_schemas_load(self, preset):
        (c,) = resolve_gateway_connectors(_profile([_gw(preset)]))
        data = load_tool_schema(c, REPO)
        assert json.loads(data)["tools"]

    def test_schema_key_changes_with_content(self):
        (c,) = resolve_gateway_connectors(_profile([_gw()]))
        assert schema_key(c, b"a") != schema_key(c, b"b")
        assert schema_key(c, b"a").startswith("tool-schemas/google-drive-")

    def test_invalid_schema_rejected(self, tmp_path):
        bad = tmp_path / "s.json"
        bad.write_text('{"tools": []}')
        (c,) = resolve_gateway_connectors(_profile([_gw("slack", tool_schema=str(bad))]))
        with pytest.raises(ConnectorConfigError, match="named tools"):
            load_tool_schema(c, REPO)


class FakeCF:
    def __init__(self):
        self.deployed, self.deleted, self.puts = [], [], []
        s3 = SimpleNamespace(put_object=lambda **kw: self.puts.append(kw["Key"]))
        self.session = SimpleNamespace(client=lambda name: s3)

    def deploy_stack(self, stack_name, template_path, parameters=None, **kw):
        self.deployed.append(
            (stack_name, Path(template_path).name, {p["ParameterKey"]: p["ParameterValue"] for p in parameters})
        )
        return SimpleNamespace(success=True, error=None)

    def get_stack_outputs(self, stack_name):
        if stack_name.endswith("-connectors"):
            return {
                "GatewayMcpEndpoint": GW_URL,
                "GatewayId": "gw-abc",
                "PortalUrl": "https://portal.example",
                "PortalCallbackUrl": "https://portal.example/callback",
                "ToolSchemaBucket": "bucket",
            }
        return {"CallbackUrl": f"https://cb/{stack_name}"}

    def delete_stack(self, stack_name):
        self.deleted.append(stack_name)
        return SimpleNamespace(success=True)


class TestDeployOrchestration:
    def test_deploys_gateway_then_targets_and_saves(self):
        p = _profile(
            [_gw("google-drive"), _gw("google-docs")],
            gateway={"portal_client_secret_arn": PORTAL_SECRET, "deployed": ["old"]},
        )
        cf, saved = FakeCF(), []
        rc = deploy_connectors(p, _quiet(), REPO, cf=cf, save_profile=saved.append)
        assert rc == 0
        assert [d[0] for d in cf.deployed] == [
            "pool-connectors",
            "pool-connectors-google-drive",
            "pool-connectors-google-docs",
        ]
        assert cf.deployed[1][1] == "connectors-target.yaml"
        assert cf.deployed[1][2]["ToolSchemaS3Uri"].startswith("s3://bucket/tool-schemas/google-drive-")
        assert len(cf.puts) == 2
        assert cf.deleted == ["pool-connectors-old"]
        assert p.connectors_gateway["gateway_url"] == GW_URL
        assert p.connectors_gateway["deployed"] == ["google-docs", "google-drive"]
        assert saved

    def test_refuses_invalid_profile(self):
        cf = FakeCF()
        assert deploy_connectors(_profile([_gw()], auth_type="idc"), _quiet(), REPO, cf=cf) == 1
        assert cf.deployed == []

    def test_destroy_deletes_targets_then_gateway(self):
        p = _deployed([_gw()])
        p.connectors_gateway["deployed"] = ["google-drive", "removed"]
        deleted = []

        class Sess:
            def client(self, name):
                if name == "cloudformation":
                    return SimpleNamespace(describe_stacks=lambda StackName: {"Stacks": [{"Outputs": []}]})
                raise AssertionError(name)

        rc = destroy_connectors(p, _quiet(), lambda s, r, c: deleted.append(s) or 0, session=Sess())
        assert rc == 0
        assert deleted == ["pool-connectors-google-drive", "pool-connectors-removed", "pool-connectors"]
        assert "gateway_url" not in p.connectors_gateway


# ---------------------------------------------------------------------------
# Template contracts
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_gateway_template_security(self):
        t = load_intrinsics(REPO / "deployment/infrastructure/connectors-gateway.yaml")
        r = t["Resources"]
        gw = r["Gateway"]["Properties"]
        assert gw["AuthorizerType"] == "CUSTOM_JWT"
        assert "2025-11-25" in gw["ProtocolConfiguration"]["Mcp"]["SupportedVersions"]  # URL elicitation
        idp = r["PortalIdpProvider"]["Properties"]["Oauth2ProviderConfigInput"]["CustomOauth2ProviderConfig"]
        assert idp["ClientSecretSource"] == "EXTERNAL" and "ClientSecret" not in idp
        assert r["ConsentPortal"]["Properties"]["Sources"][0]["Type"] == "agentcore-gateway"
        assert r["ToolSchemaBucket"]["Properties"]["PublicAccessBlockConfiguration"]["BlockPublicPolicy"] is True

    def test_target_template_per_user_oauth(self):
        t = load_intrinsics(REPO / "deployment/infrastructure/connectors-target.yaml")
        r = t["Resources"]
        cp = r["CredentialProvider"]["Properties"]["Oauth2ProviderConfigInput"]["CustomOauth2ProviderConfig"]
        assert cp["ClientSecretSource"] == "EXTERNAL" and "ClientSecret" not in cp
        oauth = r["Target"]["Properties"]["CredentialProviderConfigurations"][0]["CredentialProvider"][
            "OauthCredentialProvider"
        ]
        assert oauth["GrantType"] == "AUTHORIZATION_CODE"
        assert oauth["DefaultReturnUrl"] == {"Fn::Sub": "${PortalUrl}/connect/callback"}
        assert "McpToolSchema" in r["Target"]["Properties"]["TargetConfiguration"]["Mcp"]["McpServer"]
        assert t["Parameters"]["ClientSecretArn"]["AllowedPattern"].endswith("secret:ccwb-connectors/.+$")


@pytest.mark.parametrize("schema", sorted((REPO / "deployment/infrastructure/connector-tool-schemas").glob("*.json")))
def test_bundled_schemas_are_not_personalised(schema):
    """Vendors may personalise tool descriptions for the capturing user (Slack embeds the
    signed-in user's ID). A shared schema must not, or every user's Claude acts as that
    person (e.g. "message me" DMs the admin). Capture with fetch-mcp-tools.py --redact."""
    import re

    text = schema.read_text(encoding="utf-8")
    personal = re.findall(r"(?:logged in user'?s (?:Slack )?user_id is|Your user_id:)\s*U[0-9A-Z]{6,}", text)
    assert not personal, f"{schema.name} names the capturing user: {personal}"
