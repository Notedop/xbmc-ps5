#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-cache-diagnostics-tests}"
CXX="${CXX:-clang++-18}"
mkdir -p "$BUILD"
"$CXX" -std=c++20 -O2 -Wall -Wextra -Werror -pthread -I"$ROOT/xbmc" \
  "$ROOT/tools/ps5/tests/cache-diagnostics.cpp" -o "$BUILD/cache-diagnostics"
"$BUILD/cache-diagnostics"
