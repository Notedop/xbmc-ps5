#!/usr/bin/env bash
# Apply the worker shutdown fix and opt-in diagnostics to the selected add-on build.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WRAPPER="${PS5_ADDON_BUILD:-$ROOT/tools/depends/target/binary-addons/x86_64-unknown-freebsd-debug}"
SOURCE="$WRAPPER/build/inputstream.adaptive"
BUILD="$WRAPPER/inputstream.adaptive-prefix/src/inputstream.adaptive-build"
[[ -f "$SOURCE/src/Stream.cpp" && -f "$BUILD/CMakeCache.txt" ]] || {
  echo "!! configured adaptive source/build missing under $WRAPPER" >&2
  exit 1
}
cmake "-DSOURCE_DIR=$SOURCE" -P "$ROOT/tools/ps5/apply-adaptive-patch.cmake"
cmake --build "$BUILD" --target inputstream.adaptive --parallel "${PS5_ADDON_JOBS:-4}"
cmake --install "$BUILD"
echo "Worker shutdown fix and stop diagnostics installed; repackage with PS5_ADAPTIVE_TEST=1."
