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

AUX="$BUILD/auxiliary-tests"
mkdir -p "$AUX/outside"
COMMON=(-std=c++17 -O2 -fPIC -shared -nostdlib -fuse-ld=lld
        -Wl,--hash-style=both,--eh-frame-hdr)
"$CXX" "${COMMON[@]}" "$ROOT/tools/ps5/tests/auxiliary-parent.cpp" -o "$AUX/parent.so"
"$CXX" "${COMMON[@]}" "$ROOT/tools/ps5/tests/auxiliary-state.cpp" -o "$AUX/state-a.so"
"$CXX" "${COMMON[@]}" -DHELPER_ID=2 "$ROOT/tools/ps5/tests/auxiliary-state.cpp" -o "$AUX/state-b.so"
"$CXX" "${COMMON[@]}" -DHELPER_TLS "$ROOT/tools/ps5/tests/auxiliary-state.cpp" -o "$AUX/tls.so"
"$CXX" "${COMMON[@]}" -DHELPER_UNRESOLVED "$ROOT/tools/ps5/tests/auxiliary-state.cpp" -o "$AUX/unresolved.so"
"$CXX" "${COMMON[@]}" "$ROOT/tools/ps5/tests/auxiliary-state.cpp" -Wl,--no-as-needed -lc -o "$AUX/needed.so"
"$CXX" "${COMMON[@]}" -DLOADER_THROWING_CTOR \
  "$ROOT/tools/ps5/tests/lifecycle-addon.cpp" -o "$AUX/throwing.so"
printf 'not ELF\n' > "$AUX/invalid.so"
cp "$AUX/state-a.so" "$AUX/outside/state-a.so"
ln -sfn outside/state-a.so "$AUX/escape.so"
"$CXX" -std=c++17 -Wall -Wextra -Werror -Wno-unused-const-variable -pthread -I"$ROOT/xbmc" \
  "$ROOT/xbmc/platform/ps5/elf/ElfLoader.cpp" \
  "$ROOT/tools/ps5/tests/elf-loader-auxiliary.cpp" -ldl -o "$AUX/elf-loader-auxiliary"
"$AUX/elf-loader-auxiliary" "$AUX"
echo "PASS: scoped auxiliary paths, symbols, native handles, lifetime, refcounts, errors and isolation"
mkdir -p "$AUX/pinned" "$AUX/replacement"
cp "$AUX/parent.so" "$AUX/state-a.so" "$AUX/pinned/"
cp "$AUX/state-a.so" "$AUX/replacement/"
"$CXX" -std=c++17 -Wall -Wextra -Werror -Wno-unused-const-variable -pthread -I"$ROOT/xbmc" \
  -DLOADER_TEST_NO_DIRECTORY_OPENAT -Wl,--wrap=openat \
  "$ROOT/xbmc/platform/ps5/elf/ElfLoader.cpp" \
  "$ROOT/tools/ps5/tests/elf-loader-auxiliary.cpp" -ldl -o "$AUX/elf-loader-auxiliary-no-openat"
"$AUX/elf-loader-auxiliary-no-openat" "$AUX"
echo "PASS: scoped auxiliary loading with PS5-style ENOSYS directory-relative openat"
