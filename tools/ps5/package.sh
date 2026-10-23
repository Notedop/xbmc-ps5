#!/usr/bin/env bash
# Package the built Kodi as a ShadowMountPlus folder title.
#
# kodi.bin proves the normal Kodi link graph resolves. Packaging reuses the
# native-app template's assets/tooling and the configured native SDK to produce
# a system-only launcher, shared core/graphics, and private Kodi SPRX:
#   1. cmake --install (DESTDIR) -> staged /app0 data and native add-ons
#   2. Kodi's normal link inputs -> private application link
#   3. prepare-shared-title.py -> SDK providers and native title builders
#   4. make app -> dist/<TITLE_ID>/{eboot.bin, sce_module, sce_sys}
#   5. copy data and verify the completed native title; no console contact.
#
#   BUILD       Kodi build dir           default build/ps5-release
#   STAGE       fresh staging dir        default unique directory below BUILD
#   WORK        external sources dir     default build/ps5-external (bootstrap.sh)
#   APP_TEMPLATE demo app dir            default $WORK/ps5-opengl/build/native-app/PPSA99005
#   TITLE_ID    default PPSA99420        HEAP_MIB app malloc heap (default 2048)
#   KODI_CATEGORY  game (default) or media: home-screen area (media is
#               experimental: the GL driver does not start in its sandbox)
#   PS5_TEST_BINARY_ADDON  1 includes the native SPRX screensaver (default 0)
#   PS5_BUNNY_DIAGNOSTIC  1 includes the Python DASH diagnostic (default 0);
#               requires native Adaptive among the selected catalogue prefixes
#   PS5_SPRX_START_TRACE_DIAGNOSTIC  1 traces sample native initialization (default 0)
#   PS5_NATIVE_RUNTIME_SDK  shared SDK (must match the configured Kodi build)
#   PS5_NATIVE_TITLE_SYSTEM_SDK  optional additional title-only SDK contracts
#   PS5_ADDON_PREFIXES  colon-separated native catalogue install prefixes;
#               default prefixes from the catalogue build record
#   PS5_HOMEBREW_PREFIX  homebrew dependency prefix (defaults to the configured build)
#   Legacy SO prefix/ELF/Adaptive selections are unsupported on this branch.
set -euo pipefail

export PS5_PAYLOAD_SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
export PS5_OPENGL_PREFIX="${PS5_OPENGL_PREFIX:-/opt/ps5-opengl-gl46}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT"
BUILD="${BUILD:-$ROOT/build/ps5-release}"
STAGE="${STAGE:-$BUILD/title-$(date -u +%Y%m%dT%H%M%S)-$$}"
WORK="${WORK:-$ROOT/build/ps5-external}"
APP_TEMPLATE="${APP_TEMPLATE:-$WORK/ps5-opengl/build/native-app/PPSA99005}"
TITLE_ID="${TITLE_ID:-PPSA99420}"
HEAP_MIB="${HEAP_MIB:-2048}"
PACKAGE_ZIP="${PACKAGE_ZIP:-0}"
CONFIGURED_HBROOT="$(sed -n 's/^PS5_HOMEBREW_PREFIX:[^=]*=//p' "$BUILD/CMakeCache.txt")"
HB="${PS5_HOMEBREW_PREFIX:-${CONFIGURED_HBROOT:-$PS5_PAYLOAD_SDK/target/user/homebrew}}"
if [ -n "$CONFIGURED_HBROOT" ]; then
  [ "$(realpath -e "$HB")" = "$(realpath -e "$CONFIGURED_HBROOT")" ] || {
    echo "!! packaging homebrew prefix differs from the configured Kodi prefix" >&2; exit 1; }
fi
LLD="$PS5_PAYLOAD_SDK/bin/prospero-lld"
AR="$PS5_PAYLOAD_SDK/bin/prospero-ar"
PS5_TEST_BINARY_ADDON="${PS5_TEST_BINARY_ADDON:-0}"
PS5_SPRX_START_TRACE_DIAGNOSTIC="${PS5_SPRX_START_TRACE_DIAGNOSTIC:-0}"
CONFIGURED_SHARED_SDK="$(sed -n 's/^PS5_NATIVE_RUNTIME_SDK:[^=]*=//p' "$BUILD/CMakeCache.txt")"
SHARED_SDK="${PS5_NATIVE_RUNTIME_SDK:-$CONFIGURED_SHARED_SDK}"
[ -n "$SHARED_SDK" ] && [ -n "$CONFIGURED_SHARED_SDK" ] || {
  echo "!! Native runtime SDK required; configure with tools/ps5/configure.sh first" >&2; exit 1; }
