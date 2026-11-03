# ABOUTME: Deploys/destroys the AgentCore Gateway for MCP connectors (connectors-gateway.yaml + one
# ABOUTME: connectors-target.yaml stack per gateway connector) and records outputs on the profile.

"""``ccwb deploy connectors`` / ``ccwb destroy connectors``.

Stacks (region: ``connectors_gateway.region`` or the profile's ``aws_region``):

* ``<pool>-connectors`` — gateway, consent portal, roles, tool-schema bucket.
* ``<pool>-connectors-<name>`` — one per gateway connector: outbound OAuth2
  credential provider (secret read from Secrets Manager) + gateway target with
  the tool schema from S3 (supplied up front, so no admin sign-in).

Tool schemas are uploaded under a content-hash key so a changed schema updates
the target. Connector stacks removed from the profile are deleted.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from claude_code_with_bedrock.cli.utils.connectors import (
    AUTH_PARAM_KEYS,
    ConnectorConfigError,
    GatewayConnector,
    gateway_settings,
    resolve_gateway_connectors,
    validate_connectors,
)

BUNDLED_SCHEMA_DIR = ("deployment", "infrastructure", "connector-tool-schemas")
GATEWAY_TEMPLATE = ("deployment", "infrastructure", "connectors-gateway.yaml")
TARGET_TEMPLATE = ("deployment", "infrastructure", "connectors-target.yaml")


def gateway_region(profile) -> str:
    return str(gateway_settings(profile).get("region") or profile.aws_region)


def base_stack_name(profile) -> str:
    return profile.stack_names.get("connectors", f"{profile.identity_pool_name}-connectors")


def target_stack_name(profile, connector_name: str) -> str:
    return f"{base_stack_name(profile)}-{connector_name}"


def portal_scopes(profile) -> list[str]:
    scopes = gateway_settings(profile).get("portal_scopes")
    if scopes:
        return list(scopes)
    # Entra ID only issues a JWT access token for the app's own API scope.
    if getattr(profile, "provider_type", None) == "azure":
        return ["openid", "profile", "email", f"api://{profile.client_id}/connectors.access"]
    return ["openid", "profile", "email"]


def build_gateway_params(profile) -> list[dict]:
    from claude_code_with_bedrock.cli.commands.deploy import _websearch_discovery_url

    return [
        {"ParameterKey": "DiscoveryUrl", "ParameterValue": _websearch_discovery_url(profile)},
        {"ParameterKey": "ClientId", "ParameterValue": profile.client_id},
        {
            "ParameterKey": "PortalClientSecretArn",
            "ParameterValue": gateway_settings(profile)["portal_client_secret_arn"],
        },
        {"ParameterKey": "PortalScopes", "ParameterValue": ",".join(portal_scopes(profile))},
    ]


def load_tool_schema(connector: GatewayConnector, project_root: Path) -> bytes:
    """Return the schema file bytes after validating ``{"tools": [...]}``."""
    path = Path(connector.tool_schema).expanduser()
    if not path.is_absolute() and len(path.parts) == 1:
        bundled = project_root.joinpath(*BUNDLED_SCHEMA_DIR, connector.tool_schema)
        path = bundled if bundled.exists() else path
    if not path.exists():
        raise ConnectorConfigError(f"Tool schema for '{connector.name}' not found: {connector.tool_schema}")
    data = path.read_bytes()
    try:
        parsed = json.loads(data)
    except ValueError as e:
        raise ConnectorConfigError(f"Tool schema for '{connector.name}' is not valid JSON: {e}") from e
    tools = parsed.get("tools") if isinstance(parsed, dict) else None
    if not isinstance(tools, list) or not tools or not all(isinstance(t, dict) and t.get("name") for t in tools):
        raise ConnectorConfigError(f"Tool schema for '{connector.name}' must be {{\"tools\": [...]}} with named tools")
    return data


def schema_key(connector: GatewayConnector, data: bytes) -> str:
    return f"tool-schemas/{connector.name}-{hashlib.sha256(data).hexdigest()[:12]}.json"


def build_target_params(connector: GatewayConnector, gateway_outputs: dict, schema_uri: str) -> list[dict]:
    values = {
        "GatewayIdentifier": gateway_outputs["GatewayId"],
        "PortalUrl": gateway_outputs["PortalUrl"].rstrip("/"),
        "ToolSchemaS3Uri": schema_uri,
        "ConnectorName": connector.name,
        "ConnectorDescription": connector.description,
        "McpEndpoint": connector.url,
        "ClientId": connector.client_id,
        "ClientSecretArn": connector.client_secret_arn,
        "ClientSecretJsonKey": "client_secret",
        "ClientAuthenticationMethod": connector.client_auth_method,
        "DiscoveryUrl": connector.discovery_url,
        "Issuer": connector.issuer,
        "AuthorizationEndpoint": connector.authorization_endpoint,
        "TokenEndpoint": connector.token_endpoint,
        "Scopes": ",".join(connector.scopes),
    }
    params = dict(connector.auth_params)
    for key, param_name in AUTH_PARAM_KEYS.items():
        values[param_name] = params.get(key, "")
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in values.items()]


def deploy_connectors(profile, console, project_root: Path, cf=None, save_profile=None) -> int:
    """Deploy the gateway stack and one stack per gateway connector. Returns 0 on success."""
    from claude_code_with_bedrock.cli.utils.cloudformation import CloudFormationManager

    errors = [m for sev, m in validate_connectors(profile) if sev == "error"]
    if errors:
        for m in errors:
            console.print(f"[red]{m}[/red]")
        return 1
    connectors = resolve_gateway_connectors(profile)
    if not connectors:
        console.print('[yellow]No gateway connectors configured (connectors items with "via": "gateway").[/yellow]')
        return 1
    try:
        schemas = {c.name: load_tool_schema(c, project_root) for c in connectors}
    except ConnectorConfigError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    region = gateway_region(profile)
    cf = cf or CloudFormationManager(region=region)
    tags = profile.tags if getattr(profile, "tags", None) else None

    base = base_stack_name(profile)
    console.print(f"[dim]Deploying connectors gateway stack {base} in {region}...[/dim]")
    result = cf.deploy_stack(
        stack_name=base,
        template_path=project_root.joinpath(*GATEWAY_TEMPLATE),
        parameters=build_gateway_params(profile),
        capabilities=["CAPABILITY_IAM"],
        tags=tags,
    )
    if not result.success:
        console.print(f"[red]✗ Connectors gateway stack failed: {result.error}[/red]")
        return 1
    outputs = cf.get_stack_outputs(base)

    settings = dict(gateway_settings(profile))
    settings["gateway_url"] = outputs["GatewayMcpEndpoint"]
    settings["portal_url"] = outputs["PortalUrl"].rstrip("/")
    profile.connectors_gateway = settings
    if save_profile:
        save_profile(profile)
    console.print(f"[green]✓ Connectors gateway: {settings['gateway_url']}[/green]")

    s3 = cf.session.client("s3")
    bucket = outputs["ToolSchemaBucket"]
    callbacks: dict[str, str] = {}
    failed = False
    for c in connectors:
        key = schema_key(c, schemas[c.name])
        s3.put_object(Bucket=bucket, Key=key, Body=schemas[c.name], ContentType="application/json")
        stack = target_stack_name(profile, c.name)
        console.print(f"[dim]Deploying connector {c.name} ({stack})...[/dim]")
        r = cf.deploy_stack(
            stack_name=stack,
            template_path=project_root.joinpath(*TARGET_TEMPLATE),
            parameters=build_target_params(c, outputs, f"s3://{bucket}/{key}"),
            tags=tags,
        )
        if not r.success:
            console.print(f"[red]✗ Connector {c.name} failed: {r.error}[/red]")
            failed = True
            continue
        callbacks[c.name] = cf.get_stack_outputs(stack).get("CallbackUrl", "")
        console.print(f"[green]✓ Connector {c.name}[/green]")

    # Remove connector stacks that are no longer configured.
    wanted = {c.name for c in connectors}
    for old in settings.get("deployed", []) or []:
        if old not in wanted:
            console.print(f"[dim]Removing connector {old}...[/dim]")
            if not cf.delete_stack(target_stack_name(profile, old)).success:
                console.print(f"[yellow]⚠ Could not delete connector stack for {old}[/yellow]")
                failed = True

    settings["deployed"] = sorted(n for n in wanted if n in callbacks) + sorted(
        n for n in settings.get("deployed", []) or [] if n in wanted and n not in callbacks
    )
    profile.connectors_gateway = settings
    if save_profile:
        save_profile(profile)

    console.print("\n[bold]Register these redirect URIs:[/bold]")
    console.print(f"  OIDC app (consent portal sign-in): {outputs['PortalCallbackUrl']}")
    for name, url in callbacks.items():
        console.print(f"  {name} OAuth app: {url}")
    console.print(f"\n[bold]Consent portal:[/bold] {settings['portal_url']}")
    console.print("[dim]Users can connect accounts there up front, or on first use from Claude.[/dim]")
    console.print("[dim]Run 'ccwb package' to distribute the gateway to Claude Code and Claude Desktop.[/dim]")
    return 1 if failed else 0


def destroy_connectors(profile, console, delete_stack, region: str | None = None, session=None) -> int:
    """Delete connector stacks, empty the schema bucket, then delete the gateway stack.

    ``delete_stack(stack_name, region, console) -> int`` is the destroy command's deleter.
    """
    import boto3

    region = region or gateway_region(profile)
    settings = dict(gateway_settings(profile))
    names = set(settings.get("deployed", []) or [])
    try:
        names |= {c.name for c in resolve_gateway_connectors(profile)}
    except ConnectorConfigError:
        pass
    rc = 0
    for name in sorted(names):
        console.print(f"Destroying connector stack: [cyan]{target_stack_name(profile, name)}[/cyan]")
        rc |= delete_stack(target_stack_name(profile, name), region, console)

    session = session or boto3.Session(region_name=region)
    base = base_stack_name(profile)
    try:
        outputs = {
            o["OutputKey"]: o["OutputValue"]
            for o in session.client("cloudformation").describe_stacks(StackName=base)["Stacks"][0].get("Outputs", [])
        }
    except Exception:
        outputs = {}
    bucket = outputs.get("ToolSchemaBucket")
    if bucket:
        s3 = session.client("s3")
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            objs = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs, "Quiet": True})
    console.print(f"Destroying connectors gateway stack: [cyan]{base}[/cyan]")
    rc |= delete_stack(base, region, console)
    if rc == 0:
        for k in ("gateway_url", "portal_url", "deployed"):
            settings.pop(k, None)
        profile.connectors_gateway = settings
    return rc
