#!/bin/sh
# Runs as /docker-entrypoint.d/25-tls.sh, i.e. after the nginx image has
# rendered the *.template files (20-envsubst-on-templates.sh), as the
# unprivileged nginx user.
#
#   TLS_ENABLED=false      -> drop the HTTPS server; plain HTTP on 8080 only
#   /etc/nginx/tls/server.{crt,key} present (mounted Secret / bind mount)
#                          -> use them
#   otherwise              -> generate a self-signed cert whose SANs cover
#                             TLS_HOSTNAME (comma-separated names and/or IPs),
#                             localhost and 127.0.0.1
set -e
CONF=/etc/nginx/conf.d/tls.conf
TLS_DIR=/etc/nginx/tls

if [ "${TLS_ENABLED:-true}" != "true" ]; then
  echo "[frontend] TLS_ENABLED=${TLS_ENABLED}: HTTPS server disabled, serving plain HTTP on 8080"
  rm -f "$CONF"
  exit 0
fi

CRT="$TLS_DIR/server.crt"; KEY="$TLS_DIR/server.key"
if [ -s "$CRT" ] && [ -s "$KEY" ]; then
  echo "[frontend] using TLS certificate at $CRT"
  exit 0
fi

# Generate. Prefer the standard directory; if it isn't writable (read-only
# mount without the files), fall back to /tmp and point the config there.
if ! { mkdir -p "$TLS_DIR" && touch "$TLS_DIR/.writable"; } 2>/dev/null; then
  TLS_DIR=/tmp/nginx-tls
  mkdir -p "$TLS_DIR"
  sed -i "s#/etc/nginx/tls/#$TLS_DIR/#g" "$CONF"
  CRT="$TLS_DIR/server.crt"; KEY="$TLS_DIR/server.key"
fi
rm -f "$TLS_DIR/.writable" 2>/dev/null || true

NAMES="${TLS_HOSTNAME:-localhost}"
SAN="DNS:localhost,IP:127.0.0.1"
CN=""
for n in $(echo "$NAMES" | tr ',' ' '); do
  [ -n "$CN" ] || CN="$n"
  case "$n" in
    localhost|127.0.0.1) ;;
    *[!0-9.]*) SAN="$SAN,DNS:$n" ;;
    *)         SAN="$SAN,IP:$n" ;;
  esac
done
echo "[frontend] no TLS certificate found -- generating a self-signed one (SAN: $SAN)"
openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
  -keyout "$KEY" -out "$CRT" \
  -subj "/CN=${CN:-localhost}/O=Couchbase Labs Onboarding Agent" \
  -addext "subjectAltName=$SAN" >/dev/null 2>&1
chmod 600 "$KEY"
echo "[frontend] self-signed certificate written to $CRT (the browser warns until you trust it or mount a real one)"
