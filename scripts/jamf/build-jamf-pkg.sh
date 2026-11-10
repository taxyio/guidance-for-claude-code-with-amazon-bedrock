#!/bin/bash
# ABOUTME: Wraps a 'ccwb package' macOS build into a Jamf installer .pkg plus a Jamf-ready Claude Desktop profile.
# ABOUTME: Usage: build-jamf-pkg.sh <ccwb-dist-dir> --version <x.y.z> [--prefix DIR] [--identifier ID] [--cert-dir DIR] [--binaries-from DIR]
#
# Output (in <ccwb-dist-dir>/jamf/):
#   <name>-<version>.pkg          payload in <prefix>; postinstall runs install.sh as the console user
#   cowork-3p-jamf.mobileconfig   Claude Desktop profile with absolute helper paths (<prefix>/shims/...)
#
# Why the shims: Claude Desktop reads helper paths from MDM literally (no ~ or $HOME), and a
# centrally pushed profile cannot contain per-user paths. The shims live at a fixed path and
# exec the per-user helpers that install.sh puts in ~/claude-code-with-bedrock.
#
# --binaries-from DIR: ship the macOS binaries from an earlier release instead of this build.
# Keychain items (credential_storage=keyring) trust the exact binary; ad-hoc signed Go builds
# embed the git version, so every rebuild is a "new app" and each user gets keychain prompts
# after the update. If source/go is unchanged since that release, reuse its binaries.
#
# Upload BOTH files: the plain cowork-3p.mobileconfig from 'ccwb package' still contains the
# __CCWB_HOME__ placeholder (install.sh resolves it locally) and breaks Desktop if pushed via MDM.
set -euo pipefail

usage() { sed -n '2,3p' "$0" | sed 's/^# ABOUTME: //'; exit 2; }

DIST="" VERSION="" PREFIX="/usr/local/taxyio-ccwb" IDENTIFIER="io.taxy.claude-code-with-bedrock" CERT_DIR="" BIN_FROM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --prefix) PREFIX="${2%/}"; shift 2 ;;
        --identifier) IDENTIFIER="$2"; shift 2 ;;
        --cert-dir) CERT_DIR="$2"; shift 2 ;;
        --binaries-from) BIN_FROM="${2%/}"; shift 2 ;;
        -h|--help) usage ;;
        -*) echo "Unknown option: $1" >&2; usage ;;
        *) DIST="${1%/}"; shift ;;
    esac
