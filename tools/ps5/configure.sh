#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-release}"
NATIVE="${NATIVE:-/home/raoul/kodi-ps5-native}"
TOOLCHAIN_FILE="${TOOLCHAIN_FILE:-$ROOT/toolchain/ps5-kodi.cmake}"
PS5_PAYLOAD_SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
PS5_DEPENDS_PREFIX="${PS5_DEPENDS_PREFIX:-$ROOT/build/ps5-depends-test/x86_64-unknown-freebsd-debug}"

[ -f "$ROOT/version.txt" ] || {
  echo "Kodi source tree not found at $ROOT"
  exit 1
}
[ -f "$TOOLCHAIN_FILE" ] || {
  echo "Missing toolchain file: $TOOLCHAIN_FILE"
  exit 1
}
[ -x "$PS5_PAYLOAD_SDK/bin/prospero-pkg-config" ] || {
  echo "Missing prospero-pkg-config in $PS5_PAYLOAD_SDK"
  exit 1
}

mkdir -p "$BUILD"

cat > "$BUILD/kodi-pkg-config" <<WRAP
#!/usr/bin/env bash
DEPENDS_PC="$BUILD/build/lib/pkgconfig:$BUILD/build/libdata/pkgconfig"
TOOLS_DEPENDS_PC="$PS5_DEPENDS_PREFIX/lib/pkgconfig:$PS5_DEPENDS_PREFIX/share/pkgconfig"
if [ -d "$PS5_DEPENDS_PREFIX/lib/pkgconfig" ]; then
  DEPENDS_PC="$TOOLS_DEPENDS_PC:$DEPENDS_PC"
fi
case "\${PKG_CONFIG_LIBDIR:-}" in
  "$BUILD/build/"*)
    env -u PKG_CONFIG_SYSROOT_DIR PKG_CONFIG_LIBDIR="\$DEPENDS_PC" PKG_CONFIG_PATH= pkg-config "\$@" 2>/dev/null && exit 0
    ;;
esac
exec "$PS5_PAYLOAD_SDK/bin/prospero-pkg-config" "\$@"
WRAP
chmod +x "$BUILD/kodi-pkg-config"

PYTHON_ARGS=(-DENABLE_PYTHON=OFF)
PY_ROOT="$PS5_PAYLOAD_SDK/target/user/homebrew"
if [ -f "$PY_ROOT/lib/libpython3.14.a" ]; then
  command -v swig >/dev/null && command -v java >/dev/null || {
    echo "Python sysroot found but host tools missing: install swig + java"
    exit 1
  }
  PYTHON_ARGS=(
    -DENABLE_PYTHON=ON
    -DENABLE_INTERNAL_SWIG=ON
    -DPYTHON_PATH=/user/homebrew
    -DPYTHON_VER=3.14
    -DPython3_USE_STATIC_LIBS=ON
  )
fi

cmake -S "$ROOT" -B "$BUILD" -G Ninja \
  -U "FFMPEG_*" -U "Python3_*" -U "PYTHON_*" \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN_FILE" \
  -DCMAKE_BUILD_TYPE="${BUILD_TYPE:-Release}" \
  -DCMAKE_C_FLAGS_RELEASE="-O2 -g -DNDEBUG" \
  -DCMAKE_CXX_FLAGS_RELEASE="-O2 -g -DNDEBUG" \
  -DCMAKE_INSTALL_PREFIX=/app0 \
  -DWITH_TEXTUREPACKER="$NATIVE/bin" \
  -DWITH_JSONSCHEMABUILDER="$NATIVE/bin" \
  -DNATIVEPREFIX="$NATIVE" \
  -DPKG_CONFIG_EXECUTABLE="$BUILD/kodi-pkg-config" \
  -DINTERNAL_TEXTUREPACKER_INSTALLABLE=FALSE \
  -DENABLE_INTERNAL_FFMPEG=OFF \
  -DENABLE_TESTING=OFF \
  -DVERBOSE_FIND=ON \
  "${PYTHON_ARGS[@]}" \
  "$@"

echo
echo "Configured: $BUILD"
echo "Build with: cmake --build $BUILD -j\$(nproc)"