SHARED_SDK="$(realpath -e "$SHARED_SDK")"
[ "$SHARED_SDK" = "$(realpath -e "$CONFIGURED_SHARED_SDK")" ] || {
  echo "!! packaging SDK differs from the configured Kodi SDK" >&2; exit 1; }
NATIVE_DEPENDS_MAP="$(sed -n 's/^PS5_NATIVE_DEPENDS_MAP:[^=]*=//p' "$BUILD/CMakeCache.txt")"
[ -n "$NATIVE_DEPENDS_MAP" ] || {
  echo "!! PS5_NATIVE_DEPENDS_MAP required; reconfigure the native title" >&2; exit 1; }
python3 "$ROOT/tools/ps5/native_contract.py" --sdk "$SHARED_SDK" \
  --archive-map "$NATIVE_DEPENDS_MAP" >/dev/null
case "$PS5_SPRX_START_TRACE_DIAGNOSTIC" in
  0) ;;
  1) [ "$PS5_TEST_BINARY_ADDON" = "1" ] || {
       echo "!! SPRX start tracing requires PS5_TEST_BINARY_ADDON=1" >&2; exit 1; } ;;
  *) echo "!! PS5_SPRX_START_TRACE_DIAGNOSTIC must be 0 or 1" >&2; exit 1 ;;
esac
TEST_ADDON="$BUILD/test-addons/sprx"
case "$PS5_TEST_BINARY_ADDON" in
  0) ;;
  1) PS5_SPRX_START_TRACE_DIAGNOSTIC="$PS5_SPRX_START_TRACE_DIAGNOSTIC" \
       bash "$ROOT/tools/ps5/build-sprx-test-addon.sh" "$TEST_ADDON" ;;
  *) echo "!! PS5_TEST_BINARY_ADDON must be 0 or 1" >&2; exit 1 ;;
esac

PS5_ADAPTIVE_TEST="${PS5_ADAPTIVE_TEST:-0}"
PS5_BUNNY_DIAGNOSTIC="${PS5_BUNNY_DIAGNOSTIC:-0}"
case "$PS5_BUNNY_DIAGNOSTIC" in
  0|1) ;;
  *) echo "!! PS5_BUNNY_DIAGNOSTIC must be 0 or 1" >&2; exit 1 ;;
esac
if [ "$PS5_ADAPTIVE_TEST" != "0" ] || [ -n "${PS5_ADDON_ELFS:-}" ] ||
   [ -n "${PS5_AUDIO_DECODERS:-}" ]; then
  echo "!! Legacy add-on selections are unsupported; use the native catalogue pipeline." >&2
  exit 1
fi

[ -f "$BUILD/kodi.bin" ] || { echo "!! $BUILD/kodi.bin not found: build Kodi first"; exit 1; }
# A failed/incomplete Kodi build leaves kodi.bin stale relative to its own
# link inputs: packaging would silently ship the previous build. Ask ninja
# itself whether the kodi.bin target specifically has pending work, rather
# than comparing mtimes against every *.o under $BUILD - the tree also
# contains leaf objects with no edge to kodi.bin at all (e.g.
# cores/dll-loader/exports/wrapper.c.o, the experimental binary-addon
# loader's always-rebuilt helper target), which can legitimately be newer
# than kodi.bin from an earlier, unrelated partial build and would
# otherwise cause a false positive here. A few always-run generator/copy
# utility steps (ExportFiles/GenerateSystemAddons/GenerateAddonXml) show up
# in a dry run regardless of staleness; only a line that actually produces
# kodi.bin counts as real pending work.
if [ -f "$BUILD/build.ninja" ] && command -v ninja >/dev/null 2>&1; then
  if (cd "$BUILD" && ninja -n kodi.bin 2>/dev/null) | grep -qE 'kodi\.bin$|-o kodi\.bin'; then
    echo "!! $BUILD/kodi.bin has pending link work per 'ninja -n kodi.bin':"
    echo "!! the Kodi build did not finish. Check: grep -n 'FAILED:\| error:' ~/kodi-build.log"
    exit 1
  fi
