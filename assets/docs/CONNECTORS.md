# MCP Connectors

Distribute MCP connectors (Stripe, Taxy Workspace, Slack, HubSpot, Google Drive/Docs/Sheets, ...) to Claude Code and Claude Cowork. **Every user signs in to the vendor with their own account**, so the vendor enforces that user's own permissions. Nothing is shared between users, and no secret ever reaches a user's device.

There are two kinds of connector:

| | Direct | Gateway |
|---|---|---|
| For | Vendor MCP servers with OAuth **Dynamic Client Registration** (Stripe, Taxy Workspace, Atlassian, Amplitude) | Vendor MCP servers that need a pre-registered OAuth app **with a client secret** (Slack, HubSpot, Google) |
| Infrastructure | None | Amazon Bedrock AgentCore Gateway + consent portal (`ccwb deploy connectors`) |
| Where user tokens live | On the device (Claude Code / Claude Desktop) | AgentCore Identity token vault (per user) |
| Client secrets | None | AWS Secrets Manager only |
| Auth modes | OIDC, IDC, none | OIDC only |

## Direct connectors

Add presets or custom servers to the profile (`~/.ccwb/profiles/<profile>.json`), then run `ccwb package`:

```json
"connectors": [
  "stripe",
  "taxy-workspace",
  {"name": "acme", "url": "https://mcp.acme.example/mcp", "surfaces": ["cowork"]}
]
```

| Preset | URL |
|--------|-----|
| `stripe` | `https://mcp.stripe.com` |
| `taxy-workspace` | `https://mcp.workspace.taxy.io/mcp` |
| `atlassian` | `https://mcp.atlassian.com/v1/mcp` (Jira, Confluence) |
| `amplitude` | `https://mcp.amplitude.com/mcp` (US data center) |
| `amplitude-eu` | `https://mcp.eu.amplitude.com/mcp` (EU data center) |

Custom entries need a `name` (lowercase letters, digits, `-`) and an `https://` `url`. `surfaces` is optional and defaults to `["claude-code", "cowork"]`. Only add servers that support Dynamic Client Registration. To check, look for `registration_endpoint` in the vendor's `/.well-known/oauth-authorization-server` metadata.

**User experience:**
- **Cowork:** the connector appears automatically. The user signs in when it is first used.
- **Claude Code:** run `/mcp`, select the connector, and sign in.

## Gateway connectors

```
Claude Code / Desktop ──id_token (headersHelper)──▶ AgentCore Gateway (eu-central-1, CUSTOM_JWT)
                                                     ├─ target "slack"  ──user's Slack token──▶ mcp.slack.com
                                                     ├─ target "google-drive" ──user's Google token──▶ drivemcp.googleapis.com
                                                     └─ ...
First use of a target → MCP URL elicitation → user signs in at the vendor → consent portal binds it to the user's OIDC identity
```

How it works:
- Users reach the gateway with the same OIDC id_token that `credential-process` already caches, via the `websearch-headers` helper. They do not sign in to the gateway itself.
- All gateway connectors appear to clients as **one** MCP server named `connectors`. Tools are named `<connector>___<tool>`, for example `slack___search_messages`.
- The first time a user calls a connector they haven't connected, the gateway returns a standard MCP **URL elicitation** (`-32042`). Claude Code shows the link and retries the call once the user confirms the dialog.
- **Users must return to Claude Code and confirm the dialog after signing in.** The gateway does not send `notifications/elicitation/complete`, so nothing tells Claude Code the sign-in has finished, and the browser tab ends on the consent portal. Until the user confirms, the call looks stuck.
- Users can also connect all their accounts up front on the **consent portal** page (recommended in onboarding). This avoids the dialog entirely: calls then succeed on first use.

### Prerequisites: the OIDC app (one time)

The consent portal signs users in with the **same OIDC app** as `credential-process`. This guarantees the user identity matches what the gateway sees. The app needs:

1. **A client secret**, stored in Secrets Manager under the `ccwb-connectors/` prefix. Write it from a file so it doesn't land in your shell history:
   ```bash
   aws secretsmanager create-secret --region <region> \
     --name ccwb-connectors/<pool>-connectors/portal-idp \
     --secret-string file://portal-secret.json   # {"client_secret": "..."}
   ```
2. **JWT access tokens** for the portal's scopes:
   - **Entra ID:** under *Expose an API*, set the Application ID URI `api://<client-id>` and add a scope named `connectors.access`. In the manifest, set `"requestedAccessTokenVersion": 2`. `ccwb` requests `api://<client-id>/connectors.access` automatically.
   - **Okta and Cognito:** access tokens are already JWTs. If needed, set `connectors_gateway.portal_scopes`.
3. **The portal redirect URI** `<PortalUrl>/callback` as a web redirect. `ccwb deploy connectors` prints it after the first deploy.

### Configure

```json
"connectors_gateway": {
  "portal_client_secret_arn": "arn:aws:secretsmanager:<region>:<account>:secret:ccwb-connectors/<pool>-connectors/portal-idp-AbCdEf"
},
"connectors": [
  {"via": "gateway", "preset": "slack", "client_id": "<slack-client-id>",
   "client_secret_arn": "arn:aws:secretsmanager:...:secret:ccwb-connectors/<pool>-connectors/slack-...",
   "tool_schema": "~/ccwb/slack-tools.json"},
  {"via": "gateway", "preset": "google-drive", "client_id": "<google-client-id>",
   "client_secret_arn": "arn:aws:secretsmanager:...:secret:ccwb-connectors/<pool>-connectors/google-..."}
]
```

