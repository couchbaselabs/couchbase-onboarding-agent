#!/usr/bin/env bash
set -e

mkdir -p /data/state /data/scratch

exec "$@"
