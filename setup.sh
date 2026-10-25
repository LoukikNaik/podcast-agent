#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
  echo "The automatic installer supports Apple Silicon Macs. See README.md for manual setup."
  exit 1
fi
MACOS_VERSION="$(sw_vers -productVersion)"
if [[ "${MACOS_VERSION%%.*}" -lt 14 ]]; then
  echo "The Homebrew PodClipper installer requires macOS 14 or newer."
  exit 1
fi
if ! command -v brew >/dev/null 2>&1; then
  echo "Install Homebrew from https://brew.sh, then run ./setup.sh again."
  exit 1
fi
for TOOL in python3.12 ffmpeg deno; do
  if ! command -v "$TOOL" >/dev/null 2>&1; then
    PACKAGE="$TOOL"
    [[ "$TOOL" != python3.12 ]] || PACKAGE="python@3.12"
    brew install "$PACKAGE"
  fi
done
if ! command -v codex >/dev/null 2>&1; then
  brew install --cask codex
fi
if ! command -v podclipper >/dev/null 2>&1; then
  brew install loukiknaik/tap/podclipper
fi
if [[ ! -x .venv/bin/python ]]; then
  python3.12 -m venv .venv
fi
.venv/bin/python -m pip install -e .
./run.sh setup
./run.sh doctor
