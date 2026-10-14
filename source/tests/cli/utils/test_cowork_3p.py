# ABOUTME: Tests for cowork_3p.py monitoring endpoint resolution and inferenceModels derivation
# ABOUTME: Covers stack output fallbacks and region-correct model IDs for Claude Desktop

"""Tests for CoWork 3P monitoring configuration (endpoint resolution + auth headers)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from claude_code_with_bedrock.cli.utils.cowork_3p import (
    COWORK_DEFAULT_ALIASES,
    COWORK_MODELS_PER_TIER,
    add_monitoring_config,
    build_inference_models,
    derive_model_aliases,
)


class FakeProfile:
    """Minimal profile stub for testing add_monitoring_config."""

    def __init__(
        self,
        monitoring_enabled=True,
        monitoring_mode="central",
        otel_collector_endpoint=None,
        identity_pool_name="test-pool",
        aws_region="us-east-1",
    ):
        self.monitoring_enabled = monitoring_enabled
        self.monitoring_mode = monitoring_mode
        self.otel_collector_endpoint = otel_collector_endpoint
        self.identity_pool_name = identity_pool_name
        self.aws_region = aws_region
        self.stack_names = {}


class TestAddMonitoringConfig:
    """Tests for add_monitoring_config endpoint resolution logic."""

    def _make_console(self):
        return MagicMock()

    def test_monitoring_disabled_skips(self):
        """When monitoring_enabled=False, nothing is set."""
        profile = FakeProfile(monitoring_enabled=False)
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpEndpoint" not in mdm

    def test_sidecar_mode_uses_local_proxy(self):
        """Sidecar mode configures CoWork to send to localhost otel-helper proxy."""
        profile = FakeProfile(monitoring_mode="sidecar")
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert mdm["otlpEndpoint"] == "http://localhost:4318"
        assert mdm["otlpProtocol"] == "http/protobuf"

    def test_sidecar_mode_without_cowork_token(self):
        """Sidecar mode without cowork_service_token omits otlpHeaders."""
        profile = FakeProfile(monitoring_mode="sidecar")
        profile.cowork_service_token = None
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpHeaders" not in mdm

    @patch("claude_code_with_bedrock.cli.utils.cowork_3p.get_stack_outputs")
    def test_stack_output_success(self, mock_get_outputs):
        """When stack outputs resolve, endpoint is set from CollectorEndpoint."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://telemetry.example.com"}
        profile = FakeProfile()
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert mdm["otlpEndpoint"] == "https://telemetry.example.com"
        assert mdm["otlpProtocol"] == "http/protobuf"

    @patch("claude_code_with_bedrock.cli.utils.cowork_3p.get_stack_outputs")
    def test_stack_failure_falls_back_to_profile(self, mock_get_outputs):
        """When stack query fails, falls back to profile.otel_collector_endpoint."""
        mock_get_outputs.side_effect = Exception("stack not found")
        profile = FakeProfile(otel_collector_endpoint="https://fallback.example.com")
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert mdm["otlpEndpoint"] == "https://fallback.example.com"
        assert mdm["otlpProtocol"] == "http/protobuf"

    @patch("claude_code_with_bedrock.cli.utils.cowork_3p.get_stack_outputs")
    def test_stack_returns_no_endpoint_falls_back_to_profile(self, mock_get_outputs):
        """When stack outputs exist but CollectorEndpoint is missing, use profile fallback."""
        mock_get_outputs.return_value = {"SomeOtherOutput": "value"}
        profile = FakeProfile(otel_collector_endpoint="https://profile-endpoint.example.com")
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert mdm["otlpEndpoint"] == "https://profile-endpoint.example.com"

    @patch("claude_code_with_bedrock.cli.utils.cowork_3p.get_stack_outputs")
    def test_both_missing_shows_warning(self, mock_get_outputs):
        """When stack query fails and no profile endpoint, no otlpEndpoint is set."""
        mock_get_outputs.side_effect = Exception("stack not found")
        profile = FakeProfile(otel_collector_endpoint=None)
        mdm = {}
        console = self._make_console()
        add_monitoring_config(mdm, profile, console)
        assert "otlpEndpoint" not in mdm
        # Should print a warning
        console.print.assert_called()
        warning_text = str(console.print.call_args_list[-1])
        assert "Could not resolve" in warning_text or "warning" in warning_text.lower()

    @patch("claude_code_with_bedrock.cli.utils.cowork_3p.get_stack_outputs")
    def test_custom_stack_name_from_profile(self, mock_get_outputs):
        """Uses stack name from profile.stack_names if configured."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://custom-stack.example.com"}
        profile = FakeProfile()
        profile.stack_names = {"monitoring": "my-custom-monitoring-stack"}
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        mock_get_outputs.assert_called_once_with("my-custom-monitoring-stack", "us-east-1")
        assert mdm["otlpEndpoint"] == "https://custom-stack.example.com"

    @patch("claude_code_with_bedrock.cli.utils.cowork_3p.get_stack_outputs")
    def test_cowork_service_token_adds_otlp_headers(self, mock_get_outputs):
        """When cowork_service_token is set, otlpHeaders includes X-Cowork-Token."""
        import json

        mock_get_outputs.return_value = {"CollectorEndpoint": "https://collector.example.com"}
        profile = FakeProfile()
        profile.cowork_service_token = "test-token-abc123"
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpHeaders" in mdm
        headers = json.loads(mdm["otlpHeaders"])
        assert headers == {"X-Cowork-Token": "test-token-abc123"}

    @patch("claude_code_with_bedrock.cli.utils.cowork_3p.get_stack_outputs")
    def test_no_cowork_service_token_omits_otlp_headers(self, mock_get_outputs):
        """When cowork_service_token is not set, otlpHeaders is not added."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://collector.example.com"}
        profile = FakeProfile()
        # No cowork_service_token attribute
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpHeaders" not in mdm

    @patch("claude_code_with_bedrock.cli.utils.cowork_3p.get_stack_outputs")
    def test_empty_cowork_service_token_omits_otlp_headers(self, mock_get_outputs):
        """When cowork_service_token is empty string, otlpHeaders is not added."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://collector.example.com"}
        profile = FakeProfile()
        profile.cowork_service_token = ""
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpHeaders" not in mdm


class TestDeriveModelAliases:
    """Regression: bare opus/sonnet/haiku aliases let Claude Desktop resolve model
    IDs that don't exist in the deployment region (e.g. us.* on eu-central-1)."""

    @staticmethod
    def _profile(prefix, selected=None):
        return SimpleNamespace(cross_region_profile=prefix, selected_model=selected)

    def test_eu_profile_emits_only_eu_model_ids(self):
        models = derive_model_aliases(self._profile("eu", "eu.anthropic.claude-sonnet-5"))
        assert models[0] == "eu.anthropic.claude-sonnet-5"
        assert all(m.startswith("eu.") for m in models)
        assert not set(models) & set(COWORK_DEFAULT_ALIASES)

    def test_legacy_europe_key_resolves_to_eu(self):
        assert derive_model_aliases(self._profile("europe")) == derive_model_aliases(self._profile("eu"))

    def test_list_covers_multiple_tiers_without_duplicates(self):
        models = derive_model_aliases(self._profile("us"))
        assert len(models) == len(set(models))
        tiers = [e["anthropicFamilyTier"] for e in build_inference_models(models)]
        assert {"sonnet", "opus", "haiku"} <= set(tiers)
        assert all(tiers.count(t) <= COWORK_MODELS_PER_TIER for t in set(tiers))
        assert sum(t == "sonnet" for t in tiers) > 1  # wider than one model per tier

    def test_each_tier_has_one_family_default(self):
        entries = build_inference_models(derive_model_aliases(self._profile("global")))
        defaults = [e["anthropicFamilyTier"] for e in entries if e.get("isFamilyDefault")]
        assert len(defaults) == len(set(defaults))

    def test_selected_model_moves_to_front_once(self):
        models = derive_model_aliases(self._profile("eu", "eu.anthropic.claude-opus-4-8"))
        assert models[0] == "eu.anthropic.claude-opus-4-8"
        assert models.count("eu.anthropic.claude-opus-4-8") == 1

    def test_falls_back_to_aliases_without_profile_or_catalog_match(self):
        assert derive_model_aliases() == COWORK_DEFAULT_ALIASES
        assert derive_model_aliases(self._profile(None)) == COWORK_DEFAULT_ALIASES
        assert derive_model_aliases(self._profile("nowhere")) == COWORK_DEFAULT_ALIASES
