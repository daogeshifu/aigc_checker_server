#!/usr/bin/env bash
# One entry point for the existing API, Ollama, and optional original-text scorer.
set -Eeuo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")"
exec python3 -m scripts.manage "$@"
