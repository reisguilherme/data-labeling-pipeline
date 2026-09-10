#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."
exec docker compose -f compose.yml run --rm --no-deps app reconcile-pipeline-projection "$@"