`connectors_gateway` keys:

| Key | Required | Description |
|---|---|---|
| `portal_client_secret_arn` | yes | OIDC app client secret (JSON key `client_secret`) |
| `region` | no | Gateway region (default: the profile's `aws_region`) |
| `portal_scopes` | no | Override the portal scopes |
| `server_name` | no | Client-side MCP server name (default `connectors`) |
| `gateway_url`, `portal_url`, `deployed` | — | Written by `ccwb deploy connectors` |

Gateway connector keys:
- `preset`, `name`, `client_id`, `client_secret_arn` (a secret under `ccwb-connectors/`, JSON key `client_secret`), `tool_schema`.
- For custom vendors: `url`, plus either `discovery_url` **or** `issuer` + `authorization_endpoint` + `token_endpoint`.
- Optional: `scopes`, `auth_params` (`resource`, `audience`, `access_type`, `prompt`), `description`, `client_auth_method`.

### Deploy

1. `ccwb deploy connectors` deploys `<pool>-connectors` (the gateway, consent portal and tool-schema bucket) and one `<pool>-connectors-<name>` stack per connector. It prints the redirect URIs to register:
   - `<PortalUrl>/callback` on the OIDC app
   - each connector's `CallbackUrl` on the vendor's OAuth app
2. Register those URIs, then run `ccwb package`. The Cowork config gets a `connectors` entry, and the installers add it to Claude Code at user scope.

Re-running `ccwb deploy connectors` updates changed connectors and removes stacks for connectors you deleted from the profile. `ccwb destroy connectors` removes everything.

### Tool schemas

With per-user OAuth, the gateway cannot list a vendor's tools by itself. Each target is therefore created with its tool list up front, stored in S3. As a result, creating or updating a connector needs no admin sign-in.

- **Bundled:** `slack`, `hubspot`, `google-drive`, `google-docs`, `google-sheets` and `taxy-workspace`, in `deployment/infrastructure/connector-tool-schemas/`.
- **Captured:** for other vendors, use a user token you already have:
  ```bash
  scripts/fetch-mcp-tools.py <mcp-url> slack-tools.json --token <user-access-token>
  ```
  Or let the script sign you in through the vendor app. Temporarily add `http://127.0.0.1:8765/callback` as a redirect URI on the app, and keep the client secret in a file:
  ```bash
  scripts/fetch-mcp-tools.py https://mcp.hubspot.com hubspot-tools.json --oauth \
    --authorize-url https://mcp.hubspot.com/oauth/authorize/user \
    --token-url https://mcp.hubspot.com/oauth/v3/token \
    --client-id <client-id> --client-secret-file ./hubspot-secret.txt
  ```
- **Redact personal details.** Some vendors write the signed-in user into tool descriptions; Slack, for example, adds "the current logged in user's user_id is U…". The schema is shared by every user, so pass `--redact <your-user-id>` when capturing. Otherwise every user's Claude believes it is you, and "message me" would DM you. A test fails if a bundled schema names a user.
- Re-capture when a vendor adds tools, then re-run `ccwb deploy connectors`.

### Vendor setup

| Preset | OAuth app | Tool schema |
|---|---|---|
| `google-drive` / `google-docs` / `google-sheets` | Google Cloud: OAuth consent screen **Internal**, client type **Web application**, one redirect URI per connector `CallbackUrl`. One client can serve all three; they can share the same secret ARN. | Bundled |
| `slack` | api.slack.com/apps: create an app; under *Agents*, turn on **Slack Model Context Protocol (MCP) Server**; under *OAuth & Permissions*, add the preset's **User Token Scopes** (all 30 scopes Slack's MCP server supports) and the `CallbackUrl` as a redirect URL. | Bundled (27 tools, read and write). For read-only, override `scopes` and pass a `tool_schema` without the write tools |
| `hubspot` | HubSpot developer account: create an **MCP auth app** with the `CallbackUrl` as its redirect URL. Scopes are configured on the app. | Bundled (30 tools) |
| `taxy-workspace` (gateway variant) | Register a client via Dynamic Client Registration (`POST https://login.taxy.io/oidc/register`) with the `CallbackUrl` as its redirect URI | Bundled |

If a vendor reports that the redirect URL doesn't match right after you registered it, request a **new** sign-in link: links created before the change was saved (or before the vendor applied it) keep failing. This happened with HubSpot during testing.

Credential providers are created with the `client_id` you configure. If a vendor only issues a client after you register the redirect URI, deploy with a placeholder `client_id` and a placeholder secret, register the printed `CallbackUrl`, then update the secret and `client_id` and redeploy. The `CallbackUrl` stays the same across updates.

## Installer behavior (Claude Code)

- Connectors are added at **user scope**: `claude mcp add`, or `claude mcp add-json` with `headersHelper` for the gateway. Users can still add their own servers.
- Side-by-side installs target `~/.claude-bedrock`.
- Servers the user already configured under the same name are never overwritten (the installer prints `SKIP`).
- The installer records what it added in `~/claude-code-with-bedrock/connectors-managed.txt`. When you re-run it:
  - unchanged connectors are left alone, so users stay signed in
  - connectors with a changed URL are replaced
  - connectors removed from the profile are removed
- If `claude` is not on `PATH`, the installer prints the commands instead. Install Claude Code and re-run the installer.
