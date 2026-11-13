#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$HERE/check-sdk.py" --sdk "${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