done
[ -n "$DIST" ] && [ -n "$VERSION" ] || usage
case "$PREFIX" in /*) ;; *) echo "--prefix must be absolute" >&2; exit 2 ;; esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES="$HERE/templates"
OUT="$DIST/jamf"
ROOT="$OUT/root"
PAYLOAD="$ROOT$PREFIX"

# Inputs produced by 'ccwb package --target-platform macos-arm64' (and/or macos-intel).
required=(install.sh config.json cowork-3p.mobileconfig cowork-credential-helper.sh claude-settings/settings.json)
for f in "${required[@]}"; do
    [ -e "$DIST/$f" ] || { echo "Missing $DIST/$f — is this a 'ccwb package' output dir?" >&2; exit 1; }
done
ls "$DIST"/credential-process-macos-* >/dev/null 2>&1 || { echo "No macOS credential-process binary in $DIST" >&2; exit 1; }
if [ -n "$CERT_DIR" ]; then
    for pem in cert.pem key.pem; do [ -f "$CERT_DIR/$pem" ] || { echo "Missing $CERT_DIR/$pem" >&2; exit 1; }; done
fi
if [ -n "$BIN_FROM" ]; then
    for b in "$DIST"/credential-process-macos-* "$DIST"/otel-helper-macos-*; do
        [ -e "$b" ] || continue
        [ -f "$BIN_FROM/$(basename "$b")" ] || { echo "Missing $BIN_FROM/$(basename "$b") (--binaries-from)" >&2; exit 1; }
    done
fi

rm -rf "$OUT"
mkdir -p "$PAYLOAD/shims" "$PAYLOAD/claude-settings" "$OUT/scripts"

# Payload: the ccwb package files install.sh needs (same set as earlier releases).
for f in README.md config.json cowork-3p-config.json cowork-3p.mobileconfig cowork-3p.reg \
         cowork-credential-helper.cmd cowork-credential-helper.sh install.sh; do
    [ -e "$DIST/$f" ] && cp -p "$DIST/$f" "$PAYLOAD/"
done
BIN_SRC="${BIN_FROM:-$DIST}"
for b in "$DIST"/credential-process-macos-* "$DIST"/otel-helper-macos-*; do
    [ -e "$b" ] && cp -p "$BIN_SRC/$(basename "$b")" "$PAYLOAD/"
done
cp -p "$DIST/claude-settings/settings.json" "$PAYLOAD/claude-settings/"
if [ -n "$CERT_DIR" ]; then
    cp "$CERT_DIR/cert.pem" "$PAYLOAD/cert.pem" && chmod 644 "$PAYLOAD/cert.pem"
    cp "$CERT_DIR/key.pem" "$PAYLOAD/key.pem" && chmod 600 "$PAYLOAD/key.pem"
fi
cp "$TEMPLATES"/shims/* "$PAYLOAD/shims/"
sed "s|__PKG_DIR__|$PREFIX|g" "$TEMPLATES/postinstall" > "$OUT/scripts/postinstall"
chmod 755 "$OUT/scripts/postinstall" "$PAYLOAD/install.sh" "$PAYLOAD/shims/"* "$PAYLOAD"/credential-process-macos-*
chmod 755 "$PAYLOAD"/otel-helper-macos-* 2>/dev/null || true

# Jamf profile: replace the per-user placeholder paths with the fixed shim paths.
# Also relabel it: ccwb names placeholder profiles "... (manual install: run install.sh
# first; not for MDM)"; the Jamf one says "(Jamf <version>)" so the two can't be confused.
MANUAL_SUFFIX=" (manual install: run install.sh first; not for MDM)"
sed -e "s|__CCWB_HOME__/claude-code-with-bedrock/websearch-headers|$PREFIX/shims/websearch-headers|g" \
    -e "s|__CCWB_HOME__/claude-code-with-bedrock/cowork-credential-helper.sh|$PREFIX/shims/cowork-credential-helper|g" \
    -e "s|$MANUAL_SUFFIX</string>|</string>|g" \
    -e "s|<string>Claude Cowork with Amazon Bedrock</string>|<string>Claude Cowork with Amazon Bedrock (Jamf $VERSION)</string>|" \
    "$DIST/cowork-3p.mobileconfig" > "$OUT/cowork-3p-jamf.mobileconfig"
if grep -q "__CCWB_HOME__" "$OUT/cowork-3p-jamf.mobileconfig"; then
    echo "ERROR: unresolved __CCWB_HOME__ left in cowork-3p-jamf.mobileconfig:" >&2
    grep -o "__CCWB_HOME__[^<\"]*" "$OUT/cowork-3p-jamf.mobileconfig" | sort -u >&2
    exit 1
fi
if grep -q "not for MDM" "$OUT/cowork-3p-jamf.mobileconfig"; then
    echo "ERROR: cowork-3p-jamf.mobileconfig is still labelled 'not for MDM'" >&2
    exit 1
fi
plutil -lint "$OUT/cowork-3p-jamf.mobileconfig" >/dev/null

NAME="$(basename "$PREFIX")"
# Some endpoint-security setups make pkgbuild print a harmless "write: Permission denied"
# (even for trivial packages); drop exactly that line, keep every other error.
pkgbuild --root "$ROOT" --scripts "$OUT/scripts" --identifier "$IDENTIFIER" \
    --version "$VERSION" --install-location / "$OUT/$NAME-$VERSION.pkg" \
    > /dev/null 2> >(grep -v '^write: Permission denied$' >&2)

echo "Upload to Jamf (only these two):"
echo "  package:               $OUT/$NAME-$VERSION.pkg  ($IDENTIFIER $VERSION, payload in $PREFIX)"
echo "  configuration profile: $OUT/cowork-3p-jamf.mobileconfig  (shows as 'Claude Cowork with Amazon Bedrock (Jamf $VERSION)')"
echo "Do NOT upload $DIST/cowork-3p.mobileconfig: it is for manual installs and breaks Claude Desktop via MDM."
servers=$(python3 - "$OUT/cowork-3p-jamf.mobileconfig" <<'PY'
import json, plistlib, sys
p = plistlib.load(open(sys.argv[1], "rb"))
d = next(c for c in p["PayloadContent"] if c.get("PayloadType") == "com.anthropic.claudefordesktop")
print(", ".join(s["name"] for s in json.loads(d.get("managedMcpServers", "[]"))))
PY
)
echo "  Claude Desktop MCP servers: ${servers:-none}"
[ -n "$CERT_DIR" ] || echo "  (no --cert-dir: package carries no client certificate)"
for b in "$PAYLOAD"/credential-process-macos-*; do
    echo "  $(basename "$b") CDHash: $(codesign -dv --verbose=4 "$b" 2>&1 | sed -n 's/^CDHash=//p')${BIN_FROM:+ (reused from $BIN_FROM)}"
done
[ -n "$BIN_FROM" ] || echo "  New binaries: users with credential_storage=keyring get keychain prompts after this update (see README)."
