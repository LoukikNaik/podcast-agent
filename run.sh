#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$ROOT/.venv/bin:$PATH"
if [[ ! -x "$ROOT/.venv/bin/podcast-agent" ]]; then
  echo "Run ./setup.sh first."
  exit 1
fi
if [[ $# -eq 0 ]]; then
  set -- run
fi
exec "$ROOT/.venv/bin/podcast-agent" --project "$ROOT" "$@"