fi
[ -f "$APP_TEMPLATE/Makefile" ] && [ -d "$APP_TEMPLATE/.deps/native" ] || {
  echo "!! app template not found at $APP_TEMPLATE (build ps5-opengl's imgui-demo first: make -C $WORK/ps5-opengl imgui-demo)"; exit 1; }

echo "==> 1. staging Kodi's data into $STAGE"
[ ! -e "$STAGE" ] && [ ! -L "$STAGE" ] || {
  echo "!! Stage already exists: $STAGE; choose a fresh STAGE to preserve build evidence" >&2; exit 1; }
mkdir -p "$STAGE"
DESTDIR="$STAGE" cmake --install "$BUILD" >"$STAGE/install.log" 2>&1 || { tail -20 "$STAGE/install.log"; exit 1; }
[ -d "$STAGE/app0/share/kodi" ] || { echo "!! install produced no app0/share/kodi"; exit 1; }
# Native add-on prefix selection.
PREFIX_LIST="$(python3 - "$ROOT" "${PS5_ADDON_PREFIXES:-}" "${PS5_ADDON_PREFIXES+x}" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
record = root / "build/ps5-addon-repository/work/build-record.json"
prefixes = ([p for p in sys.argv[2].split(":") if p] if sys.argv[3] else
            json.loads(record.read_text()).get("prefixes", []) if record.is_file() else [])
if not isinstance(prefixes, list):
    sys.exit("invalid catalogue installed prefix record")
for prefix in dict.fromkeys(prefixes):
    if not isinstance(prefix, str) or "\n" in prefix or not Path(prefix).is_absolute() or not Path(prefix).is_dir():
        sys.exit(f"invalid or missing installed prefix: {prefix}")
    print(prefix)
PY
)"
ADDON_PREFIXES=()
if [ -n "$PREFIX_LIST" ]; then
  mapfile -t ADDON_PREFIXES <<< "$PREFIX_LIST"
fi
# End native add-on prefix selection.
ADDON_ARGS=(--report "$STAGE/native-addons.json" --runtime-sdk "$SHARED_SDK"
  --catalogue "$ROOT/tools/ps5/addon-catalogue.json" --bundled-only
  --destination "$STAGE/app0/share/kodi" --system-addons-source "$ROOT/addons")
if [ "$PS5_BUNNY_DIAGNOSTIC" = "1" ]; then
  ADDON_ARGS+=(--bunny-diagnostic)
fi
for prefix in "${ADDON_PREFIXES[@]}"; do
  ADDON_ARGS+=(--prefix "$prefix")
done
python3 "$ROOT/tools/ps5/stage-installed-addons.py" "${ADDON_ARGS[@]}" >"$STAGE/native-addons.list"
if [ "$PS5_TEST_BINARY_ADDON" = "1" ]; then
  cp -a "$TEST_ADDON/screensaver.ps5.sprx-test" "$STAGE/app0/share/kodi/addons/"
fi

echo "==> 2. app directory from the demo template"
APP="$STAGE/app"
mkdir -p "$APP"
for entry in Makefile assets runtime sce_sys tooling tools include; do
  [ -e "$APP_TEMPLATE/$entry" ] && cp -a "$APP_TEMPLATE/$entry" "$APP/"
done
cp -a "$APP_TEMPLATE/.deps" "$APP/.deps"        # SDK copy incl. libSceAgc/AgcDriver/VideoOut stubs
patch --batch --forward -d "$APP" -p1 < "$ROOT/tools/ps5/patches/native-app-reserve-process-parameters.patch"
rm -rf "$APP/src" "$APP/vendor" "$APP/build" "$APP/dist"
mkdir -p "$APP/src" "$APP/vendor"
cp "$HERE/shims/native-app/weak_shims.c" "$APP/src/"   # weak refs the converter must not see
cp "$HERE/shims/native-app/libc_extras.c" "$APP/src/"  # libc bits the system libc lacks
cp "$HERE/shims/native-app/libc_socket.c" "$APP/src/"  # Python socket redirects, ahead of SDK libkodishim.a
cp "$HERE/shims/native-app/pipe_fallback.c" "$APP/src/" # pipe() via socketpair, fcntl F_SETFD as success (--wrap=pipe, --wrap=fcntl)
cp "$HERE/shims/native-app/stdio_tee.c" "$APP/src/"    # fd 1/2 -> klog (--wrap=write): Python fatal init errors, aborts
SPRX_TRACE_LINK_FLAG=""
if [ "$PS5_SPRX_START_TRACE_DIAGNOSTIC" = "1" ]; then
  cp "$HERE/shims/native-app/sprx_start_trace.c" "$APP/src/"
  cp "$ROOT/tools/ps5/tests/native_start_trace.h" "$APP/src/"
  SPRX_TRACE_LINK_FLAG="--wrap=sceKernelLoadStartModule"
