# Jamf packaging

`build-jamf-pkg.sh` wraps a `ccwb package` macOS build into what Jamf deploys:

| Output (`<dist>/jamf/`) | Upload to Jamf as |
|---|---|
| `<name>-<version>.pkg` | Package in the Claude policy. Installs to `--prefix` (default `/usr/local/taxyio-ccwb`); `postinstall` runs `install.sh` as the logged-in user |
| `cowork-3p-jamf.mobileconfig` | Claude Desktop configuration profile |

```bash
cd source
poetry run ccwb package --profile <profile> --target-platform macos-arm64
../scripts/jamf/build-jamf-pkg.sh dist/<profile>/<timestamp> --version 2026.11.03.2 --cert-dir ~/.ccwb/jamf-certs
```

- `--version`: bump for every release (Jamf and `pkgutil` compare versions).
- `--cert-dir`: directory with `cert.pem` / `key.pem` for Entra ID client-certificate auth. Omit for deployments without a client certificate. Never commit these files.
- `--binaries-from <dir>`: reuse `credential-process` / `otel-helper` from an earlier release (a ccwb dist dir or a previous `jamf/root/<prefix>`). **Use it whenever `source/go` hasn't changed since that release.** See the next section.
- `--prefix`, `--identifier`: defaults match earlier Taxy releases (`/usr/local/taxyio-ccwb`, `io.taxy.claude-code-with-bedrock`).

**Upload `cowork-3p-jamf.mobileconfig`, not `cowork-3p.mobileconfig`.** The plain one still contains the `__CCWB_HOME__` placeholder (resolved by `install.sh` for manual installs). Claude Desktop rejects it when pushed via MDM ("Path must be absolute"). Claude Desktop does not expand `~` or `$HOME` in MDM values, so the Jamf profile points at fixed shims in `<prefix>/shims/`, which exec the per-user helpers in `~/claude-code-with-bedrock/`.

Before uploading, confirm the script printed the Claude Desktop MCP servers you expect.

## Keychain prompts after an update

With `credential_storage: keyring`, macOS lets only the binary that created a keychain item read it without asking. `credential-process` is ad-hoc signed and embeds its git version, so **every rebuild counts as a different app**. After an update, each user sees "credential-process wants to access key in your keychain", once per item (`-credentials`, `-monitoring`). Clicking **Allow** covers only that single read, so the prompt keeps coming back. **Always Allow** stops it until the next new binary.

To avoid this:
- If `source/go` is unchanged since the last release (`git log <last-release-commit>.. -- source/go` is empty), build with `--binaries-from <last release>`. The script prints the CDHash; it should match the previous release.
- Long term, sign the binaries with a Developer ID certificate. The keychain then trusts the signing identity instead of the exact build.
