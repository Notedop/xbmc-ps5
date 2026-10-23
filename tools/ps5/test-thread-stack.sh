#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-thread-stack-tests}"
CC="${CC:-clang-18}"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
mkdir -p "$BUILD"
"$CC" -std=c11 -O2 -Wall -Wextra -Werror \
  "$ROOT/tools/ps5/tests/thread-stack.c" -o "$BUILD/thread-stack"
"$BUILD/thread-stack"
"$SDK/bin/prospero-clang" -O2 -Wall -Wextra -Werror -fPIC \
  -c "$ROOT/shims/native-app/thread_stack.c" -o "$BUILD/thread_stack.ps5.o"
echo "PASS: thread stack policy, shared TLS drain, caller ownership, errors and native SDK compile"
