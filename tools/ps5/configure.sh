#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-release}"
# Produced by `make -C tools/depends/native` (TexturePacker, swig, etc.) against
# the prefix passed to `tools/depends/configure --prefix=...`. See
# docs/README.PS5.md for the full dependency-build sequence.
NATIVE="${NATIVE:-$ROOT/build/ps5-depends-test/x86_64-linux-gnu-native}"
TOOLCHAIN_FILE="${TOOLCHAIN_FILE:-$ROOT/toolchain/ps5-kodi.cmake}"
PS5_PAYLOAD_SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
PS5_DEPENDS_PREFIX="${PS5_DEPENDS_PREFIX:-$ROOT/build/ps5-depends-test/x86_64-unknown-freebsd-debug}"
export PS5_DEPENDS_PREFIX

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

# fontconfig.pc's "Requires.private: expat" is resolved by pkg-config's own
# internal recursion while answering a single top-level query (e.g. for
# libass, which Requires: fontconfig) - our wrapper can't intercept that
# recursion the way it routes whole top-level package names (FFmpeg/dav1d
# below) elsewhere in this script. To make that recursive lookup land on
# our tools/depends-built expat (2.8.1 - needed for the newer expat API our
# Python build's pyexpat.c calls, see docs/README.PS5.md ss0.3) instead of
# the SDK pacbrew sysroot's older bundled one (2.6.2), both .pc files need
# to be resolvable from the *same* pkg-config invocation without
# PKG_CONFIG_SYSROOT_DIR: the SDK's .pc files use "prefix=/user/homebrew"
# as a sysroot-relative placeholder (meaningless as a real host path
# without that env var), while our depends-built .pc files already carry
# real, absolute host paths - mixing the two under one PKG_CONFIG_SYSROOT_DIR
# makes pkg-config wrongly re-prefix our already-absolute paths too (they
# don't start with the sysroot, so pkgconf's "already prefixed" skip never
# applies), producing bogus concatenated paths. So: patch one-time copies
# of the SDK .pc files here with their real absolute prefix baked in, and
# drop PKG_CONFIG_SYSROOT_DIR entirely for the combined query below.
PS5_HBROOT="$PS5_PAYLOAD_SDK/target/user/homebrew"
PATCHED_PC="$BUILD/sdk-pkgconfig-patched"
mkdir -p "$PATCHED_PC/lib/pkgconfig" "$PATCHED_PC/libdata/pkgconfig"
for _srcdir_dstdir in \
  "$PS5_HBROOT/lib/pkgconfig:$PATCHED_PC/lib/pkgconfig" \
  "$PS5_HBROOT/libdata/pkgconfig:$PATCHED_PC/libdata/pkgconfig"; do
  _srcdir="${_srcdir_dstdir%%:*}"
  _dstdir="${_srcdir_dstdir##*:}"
  if [ -d "$_srcdir" ]; then
    for _pc in "$_srcdir"/*.pc; do
      [ -e "$_pc" ] || continue
      sed "s|/user/homebrew|$PS5_HBROOT|g" "$_pc" > "$_dstdir/$(basename "$_pc")"
    done
  fi
done

cat > "$BUILD/kodi-pkg-config" <<WRAP
#!/usr/bin/env bash
DEPENDS_PC="$BUILD/build/lib/pkgconfig:$BUILD/build/libdata/pkgconfig"
TOOLS_DEPENDS_PC="$PS5_DEPENDS_PREFIX/lib/pkgconfig:$PS5_DEPENDS_PREFIX/share/pkgconfig"
if [ -d "$PS5_DEPENDS_PREFIX/lib/pkgconfig" ]; then
  DEPENDS_PC="\$TOOLS_DEPENDS_PC:\$DEPENDS_PC"
fi
case "\${PKG_CONFIG_LIBDIR:-}" in
  "$BUILD/build/"*)
    env -u PKG_CONFIG_SYSROOT_DIR PKG_CONFIG_LIBDIR="\$DEPENDS_PC" PKG_CONFIG_PATH= pkg-config "\$@" 2>/dev/null && exit 0
    ;;
esac

# FFmpeg (and anything it pulls in privately, e.g. dav1d) is built from
# source via tools/depends/target/ffmpeg instead of linked from the SDK's
# pacbrew sysroot (see docs/README.PS5.md ss1). CMake's FindFFMPEG.cmake
# queries these exact package names via pkg_check_modules() and inherits
# whatever PKG_CONFIG_LIBDIR the toolchain file set (the SDK sysroot), so
# route just these names to our own prefix first, regardless of that
# inherited env, before falling back to the SDK's pkg-config.
for _pkg in "\$@"; do
  case "\$_pkg" in
    libavcodec*|libavformat*|libavfilter*|libavutil*|libavdevice*|libpostproc*|libswscale*|libswresample*)
      env -u PKG_CONFIG_SYSROOT_DIR PKG_CONFIG_LIBDIR="\$TOOLS_DEPENDS_PC" PKG_CONFIG_PATH= pkg-config "\$@" 2>/dev/null && exit 0
      exit 1
      ;;
  esac
done

# Everything else (fontconfig/freetype2/harfbuzz/fribidi/libass/...) is still
# the SDK's own pacbrew-built package, EXCEPT for its "expat" sub-dependency:
# fontconfig.pc's "Requires.private: expat" is resolved internally by this
# one pkg-config process using whatever PKG_CONFIG_LIBDIR it is given, so it
# can't be redirected per-package the way the FFmpeg/dav1d case above is.
# Our tools/depends/target/python3 build needs a newer expat API than the
# SDK's bundled 2.6.2 provides (see docs/README.PS5.md ss0.3), so list our
# own pkgconfig dir first here too: a plain "pkg-config expat" (or anything
# that pulls expat in transitively, like fontconfig) then resolves to the
# newer tools/depends-built expat (2.8.1, API-compatible superset) instead,
# giving the whole static link exactly one expat.a - not two conflicting
# ones - while every other SDK package name (nothing else is installed into
# our prefix's pkgconfig dir) still falls through to the SDK's copy exactly
# as before. Uses the prefix-patched copies (see above) with NO
# PKG_CONFIG_SYSROOT_DIR, so our own already-absolute .pc paths and the
# patched-absolute SDK .pc paths both resolve correctly in this one
# pkg-config process.
export PKG_CONFIG_DIR=
unset PKG_CONFIG_SYSROOT_DIR
export PKG_CONFIG_LIBDIR="\$TOOLS_DEPENDS_PC:$PATCHED_PC/lib/pkgconfig"
export PKG_CONFIG_PATH="$PATCHED_PC/libdata/pkgconfig"
unset DESTDIR
exec pkg-config --static "\$@"
WRAP
chmod +x "$BUILD/kodi-pkg-config"

PYTHON_ARGS=(-DENABLE_PYTHON=OFF)
PY_ROOT="$PS5_DEPENDS_PREFIX"
if [ -f "$PY_ROOT/lib/libpython3.14.a" ]; then
  command -v swig >/dev/null && command -v java >/dev/null || {
    echo "Python sysroot found but host tools missing: install swig + java"
    exit 1
  }
  PYTHON_ARGS=(
    -DENABLE_PYTHON=ON
    -DENABLE_INTERNAL_SWIG=ON
    -DPYTHON_PATH="$PS5_DEPENDS_PREFIX"
    -DPYTHON_VER=3.14
    -DPython3_USE_STATIC_LIBS=ON
  )
fi

# JsonSchemaBuilder, unlike TexturePacker, self-builds via CMake's own
# ExternalProject machinery (using NATIVEPREFIX/share/Toolchain-Native.cmake)
# whenever -DWITH_JSONSCHEMABUILDER is left unset - so only force an explicit
# prebuilt path when one genuinely exists; otherwise let it build itself into
# $NATIVE/bin on first configure.
JSONSCHEMABUILDER_ARGS=()
if [ -x "$NATIVE/bin/JsonSchemaBuilder" ]; then
  JSONSCHEMABUILDER_ARGS=(-DWITH_JSONSCHEMABUILDER="$NATIVE/bin")
fi

# CMake's pkg_check_modules()/find_package(Python3) cache their resolved
# library paths under these prefixes, not under "FFMPEG_*"/"Python3_*"
# themselves - a stale entry here (e.g. from a previous configure against the
# SDK sysroot) makes find_library() skip re-searching entirely and keep
# linking the old path even though FFMPEG_LIBAVCODEC_VERSION etc. gets
# correctly re-reported. Purge them so every reconfigure re-resolves for real.
#
# Note on the brotli/_brotli/CURL purge below: libcurl's own installed
# CURLConfig.cmake ships (and temporarily prepends to CMAKE_MODULE_PATH) a
# second, non-PS5-aware FindBrotli.cmake used only for CURL::libcurl's own
# private "-lbrotlidec -lbrotlicommon" link interface - it runs its own
# pkg_check_modules() under the "_brotli" variable prefix (not
# "brotli*"/"pkgcfg_lib_brotli*", which only cover Kodi's own top-level
# cmake/modules/FindBrotli.cmake lookup), so a stale "_brotli_*"/"CURL_*"
# cache entry from before this routing was fixed would otherwise keep
# resolving to the SDK's older brotli even after everything else is fixed.
cmake -S "$ROOT" -B "$BUILD" -G Ninja \
  -U "FFMPEG_*" -U "pkgcfg_lib_FFMPEG_*" -U "pkgcfg_include_FFMPEG_*" \
  -U "Python3_*" -U "PYTHON_*" -U "_Python3_*" \
  -U "Dav1d_*" -U "pkgcfg_lib_Dav1d_*" -U "pkgcfg_include_Dav1d_*" \
  -U "EXPAT_*" -U "PC_EXPAT*" -U "FFI_LIBRARY" -U "GMP_LIBRARY" -U "PYEXPAT_LIBRARY" \
  -U "Iconv_*" -U "Intl_*" -U "LIBLZMA_*" \
  -U "libass_*" -U "pkgcfg_lib_libass_*" -U "pkgcfg_include_libass_*" \
  -U "PC_FONTCONFIG*" -U "pkgcfg_lib_PC_FONTCONFIG_*" -U "pkgcfg_lib_FONTCONFIG_*" \
  -U "brotli*" -U "pkgcfg_lib_brotli*" -U "pkgcfg_include_brotli*" -U "Brotli_FOUND" \
  -U "_brotli*" -U "CURL_*" -U "pkgcfg_lib_CURL_*" \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN_FILE" \
  -DCMAKE_BUILD_TYPE="${BUILD_TYPE:-Release}" \
  -DCMAKE_C_FLAGS_RELEASE="-O2 -g -DNDEBUG" \
  -DCMAKE_CXX_FLAGS_RELEASE="-O2 -g -DNDEBUG" \
  -DCMAKE_INSTALL_PREFIX=/app0 \
  -DWITH_TEXTUREPACKER="$NATIVE/bin" \
  "${JSONSCHEMABUILDER_ARGS[@]}" \
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
