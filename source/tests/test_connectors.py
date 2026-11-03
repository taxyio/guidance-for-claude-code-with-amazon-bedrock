# ABOUTME: Tests remote MCP connectors (Stripe, Taxy Workspace, custom) for Claude Code and Claude Cowork.
# ABOUTME: Covers profile parsing/validation, managedMcpServers rendering and the generated installer snippets.

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from claude_code_with_bedrock.cli.commands.package import PackageCommand
from claude_code_with_bedrock.cli.utils.connectors import (
    PRESETS,
    ConnectorConfigError,
    add_connectors_mcp_config,
    bash_connectors_block,
    resolve_connectors,
    windows_connectors_block,
)
from claude_code_with_bedrock.cli.validators import validate_profile_for_packaging
from claude_code_with_bedrock.config import Profile

STRIPE = PRESETS["stripe"]
TAXY = PRESETS["taxy-workspace"]


def _profile(connectors=None, coexist=False, **kw) -> Profile:
    base = {
        "name": "p",
        "provider_domain": "login.microsoftonline.com/tenant/v2.0",
        "client_id": "client",
        "credential_storage": "session",
        "aws_region": "eu-central-1",
        "identity_pool_name": "pool",
        "allowed_bedrock_regions": ["eu-central-1"],
        "claude_code_coexistence": coexist,
        "connectors": connectors if connectors is not None else [],
    }
    base.update(kw)
    return Profile(**base)


def _quiet() -> Console:
    return Console(file=open(os.devnull, "w"))


# ---------------------------------------------------------------------------
# Profile parsing / validation
# ---------------------------------------------------------------------------