fi
# libScePosixForWebKit is a browser-only system module: a title never gets it,
# and everything imported from it stays at address 0 (first launch: isatty()).
# Remove its link stub so nothing can bind to it; the shims above cover what
# only it provided, and lld reports anything else as undefined at link time.
rm -f "$APP/.deps/native/ps5-payload-sdk/target/lib/libScePosixForWebKit.so"
# Same for libkernel_sys: a system-process module that is not loaded into a
# title (umask() from it jumped to address 0). Anything only it provides now
# shows up as undefined at link time instead of crashing at run time.
rm -f "$APP/.deps/native/ps5-payload-sdk/target/lib/libkernel_sys.so"
# Our Sony link stubs (scripts/17-build-sce-stubs.sh put them in the SDK)
# go next to the SDK stubs the template links against.
STUBDIR="$APP/.deps/native/ps5-payload-sdk/target/lib"
for stub in "$HERE"/shims/sce_stubs/*.c; do
  so="$PS5_PAYLOAD_SDK/target/lib/$(basename "$stub" .c).so"
  [ -f "$so" ] || { echo "!! $so missing: run scripts/17-build-sce-stubs.sh"; exit 1; }
  cp "$so" "$STUBDIR/"
done
# libSceVideoOut: our extended stub (the SDK's plus the VRR unpeg function)
# replaces the template's copy, provided it exports everything that one does.
VO_EXT="$PS5_PAYLOAD_SDK/target/lib/libSceVideoOut.so"
[ -f "$PS5_PAYLOAD_SDK/target/lib/libSceVideoOut.so.sdk" ] || {
  echo "!! extended libSceVideoOut stub missing: run scripts/17-build-sce-stubs.sh"; exit 1; }
VO_NM="$PS5_PAYLOAD_SDK/bin/llvm-nm"; [ -x "$VO_NM" ] || VO_NM="$(command -v llvm-nm-18 || command -v llvm-nm)"
VO_MISSING="$(comm -23 <("$VO_NM" -D --defined-only "$STUBDIR/libSceVideoOut.so" | awk '{print $3}' | sort) \
                       <("$VO_NM" -D --defined-only "$VO_EXT" | awk '{print $3}' | sort))"
[ -z "$VO_MISSING" ] || { echo "!! extended libSceVideoOut stub lacks: $VO_MISSING"; exit 1; }
cp "$VO_EXT" "$STUBDIR/libSceVideoOut.so"
grep -q -- "--wrap=malloc" "$APP/tools/build.sh" || { echo "!! template build.sh has no malloc wraps"; exit 1; }

echo "==> 3. Kodi link inputs"
python3 "$ROOT/tools/ps5/kodi-link-inputs.py" \
  "$BUILD" "$APP/vendor" "$HB" "$PS5_PAYLOAD_SDK" "$PS5_OPENGL_PREFIX"
if [ -n "$NATIVE_DEPENDS_MAP" ]; then
  python3 - "$NATIVE_DEPENDS_MAP" "$APP/vendor/kodi-deps.txt" <<'PY'
import json
from pathlib import Path
import sys
mapping = json.loads(Path(sys.argv[1]).read_text())
inputs = set(Path(sys.argv[2]).read_text().splitlines())
remaining = inputs.intersection(mapping)
if remaining:
    sys.exit("old stdio archives remain in configured Kodi link: " + ", ".join(sorted(remaining)))
missing = set(mapping.values()) - inputs
if missing:
    sys.exit("native dependency replacements missing from Kodi link: " + ", ".join(sorted(missing)))
PY
fi
MAIN_O="$BUILD/CMakeFiles/kodi.dir/xbmc/platform/ps5/main.cpp.o"
[ -f "$MAIN_O" ] || { echo "!! $MAIN_O missing"; exit 1; }
# Kodi's own libraries must be linked whole (self-registering factories live in
# otherwise-unreferenced objects). A relocatable pre-link is not an option
# (lld refuses the multi-relocation-section object that produces), so the
# archives go straight into the boilerplate's lld invocation via a response
# file, injected the same way ps5-opengl's builder injects its malloc wraps.
{ echo "$MAIN_O"; cat "$APP/vendor/kodi-whole.txt"; } > "$APP/vendor/kodi-whole.rsp"
LINK_SCRIPT="$APP/tools/build.sh"
[ "$(grep -c -- '--wrap=malloc_usable_size \\$' "$LINK_SCRIPT")" = 1 ] || { echo "!! unexpected link line in $LINK_SCRIPT"; exit 1; }
sed -i "/--wrap=malloc_usable_size \\\\$/a\\    --error-limit=0 --wrap=pthread_create --wrap=pthread_setspecific --wrap=pipe --wrap=fcntl --wrap=chdir --wrap=write --wrap=fopen --wrap=exit $SPRX_TRACE_LINK_FLAG --whole-archive @$APP/vendor/kodi-whole.rsp --no-whole-archive \\\\" "$LINK_SCRIPT"
grep -q "kodi-whole.rsp" "$LINK_SCRIPT" || { echo "!! failed to inject the whole-archive list"; exit 1; }

# External libraries as a linker GROUP (circular deps resolve inside a group),
# followed by the C++ runtime (unwind, c++abi, c++) and clang's builtins,
# exactly the set ps5-opengl's native builder adds. No libc.a: libc and
# pthread come from the title's clean-room runtime (libc.prx).
BUILTINS="$(clang-18 --print-resource-dir)/lib/linux/libclang_rt.builtins-x86_64.a"
[ -f "$BUILTINS" ] || { echo "!! $BUILTINS not found"; exit 1; }
{
  printf 'SEARCH_DIR("%s")\n' "$APP/.deps/native/ps5-payload-sdk/target/lib"
  printf 'GROUP (\n'
  while read -r lib; do [ -n "$lib" ] && printf '  "%s"\n' "$lib"; done < "$APP/vendor/kodi-deps.txt"
  printf '  "%s"\n' "$BUILTINS"
  printf ')\n'
} > "$APP/vendor/libkodi_group.a"
printf 'APP_STATIC_ARCHIVES = vendor/libkodi_group.a\n' > "$APP/.env"
if [ -n "$SHARED_SDK" ]; then
  title_provider_args=()
  title_system_sdk="${PS5_NATIVE_TITLE_SYSTEM_SDK:-}"
  if [ -n "${PS5_NATIVE_TITLE_SYSTEM_SDK:-}" ] && [ ! -d "$title_system_sdk" ]; then
    echo "!! title-only provider SDK directory does not exist: $title_system_sdk" >&2; exit 1
  fi
  for manifest in "$title_system_sdk"/*/manifest.json; do
    [ -f "$manifest" ] || continue
    title_provider_args+=(--provider "$manifest")
  done
  python3 "$ROOT/tools/ps5/prepare-shared-title.py" --sdk "$SHARED_SDK" --app "$APP" \
    --native-tool "$APP_TEMPLATE/build/host/ps5-native-tool" \
    "${title_provider_args[@]}"
