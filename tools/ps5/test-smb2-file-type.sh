#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-smb2-file-type-tests}"
SOURCE="${SOURCE:-$ROOT/tools/depends/target/libsmb2/x86_64-unknown-freebsd-debug}"
CC="${CC:-clang-18}"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
if [[ ! -f "$SOURCE/lib/smb2-file-type.h" ]]; then
  echo "ERROR: build the patched libsmb2 dependency before running this test" >&2
  exit 1
fi
if [[ "$(grep -c 'smb2_type_from_attributes(' "$SOURCE/lib/libsmb2.c")" != 3 ]]; then
  echo "ERROR: listing, stat and fstat must all use the shared type mapping" >&2
  exit 1
fi
mkdir -p "$BUILD"
"$CC" -std=c11 -O2 -Wall -Wextra -Werror \
  -I"$SOURCE/include" -I"$SOURCE/lib" \
  "$ROOT/tools/ps5/tests/smb2-file-type.c" -o "$BUILD/smb2-file-type"
"$BUILD/smb2-file-type"
"$SDK/bin/prospero-clang" --target=x86_64-unknown-freebsd -O2 -Wall -Wextra -Werror \
  -I"$SOURCE/include" -I"$SOURCE/lib" \
  -c "$ROOT/tools/ps5/tests/smb2-file-type.c" -o "$BUILD/smb2-file-type.ps5.o"
echo "PASS: shared listing/stat/fstat mapping and PS5 SDK compile"