class TestProfile:
    def test_old_profile_without_connectors_loads_with_empty_default(self):
        p = Profile.from_dict(
            {
                "name": "old",
                "provider_domain": "x.okta.com",
                "client_id": "c",
                "aws_region": "eu-central-1",
                "identity_pool_name": "i",
            }
        )
        assert p.connectors == []
        assert resolve_connectors(p) == []

    def test_connectors_round_trip(self):
        p = _profile(["stripe", {"name": "acme", "url": "https://mcp.acme.example/mcp", "surfaces": ["cowork"]}])
        again = Profile.from_dict(p.to_dict())
        assert again.connectors == p.connectors

    def test_presets_resolve(self):
        cs = resolve_connectors(_profile(["stripe", "Taxy-Workspace"]))
        assert [(c.name, c.url) for c in cs] == [("stripe", STRIPE), ("taxy-workspace", TAXY)]
        assert all(c.surfaces == ("claude-code", "cowork") for c in cs)

    def test_dict_with_preset_name_and_no_url_uses_preset_url(self):
        (c,) = resolve_connectors(_profile([{"name": "stripe", "surfaces": "cowork"}]))
        assert c.url == STRIPE and c.surfaces == ("cowork",)

    @pytest.mark.parametrize(
        "item, match",
        [
            ("nope", "Unknown connector preset"),
            ({"name": "Bad Name", "url": "https://x.example"}, "Invalid connector name"),
            ({"name": "x", "url": "http://x.example"}, "Invalid URL"),
            ({"name": "x", "url": "https://x.example/a b"}, "Invalid URL"),
            ({"name": "x", "url": "https://x.example/$(id)"}, "Invalid URL"),
            ({"name": "x", "url": "https://x.example/'"}, "Invalid URL"),
            ({"name": "x", "url": "https://x.example", "surfaces": ["web"]}, "Invalid surfaces"),
            ({"name": "x", "url": "https://x.example", "surfaces": []}, "Invalid surfaces"),
            (42, "preset name or an object"),
        ],
    )
    def test_invalid_entries_rejected(self, item, match):
        with pytest.raises(ConnectorConfigError, match=match):
            resolve_connectors(_profile([item]))

    def test_duplicates_rejected(self):
        with pytest.raises(ConnectorConfigError, match="Duplicate"):
            resolve_connectors(_profile(["stripe", {"name": "stripe", "url": "https://other.example"}]))

    def test_validator_reports_invalid_connectors(self):
        errors = validate_profile_for_packaging(_profile(["nope"]))
        assert any(e.field == "connectors" for e in errors)

    def test_validator_accepts_valid_connectors(self):
        errors = validate_profile_for_packaging(_profile(["stripe", "taxy-workspace"]))
        assert not any(e.field == "connectors" for e in errors)

    def test_validator_rejects_windows_command_line_overflow(self):
        # High-entropy URLs so gzip cannot shrink the installer line below the limit.
        many = [
            {"name": f"c{i}", "url": f"https://mcp{i}.example.com/{hashlib.sha256(str(i).encode()).hexdigest()}"}
            for i in range(200)
        ]
        errors = validate_profile_for_packaging(_profile(many))
        assert any(e.field == "connectors" and "Windows" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Cowork managedMcpServers
# ---------------------------------------------------------------------------


class TestCowork:
    def test_no_connectors_is_noop(self):
        cfg = {}
        add_connectors_mcp_config(cfg, _profile(), _quiet())
        assert "managedMcpServers" not in cfg

    def test_entries_use_automatic_oauth_registration(self):
        cfg = {}
        add_connectors_mcp_config(cfg, _profile(["stripe", "taxy-workspace"]), _quiet())
        servers = json.loads(cfg["managedMcpServers"])
        assert servers == [
            {"name": "stripe", "transport": "http", "url": STRIPE, "oauth": True},
            {"name": "taxy-workspace", "transport": "http", "url": TAXY, "oauth": True},
        ]

    def test_merges_with_existing_entries_and_dedupes(self):
        websearch = {"name": "agentcore-websearch", "transport": "http", "url": "https://gw/mcp"}
        stale = {"name": "stripe", "transport": "http", "url": "https://old.example"}
        cfg = {"managedMcpServers": json.dumps([websearch, stale])}
        add_connectors_mcp_config(cfg, _profile(["stripe"]), _quiet())
        add_connectors_mcp_config(cfg, _profile(["stripe"]), _quiet())  # idempotent
        servers = json.loads(cfg["managedMcpServers"])
        assert servers[0] == websearch
        assert [s["name"] for s in servers] == ["agentcore-websearch", "stripe"]
        assert servers[1]["url"] == STRIPE

    def test_claude_code_only_connector_not_in_cowork(self):
        cfg = {}
        add_connectors_mcp_config(cfg, _profile([{"name": "stripe", "surfaces": ["claude-code"]}]), _quiet())
        assert "managedMcpServers" not in cfg

    def test_package_mdm_generation_includes_connectors(self, tmp_path):
        p = _profile(["taxy-workspace"], cowork_3p_enabled=True)
        PackageCommand()._generate_cowork_3p_mdm_config(tmp_path, p, "ClaudeCode")
        text = "".join(f.read_text(encoding="utf-8", errors="ignore") for f in tmp_path.rglob("*") if f.is_file())
        assert TAXY in text


# ---------------------------------------------------------------------------
# Installers embed the snippets
# ---------------------------------------------------------------------------


class TestInstallerWiring:
    def test_bash_installer_includes_connectors(self, tmp_path):
        p = _profile(["stripe"])
        text = PackageCommand()._create_installer(tmp_path, p, [("linux", Path("/tmp/x"))]).read_text(encoding="utf-8")
        assert "MCP connectors for Claude Code" in text
        assert f"stripe={STRIPE}" in text

    def test_windows_installer_includes_connectors_within_cmd_limit(self, tmp_path):
        p = _profile(["stripe", "taxy-workspace"], coexist=True)
        text = PackageCommand()._create_windows_installer(tmp_path, p).read_text(encoding="utf-8")
        assert "MCP connectors for Claude Code" in text
        assert max(len(line) for line in text.splitlines()) <= 8191


# ---------------------------------------------------------------------------
# Execute the generated snippets against a fake `claude` CLI
# ---------------------------------------------------------------------------

FAKE_CLAUDE_SH = """#!/bin/sh
echo "${CLAUDE_CONFIG_DIR}|$*" >> "$FAKE_LOG"
case "$*" in *"mcp add"*" taken "*) exit 1 ;; esac
exit 0
"""

FAKE_CLAUDE_CMD = """@echo off
>> "%FAKE_LOG%" echo %CLAUDE_CONFIG_DIR%^|%*
echo %* | findstr /C:" taken " >nul && exit /b 1
exit /b 0
"""


def _fake_env(tmp_path: Path, with_claude: bool = True) -> dict:
    home = tmp_path / "home"
    (home / "claude-code-with-bedrock").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if with_claude:
        sh = bindir / "claude"
        sh.write_text(FAKE_CLAUDE_SH)
        sh.chmod(0o755)
        (bindir / "claude.cmd").write_text(FAKE_CLAUDE_CMD.replace("\n", "\r\n"))
    env = {k: v for k, v in os.environ.items() if k not in ("SUDO_USER", "CLAUDE_CONFIG_DIR")}
    # Keep only system dirs on PATH so a real `claude` is never picked up.
    system_path = [d for d in env.get("PATH", "").split(os.pathsep) if "claude" not in d.lower()]
    env["PATH"] = os.pathsep.join([str(bindir)] + [d for d in system_path if not Path(d, "claude").exists()])
    env["FAKE_LOG"] = str(tmp_path / "calls.log")
    env["HOME"] = env["USERPROFILE"] = str(home)
    return env


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    if not log.exists():
        return []
    return [line.strip() for line in log.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]


def _state(tmp_path: Path) -> list[str]:
    f = tmp_path / "home" / "claude-code-with-bedrock" / "connectors-managed.txt"
    text = f.read_text(encoding="utf-8-sig") if f.exists() else ""
    return sorted(line.strip() for line in text.splitlines() if line.strip())


def _run_bash(profile: Profile, env: dict) -> str:
    # Hide system-wide claude installs (e.g. /opt/homebrew/bin) so tests only see the fake.
    block = bash_connectors_block(profile).replace(
        'CCWB_SYSTEM_CLAUDE_PATHS="/opt/homebrew/bin/claude /usr/local/bin/claude"', 'CCWB_SYSTEM_CLAUDE_PATHS=""'
    )
    script = 'ACTUAL_HOME="$HOME"; ACTUAL_USER="$USER"\n' + block
    r = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="bash installer runs on macOS/Linux")
