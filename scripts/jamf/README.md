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
- `--prefix`, `--identifier`: defaults match earlier Taxy releases (`/usr/local/taxyio-ccwb`, `io.taxy.claude-code-with-bedrock`).

**Upload `cowork-3p-jamf.mobileconfig`, not `cowork-3p.mobileconfig`.** The plain one still contains the `__CCWB_HOME__` placeholder (resolved by `install.sh` for manual installs). Claude Desktop rejects it when pushed via MDM ("Path must be absolute"). Claude Desktop does not expand `~` or `$HOME` in MDM values, so the Jamf profile points at fixed shims in `<prefix>/shims/`, which exec the per-user helpers in `~/claude-code-with-bedrock/`.

Before uploading, confirm the script printed the Claude Desktop MCP servers you expect.
