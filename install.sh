#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)

case "${1-}" in
  -h|--help)
    printf 'Usage: install.sh [--prepare-only] [--force-deps]\n'
    exit 0
    ;;
esac

exec "$repo_dir/bin/install" "$@"
