# ABOUTME: Regression test: install.sh must replace executables with a fresh file (rm, then cp), never overwrite in place.
# ABOUTME: macOS caches code signatures per file; an executable overwritten in place is SIGKILLed ("Killed: 9") at launch.

import re
from pathlib import Path

from claude_code_with_bedrock.cli.commands.package import PackageCommand
from claude_code_with_bedrock.config import Profile


def _installer(tmp_path: Path) -> str:
    profile = Profile(
        name="p",
        provider_domain="x.okta.com",
        client_id="c",
        credential_storage="session",
        aws_region="eu-central-1",
        identity_pool_name="i",
        monitoring_enabled=True,
        monitoring_mode="sidecar",
    )
    return PackageCommand()._create_installer(tmp_path, profile, [("macos", Path("/tmp/x"))]).read_text()


def test_binaries_are_removed_before_copy(tmp_path):
    lines = [x.strip() for x in _installer(tmp_path).splitlines()]
    copies = [
        (i, m.group(1))
        for i, x in enumerate(lines)
        if (m := re.match(r'cp "\$(?:CREDENTIAL|OTEL|OTELCOL)_BINARY" (".*")$', x))
    ]
    assert len(copies) >= 2, copies  # credential-process and otel-helper at least
    for i, dest in copies:
        assert lines[i - 1].split("  #")[0] == f"rm -f {dest}", (
            f"{dest} is overwritten in place (line before: {lines[i - 1]!r})"
        )