fi

echo "==> 4. title metadata"
python3 - "$APP/sce_sys/param.json" "$TITLE_ID" <<'PY'
import json, os, sys
path, tid = sys.argv[1], sys.argv[2]
p = json.load(open(path))
p["titleId"] = tid
p["conceptId"] = tid[4:]
p["contentId"] = f"UP9000-{tid}_00-KODIPS5000000001"
p["contentVersion"] = "01.000.000"
p["masterVersion"] = "01.00"
p["downloadDataSize"] = max(int(p.get("downloadDataSize", 0)), 2048)  # /download0 = Kodi's home
# Home-screen area (boilerplate docs/CONFIGURATION.md):
#   media: applicationCategoryType 65536, contentBadgeType 2, no gameIntent
#   game:  applicationCategoryType 0, contentBadgeType 1, gameIntent launchActivity
# Default: game. As a Media app the kernel halves the page tables (128 MiB
# max) and applies a stricter sandbox; the PS5 GL driver then fails with
# EGL_BAD_ALLOC. KODI_CATEGORY=media stays available for experiments.
# High-refresh entitlement (the flags ps5-opengl sets for its 120 Hz builds,
# as ProsperoLight declares): the PS5 runs VRR through this preset, which Kodi
# uses only for VRR during playback, never as a fixed 120 Hz output.
p["attribute3"] = int(p.get("attribute3", 0)) | 0x80040
# HDR-capable title metadata (the flags ProsperoLight declares): without it
# the HDR scanout format is refused (BlackBearReloaded's video research). With
# it, the PS5 keeps the TV in HDR for the whole session when its HDR setting is
# "On When Supported", mapping SDR content itself - its standard behaviour fo
# HDR-capable titles. KODI_HDR_TITLE=0 builds an SDR title instead (TV stays
# SDR; HDR video is tone mapped by Kodi).
if os.environ.get("KODI_HDR_TITLE", "1") != "0":
    p["attribute"] = int(p.get("attribute", 0)) | 0x62000000
