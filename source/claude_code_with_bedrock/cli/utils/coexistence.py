# ABOUTME: Side-by-side install of Claude Code on claude.ai (`claude`) and Amazon Bedrock (`claude-bedrock`).
# ABOUTME: Generates the installer snippets (bash + Windows) that isolate Bedrock settings in ~/.claude-bedrock.

"""Side-by-side ("coexistence") mode for Claude Code.

By default the installer writes the Bedrock settings (``CLAUDE_CODE_USE_BEDROCK``,
credential hooks, OTEL) into ``~/.claude/settings.json``, so every ``claude``
launch goes to Bedrock and the user's claude.ai login — including its claude.ai
connectors (Gmail, Slack, Atlassian, ...) — is unreachable.

With ``Profile.claude_code_coexistence`` enabled the installer instead:

1. Writes the Bedrock settings to ``~/.claude-bedrock/settings.json``.
2. Removes Bedrock keys that earlier installs left in ``~/.claude/settings.json``
   (backing the file up first) so plain ``claude`` goes back to claude.ai.
3. Shares ``skills/``, ``agents/`` and ``commands/`` with ``~/.claude`` (symlinks
   on macOS/Linux, directory junctions on Windows) and seeds a ``CLAUDE.md`` that
   imports ``~/.claude/CLAUDE.md``.
4. Installs a ``claude-bedrock`` launcher that sets ``CLAUDE_CONFIG_DIR`` to the
   Bedrock directory before running ``claude``.

Telemetry and quota stay Bedrock-only because the hooks and OTEL env live only
in the Bedrock settings file. Requires ``settings_target == "user"``:
``managed-settings.json`` applies to every session regardless of
``CLAUDE_CONFIG_DIR`` (enforced by ``validate_profile_for_packaging``).
"""

from __future__ import annotations

import base64

BEDROCK_CONFIG_DIR_NAME = ".claude-bedrock"
DEFAULT_CONFIG_DIR_NAME = ".claude"
LAUNCHER_NAME = "claude-bedrock"
INSTALL_DIR_MARKER = "claude-code-with-bedrock"

# Directories shared between ~/.claude and ~/.claude-bedrock.
SHARED_DIRS = ("skills", "agents", "commands")

# Every env key _create_claude_settings may emit. The installer additionally
# strips whatever env keys the packaged settings.json carries, so new keys added
# there later are covered automatically.
BEDROCK_ENV_KEYS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_REGION",
    "AWS_PROFILE",
    "AWS_CREDENTIAL_PROCESS",
    "AWS_EC2_METADATA_DISABLED",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_RESOURCE_ATTRIBUTES",
)

# Top-level hooks the installer writes. Removed only when they point at the
# claude-code-with-bedrock install dir, so user-defined hooks survive.
BEDROCK_HOOK_KEYS = ("awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper")


def coexistence_enabled(profile) -> bool:
    """True when the profile asks for side-by-side mode AND uses user-scope settings."""
    return bool(getattr(profile, "claude_code_coexistence", False)) and (
        getattr(profile, "settings_target", "user") == "user"
    )


def claude_dir_name(profile) -> str:
    """Config directory (relative to home) the installer writes Bedrock settings into."""
    return BEDROCK_CONFIG_DIR_NAME if coexistence_enabled(profile) else DEFAULT_CONFIG_DIR_NAME


