#!/usr/bin/env bash
# Exports the root CA(s) this Mac trusts for TLS-inspecting corporate proxies
# (Zscaler, Netskope, Palo Alto GlobalProtect, etc.) so the Docker build can trust
# them too. See couchbase-migration-agent's README for the full explanation of why
# this is needed -- the short version: `pip install`/`npm install` at build time,
# and Ollama's model pull at container startup, each verify TLS against their own
# bundled CA store, not macOS's system trust store that Docker Desktop's own image
# pulls rely on.
#
# Drops the exported cert into certs/ (repo root), backend/certs/, frontend/certs/,
# and qwen-service/certs/ -- one per Docker build context. Gitignored,
# machine-specific, and a no-op on machines that don't need it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIRS=("$REPO_ROOT/certs" "$REPO_ROOT/backend/certs" "$REPO_ROOT/frontend/certs" "$REPO_ROOT/qwen-service/certs")

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script currently only supports macOS (it uses the 'security' CLI to read the keychain)." >&2
  echo "On Linux, ask IT for your org's proxy root CA .pem and copy it manually to:" >&2
  for d in "${DEST_DIRS[@]}"; do echo "  ${d#$REPO_ROOT/}/corporate-ca.crt" >&2; done
  exit 1
fi

TMP_CERT="$(mktemp)"
trap 'rm -f "$TMP_CERT"' EXIT

echo "Exporting certificates from the System keychain..."
security find-certificate -a -p /Library/Keychains/System.keychain > "$TMP_CERT" 2>/dev/null || true

CERT_COUNT=$(grep -c "BEGIN CERTIFICATE" "$TMP_CERT" 2>/dev/null || echo 0)

if [[ "$CERT_COUNT" -eq 0 ]]; then
  echo "No certificates found in the System keychain -- nothing to export."
  echo "If your build still fails with a self-signed-certificate error, ask IT for"
  echo "the proxy's root CA and save it as corporate-ca.crt in each of:"
  for d in "${DEST_DIRS[@]}"; do echo "  ${d#$REPO_ROOT/}/"; done
  for d in "${DEST_DIRS[@]}"; do
    mkdir -p "$d"
    : > "$d/corporate-ca.crt"
  done
  exit 0
fi

for d in "${DEST_DIRS[@]}"; do
  mkdir -p "$d"
  cp "$TMP_CERT" "$d/corporate-ca.crt"
done

echo "Exported $CERT_COUNT certificate(s) to:"
for d in "${DEST_DIRS[@]}"; do echo "  ${d#$REPO_ROOT/}/corporate-ca.crt"; done
echo
echo "Now run: docker compose build --no-cache"