else:
    p["attribute"] = int(p.get("attribute", 0)) & ~0x62000000
category = os.environ.get("KODI_CATEGORY", "game")
if category == "media":
    p["applicationCategoryType"] = 65536
    p["contentBadgeType"] = 2
    p.pop("gameIntent", None)
    # The kernel caps page tables at 128 MiB for Media apps (games: 256 MiB,
    # which the GL template asks for) and refuses to launch otherwise:
    #   property "kernel.cpuPageTableSize" of param.json is not valid.
    #   value: 268435456, min: 2097152, max: 134217728
    kernel = p.setdefault("kernel", {})
    for key in ("cpuPageTableSize", "gpuPageTableSize"):
        kernel[key] = min(int(kernel.get(key, 134217728)), 134217728)
else:
    p["applicationCategoryType"] = 0
    p["contentBadgeType"] = 1
    p["gameIntent"] = {"permittedIntents": [{"intentType": "launchActivity"}]}
lp = p.setdefault("localizedParameters", {"defaultLanguage": "en-US"})
lp.setdefault("en-US", {})["titleName"] = "Kodi"
# The save-data entry (Settings > Storage > Saved Data) inherits the title
# name and icon0 above, so /download0 shows as "Kodi" with the Kodi icon. Make
# that explicit so a data manager that reads its own field labels it clearly.
sd = p.setdefault("saveDataInfo", {})
sd["title"] = "Kodi"
sd["subtitle"] = "Settings, add-ons and library"
sd["detail"] = "Kodi configuration, installed add-ons and media library"
json.dump(p, open(path, "w"), indent=2)
print(f"    titleId {tid}, {category} app, downloadDataSize {p['downloadDataSize']} MiB")
PY
# Kodi's icon ships in the repository (title/sce_sys/icon0.png, 512x512).
cp "$HERE/title/sce_sys/icon0.png" "$APP/sce_sys/icon0.png" || { echo "!! title/sce_sys/icon0.png missing"; exit 1; }
# Home-screen artwork: the template carries the GL demo's backgrounds and
# music. Use ours from title/sce_sys if present, otherwise ship none (the
# shell then shows its default). pic0 = selected-app background, pic1 =
# launch transition; the builder requires both or neither.
rm -f "$APP/sce_sys/pic0.dds" "$APP/sce_sys/pic1.dds" "$APP/sce_sys/snd0.at9"
if [ -f "$HERE/title/sce_sys/pic0.dds" ] && [ -f "$HERE/title/sce_sys/pic1.dds" ]; then
  cp "$HERE/title/sce_sys/pic0.dds" "$HERE/title/sce_sys/pic1.dds" "$APP/sce_sys/"
  echo "    backgrounds: title/sce_sys/pic0.dds + pic1.dds"
else
  echo "    backgrounds: none (add pic0.dds + pic1.dds to title/sce_sys to set them)"
fi
[ -f "$HERE/title/sce_sys/snd0.at9" ] && cp "$HERE/title/sce_sys/snd0.at9" "$APP/sce_sys/"