# ---------------------------------------------------------------------------
# Strip script — runs on the END USER's machine with the system Python (bash
# installer). Takes argv: <settings.json to clean> <packaged settings.json>.
# Only touches files the installer previously wrote: CLAUDE_CODE_USE_BEDROCK set,
# or a hook pointing at the install dir. Never fails the install.
# ---------------------------------------------------------------------------
STRIP_SCRIPT_PY = """\
import json, os, shutil, sys, time

path, incoming_path = sys.argv[1], sys.argv[2]
ENV_KEYS = set(__ENV_KEYS__)
HOOK_KEYS = __HOOK_KEYS__
MARKER = __MARKER__

if not os.path.isfile(path):
    sys.exit(0)
try:
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)
except ValueError:
    print("  WARN Could not parse " + path + " - left unchanged")
    sys.exit(0)
if not isinstance(settings, dict):
    sys.exit(0)

env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
our_hooks = [k for k in HOOK_KEYS if MARKER in str(settings.get(k, ""))]
if not env.get("CLAUDE_CODE_USE_BEDROCK") and not our_hooks:
    sys.exit(0)

try:
    with open(incoming_path, encoding="utf-8") as f:
        ENV_KEYS.update((json.load(f).get("env") or {}).keys())
except (OSError, ValueError, AttributeError):
    pass

backup = path + ".backup-" + time.strftime("%Y%m%d-%H%M%S")
shutil.copy2(path, backup)
removed = []
for k in sorted(ENV_KEYS):
    if k in env:
        del env[k]
        removed.append("env." + k)
if "env" in settings and not env:
    del settings["env"]
for k in our_hooks:
    del settings[k]
    removed.append(k)
with open(path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\\n")
print("  OK Removed Bedrock settings from " + path + ": " + ", ".join(removed))
print("     Backup: " + backup)
"""
STRIP_SCRIPT_PY = (
    STRIP_SCRIPT_PY.replace("__ENV_KEYS__", repr(tuple(BEDROCK_ENV_KEYS)))
    .replace("__HOOK_KEYS__", repr(tuple(BEDROCK_HOOK_KEYS)))
    .replace("__MARKER__", repr(INSTALL_DIR_MARKER))
)


def _ps_array(items) -> str:
    return "@(" + ",".join(f"'{i}'" for i in items) + ")"


# PowerShell twin of STRIP_SCRIPT_PY for install.bat (no Python on Windows).
# Cleans %USERPROFILE%\.claude\settings.json; reads the packaged
# claude-settings\settings.json relative to the installer's working directory.
STRIP_SCRIPT_PS1 = f"""\
$ErrorActionPreference = 'Stop'
try {{
    $path = Join-Path $env:USERPROFILE '.claude\\settings.json'
    if (-not (Test-Path $path)) {{ exit 0 }}
    try {{ $s = Get-Content $path -Raw | ConvertFrom-Json }} catch {{
        Write-Host ('  WARN Could not parse ' + $path + ' - left unchanged'); exit 0
    }}
    if ($null -eq $s) {{ exit 0 }}
    $marker = '{INSTALL_DIR_MARKER}'
    $envObj = $null
    if ($s.PSObject.Properties['env']) {{ $envObj = $s.env }}
    $ours = $false
    if (($null -ne $envObj) -and $envObj.PSObject.Properties['CLAUDE_CODE_USE_BEDROCK'] -and $envObj.CLAUDE_CODE_USE_BEDROCK) {{ $ours = $true }}
    $ourHooks = @({_ps_array(BEDROCK_HOOK_KEYS)} | Where-Object {{ $s.PSObject.Properties[$_] -and ([string]$s.$_).Contains($marker) }})
    if ($ourHooks.Count -gt 0) {{ $ours = $true }}
    if (-not $ours) {{ exit 0 }}
    $envKeys = {_ps_array(BEDROCK_ENV_KEYS)}
    $incomingPath = 'claude-settings\\settings.json'
    if (Test-Path $incomingPath) {{
        try {{
            $inc = Get-Content $incomingPath -Raw | ConvertFrom-Json
            if ($inc.env) {{ $envKeys += @($inc.env.PSObject.Properties.Name) }}
        }} catch {{ }}
    }}
    $backup = $path + '.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Copy-Item $path $backup
    $removed = @()
    if ($null -ne $envObj) {{
        foreach ($k in ($envKeys | Sort-Object -Unique)) {{
            if ($envObj.PSObject.Properties[$k]) {{ $envObj.PSObject.Properties.Remove($k); $removed += ('env.' + $k) }}
        }}
        if (@($envObj.PSObject.Properties).Count -eq 0) {{ $s.PSObject.Properties.Remove('env') }}
    }}
    foreach ($k in $ourHooks) {{ $s.PSObject.Properties.Remove($k); $removed += $k }}
    $s | ConvertTo-Json -Depth 10 | Set-Content $path
    Write-Host ('  OK Removed Bedrock settings from ' + $path + ': ' + ($removed -join ', '))
    Write-Host ('     Backup: ' + $backup)
}} catch {{
    Write-Host ('  WARN Could not clean ~/.claude/settings.json: ' + $_)
    exit 0
}}
"""


