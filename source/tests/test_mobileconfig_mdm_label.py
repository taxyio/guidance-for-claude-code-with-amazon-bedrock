# ABOUTME: Regression tests: a .mobileconfig with unresolved __CCWB_HOME__ paths must say "not for MDM" in its name,
# ABOUTME: install.sh must drop that label when resolving locally, and the Jamf build must relabel it "(Jamf <version>)".

"""Pushing the placeholder profile via Jamf broke Claude Desktop twice during rollout
("Credential helper failed to start: Path must be absolute"). Both files had the same
display name, so the mistake was invisible in Jamf. The name now tells them apart."""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claude_code_with_bedrock.cli.commands.package import PackageCommand
from claude_code_with_bedrock.cli.utils.cowork_3p import (
    MANUAL_INSTALL_SUFFIX,
    build_mdm_config,
    generate_mobileconfig,
)
from claude_code_with_bedrock.config import Profile

REPO = Path(__file__).resolve().parents[2]


def _names(path: Path) -> list[str]:
    p = plistlib.loads(path.read_bytes())
    return [p["PayloadDisplayName"]] + [c["PayloadDisplayName"] for c in p["PayloadContent"]]


def _mdm(**kw) -> dict:
    return build_mdm_config(bedrock_region="eu-central-1", model_aliases=["sonnet"], **kw)


def test_placeholder_profile_is_labelled_not_for_mdm(tmp_path):
    cfg = _mdm()
    assert "__CCWB_HOME__" in str(cfg)  # helper mode uses the per-user placeholder path
    names = _names(generate_mobileconfig(tmp_path, cfg))
    assert all(n.endswith(MANUAL_INSTALL_SUFFIX) for n in names)


def test_profile_without_placeholders_keeps_plain_name(tmp_path):
    cfg = _mdm(credential_mode="profile")
    cfg.pop("inferenceCredentialHelper", None)
    assert "__CCWB_HOME__" not in str(cfg)
    assert _names(generate_mobileconfig(tmp_path, cfg)) == [
        "Claude Cowork with Amazon Bedrock",
        "Claude Cowork - Bedrock Configuration",
    ]


@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="install.sh runs on macOS/Linux")
def test_install_sh_resolves_paths_and_drops_label(tmp_path):
    profile = Profile(
        name="p",
        provider_domain="x.okta.com",
        client_id="c",
        credential_storage="session",
        aws_region="eu-central-1",
        identity_pool_name="i",
    )
    installer = PackageCommand()._create_installer(tmp_path, profile, [("macos", Path("/tmp/x"))]).read_text()
    line = next(x.strip() for x in installer.splitlines() if "__CCWB_HOME__|" in x and x.strip().startswith("sed"))
    generate_mobileconfig(tmp_path, _mdm())
    home = tmp_path / "home"
    r = subprocess.run(
        ["bash", "-c", f'ACTUAL_HOME="{home}"; _ccwb_mdm=cowork-3p.mobileconfig; {line}'],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "cowork-3p.mobileconfig").read_text()
    assert "__CCWB_HOME__" not in text and str(home) in text
    assert _names(tmp_path / "cowork-3p.mobileconfig")[0] == "Claude Cowork with Amazon Bedrock"


@pytest.mark.skipif(not shutil.which("pkgbuild") or not shutil.which("plutil"), reason="needs macOS pkgbuild/plutil")
def test_jamf_build_relabels_profile_and_resolves_paths(tmp_path):
    dist = tmp_path / "dist"
    (dist / "claude-settings").mkdir(parents=True)
    generate_mobileconfig(dist, _mdm())
    for f in ("install.sh", "config.json", "cowork-credential-helper.sh", "credential-process-macos-arm64"):
        (dist / f).write_text("x")
    (dist / "claude-settings" / "settings.json").write_text("{}")
    r = subprocess.run(
        [str(REPO / "scripts/jamf/build-jamf-pkg.sh"), str(dist), "--version", "9.9.9"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    jamf = dist / "jamf" / "cowork-3p-jamf.mobileconfig"
    names = _names(jamf)
    assert names[0] == "Claude Cowork with Amazon Bedrock (Jamf 9.9.9)"
    assert not any("not for MDM" in n for n in names)
    assert "__CCWB_HOME__" not in jamf.read_text()
    assert "/usr/local/taxyio-ccwb/shims/cowork-credential-helper" in jamf.read_text()
    assert (dist / "jamf" / "taxyio-ccwb-9.9.9.pkg").exists()
    assert "Do NOT upload" in r.stdout
