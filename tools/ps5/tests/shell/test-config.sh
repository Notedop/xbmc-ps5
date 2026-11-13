#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-config-tests}"
NLOHMANN_INCLUDE_DIR="${NLOHMANN_INCLUDE_DIR:-$ROOT/build/ps5-release/build/include}"
CXX="${CXX:-clang++-18}"
if [[ ! -f "$NLOHMANN_INCLUDE_DIR/nlohmann/json.hpp" ]]; then
  echo "Set NLOHMANN_INCLUDE_DIR to the existing dependency headers containing nlohmann/." >&2
  exit 1
fi
mkdir -p "$BUILD"
"$CXX" -std=c++20 -O2 -Wall -Wextra -Werror -I"$ROOT/xbmc" \
  -isystem "$NLOHMANN_INCLUDE_DIR" \
  "$ROOT/tools/ps5/tests/config.cpp" \
  "$ROOT/xbmc/platform/ps5/ConfigPS5.cpp" -o "$BUILD/config"
"$BUILD/config" "$ROOT/title/default-config.json"