def encode_powershell(script: str) -> str:
    """Encode a script for ``powershell -EncodedCommand`` (base64 of UTF-16LE).

    Avoids every batch quoting hazard (``%``, ``!`` under delayed expansion,
    nested quotes) when embedding multi-line PowerShell in install.bat.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


# ---------------------------------------------------------------------------
# bash snippets (install.sh). Use the installer's $ACTUAL_HOME / $ACTUAL_USER /
# $SUDO_USER / $PYTHON variables. Plain (non-f) strings: safe to splice into the
# installer's f-string via substitution.
# ---------------------------------------------------------------------------


def bash_migration_block() -> str:
    """Clean ~/.claude/settings.json and share personal assets with ~/.claude-bedrock."""
    shared = " ".join(SHARED_DIRS)
    return (
        r"""
# --- Side-by-side Claude Code (claude.ai + Bedrock) ---
# Plain 'claude' must stay on the user's claude.ai login, so remove Bedrock keys
# that earlier installs wrote to ~/.claude/settings.json (backed up first).
BEDROCK_DIR="$ACTUAL_HOME/"""
        + BEDROCK_CONFIG_DIR_NAME
        + r""""
mkdir -p "$BEDROCK_DIR"
if [ -z "$PYTHON" ]; then
    PYTHON="$(command -v python3 || command -v python || true)"
fi
if [ -f "$ACTUAL_HOME/.claude/settings.json" ]; then
    if [ -n "$PYTHON" ]; then
        "$PYTHON" - "$ACTUAL_HOME/.claude/settings.json" "claude-settings/settings.json" <<'CCWB_STRIP_PY' || true
"""
        + STRIP_SCRIPT_PY
        + r"""CCWB_STRIP_PY
        if [ -n "$SUDO_USER" ]; then chown "$ACTUAL_USER" "$ACTUAL_HOME/.claude/"settings.json* 2>/dev/null || true; fi
    else
        echo "  WARN Python not found: remove CLAUDE_CODE_USE_BEDROCK from $ACTUAL_HOME/.claude/settings.json manually"
    fi
fi

# Share personal skills/agents/commands and CLAUDE.md between both installs.
for _ccwb_d in """
        + shared
        + r"""; do
    mkdir -p "$ACTUAL_HOME/.claude/$_ccwb_d"
    if [ ! -e "$BEDROCK_DIR/$_ccwb_d" ] && [ ! -L "$BEDROCK_DIR/$_ccwb_d" ]; then
        ln -s "$ACTUAL_HOME/.claude/$_ccwb_d" "$BEDROCK_DIR/$_ccwb_d"
    fi
    if [ -n "$SUDO_USER" ]; then chown -h "$ACTUAL_USER" "$ACTUAL_HOME/.claude/$_ccwb_d" "$BEDROCK_DIR/$_ccwb_d" 2>/dev/null || true; fi
done
if [ ! -e "$BEDROCK_DIR/CLAUDE.md" ]; then
    printf '%s\n' "<!-- Shared with claude.ai Claude Code; add Bedrock-only instructions below. -->" "@~/.claude/CLAUDE.md" > "$BEDROCK_DIR/CLAUDE.md"
fi
if [ -n "$SUDO_USER" ]; then chown "$ACTUAL_USER" "$ACTUAL_HOME/.claude" "$BEDROCK_DIR" "$BEDROCK_DIR/CLAUDE.md" 2>/dev/null || true; fi
echo "OK Side-by-side mode: 'claude' -> claude.ai, 'claude-bedrock' -> Bedrock ($BEDROCK_DIR)"
"""
    )


