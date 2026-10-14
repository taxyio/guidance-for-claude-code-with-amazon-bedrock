# ABOUTME: Tests side-by-side Claude Code (claude.ai `claude` + Bedrock `claude-bedrock`) packaging and install.
# ABOUTME: Covers profile/validator/init round-trip, generated installers, settings strip scripts and doctor.

"""Side-by-side ("coexistence") mode.

Without it, the installer writes CLAUDE_CODE_USE_BEDROCK into
~/.claude/settings.json, so plain `claude` always goes to Bedrock and users
lose their claude.ai login (and its connectors). With it, Bedrock settings
live in ~/.claude-bedrock and a `claude-bedrock` launcher selects that dir.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_code_with_bedrock.cli.commands.doctor import _check_coexistence
from claude_code_with_bedrock.cli.commands.init import InitCommand
from claude_code_with_bedrock.cli.commands.package import PackageCommand
from claude_code_with_bedrock.cli.utils.coexistence import (
    STRIP_SCRIPT_PS1,
    STRIP_SCRIPT_PY,
    claude_dir_name,
    coexistence_enabled,
)
from claude_code_with_bedrock.cli.validators import validate_profile_for_packaging
from claude_code_with_bedrock.config import Config, Profile

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _profile(auth: str = "oidc", coexist: bool = True, **kw) -> Profile:
    base = {
        "name": "p",
        "provider_domain": "example.okta.com",
        "client_id": "client",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "pool",
        "allowed_bedrock_regions": ["us-east-1"],
        "cross_region_profile": "us",
        "claude_code_coexistence": coexist,
    }
    if auth == "idc":
        base.update(
            auth_type="idc",
            sso_enabled=False,
            idc_start_url="https://d-1234567890.awsapps.com/start",
            idc_account_id="123456789012",
            idc_permission_set_name="ClaudeCodeRole",
            quota_api_endpoint="https://quota.example.com",
        )
    elif auth == "idc-zero-binary":
        base.update(
            auth_type="idc",
            sso_enabled=False,
            idc_start_url="https://d-1234567890.awsapps.com/start",
            idc_account_id="123456789012",
            idc_permission_set_name="ClaudeCodeRole",
        )
    base.update(kw)
    return Profile(**base)


def _bash_installer(profile: Profile, tmp_path: Path, binaries=True) -> str:
    built = [("linux", Path("/tmp/x"))] if binaries else []
    tmp_path.mkdir(parents=True, exist_ok=True)
    return PackageCommand()._create_installer(tmp_path, profile, built).read_text(encoding="utf-8")


def _bat_installer(profile: Profile, tmp_path: Path) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return PackageCommand()._create_windows_installer(tmp_path, profile).read_text(encoding="utf-8")


# Settings an earlier (non-coexistence) install left in ~/.claude/settings.json,
# mixed with the user's own customizations.
LEGACY_SETTINGS = {
    "model": "opus",
    "env": {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "eu-west-1",
        "AWS_PROFILE": "p",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "eu.anthropic.claude-sonnet-4-6",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com",
        "MY_CUSTOM_VAR": "keep-me",
    },
    "awsAuthRefresh": "/home/u/claude-code-with-bedrock/credential-process --profile p",
    "otelHeadersHelper": "/usr/local/bin/my-own-helper",  # not ours: keep
    "hooks": {"Stop": []},
}


# ---------------------------------------------------------------------------
# Profile / validation / init round-trip
# ---------------------------------------------------------------------------


class TestProfileAndValidation:
    def test_defaults_off_and_old_profiles_load(self):
        old = {
            "name": "legacy",
            "provider_domain": "x.okta.com",
            "client_id": "c",
            "credential_storage": "keyring",
            "aws_region": "us-east-1",
            "identity_pool_name": "pool",
        }
        profile = Profile(**old)
        assert profile.claude_code_coexistence is False
        assert coexistence_enabled(profile) is False
        assert claude_dir_name(profile) == ".claude"

    def test_enabled_only_for_user_scope(self):
        assert coexistence_enabled(_profile()) is True
        assert claude_dir_name(_profile()) == ".claude-bedrock"
        managed = _profile(settings_target="managed")
        assert coexistence_enabled(managed) is False
        assert claude_dir_name(managed) == ".claude"

    def test_validator_rejects_managed_with_coexistence(self):
        errors = validate_profile_for_packaging(_profile(settings_target="managed"))
        coexist_errors = [e for e in errors if e.field == "claude_code_coexistence"]
        assert len(coexist_errors) == 1
        assert coexist_errors[0].severity == "error"

    def test_validator_accepts_user_scope(self):
        errors = validate_profile_for_packaging(_profile())
        assert not [e for e in errors if e.field == "claude_code_coexistence"]


class TestInitRoundTrip:
    """Regression: settings_target was saved but never restored on re-run, so
    re-running `ccwb init` defaulted the prompt back to user scope."""

    def _rebuild(self, profile: Profile) -> dict:
        command = InitCommand()
        fake_config = Config()
        with (
            patch.object(Config, "load", return_value=fake_config),
            patch.object(fake_config, "get_profile", return_value=profile),
            patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
        ):
            return command._check_existing_deployment("p")

    def test_restores_coexistence(self):
        rebuilt = self._rebuild(_profile())
        assert rebuilt["claude_code_coexistence"] is True
        assert rebuilt["settings_target"] == "user"

    def test_restores_managed_settings_target(self):
        rebuilt = self._rebuild(_profile(coexist=False, settings_target="managed"))
        assert rebuilt["settings_target"] == "managed"
        assert rebuilt["claude_code_coexistence"] is False


# ---------------------------------------------------------------------------
# bash installer
# ---------------------------------------------------------------------------


class TestBashInstaller:
    def test_default_install_untouched(self, tmp_path):
        """Coexistence off: settings still go to ~/.claude and no migration runs."""
        content = _bash_installer(_profile(coexist=False), tmp_path)
        assert ".claude-bedrock" not in content
        assert "CCWB_STRIP_PY" not in content
        assert '"$ACTUAL_HOME/.claude/settings.json"' in content

    @pytest.mark.parametrize("auth", ["oidc", "idc", "idc-zero-binary"])
    def test_settings_target_bedrock_dir(self, tmp_path, auth):
        content = _bash_installer(_profile(auth), tmp_path, binaries=auth != "idc-zero-binary")
        assert "$ACTUAL_HOME/.claude-bedrock/settings.json" in content
        # Only the strip step may touch ~/.claude/settings.json.
        assert 'cp "claude-settings/settings.json" "$ACTUAL_HOME/.claude/settings.json"' not in content
        assert "CCWB_STRIP_PY" in content
        assert 'export CLAUDE_CONFIG_DIR="$BEDROCK_DIR"' in content

    def test_launcher_login_only_for_idc_with_binary(self, tmp_path):
        oidc = _bash_installer(_profile("oidc"), tmp_path / "o")
        idc = _bash_installer(_profile("idc"), tmp_path / "i")
        zero = _bash_installer(_profile("idc-zero-binary"), tmp_path / "z", binaries=False)
        assert '--login --profile "\\$PROFILE"' in idc
        assert '--login --profile "\\$PROFILE"' not in oidc
        assert '--login --profile "\\$PROFILE"' not in zero
        assert 'FIRST_PROFILE="ClaudeCode"' in zero

    @pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")
    @pytest.mark.parametrize("auth", ["oidc", "idc", "idc-zero-binary"])
    def test_installer_is_valid_bash(self, tmp_path, auth):
        content = _bash_installer(_profile(auth), tmp_path, binaries=auth != "idc-zero-binary")
        script = tmp_path / "check.sh"
        script.write_text(content, encoding="utf-8")
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")
    def test_install_end_to_end(self, tmp_path):
        """Run the real install.sh in a sandbox HOME: Bedrock settings land in
        ~/.claude-bedrock, ~/.claude/settings.json loses only our keys, assets
        are shared, and the launcher selects the Bedrock dir."""
        pkg, home = tmp_path / "pkg", tmp_path / "home"
        (pkg / "claude-settings").mkdir(parents=True)
        (home / ".claude").mkdir(parents=True)
        (home / ".local" / "bin").mkdir(parents=True)
        _bash_installer(_profile("idc"), pkg)
        for suffix in ("macos-arm64", "macos-intel", "linux-x64", "linux-arm64"):
            fake = pkg / f"credential-process-{suffix}"
            fake.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/cred.log"\n', encoding="utf-8")
            fake.chmod(0o755)
        (pkg / "config.json").write_text(json.dumps({"p": {"aws_region": "us-east-1"}}), encoding="utf-8")
        (pkg / "claude-settings" / "settings.json").write_text(
            json.dumps(
                {
                    "env": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_PROFILE": "p"},
                    "awsAuthRefresh": "__CREDENTIAL_PROCESS_PATH__ --login --profile p",
                }
            ),
            encoding="utf-8",
        )
        legacy = dict(LEGACY_SETTINGS, awsAuthRefresh=f"{home}/claude-code-with-bedrock/credential-process")
        (home / ".claude" / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")

        env = {"HOME": str(home), "USER": "tester", "PATH": os.environ["PATH"]}
        result = subprocess.run(["bash", "install.sh"], cwd=pkg, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

        cleaned = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert cleaned == {
            "model": "opus",
            "env": {"MY_CUSTOM_VAR": "keep-me"},
            "otelHeadersHelper": "/usr/local/bin/my-own-helper",
            "hooks": {"Stop": []},
        }
        assert list((home / ".claude").glob("settings.json.backup-*"))

        bedrock = json.loads((home / ".claude-bedrock" / "settings.json").read_text(encoding="utf-8"))
        assert bedrock["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert "__CREDENTIAL_PROCESS_PATH__" not in bedrock["awsAuthRefresh"]
        for shared in ("skills", "agents", "commands"):
            assert (home / ".claude-bedrock" / shared).is_symlink()
        assert "@~/.claude/CLAUDE.md" in (home / ".claude-bedrock" / "CLAUDE.md").read_text(encoding="utf-8")

        # Launcher: linked onto PATH, sets CLAUDE_CONFIG_DIR, signs in first (IDC).
        launcher = home / ".local" / "bin" / "claude-bedrock"
        assert launcher.is_symlink()
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir()
        fake_claude = fakebin / "claude"
        fake_claude.write_text('#!/bin/sh\necho "DIR=$CLAUDE_CONFIG_DIR BEDROCK=$CLAUDE_CODE_USE_BEDROCK ARGS=$*"\n')
        fake_claude.chmod(0o755)
        run = subprocess.run(
            [str(launcher), "--resume"],
            env={"HOME": str(home), "PATH": f"{fakebin}{os.pathsep}/usr/bin{os.pathsep}/bin"},
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr
        assert f"DIR={home}/.claude-bedrock BEDROCK=1 ARGS=--resume" in run.stdout
        assert "--login --profile p" in (tmp_path / "cred.log").read_text(encoding="utf-8")

        # Re-running the installer is idempotent (nothing left to strip).
        again = subprocess.run(["bash", "install.sh"], cwd=pkg, env=env, capture_output=True, text=True)
        assert again.returncode == 0, again.stdout + again.stderr
        assert "Removed Bedrock settings" not in again.stdout
        assert len(list((home / ".claude").glob("settings.json.backup-*"))) == 1


# ---------------------------------------------------------------------------
# Strip script (Python, used by install.sh)
# ---------------------------------------------------------------------------


def _run_strip_py(tmp_path: Path, settings, incoming=None) -> tuple[Path, subprocess.CompletedProcess]:
    target = tmp_path / "settings.json"
    if settings is not None:
        target.write_text(settings if isinstance(settings, str) else json.dumps(settings), encoding="utf-8")
    incoming_path = tmp_path / "incoming.json"
    incoming_path.write_text(json.dumps(incoming or {}), encoding="utf-8")
    script = tmp_path / "strip.py"
    script.write_text(STRIP_SCRIPT_PY, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(target), str(incoming_path)], capture_output=True, text=True
    )
    return target, result


class TestStripScriptPython:
    def test_removes_only_installer_keys(self, tmp_path):
        target, result = _run_strip_py(tmp_path, LEGACY_SETTINGS)
        assert result.returncode == 0, result.stderr
        assert json.loads(target.read_text(encoding="utf-8")) == {
            "model": "opus",
            "env": {"MY_CUSTOM_VAR": "keep-me"},
            "otelHeadersHelper": "/usr/local/bin/my-own-helper",
            "hooks": {"Stop": []},
        }
        backups = list(tmp_path.glob("settings.json.backup-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8")) == LEGACY_SETTINGS

    def test_strips_new_env_keys_from_packaged_settings(self, tmp_path):
        settings = {"env": {"CLAUDE_CODE_USE_BEDROCK": "1", "SOME_FUTURE_KEY": "x"}}
        target, _ = _run_strip_py(tmp_path, settings, incoming={"env": {"SOME_FUTURE_KEY": "y"}})
        assert json.loads(target.read_text(encoding="utf-8")) == {}

    def test_leaves_non_bedrock_settings_alone(self, tmp_path):
        """A claude.ai user's own settings (AWS_REGION for an MCP server, etc.)
        must not be touched when nothing was installed by us."""
        settings = {"env": {"AWS_REGION": "us-west-2"}, "otelHeadersHelper": "/opt/mine"}
        target, result = _run_strip_py(tmp_path, settings)
        assert result.returncode == 0
        assert json.loads(target.read_text(encoding="utf-8")) == settings
        assert not list(tmp_path.glob("settings.json.backup-*"))

    def test_hook_only_install_is_detected(self, tmp_path):
        settings = {"otelHeadersHelper": "/home/u/claude-code-with-bedrock/otel-helper --profile p"}
        target, _ = _run_strip_py(tmp_path, settings)
        assert json.loads(target.read_text(encoding="utf-8")) == {}

    def test_invalid_json_is_left_unchanged(self, tmp_path):
        target, result = _run_strip_py(tmp_path, "{not json")
        assert result.returncode == 0
        assert "WARN" in result.stdout
        assert target.read_text(encoding="utf-8") == "{not json"

    def test_missing_file_is_noop(self, tmp_path):
        target, result = _run_strip_py(tmp_path, None)
        assert result.returncode == 0
        assert not target.exists()


# ---------------------------------------------------------------------------
# Windows installer
# ---------------------------------------------------------------------------


def _encoded_command(content: str) -> str:
    for line in content.splitlines():
        if "-EncodedCommand" in line:
            return line.split("-EncodedCommand", 1)[1].strip()
    raise AssertionError("no -EncodedCommand in install.bat")


class TestWindowsInstaller:
    def test_default_install_untouched(self, tmp_path):
        content = _bat_installer(_profile(coexist=False), tmp_path)
        assert ".claude-bedrock" not in content
        assert "-EncodedCommand" not in content

    def test_settings_target_bedrock_dir(self, tmp_path):
        content = _bat_installer(_profile(), tmp_path)
        assert 'if exist "%USERPROFILE%\\.claude-bedrock\\settings.json" (' in content
        assert "'.claude-bedrock\\settings.json'" in content
        assert "'.claude\\settings.json'" not in content  # only via the encoded strip script
        assert "{{" not in content  # no PowerShell no-op scriptblocks

    def test_encoded_strip_script_round_trips(self, tmp_path):
        encoded = _encoded_command(_bat_installer(_profile(), tmp_path))
        assert base64.b64decode(encoded).decode("utf-16-le") == STRIP_SCRIPT_PS1
        assert len(encoded) < 8000  # cmd.exe line limit is 8191

    def test_launcher_isolates_env_and_sets_config_dir(self, tmp_path):
        content = _bat_installer(_profile(), tmp_path)
        assert '>> "%LAUNCHER%" echo setlocal' in content
        assert '>> "%LAUNCHER%" echo set "CLAUDE_CONFIG_DIR=%USERPROFILE%\\.claude-bedrock"' in content
        assert '>> "%LAUNCHER%" echo claude %%*' in content
        assert "--login" not in content.split("Creating launcher")[1]

    def test_idc_launcher_signs_in_first(self, tmp_path):
        content = _bat_installer(_profile("idc"), tmp_path)
        launcher = content.split("Creating launcher")[1]
        assert '"%CRED_PROC%" --login --profile %%AWS_PROFILE%%' in launcher
        assert launcher.count("Installation complete!") == 1

    def test_junctions_for_shared_dirs(self, tmp_path):
        content = _bat_installer(_profile(), tmp_path)
        assert "mklink /J" in content
        assert "for %%d in (skills agents commands) do (" in content

    @pytest.mark.skipif(sys.platform != "win32", reason="executes the strip script under real PowerShell")
    def test_strip_script_executes(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        legacy = dict(LEGACY_SETTINGS, awsAuthRefresh="C:/Users/u/claude-code-with-bedrock/credential-process.exe")
        (home / ".claude" / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")
        pkg = tmp_path / "pkg"
        (pkg / "claude-settings").mkdir(parents=True)
        (pkg / "claude-settings" / "settings.json").write_text(json.dumps({"env": {}}), encoding="utf-8")
        encoded = _encoded_command(_bat_installer(_profile(), tmp_path / "out"))
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            cwd=pkg,
            env=dict(os.environ, USERPROFILE=str(home)),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        cleaned = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8-sig"))
        assert cleaned["env"] == {"MY_CUSTOM_VAR": "keep-me"}
        assert "awsAuthRefresh" not in cleaned
        assert cleaned["otelHeadersHelper"] == "/usr/local/bin/my-own-helper"
        assert cleaned["model"] == "opus"


class TestPackageReadme:
    def test_readme_explains_both_commands(self, tmp_path):
        PackageCommand()._create_documentation(tmp_path, _profile(), "t")
        readme = (tmp_path / "README.md").read_text(encoding="utf-8")
        assert "## Two Ways to Run Claude Code" in readme
        assert "`claude-bedrock`" in readme

    def test_readme_unchanged_without_coexistence(self, tmp_path):
        PackageCommand()._create_documentation(tmp_path, _profile(coexist=False), "t")
        assert "claude-bedrock" not in (tmp_path / "README.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


class TestDoctorCoexistence:
    def _home(self, tmp_path: Path, default_env=None, launcher=True) -> tuple[Path, Path]:
        home = tmp_path
        (home / ".claude-bedrock").mkdir()
        (home / ".claude-bedrock" / "settings.json").write_text("{}", encoding="utf-8")
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(json.dumps({"env": default_env or {}}), encoding="utf-8")
        install_dir = home / "claude-code-with-bedrock"
        install_dir.mkdir()
        if launcher:
            name = "claude-bedrock.cmd" if sys.platform == "win32" else "claude-bedrock"
            (install_dir / name).write_text("", encoding="utf-8")
        return home, install_dir

    def test_skipped_when_not_installed(self, tmp_path):
        check = _check_coexistence(tmp_path, tmp_path / "x", environ={})
        assert check.status == "skipped"

    def test_passes(self, tmp_path):
        home, install_dir = self._home(tmp_path)
        assert _check_coexistence(home, install_dir, environ={}).status == "pass"

    def test_fails_when_default_settings_still_use_bedrock(self, tmp_path):
        home, install_dir = self._home(tmp_path, default_env={"CLAUDE_CODE_USE_BEDROCK": "1"})
        check = _check_coexistence(home, install_dir, environ={})
        assert check.status == "fail"
        assert "~/.claude/settings.json" in check.message

    def test_fails_when_shell_exports_bedrock(self, tmp_path):
        home, install_dir = self._home(tmp_path)
        check = _check_coexistence(home, install_dir, environ={"CLAUDE_CODE_USE_BEDROCK": "1"})
        assert check.status == "fail"

    def test_warns_when_shell_sets_config_dir(self, tmp_path):
        home, install_dir = self._home(tmp_path)
        check = _check_coexistence(home, install_dir, environ={"CLAUDE_CONFIG_DIR": "/x"})
        assert check.status == "warn"

    def test_warns_when_launcher_missing(self, tmp_path):
        home, install_dir = self._home(tmp_path, launcher=False)
        assert _check_coexistence(home, install_dir, environ={}).status == "warn"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanupCoexistence:
    def test_removes_bedrock_settings_and_path_launcher_keeps_history(self, tmp_path):
        from cleo.testers.command_tester import CommandTester

        from claude_code_with_bedrock.cli.commands.cleanup import CleanupCommand

        bedrock = tmp_path / ".claude-bedrock"
        (bedrock / "projects").mkdir(parents=True)
        (bedrock / "settings.json").write_text("{}", encoding="utf-8")
        local_bin = tmp_path / ".local" / "bin"
        local_bin.mkdir(parents=True)
        launcher = local_bin / ("claude-bedrock.cmd" if sys.platform == "win32" else "claude-bedrock")
        launcher.write_text("", encoding="utf-8")

        with patch("claude_code_with_bedrock.cli.commands.cleanup.Path.home", return_value=tmp_path):
            tester = CommandTester(CleanupCommand())
            assert tester.execute("--force") == 0

        assert not (bedrock / "settings.json").exists()
        assert not launcher.exists()
        assert (bedrock / "projects").is_dir()  # user's Bedrock history survives