class TestBashSnippet:
    def test_adds_connectors_at_user_scope(self, tmp_path):
        env = _fake_env(tmp_path)
        out = _run_bash(_profile(["stripe", "taxy-workspace"]), env)
        assert f"|mcp add --transport http -s user stripe {STRIPE}" in _calls(tmp_path)
        assert f"|mcp add --transport http -s user taxy-workspace {TAXY}" in _calls(tmp_path)
        assert _state(tmp_path) == sorted([f"stripe={STRIPE}", f"taxy-workspace={TAXY}"])
        assert "/mcp" in out

    def test_side_by_side_targets_bedrock_config_dir(self, tmp_path):
        env = _fake_env(tmp_path)
        _run_bash(_profile(["stripe"], coexist=True), env)
        (call,) = _calls(tmp_path)
        assert call.startswith(str(tmp_path / "home" / ".claude-bedrock") + "|mcp add")

    def test_rerun_keeps_unchanged_and_removes_stale(self, tmp_path):
        env = _fake_env(tmp_path)
        _run_bash(_profile(["stripe", "taxy-workspace"]), env)
        (tmp_path / "calls.log").unlink()
        out = _run_bash(_profile(["taxy-workspace"]), env)
        # Unchanged connector untouched (user keeps their vendor sign-in).
        assert _calls(tmp_path) == ["|mcp remove -s user stripe"]
        assert "Removed MCP connector: stripe" in out
        assert _state(tmp_path) == [f"taxy-workspace={TAXY}"]

    def test_url_change_replaces_entry(self, tmp_path):
        env = _fake_env(tmp_path)
        _run_bash(_profile([{"name": "acme", "url": "https://a.example/mcp"}]), env)
        (tmp_path / "calls.log").unlink()
        out = _run_bash(_profile([{"name": "acme", "url": "https://b.example/mcp"}]), env)
        assert _calls(tmp_path) == [
            "|mcp remove -s user acme",
            "|mcp add --transport http -s user acme https://b.example/mcp",
        ]
        assert "Removed" not in out

    def test_user_defined_server_is_not_overwritten(self, tmp_path):
        env = _fake_env(tmp_path)
        out = _run_bash(_profile([{"name": "taken", "url": "https://t.example/mcp"}, "stripe"]), env)
        assert "SKIP taken" in out
        assert _state(tmp_path) == [f"stripe={STRIPE}"]
        assert not any("remove" in c for c in _calls(tmp_path))

    def test_empty_list_removes_previously_managed(self, tmp_path):
        env = _fake_env(tmp_path)
        _run_bash(_profile(["stripe"]), env)
        (tmp_path / "calls.log").unlink()
        _run_bash(_profile([]), env)
        assert _calls(tmp_path) == ["|mcp remove -s user stripe"]
        assert _state(tmp_path) == []

    def test_no_connectors_and_no_state_is_silent(self, tmp_path):
        env = _fake_env(tmp_path)
        assert _run_bash(_profile([]), env).strip() == ""
        assert _calls(tmp_path) == []

    def test_finds_claude_outside_path_like_jamf_root(self, tmp_path):
        """Jamf runs install.sh as root with PATH=/usr/bin:/bin:/usr/sbin:/sbin, so the
        user's claude (~/.local/bin, /opt/homebrew/bin) is not on PATH. Regression:
        connectors were silently skipped on every Jamf-managed Mac."""
        env = _fake_env(tmp_path, with_claude=False)
        local_bin = tmp_path / "home" / ".local" / "bin"
        local_bin.mkdir(parents=True)
        (local_bin / "claude").write_text(FAKE_CLAUDE_SH)
        (local_bin / "claude").chmod(0o755)
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        out = _run_bash(_profile(["stripe"]), env)
        assert "not found" not in out
        assert f"|mcp add --transport http -s user stripe {STRIPE}" in _calls(tmp_path)

    def test_missing_claude_prints_manual_commands(self, tmp_path):
        env = _fake_env(tmp_path, with_claude=False)
        out = _run_bash(_profile(["stripe"]), env)
        assert "'claude' not found" in out
        assert f"claude mcp add --transport http -s user stripe {STRIPE}" in out