def bash_launcher_block(*, login_profile: bool, default_profile: str | None = None) -> str:
    """Write ~/claude-code-with-bedrock/claude-bedrock and link it into ~/.local/bin.

    Args:
        login_profile: run ``credential-process --login`` first (IDC with binary).
        default_profile: fixed AWS profile; when None the first profile in
            ``$PROFILES`` (set earlier by install.sh) is used.
    """
    first_profile = (
        f'FIRST_PROFILE="{default_profile}"'
        if default_profile
        else "FIRST_PROFILE=$(echo $PROFILES | awk '{print $1}')"
    )
    login_line = '"$CRED_PROC" --login --profile "\\$PROFILE" || exit 1\n' if login_profile else ""
    return (
        r"""
# Generate the 'claude-bedrock' launcher (side-by-side mode). It points Claude
# Code at the Bedrock config dir; plain 'claude' keeps the claude.ai login.
CRED_PROC="$ACTUAL_HOME/claude-code-with-bedrock/credential-process"
BEDROCK_DIR="$ACTUAL_HOME/"""
        + BEDROCK_CONFIG_DIR_NAME
        + r""""
mkdir -p "$ACTUAL_HOME/claude-code-with-bedrock"
LAUNCHER="$ACTUAL_HOME/claude-code-with-bedrock/"""
        + LAUNCHER_NAME
        + '"\n'
        + first_profile
        + r"""
cat > "$LAUNCHER" << EOF
#!/bin/bash
# Launch Claude Code against Amazon Bedrock. Plain 'claude' stays on claude.ai.
export CLAUDE_CONFIG_DIR="$BEDROCK_DIR"
export CLAUDE_CODE_USE_BEDROCK=1
PROFILE="\${AWS_PROFILE:-$FIRST_PROFILE}"
"""
        + login_line
        + r"""export AWS_PROFILE="\$PROFILE"
exec claude "\$@"
EOF
chmod +x "$LAUNCHER"
if [ -n "$SUDO_USER" ]; then chown "$ACTUAL_USER" "$LAUNCHER"; fi
echo "  OK Created launcher: $LAUNCHER"
# Put it on PATH next to the native 'claude' install (~/.local/bin) when present.
LAUNCHER_ON_PATH=""
if [ -d "$ACTUAL_HOME/.local/bin" ]; then
    ln -sf "$LAUNCHER" "$ACTUAL_HOME/.local/bin/"""
        + LAUNCHER_NAME
        + r""""
    if [ -n "$SUDO_USER" ]; then chown -h "$ACTUAL_USER" "$ACTUAL_HOME/.local/bin/"""
        + LAUNCHER_NAME
        + r"""" 2>/dev/null || true; fi
    LAUNCHER_ON_PATH="$ACTUAL_HOME/.local/bin/"""
        + LAUNCHER_NAME
        + r""""
    echo "  OK Linked: $LAUNCHER_ON_PATH"
fi
"""
    )


def bash_closing_block() -> str:
    """Closing message for side-by-side installs."""
    return r"""
echo
echo "======================================"
echo "Installation complete!"
echo "======================================"
echo
echo "Two ways to start Claude Code:"
echo "  claude          -> your claude.ai subscription (connectors, unchanged)"
echo "  claude-bedrock  -> Amazon Bedrock (company account, telemetry + quota)"
echo
if [ -z "$LAUNCHER_ON_PATH" ]; then
    echo "Add the launcher folder to your PATH to run 'claude-bedrock':"
    echo "  export PATH=\"$ACTUAL_HOME/claude-code-with-bedrock:\$PATH\""
    echo
fi
echo "Bedrock settings live in $BEDROCK_DIR (skills/agents/commands are shared)."
echo "IDE extensions: set CLAUDE_CONFIG_DIR=$BEDROCK_DIR in the extension's"
echo "environment to use Bedrock there."
echo
"""


# ---------------------------------------------------------------------------
# Windows snippets (install.bat, which runs with ENABLEDELAYEDEXPANSION).
# ---------------------------------------------------------------------------


