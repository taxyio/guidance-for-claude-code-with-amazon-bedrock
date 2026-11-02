# ABOUTME: Regression test: 'ccwb cowork generate' must emit the same Cowork MDM keys as 'ccwb package'.
# ABOUTME: It used to drop the beta feature keys (chatTabEnabled, ...), silently disabling the Chat tab.

import json
from unittest.mock import patch

from cleo.application import Application
from cleo.testers.command_tester import CommandTester

from claude_code_with_bedrock.cli.commands.cowork import CoworkGenerateCommand
from claude_code_with_bedrock.cli.commands.package import PackageCommand
from claude_code_with_bedrock.config import Profile


def _profile() -> Profile:
    return Profile(
        name="p",
        provider_domain="example.okta.com",
        client_id="client",
        credential_storage="session",
        aws_region="eu-central-1",
        identity_pool_name="pool",
        allowed_bedrock_regions=["eu-central-1"],
        cross_region_profile="eu",
        monitoring_enabled=False,
        cowork_chat_tab_enabled=True,
        cowork_chat_advanced_file_analysis=True,
        cowork_inference_session_lifetime_sec=28800,
        connectors=["stripe"],
    )


def _generate(tmp_path, profile) -> dict:
    app = Application()
    app.add(CoworkGenerateCommand())
    tester = CommandTester(app.find("cowork generate"))
    with patch("claude_code_with_bedrock.cli.commands.cowork.Config") as cfg:
        cfg.load.return_value.active_profile = "p"
        cfg.load.return_value.get_profile.return_value = profile
        assert tester.execute(f"--output {tmp_path} --format json") == 0
    return json.loads((tmp_path / "cowork-3p-config.json").read_text())


def test_cowork_generate_includes_beta_feature_keys(tmp_path):
    cfg = _generate(tmp_path, _profile())
    assert cfg["chatTabEnabled"] is True
    assert cfg["chatAdvancedFileAnalysisEnabled"] is True
    assert cfg["inferenceSessionLifetimeSec"] == 28800


def test_cowork_generate_matches_package(tmp_path):
    profile = _profile()
    (tmp_path / "gen").mkdir()
    (tmp_path / "pkg").mkdir()
    generated = _generate(tmp_path / "gen", profile)
    PackageCommand()._generate_cowork_3p_mdm_config(tmp_path / "pkg", profile, "p")
    packaged = json.loads((tmp_path / "pkg" / "cowork-3p-config.json").read_text())
    assert generated == packaged
