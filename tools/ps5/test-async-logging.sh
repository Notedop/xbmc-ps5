#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-async-logging-tests}"
SPDLOG_INCLUDE_DIR="${SPDLOG_INCLUDE_DIR:-$ROOT/build/ps5-release/build/include}"
FMT_INCLUDE_DIR="${FMT_INCLUDE_DIR:-/opt/ps5-payload-sdk/target/include_common}"
CXX="${CXX:-clang++-18}"
if [[ ! -f "$SPDLOG_INCLUDE_DIR/spdlog/spdlog.h" ]]; then
  echo "Set SPDLOG_INCLUDE_DIR to the existing dependency headers containing spdlog/." >&2
  exit 1
fi
if [[ ! -f "$FMT_INCLUDE_DIR/fmt/format.h" ]]; then
  echo "Set FMT_INCLUDE_DIR to the existing dependency headers containing fmt/." >&2
  exit 1
fi
mkdir -p "$BUILD"
"$CXX" -std=c++20 -O2 -Wall -Wextra -Werror -pthread \
  -DSPDLOG_FMT_EXTERNAL -DFMT_HEADER_ONLY -I"$ROOT/xbmc" \
  -isystem "$SPDLOG_INCLUDE_DIR" -idirafter "$FMT_INCLUDE_DIR" \
  "$ROOT/tools/ps5/tests/async-logging.cpp" \
  "$ROOT/xbmc/platform/ps5/AsyncLogSink.cpp" -o "$BUILD/async-logging"
"$BUILD/async-logging"
