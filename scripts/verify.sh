#!/bin/sh
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_bin="$repo_dir/.venv/bin/python"
if [ ! -x "$python_bin" ]; then
  echo "venv da API ausente: $python_bin" >&2
  exit 1
fi
export PYTHONPATH="$repo_dir/services/app:$repo_dir/packages/pipeline-core/src:$repo_dir/services/sam3-worker:$repo_dir"
"$python_bin" -W error -m unittest discover -s "$repo_dir/tests" -p 'test_*.py' -v
"$python_bin" -W error -m unittest discover -s "$repo_dir/services/app/server/tests" -p 'test_*.py' -v
(cd "$repo_dir/services/app/web" && npm run build)
docker compose -f "$repo_dir/compose.yml" config --quiet