echo "==> 5. native link + FSELF (boilerplate)"
build_result=0
make -C "$APP" --no-print-directory app 2>&1 | tee "$STAGE/app-build.log" | tail -30 || build_result=$?
DIST="$APP/dist/$TITLE_ID"
if [ "$build_result" -ne 0 ] || [ ! -s "$DIST/eboot.bin" ]; then
  echo "!! native build failed or no eboot.bin produced; see $STAGE/app-build.log"
  ELF="$(find "$APP" -name llvm-pie.elf -newer "$APP/vendor/kodi-whole.rsp" 2>/dev/null | head -1)"
  if [ -z "$ELF" ] && grep -q "undefined symbol:" "$STAGE/app-build.log"; then
    echo "   undefined at link time (no module a title has provides them), with a first user:"
    awk '/undefined symbol:/ {sym=$NF; getline; getline; if (!(sym in seen)) {seen[sym]=1; sub(/^.*in archive /,""); n=split($0,a,"/"); print "     " sym "   <- " a[n]}}' \
        "$STAGE/app-build.log" | sort
    echo "   ($(grep -c "undefined symbol:" "$STAGE/app-build.log") references; full list in $STAGE/app-build.log)"
  fi
  if [ -n "$ELF" ]; then
    # Same rule as the converter: every undefined dynamic symbol must be exported
    # by one of the NEEDED stub modules. List all offenders at once.
    NM="$PS5_PAYLOAD_SDK/bin/llvm-nm"; STUBS="$APP/.deps/native/ps5-payload-sdk/target/lib"
    for so in $(readelf -dW "$ELF" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p'); do
      [ -f "$STUBS/$so" ] && "$NM" -D --defined-only "$STUBS/$so" | awk '{print $NF}'
    done | sed 's/@.*//' | sort -u > "$STAGE/stub-exports.txt"
    "$NM" -D -u "$ELF" | awk '{print $NF}' | sed 's/@.*//' | sort -u > "$STAGE/elf-imports.txt"
    echo "   symbols no Sony stub provides (each needs a definition in shims/native-app/weak_shims.c):"
    comm -23 "$STAGE/elf-imports.txt" "$STAGE/stub-exports.txt" | sed 's/^/     /'
  fi
  exit 1
fi

KODI_IMAGE="$APP/build/kodi-app/linked.so"
APPLICATION_RUNTIME="$DIST/sce_module/libkodi-app.sprx"
python3 - "$ROOT/tools/ps5" "$APP/build/llvm-pie.elf" "$KODI_IMAGE" "$APPLICATION_RUNTIME" <<'PY'
import hashlib
import json
import mmap
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
elf = Path(sys.argv[2])
application = Path(sys.argv[3])
stable_origin = (
    b"https://raw.githubusercontent.com/Notedop/kodi-ps5-binary-addons/"
    b"main/repo/piers-ps5-1/stable/")
with application.open("rb") as stream:
    with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
        if image.find(b"repository.ps5\0") < 0 or image.find(stable_origin + b"\0") < 0:
            sys.exit("PS5 title lacks the official stable repository registration; regenerate "
                     "CompileInfo.cpp and rebuild Kodi before packaging a runtime receipt")
digest = hashlib.sha256()
with elf.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
receipt = dict(schema=1, runtime=dict(id="kodi-ps5-native-sprx-v1",
               compatibility_version="2.0.0", loader="sceKernelLoadStartModule"),
               title_sha256=digest.hexdigest())
if application != elf:
    receipt["application_sha256"] = hashlib.sha256(Path(sys.argv[4]).read_bytes()).hexdigest()
if os.environ.get("PS5_SPRX_START_TRACE_DIAGNOSTIC", "0") == "1":
    receipt["diagnostics"] = dict(native_start_trace=True)
elf.with_name("ps5-addon-runtime.json").write_text(json.dumps(receipt, indent=2) + "\n")
PY
cp "$APP/build/ps5-addon-runtime.json" "$DIST/ps5-addon-runtime.json"
cp "$APP/vendor/ps5-shared-runtime.json" "$DIST/ps5-shared-runtime.json"

echo "    native application imports per verified provider:"
python3 - "$APP/build/kodi-app/import-ownership.json" <<'PY'
from collections import Counter
import json
from pathlib import Path
import sys

ownership = json.loads(Path(sys.argv[1]).read_text())
counts = Counter(entry["module_id"] for entry in ownership["imports"])
for module in ownership["modules"]:
    print(f"      {module['name']:28s} {counts[module['id']]:4d}")
PY

echo "==> 6. Kodi data into the title folder"
mkdir -p "$DIST/share"
cp -a "$STAGE/app0/share/kodi" "$DIST/share/"
if [ "$PS5_TEST_BINARY_ADDON" = "1" ]; then
  cmp "$TEST_ADDON/screensaver.ps5.sprx-test/screensaver.ps5.sprx-test.sprx" \
    "$DIST/share/kodi/addons/screensaver.ps5.sprx-test/screensaver.ps5.sprx-test.sprx"
  cp "$TEST_ADDON/artifacts.sha256" "$DIST/sprx-addon-build.txt"
fi
python3 "$ROOT/tools/ps5/verify-shared-title.py" --app "$APP" --sdk "$SHARED_SDK" \
  --archive-map "$NATIVE_DEPENDS_MAP"
chmod -R 0755 "$DIST"
du -sh "$DIST" | awk '{print "    title folder size: "$1}'

if [ "$PACKAGE_ZIP" = "1" ]; then
  ARTIFACT_DIR="${ARTIFACT_DIR:-$STAGE/artifacts}"
  mkdir -p "$ARTIFACT_DIR"
  ZIP_PATH="$ARTIFACT_DIR/${TITLE_ID}.zip"
  if command -v zip >/dev/null 2>&1; then
    (cd "$(dirname "$DIST")" && rm -f "$ZIP_PATH" && zip -qry "$ZIP_PATH" "$TITLE_ID")
  else
    rm -f "$ZIP_PATH"
    (cd "$(dirname "$DIST")" && cmake -E tar "cf" "$ZIP_PATH" --format=zip "$TITLE_ID")
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$ZIP_PATH" > "$ZIP_PATH.sha256"
  fi
  echo "Package zip: $ZIP_PATH"

  # Build manifest: source/dependency revisions, feature config, artifact
  # hashes. Written next to the zip so a release can be reproduced/audited
  # without re-reading build logs (Phase 4 "preserve packaging and runtime
  # paths" requirement). Best-effort: a dependency recipe dir that is missing
  # or was never built via tools/depends just reports null, it never fails
  # packaging.
  python3 - "$ROOT" "$BUILD" "$ARTIFACT_DIR" "$TITLE_ID" "${KODI_CATEGORY:-game}" "${KODI_HDR_TITLE:-1}" "$ZIP_PATH" <<'PY'
import json, os, pathlib, subprocess, sys, datetime

root, build, artifact_dir, title_id, category, hdr_title, zip_path = sys.argv[1:8]

def git_describe(path):
    try:
        return subprocess.check_output(
            ["git", "-C", path, "describe", "--always", "--dirty"],
            stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return None

def dep_version(name, version_file):
    p = pathlib.Path(root, "tools/depends/target", name, version_file)
    if not p.exists():
        return None
    kv = {}
    for line in p.read_text(errors="replace").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    return kv.get("VERSION")

def cmake_cache_bool(name):
    p = pathlib.Path(build, "CMakeCache.txt")
    if not p.exists():
        return None
    for line in p.read_text(errors="replace").splitlines():
        if line.startswith(name + ":"):
            return line.split("=", 1)[-1].strip()
    return None

def sha256_of(path):
    sha = pathlib.Path(path + ".sha256")
    if sha.exists():
        return sha.read_text().split()[0]
    return None

manifest = {
    "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "title_id": title_id,
    "category": category,
    "hdr_capable": hdr_title not in ("0", "false", "False"),
    "kodi_source_revision": git_describe(root),
    "feature_config": {
        "ENABLE_PYTHON": cmake_cache_bool("ENABLE_PYTHON"),
        "ENABLE_UPNP": cmake_cache_bool("ENABLE_UPNP"),
        "ENABLE_OPTICAL": cmake_cache_bool("ENABLE_OPTICAL"),
        "ENABLE_DVDCSS": cmake_cache_bool("ENABLE_DVDCSS"),
        "ENABLE_AIRTUNES": cmake_cache_bool("ENABLE_AIRTUNES"),
    },
    "dependency_versions": {
        "dav1d": dep_version("dav1d", "DAV1D-VERSION"),
        "ffmpeg": dep_version("ffmpeg", "FFMPEG-VERSION"),
        "python3": dep_version("python3", "PYTHON3-VERSION"),
        "curl": dep_version("curl", "CURL-VERSION"),
        "libsmb2": dep_version("libsmb2", "LIBSMB2-VERSION"),
        "brotli": dep_version("brotli", "BROTLI-VERSION"),
        "libiconv": dep_version("libiconv", "LIBICONV-VERSION"),
    },
    "artifact": {
        "zip": os.path.basename(zip_path),
        "sha256": sha256_of(zip_path),
    },
}

out = pathlib.Path(artifact_dir, title_id + "-BUILD-MANIFEST.json")
out.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Build manifest: {out}")
PY
fi

echo
echo "Title folder: $DIST"
echo "Packaging only; deployment and launch are separate user-operated steps."
echo "Log: KODI_LOG_TO_CONSOLE is not set in a title, so read /download0/.kodi/temp/kodi.log over FTP;"
echo "     klog (nc \$PS5_HOST 3232) shows loader messages and Kodi's stdout."
