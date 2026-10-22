#!/usr/bin/env bash
# Thin wrapper: the demo lives in `python -m cgate demo` (added in task B4).
set -eu
cd "$(dirname "$0")/.."
exec python -m cgate demo "$@"
