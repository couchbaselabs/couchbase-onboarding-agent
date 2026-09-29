#!/usr/bin/env bash
set -euo pipefail

# State (encrypted migration records + checkpoints), scratch space and
# cbbackupmgr archives. Created in the image; this covers fresh mounts.
mkdir -p /data/state /data/scratch /data/cbbackupmgr-archives

exec "$@"
