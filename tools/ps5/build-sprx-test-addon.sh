#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
BUILD="${1:-$ROOT/build/ps5-sprx-sample}"
TEMPLATE="${APP_TEMPLATE:-$ROOT/build/ps5-external/ps5-opengl/build/native-app/PPSA99005}"
NATIVE_TOOL="$TEMPLATE/build/host/ps5-native-tool"
[[ -x "$NATIVE_TOOL" ]] || { echo "Missing existing native title tool: $NATIVE_TOOL" >&2; exit 1; }
case "${PS5_SPRX_START_TRACE_DIAGNOSTIC:-0}" in
  0) ;;
  1) ;;
  *) echo "PS5_SPRX_START_TRACE_DIAGNOSTIC must be 0 or 1" >&2; exit 1 ;;
esac
mkdir -p "$BUILD"
BUILD="$(cd "$BUILD" && pwd)"
cmake -S "$ROOT/addons/screensaver.ps5.sprx-test" -B "$BUILD/cmake" \
  -DPS5_PAYLOAD_SDK="$SDK" -DPS5_NATIVE_TOOL="$NATIVE_TOOL" \
  -DPS5_ADDON_OUTPUT_DIRECTORY="$BUILD" \
  -DPS5_SPRX_START_TRACE_DIAGNOSTIC="${PS5_SPRX_START_TRACE_DIAGNOSTIC:-0}"
cmake --build "$BUILD/cmake" --target screensaver-native
