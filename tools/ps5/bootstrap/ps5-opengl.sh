#!/usr/bin/env bash
# Clone, patch and build the PS5 OpenGL 4.6 / EGL SDK into $PS5_OPENGL_PREFIX.
#
# The pinned upstream revision (patches/ps5-opengl/PS5-OPENGL-COMMIT) is
# checked out into $WORK/ps5-opengl and patched with
# patches/ps5-opengl/kodi-additions.py, which adds the entry points Kodi's
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
export PS5_PAYLOAD_SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
export PS5_OPENGL_PREFIX="${PS5_OPENGL_PREFIX:-/opt/ps5-opengl-gl46}"

export PS5_SCANOUT_HEIGHT="${PS5_SCANOUT_HEIGHT:-2160}"
export PS5_SCANOUT_FPS="${PS5_SCANOUT_FPS:-60}"
case "$PS5_SCANOUT_HEIGHT" in 1080|1440|2160) ;; *) echo "!! PS5_SCANOUT_HEIGHT must be 1080, 1440 or 2160"; exit 1 ;; esac
case "$PS5_SCANOUT_FPS" in 60|120) ;; *) echo "!! PS5_SCANOUT_FPS must be 60 or 120"; exit 1 ;; esac
PROFILE="${PS5_SCANOUT_HEIGHT}p${PS5_SCANOUT_FPS}"

mkdir -p "$WORK"

if [ ! -d "$SRC/src/platform" ]; then
  COMMIT="$(tr -d '[:space:]' < "$HERE/patches/ps5-opengl/PS5-OPENGL-COMMIT")"
  echo "==> cloning ps5-opengl at $COMMIT"
  [ -d "$SRC/.git" ] || git clone --no-checkout https://github.com/blackbearreloaded/ps5-opengl.git "$SRC"
  git -C "$SRC" fetch --all --tags --quiet || true
  git -C "$SRC" checkout -q "$COMMIT"
fi

echo "==> applying Kodi additions to $SRC"
python3 "$HERE/patches/ps5-opengl/kodi-additions.py" "$SRC"

# The additions live in the runtime (src/platform, src/egl), which the SDK
# installer rebuilds itself; Mesa and the shader compiler are unchanged. The
# runtime's make rules do not track compiler flags: when the display profile
# changes, rebuild the runtime objects from scratch.
STAMP="$SRC/build/.kodi-display-profile"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$PROFILE" ]; then
  echo "==> display profile $PROFILE (was: $(cat "$STAMP" 2>/dev/null || echo "upstream default 1080p60")): clean runtime rebuild"
  rm -rf "$SRC/build/core33-native-runtime"
fi

# First build also needs Mesa and the shader compiler, which "make sdk-gl46"
# drives (and which downloads the Mesa sources). Afterwards the installer
# alone suffices and is much faster.
if [ ! -d "$SRC/build/sdk/ps5-opengl-gl46/lib" ]; then
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
  echo "==> first build: fetching sources and building the full SDK (this takes 1-2 hours)"
  ( cd "$SRC" && make source-fetch && make sdk-gl46 )
else
  echo "==> rebuilding the runtime and packaging the SDK ($PROFILE)"
  ( cd "$SRC" && bash toolchain/install-ps5-opengl-gl46.sh build/sdk/ps5-opengl-gl46 ) \
    > "$WORK/ps5-opengl-build.log" 2>&1 || {
    echo "!! SDK build failed, last lines of $WORK/ps5-opengl-build.log:"; tail -25 "$WORK/ps5-opengl-build.log"; exit 1; }
fi
( cd "$SRC" && python3 tests/ps5/verify_gl46_link_surface.py ) >> "$WORK/ps5-opengl-build.log" 2>&1 \
  || echo "   note: verify_gl46_link_surface.py reported a problem (see the log)"

mkdir -p "$SRC/build" && echo "$PROFILE" > "$STAMP"
echo "==> installing into $PS5_OPENGL_PREFIX"
sudo mkdir -p "$PS5_OPENGL_PREFIX"
sudo cp -a "$SRC/build/sdk/ps5-opengl-gl46/." "$PS5_OPENGL_PREFIX/"

# lib/libPS5OpenGL.a is a linker script (GROUP of the real archives); the
# runtime, and so our additions, live in lib/libps5_opengl_core33.a.
found=""
for lib in "$PS5_OPENGL_PREFIX"/lib/*.a; do
  # no "grep -q": it stops reading early, llvm-nm then dies of SIGPIPE and
  # pipefail reports the match as a failure
  if [ "$("$PS5_PAYLOAD_SDK/bin/llvm-nm" "$lib" 2>/dev/null | grep -c " T ps5_opengl_video_out_handle")" -gt 0 ]; then
    found="$lib"; break
  fi
done
if ! grep -q "PS5_OPENGL_NATIVE_HEIGHT $PS5_SCANOUT_HEIGHT" "$PS5_OPENGL_PREFIX/include/ps5_opengl_display.h"; then
  echo "!! installed SDK does not report the $PROFILE profile"; exit 1
fi
if [ -n "$found" ]; then
  echo "SDK ready ($PROFILE): ps5_opengl_video_out_handle exported (in $(basename "$found"))"
else
  echo "!! no installed archive in $PS5_OPENGL_PREFIX/lib exports ps5_opengl_video_out_handle"; exit 1
fi
