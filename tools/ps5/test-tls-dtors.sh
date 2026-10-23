#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python3}"
"$PYTHON" "$ROOT/tools/ps5/test-shared-tls.py"
"$PYTHON" "$ROOT/tools/ps5/test-shared-exit.py"
