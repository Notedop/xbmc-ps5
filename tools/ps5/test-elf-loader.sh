#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-loader-tests}"
CXX="${CXX:-clang++-18}"
mkdir -p "$BUILD"
"$CXX" -std=c++17 -fPIC -shared -nostdlib -fuse-ld=lld \
  -Wl,--hash-style=both,--eh-frame-hdr \
  "$ROOT/tools/ps5/tests/lifecycle-addon.cpp" -o "$BUILD/lifecycle-addon.so"
"$CXX" -std=c++17 -fPIC -shared -nostdlib -fuse-ld=lld \
  -DLOADER_THROWING_CTOR -Wl,--hash-style=both,--eh-frame-hdr \
  "$ROOT/tools/ps5/tests/lifecycle-addon.cpp" -o "$BUILD/throwing-addon.so"
"$CXX" -std=c++17 -Wall -Wextra -Werror -Wno-unused-const-variable -pthread -I"$ROOT/xbmc" \
  "$ROOT/xbmc/platform/ps5/elf/ElfLoader.cpp" \
  "$ROOT/tools/ps5/tests/elf-loader-lifecycle.cpp" -ldl -o "$BUILD/elf-loader-lifecycle"
"$BUILD/elf-loader-lifecycle" "$BUILD/lifecycle-addon.so" "$BUILD/throwing-addon.so"
echo "PASS: module-owned global destructors, finalizer order, EH, isolation and reload"
