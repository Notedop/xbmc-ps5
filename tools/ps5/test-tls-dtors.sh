#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-tls-dtor-tests}"
CC="${CC:-clang-18}"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
mkdir -p "$BUILD"
"$CC" -std=c11 -O2 -Wall -Wextra -Werror -pthread \
  "$ROOT/tools/ps5/tests/tls-dtors.c" -o "$BUILD/tls-dtors"
"$BUILD/tls-dtors"
"$SDK/bin/prospero-clang" --target=x86_64-unknown-freebsd -O2 -Wall -Wextra -Werror \
  -c "$ROOT/shims/native-app/weak_shims.c" -o "$BUILD/weak_shims.ps5.o"
echo "PASS: TLS destructor registration failures, cleanup, LIFO, rearming and SDK compile"
