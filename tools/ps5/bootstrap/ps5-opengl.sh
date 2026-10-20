#!/usr/bin/env bash
# Clone, patch and build the PS5 OpenGL 4.6 / EGL SDK into $PS5_OPENGL_PREFIX.
#
# The pinned upstream revision (patches/ps5-opengl/PS5-OPENGL-COMMIT) is
# checked out into $WORK/ps5-opengl and patched with
# patches/ps5-opengl/*.patch, which add the entry points Kodi's
# window system and renderer need:
#   ps5_opengl_video_out_handle()      refresh rate / vblank clock / VRR
#   ps5_opengl_set_scanout_format()    HDR output (scanout pixel format)
#   ps5_opengl_memory_image_create()   zero-copy video (EGL image over
#                                      decoder-owned memory)
# None of these exist in the stock upstream build; see
# docs/ps5/upstream-migration-tasks.md for the plan to upstream them, after
# which this patch step disappears and a release archive can be used as-is.
#
# Safe to run repeatedly; afterwards rebuild Kodi (the SDK is linked into it).
#
# Display profile (the driver's build-time render/presentation size):
#   PS5_SCANOUT_HEIGHT  2160 (default), 1440 or 1080 - match the system output
#                       Kodi reports at start ("PS5 video out: ... system output")
#   PS5_SCANOUT_FPS     60 (default) or 120
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # tools/ps5
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="${WORK:-$ROOT/build/ps5-external}"
SRC="$WORK/ps5-opengl"
SOURCE_URL="https://github.com/Notedop/ps5-opengl.git"
export PS5_PAYLOAD_SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
export PS5_OPENGL_PREFIX="${PS5_OPENGL_PREFIX:-/opt/ps5-opengl-gl46}"

export PS5_SCANOUT_HEIGHT="${PS5_SCANOUT_HEIGHT:-2160}"
export PS5_SCANOUT_FPS="${PS5_SCANOUT_FPS:-60}"
case "$PS5_SCANOUT_HEIGHT" in 1080|1440|2160) ;; *) echo "!! PS5_SCANOUT_HEIGHT must be 1080, 1440 or 2160"; exit 1 ;; esac
case "$PS5_SCANOUT_FPS" in 60|120) ;; *) echo "!! PS5_SCANOUT_FPS must be 60 or 120"; exit 1 ;; esac
PROFILE="${PS5_SCANOUT_HEIGHT}p${PS5_SCANOUT_FPS}"

mkdir -p "$WORK"

