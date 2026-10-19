#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-socket-shim-tests}"
CC="${CC:-clang-18}"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
mkdir -p "$BUILD/compat/sys"
# Linux declares FIONBIO in sys/ioctl.h and has no BSD ELAST.
printf '#include <sys/ioctl.h>\n' > "$BUILD/compat/sys/filio.h"
"$CC" -std=gnu11 -O2 -Wall -Wextra -Werror -DELAST=96 \
  -I"$BUILD/compat" "$ROOT/tools/ps5/tests/socket-shim.c" -o "$BUILD/socket-shim"
"$BUILD/socket-shim"
"$SDK/bin/prospero-clang" --target=x86_64-unknown-freebsd -O2 -Wall -Wextra -Werror \
  -c "$ROOT/shims/native-app/libc_socket.c" -o "$BUILD/libc_socket.ps5.o"
echo "PASS: socket diagnostics, captured errno, address conversion and SDK compile"
