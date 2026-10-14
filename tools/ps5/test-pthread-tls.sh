#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-pthread-tls-tests}"
CC="${CC:-clang-18}"
CXX="${CXX:-clang++-18}"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
mkdir -p "$BUILD"
"$CC" -O2 -Wall -Wextra -Werror -fPIC -c \
  "$ROOT/shims/native-app/pthread_tls.c" -o "$BUILD/pthread_tls.o"
"$CC" -O2 -fPIC -shared -nostdlib -fuse-ld=lld \
  -Wl,--hash-style=both,--eh-frame-hdr \
  "$ROOT/tools/ps5/tests/pthread-tls-addon.c" -o "$BUILD/pthread-tls-addon.so"
"$CXX" -std=c++17 -O2 -Wall -Wextra -Werror -Wno-unused-const-variable \
  -pthread -rdynamic -I"$ROOT/xbmc" "$ROOT/xbmc/platform/ps5/elf/ElfLoader.cpp" \
  "$ROOT/tools/ps5/tests/pthread-tls.cpp" "$BUILD/pthread_tls.o" \
  -ldl -o "$BUILD/pthread-tls"
"$BUILD/pthread-tls" "$BUILD/pthread-tls-addon.so"
python3 "$ROOT/tools/ps5/verify-addon-exports.py" "$BUILD/pthread-tls" "$BUILD/pthread-tls-addon.so"
if python3 "$ROOT/tools/ps5/verify-addon-exports.py" \
    "$BUILD/pthread-tls-addon.so" "$BUILD/pthread-tls-addon.so" > "$BUILD/rejected-title.txt" 2>&1; then
  echo "FAIL: preflight accepted a title without the TLS wrapper" >&2
  exit 1
fi
grep -q '__wrap_pthread_setspecific' "$BUILD/rejected-title.txt"
"$SDK/bin/prospero-clang" --target=x86_64-unknown-freebsd -O2 -Wall -Wextra -Werror \
  -c "$ROOT/shims/native-app/pthread_tls.c" -o "$BUILD/pthread_tls.ps5.o"
echo "PASS: title TLS call boundary, unwrapped negative control, values, errors, thread exit and reload"