COMMIT="$(tr -d '[:space:]' < "$HERE/patches/ps5-opengl/PS5-OPENGL-COMMIT")"
if [[ ! "$COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "!! invalid PS5-OPENGL-COMMIT: expected a full commit hash" >&2
  exit 1
fi
if [ ! -d "$SRC/src/platform" ]; then
  echo "==> cloning ps5-opengl at $COMMIT"
  if [ ! -e "$SRC/.git" ]; then
    git clone --no-checkout "$SOURCE_URL" "$SRC"
  fi
  git -C "$SRC" fetch --no-tags --quiet "$SOURCE_URL" "$COMMIT"
  git -C "$SRC" checkout --detach -q "$COMMIT"
fi
ACTUAL="$(git -C "$SRC" rev-parse HEAD)"
if [ "$ACTUAL" != "$COMMIT" ]; then
  echo "!! ps5-opengl at $SRC is $ACTUAL, but Kodi requires $COMMIT" >&2
  echo "!! Existing sources were preserved. Use a new WORK directory for the upgrade." >&2
  exit 1
fi

echo "==> applying Kodi additions to $SRC"
shopt -s nullglob
PATCHES=("$HERE"/patches/ps5-opengl/*.patch)
shopt -u nullglob
if [ "${#PATCHES[@]}" -eq 0 ]; then
  echo "!! no Kodi OpenGL integration patches found" >&2
  exit 1
fi
if git -C "$SRC" apply --reverse --check "${PATCHES[@]}" >/dev/null 2>&1; then
  echo "    Kodi integration patches already applied"
else
  git -C "$SRC" apply --check "${PATCHES[@]}"
  git -C "$SRC" apply "${PATCHES[@]}"
fi

# The runtime's make rules do not track compiler flags.
STAMP="$SRC/build/.kodi-display-profile"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$PROFILE" ]; then
  echo "==> display profile $PROFILE (was: $(cat "$STAMP" 2>/dev/null || echo "upstream default 1080p60")): clean runtime rebuild"
  rm -rf "$SRC/build/core33-native-runtime"
fi

LOG="$WORK/ps5-opengl-build.log"

# A new revision can change Mesa and the shader compiler as well as the runtime.
SDK_STAMP="$SRC/build/.kodi-sdk-build"
SDK_IDENTITY="$(printf '%s\n' "$COMMIT"; sha256sum "${PATCHES[@]}")"
if [ ! -d "$SRC/build/sdk/ps5-opengl-gl46/lib" ] ||
   [ "$(cat "$SDK_STAMP" 2>/dev/null)" != "$SDK_IDENTITY" ]; then
  # Upstream's tools/fetch-sources.py is not resumable: it creates
  # third_party/<name>/ and only then fetches into it, but on a re-run it
  # skips the whole init/fetch/checkout block whenever the directory already
  # exists and goes straight to `git rev-parse HEAD`. An interrupted fetch
  # therefore leaves a directory that fails every subsequent run. Drop any
  # third_party checkout without a resolvable HEAD so it is fetched again.
  if [ -d "$SRC/third_party" ]; then
    for _tp in "$SRC"/third_party/*/; do
      [ -d "$_tp" ] || continue
      if ! git -C "$_tp" rev-parse HEAD >/dev/null 2>&1; then
        echo "    removing incomplete third_party/$(basename "$_tp") checkout (interrupted fetch)"
        rm -rf "$_tp"
      fi
    done
  fi
  echo "==> building pinned sources, shader compiler and Mesa (log: $LOG)"
  ( cd "$SRC" \
      && make source-fetch \
      && bash toolchain/build-opengnm-psbc.sh \
      && bash toolchain/build-opengnm-psbc-ps5.sh \
      && PS5_MESA_CROSS_FILE="$PS5_PAYLOAD_SDK/toolchain/prospero.ini" \
         bash toolchain/build-mesa-ps5.sh ) || {
    echo "!! compiler/Mesa build failed"; exit 1; }
fi

echo "==> building the runtime and packaging the SDK ($PROFILE)"
( cd "$SRC" && bash toolchain/install-ps5-opengl-gl46.sh build/sdk/ps5-opengl-gl46 ) \
  > "$LOG" 2>&1 || {
  echo "!! SDK build failed, last lines of $LOG:"; tail -25 "$LOG"; exit 1; }
( cd "$SRC" && python3 tests/ps5/verify_gl46_link_surface.py ) >> "$LOG" 2>&1 \
  || { echo "!! OpenGL link-surface verification failed (see $LOG)"; exit 1; }

echo "==> installing into $PS5_OPENGL_PREFIX"
if { [ -d "$PS5_OPENGL_PREFIX" ] && [ -w "$PS5_OPENGL_PREFIX" ]; } ||
   { [ ! -e "$PS5_OPENGL_PREFIX" ] && [ -w "$(dirname "$PS5_OPENGL_PREFIX")" ]; }; then
  mkdir -p "$PS5_OPENGL_PREFIX"
  cp -a "$SRC/build/sdk/ps5-opengl-gl46/." "$PS5_OPENGL_PREFIX/"
else
  sudo -n mkdir -p "$PS5_OPENGL_PREFIX"
  sudo -n cp -a "$SRC/build/sdk/ps5-opengl-gl46/." "$PS5_OPENGL_PREFIX/"
fi

# lib/libPS5OpenGL.a is a linker script (GROUP of the real archives); the
# runtime, and so our additions, live in lib/libps5_opengl_core33.a.
RUNTIME_LIB="$PS5_OPENGL_PREFIX/lib/libps5_opengl_core33.a"
SYMBOLS="$("$PS5_PAYLOAD_SDK/bin/llvm-nm" --defined-only "$RUNTIME_LIB")"
for symbol in ps5_opengl_video_out_handle ps5_opengl_set_scanout_format ps5_opengl_memory_image_create; do
  if ! grep -Eq " T ${symbol}$" <<< "$SYMBOLS"; then
    echo "!! installed runtime archive does not export $symbol" >&2
    exit 1
  fi
done
if ! grep -q "PS5_OPENGL_NATIVE_HEIGHT $PS5_SCANOUT_HEIGHT" "$PS5_OPENGL_PREFIX/include/ps5_opengl_display.h"; then
  echo "!! installed SDK does not report the $PROFILE profile"; exit 1
fi
printf '%s\n' "$PROFILE" > "$STAMP"
printf '%s\n' "$SDK_IDENTITY" > "$SDK_STAMP"
echo "SDK ready ($PROFILE): all three Kodi integration entry points exported"