def windows_migration_block() -> str:
    """Clean %USERPROFILE%\\.claude\\settings.json and share assets via junctions."""
    shared = " ".join(SHARED_DIRS)
    bdir = "%USERPROFILE%\\" + BEDROCK_CONFIG_DIR_NAME
    return f"""
REM --- Side-by-side Claude Code [claude.ai + Bedrock] ---
REM Remove Bedrock keys that earlier installs wrote to .claude\\settings.json so
REM plain 'claude' stays on the user's claude.ai login [file is backed up first].
if not exist "{bdir}" mkdir "{bdir}"
powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encode_powershell(STRIP_SCRIPT_PS1)}

REM Share personal skills/agents/commands [directory junctions, no admin needed]
REM and CLAUDE.md [imported] between both installs.
for %%d in ({shared}) do (
    if not exist "%USERPROFILE%\\.claude\\%%d" mkdir "%USERPROFILE%\\.claude\\%%d"
    if not exist "{bdir}\\%%d" mklink /J "{bdir}\\%%d" "%USERPROFILE%\\.claude\\%%d" >nul
)
if not exist "{bdir}\\CLAUDE.md" (
    > "{bdir}\\CLAUDE.md" echo @~/.claude/CLAUDE.md
)
echo OK Side-by-side mode: 'claude' = claude.ai, 'claude-bedrock' = Bedrock
"""


def windows_launcher_block(*, login_profile: bool) -> str:
    """Write claude-bedrock.cmd (and copy it into %USERPROFILE%\\.local\\bin when present)."""
    login_lines = (
        '>> "%LAUNCHER%" echo "%CRED_PROC%" --login --profile %%AWS_PROFILE%%\n'
        '>> "%LAUNCHER%" echo if errorlevel 1 exit /b 1\n'
        if login_profile
        else ""
    )
    bdir = "%USERPROFILE%\\" + BEDROCK_CONFIG_DIR_NAME
    return (
        f"""
REM Generate the 'claude-bedrock.cmd' launcher [side-by-side mode]. It points
REM Claude Code at the Bedrock config dir; plain 'claude' keeps claude.ai.
REM setlocal keeps CLAUDE_CONFIG_DIR from leaking into the user's console, which
REM would otherwise switch later plain 'claude' runs to Bedrock.
echo.
echo Creating launcher...
set "LAUNCHER=%USERPROFILE%\\claude-code-with-bedrock\\{LAUNCHER_NAME}.cmd"
set "CRED_PROC=%USERPROFILE%\\claude-code-with-bedrock\\credential-process.exe"
set "FIRST_PROFILE="
for /f %%p in ('powershell -NoProfile -Command "(Get-Content config.json | ConvertFrom-Json).PSObject.Properties.Name | Select-Object -First 1"') do set "FIRST_PROFILE=%%p"
> "%LAUNCHER%" echo @echo off
>> "%LAUNCHER%" echo setlocal
>> "%LAUNCHER%" echo set "CLAUDE_CONFIG_DIR={bdir}"
>> "%LAUNCHER%" echo set CLAUDE_CODE_USE_BEDROCK=1
>> "%LAUNCHER%" echo if "%%AWS_PROFILE%%"=="" set AWS_PROFILE=!FIRST_PROFILE!
"""
        + login_lines
        + f""">> "%LAUNCHER%" echo claude %%*
echo   OK Created launcher: %LAUNCHER%
set "LAUNCHER_ON_PATH=0"
if exist "%USERPROFILE%\\.local\\bin" (
    copy /Y "%LAUNCHER%" "%USERPROFILE%\\.local\\bin\\{LAUNCHER_NAME}.cmd" >nul
    set "LAUNCHER_ON_PATH=1"
    echo   OK Copied launcher to %USERPROFILE%\\.local\\bin
)

echo.
echo ======================================
echo Installation complete!
echo ======================================
echo.
echo Two ways to start Claude Code:
echo   claude          -^> your claude.ai subscription [connectors, unchanged]
echo   claude-bedrock  -^> Amazon Bedrock [company account, telemetry + quota]
echo.
if "!LAUNCHER_ON_PATH!"=="0" (
    echo Add %USERPROFILE%\\claude-code-with-bedrock to your PATH to run 'claude-bedrock'.
    echo.
)
echo Bedrock settings live in {bdir} [skills/agents/commands are shared].
echo IDE extensions: set CLAUDE_CONFIG_DIR={bdir} in the extension's
echo environment to use Bedrock there.
echo.
pause
"""
    )
