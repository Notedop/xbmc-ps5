#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-native-loader-tests}"
mkdir -p "$BUILD"
"${CXX:-c++}" -std=c++20 -Wall -Wextra -Werror -Wno-unused-parameter -g \
  -fsanitize=address,undefined -fno-omit-frame-pointer -fno-pie -no-pie \
  -DTARGET_PS5 -DTARGET_POSIX \
  -I"$ROOT/tools/ps5/tests/native-loader" -I"$ROOT/xbmc" \
  "$ROOT/xbmc/cores/DllLoader/LibraryLoader.cpp" \
  "$ROOT/xbmc/cores/DllLoader/DllLoaderContainer.cpp" \
  "$ROOT/xbmc/platform/ps5/elf/PS5AddonLoader.cpp" \
  "$ROOT/tools/ps5/tests/native-loader.cpp" -Wl,--wrap=chmod -o "$BUILD/native-loader"
ASAN_OPTIONS=detect_leaks=1 "$BUILD/native-loader"
python3 - "$BUILD" <<'PY'
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import zipfile

build = Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix="zip-install-", dir=build) as temporary:
    root = Path(temporary)
    archive = root / "native.zip"
    entry = zipfile.ZipInfo("addon/native.sprx")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFREG | 0o755) << 16
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(entry, b"\x54\x14\xf5\xee")
    with zipfile.ZipFile(archive) as package:
        package.extractall(root / "installed")
    module = root / "installed/addon/native.sprx"
    mode = stat.S_IMODE(module.stat().st_mode)
    assert not mode & 0o111, oct(mode)
    subprocess.run([str(build / "native-loader"), str(module)], check=True)
    assert stat.S_IMODE(module.stat().st_mode) == mode | 0o111
print("PASS: ZIP-installed SPRX loads after automatic executable permission repair")
PY
"${CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -g \
  -fsanitize=address,undefined -fno-omit-frame-pointer -fno-pie -no-pie \
  -I"$ROOT/xbmc/addons/kodi-dev-kit/include" \
  "$ROOT/addons/screensaver.ps5.sprx-test/main.c" \
  "$ROOT/tools/ps5/tests/sprx-addon.c" -o "$BUILD/sprx-addon"
ASAN_OPTIONS=detect_leaks=1 "$BUILD/sprx-addon"
"${CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -g \
  -fsanitize=address,undefined -fno-omit-frame-pointer -fno-pie -no-pie \
  -DPS5_SPRX_START_TRACE_DIAGNOSTIC=1 \
  -I"$ROOT/tools/ps5/tests" -I"$ROOT/xbmc/addons/kodi-dev-kit/include" \
  "$ROOT/addons/screensaver.ps5.sprx-test/main.c" \
  "$ROOT/shims/native-app/sprx_start_trace.c" \
  "$ROOT/tools/ps5/tests/sprx-start-trace.c" -o "$BUILD/sprx-start-trace"
ASAN_OPTIONS=detect_leaks=1 "$BUILD/sprx-start-trace"