def _powershell() -> str | None:
    return shutil.which("powershell") if sys.platform == "win32" else shutil.which("pwsh")


@pytest.mark.skipif(_powershell() is None, reason="PowerShell not available")
class TestPowerShellSnippet:
    def _run(self, profile: Profile, env: dict) -> str:
        # Run the exact -Command string install.bat passes to PowerShell.
        line = next(line for line in windows_connectors_block(profile).splitlines() if " -Command " in line)
        command = re.search(r'-Command "(.*)"$', line).group(1)
        r = subprocess.run(
            [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    def test_adds_then_prunes(self, tmp_path):
        env = _fake_env(tmp_path)
        self._run(_profile(["stripe", "taxy-workspace"], coexist=True), env)
        calls = _calls(tmp_path)
        assert len(calls) == 2 and all(".claude-bedrock|mcp add" in c for c in calls)
        assert _state(tmp_path) == sorted([f"stripe={STRIPE}", f"taxy-workspace={TAXY}"])

        (tmp_path / "calls.log").unlink()
        out = self._run(_profile(["taxy-workspace"], coexist=True), env)
        (call,) = _calls(tmp_path)
        assert call.endswith("mcp remove -s user stripe")
        assert "Removed MCP connector: stripe" in out
        assert _state(tmp_path) == [f"taxy-workspace={TAXY}"]

    def test_user_defined_server_is_not_overwritten(self, tmp_path):
        env = _fake_env(tmp_path)
        out = self._run(_profile([{"name": "taken", "url": "https://t.example/mcp"}]), env)
        assert "SKIP taken" in out
        assert _state(tmp_path) == []

    def test_missing_claude_prints_manual_commands(self, tmp_path):
        env = _fake_env(tmp_path, with_claude=False)
        out = self._run(_profile(["stripe"]), env)
        assert "'claude' not found" in out
        assert f"claude mcp add --transport http -s user stripe {STRIPE}" in out
