# ABOUTME: Regression test ensuring re-running init preserves the Claude Code model alias
# ABOUTME: Guards _check_existing_deployment against dropping model_alias (e.g. "haiku")

"""Regression: `model_alias` was saved by `_save_configuration` (from
config["aws"]["model_alias"]) but never restored by `_check_existing_deployment`,
and no wizard step sets it. Re-running `ccwb init` therefore reset it to None,
silently switching claude-bedrock's default model back to the selected_model tier.
"""

from unittest.mock import patch

from claude_code_with_bedrock.cli.commands.init import InitCommand
from claude_code_with_bedrock.config import Config, Profile


def _rebuild(profile: Profile) -> dict:
    command = InitCommand()
    fake_config = Config()
    with (
        patch.object(Config, "load", return_value=fake_config),
        patch.object(fake_config, "get_profile", return_value=profile),
        patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
    ):
        return command._check_existing_deployment("test")


def _profile(**kw) -> Profile:
    return Profile(
        name="test",
        provider_domain="example.okta.com",
        client_id="client",
        identity_pool_name="pool",
        credential_storage="keyring",
        aws_region="eu-central-1",
        selected_model="eu.anthropic.claude-sonnet-5",
        **kw,
    )


def test_rerun_preserves_model_alias():
    assert _rebuild(_profile(model_alias="haiku"))["aws"]["model_alias"] == "haiku"


def test_no_alias_stays_unset():
    assert "model_alias" not in _rebuild(_profile())["aws"]
