#!/usr/bin/env bash
set -euo pipefail

APP_UID=10001
APP_GID=10001
DATA_DIRS=(/data/state /data/scratch /data/cbbackupmgr-archives)

if [ "$(id -u)" = "0" ]; then
  # Docker Compose: start as root, make sure the data volumes belong to the app
  # user (volumes created by versions that ran as root are root-owned), then
  # drop privileges. Only files with the wrong owner are touched, so this is a
  # no-op after the first start.
  mkdir -p "${DATA_DIRS[@]}"
  find "${DATA_DIRS[@]}" \( ! -user "$APP_UID" -o ! -group "$APP_GID" \) \
    -exec chown "$APP_UID:$APP_GID" {} + 2>/dev/null || true
  export HOME=/tmp
  # Drop to the app user with Python (always present in this image) rather than
  # gosu/setpriv, which slim Debian images don't ship.
  exec python - "$APP_UID" "$APP_GID" "$@" <<'PY'
import os, sys
uid, gid, argv = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:]
os.setgroups([gid])
os.setgid(gid)
os.setuid(uid)
os.execvp(argv[0], argv)
PY
fi

# Kubernetes (or `docker run --user`): already unprivileged.
mkdir -p "${DATA_DIRS[@]}" 2>/dev/null || true
exec "$@"
