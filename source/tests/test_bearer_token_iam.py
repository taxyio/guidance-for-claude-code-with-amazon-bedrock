# ABOUTME: Guards the IAM shape of bedrock:CallWithBearerToken in auth templates.
# ABOUTME: Bedrock authorizes it against Resource '*', so a model-ARN scope denies it.

"""Regression: Claude Desktop helper-script mode got 403 on every call.

``bedrock:CallWithBearerToken`` is evaluated against ``Resource: '*'`` (the
error reads ``... on resource: *``). Granting it alongside InvokeModel on
foundation-model / inference-profile ARNs never matches, so the federated role
could mint a bearer token but Bedrock rejected every request made with it.
"""

import pytest

from tests.cfn_yaml import iter_templates, load_resolved

ACTION = "bedrock:CallWithBearerToken"

TEMPLATES = [p for p in iter_templates() if ACTION in p.read_text(encoding="utf-8")]


def _statements(template: dict):
    for resource in template.get("Resources", {}).values():
        if resource.get("Type") != "AWS::IAM::ManagedPolicy":
            continue
        for stmt in resource["Properties"]["PolicyDocument"]["Statement"]:
            if isinstance(stmt, dict):  # skip !If-wrapped optional statements
                yield stmt


def test_auth_templates_grant_bearer_token():
    names = {p.name for p in TEMPLATES}
    assert {"bedrock-auth-azure.yaml", "bedrock-auth-okta.yaml", "bedrock-auth-idc.yaml"} <= names


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_bearer_token_granted_on_wildcard_resource(path):
    grants = [s for s in _statements(load_resolved(path)) if ACTION in s.get("Action", [])]
    assert grants, f"{path.name}: no ManagedPolicy statement grants {ACTION}"
    for stmt in grants:
        assert stmt["Resource"] == "*", f"{path.name}: {stmt.get('Sid')} scopes {ACTION} to {stmt['Resource']}"
