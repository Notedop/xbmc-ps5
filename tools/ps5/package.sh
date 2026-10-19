#!/usr/bin/env bash
# Package the built Kodi as a ShadowMountPlus folder title.
#
# kodi.bin (Kodi's own link) proves the symbol graph resolves, but a home-screen
# title must be linked the native-app way: the boilerplate's startup code and
# linker script, its ELF -> FSELF converter and its clean-room libc.prx. This
# is exactly how ps5-opengl's imgui-demo was built, so we reuse that demo's
# prepared app directory as the template (same runtime, same Agc/VideoOut
# stubs, same heap + malloc wraps) and swap the demo's code for Kodi:
#   1. cmake --install (DESTDIR)      -> staged /app0 tree (share/kodi data)
#   2. Kodi's archives (whole-archive) -> vendor/kodi-whole.rsp, injected into the link
#   3. GROUP() of every external lib   -> vendor/libkodi_group.a (from Kodi's link line)
#   4. boilerplate `make app`          -> dist/<TITLE_ID>/{eboot.bin, sce_module, sce_sys}
#   5. share/kodi copied into the title folder; optional FTP upload.
#
#   BUILD       Kodi build dir           default build/ps5-release
#   STAGE       staging dir              default build/ps5-stage
#   WORK        external sources dir     default build/ps5-external (bootstrap.sh)
#   APP_TEMPLATE demo app dir            default $WORK/ps5-opengl/build/native-app/PPSA99005
#   TITLE_ID    default PPSA99420        HEAP_MIB app malloc heap (default 2048)
#   KODI_CATEGORY  game (default) or media: home-screen area (media is
#               experimental: the GL driver does not start in its sandbox)
#   PS5_HOST    console IP for FTP upload (optional)
#   PS5_DEPLOY_JOBS concurrent FTP uploads (default 3, range 1..8)
#   PS5_TEST_BINARY_ADDON  1 includes the isolated loader probe (default 0)
#   PS5_ADDON_PREFIXES colon-separated installed Kodi prefixes (default: repository
#                      build-record prefixes, else historical depends/audio/screensavers)
#                      Only catalogue-bundled add-ons are copied; all whitelisted
#                      candidate ELFs contribute to the title's runtime exports.
#   PS5_ADAPTIVE_TEST  1 requires adaptive and adds its DASH test (default 0)
#   PS5_ADAPTIVE_PREFIX  override the default adaptive depends prefix (optional)
#   PS5_AUDIO_DECODERS explicit legacy audio selection (unset: discover automatically)
#   PS5_AUDIO_DECODER_PREFIX installed decoder prefix (default isolated audit install)
#   PS5_ADDON_ELFS  colon-separated trusted installed .so files whose runtime
#                   archive members must be retained (export policy only)
set -euo pipefail

export PS5_PAYLOAD_SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
export PS5_OPENGL_PREFIX="${PS5_OPENGL_PREFIX:-/opt/ps5-opengl-gl46}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT"
BUILD="${BUILD:-$ROOT/build/ps5-release}"
STAGE="${STAGE:-$ROOT/build/ps5-stage}"
WORK="${WORK:-$ROOT/build/ps5-external}"
APP_TEMPLATE="${APP_TEMPLATE:-$WORK/ps5-opengl/build/native-app/PPSA99005}"
TITLE_ID="${TITLE_ID:-PPSA99420}"
HEAP_MIB="${HEAP_MIB:-2048}"
PACKAGE_ZIP="${PACKAGE_ZIP:-0}"
HB="$PS5_PAYLOAD_SDK/target/user/homebrew"
LLD="$PS5_PAYLOAD_SDK/bin/prospero-lld"
AR="$PS5_PAYLOAD_SDK/bin/prospero-ar"
PS5_TEST_BINARY_ADDON="${PS5_TEST_BINARY_ADDON:-0}"
TEST_ADDON="$BUILD/test-addons/test.binary.ps5"
case "$PS5_TEST_BINARY_ADDON" in
  0) ;;
  1) bash "$ROOT/tools/ps5/stage-test-addon.sh" "$TEST_ADDON" --check ;;
  *) echo "!! PS5_TEST_BINARY_ADDON must be 0 or 1" >&2; exit 1 ;;
esac

PS5_ADAPTIVE_TEST="${PS5_ADAPTIVE_TEST:-0}"
case "$PS5_ADAPTIVE_TEST" in
  0|1) ;;
  *) echo "!! PS5_ADAPTIVE_TEST must be 0 or 1" >&2; exit 1 ;;
esac

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

ADDON_ARGS=(--catalogue "$ROOT/tools/ps5/addon-catalogue.json" --bundled-only)
ADDON_PREFIXES=()
ADDON_BUILD_RECORD="$ROOT/build/ps5-addon-repository/work/build-record.json"
if [ "${PS5_ADDON_PREFIXES+x}" = x ]; then
  if [ -n "$PS5_ADDON_PREFIXES" ]; then
    [[ "$PS5_ADDON_PREFIXES" != :* && "$PS5_ADDON_PREFIXES" != *: && "$PS5_ADDON_PREFIXES" != *::* ]] || {
      echo "!! PS5_ADDON_PREFIXES contains an empty prefix" >&2; exit 1; }
    IFS=: read -r -a ADDON_PREFIXES <<< "$PS5_ADDON_PREFIXES"
    for prefix in "${ADDON_PREFIXES[@]}"; do
      [ -d "$prefix" ] || { echo "!! configured add-on prefix missing: $prefix" >&2; exit 1; }
    done
  fi
elif [ -f "$ADDON_BUILD_RECORD" ]; then
  RECORDED_PREFIXES="$(python3 - "$ADDON_BUILD_RECORD" <<'PY'
import json
from pathlib import Path
import sys

try:
    record = json.loads(Path(sys.argv[1]).read_text())
    prefixes = record.get("prefixes") if isinstance(record, dict) else None
    if not isinstance(prefixes, list) or not prefixes:
        raise ValueError("prefixes must be a nonempty list of absolute installed paths")
    for prefix in prefixes:
        if (not isinstance(prefix, str) or not prefix or "\n" in prefix or "\r" in prefix
                or not Path(prefix).is_absolute() or not Path(prefix).is_dir()):
            raise ValueError(f"invalid or missing installed prefix: {prefix!r}")
        print(prefix)
except (OSError, ValueError) as error:
    sys.exit(f"PS5 add-on build record: {error}; rebuild the add-on inventory or set PS5_ADDON_PREFIXES")
PY
)"
  mapfile -t ADDON_PREFIXES <<< "$RECORDED_PREFIXES"
else
  echo "!! No repository build record; using historical installed add-on prefixes." >&2
  echo "!! Before publishing new repository candidates, build/install the full candidate inventory" >&2
  echo "!! and rebuild this title so prepare/verify-addon-exports cover every candidate ELF." >&2
  ADDON_PREFIXES=(
    "${PS5_ADAPTIVE_PREFIX:-$ROOT/build/ps5-depends-test/x86_64-unknown-freebsd-debug}"
    "${PS5_AUDIO_DECODER_PREFIX:-$ROOT/build/ps5-audiodecoders/install}"
    "$ROOT/build/ps5-screensavers/install"
  )
fi
for prefix in "${ADDON_PREFIXES[@]}"; do
  ADDON_ARGS+=(--prefix "$prefix")
done
if [ "${PS5_AUDIO_DECODERS+x}" = x ]; then
  ADDON_ARGS+=(--audio-selection "$PS5_AUDIO_DECODERS")
  read -r -a AUDIO_DECODER_IDS <<< "$PS5_AUDIO_DECODERS"
  for addon in "${AUDIO_DECODER_IDS[@]}"; do
    [[ "$addon" =~ ^audiodecoder\.[a-z0-9]+$ ]] || {
      echo "!! invalid audio decoder ID: $addon" >&2; exit 1; }
    ADDON_ARGS+=(--include "$addon")
  done
fi
if [ "$PS5_ADAPTIVE_TEST" = "1" ]; then
  ADDON_ARGS+=(--include inputstream.adaptive)
fi
python3 "$ROOT/tools/ps5/stage-installed-addons.py" "${ADDON_ARGS[@]}" \
  --report "$BUILD/installed-addons-report.json" > "$BUILD/installed-addons-elfs.txt"

echo "==> 1. staging Kodi's data into $STAGE"
rm -rf "$STAGE"; mkdir -p "$STAGE"
DESTDIR="$STAGE" cmake --install "$BUILD" >"$STAGE/install.log" 2>&1 || { tail -20 "$STAGE/install.log"; exit 1; }
[ -d "$STAGE/app0/share/kodi" ] || { echo "!! install produced no app0/share/kodi"; exit 1; }
if [ "$PS5_TEST_BINARY_ADDON" = "1" ]; then
  bash "$ROOT/tools/ps5/stage-test-addon.sh" "$TEST_ADDON" "$STAGE/app0/share/kodi"
  python3 - "$STAGE/app0/share/kodi/addons/test.binary.ps5/addon.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

manifest = sys.argv[1]
tree = ET.parse(manifest)
platform = tree.find("./extension[@point='xbmc.addon.metadata']/platform")
if platform is None:
    sys.exit("PS5 binary loader probe lacks platform metadata")
platform.text = "ps5"
tree.write(manifest, encoding="utf-8", xml_declaration=True)
PY
fi
if [ "$PS5_ADAPTIVE_TEST" = "1" ]; then
  cp -a "$ROOT/addons/plugin.video.ps5.adaptive.test" "$STAGE/app0/share/kodi/addons/"
fi
ADDON_ELF_LIST="$(python3 "$ROOT/tools/ps5/stage-installed-addons.py" "${ADDON_ARGS[@]}" \
  --report "$STAGE/installed-addons-report.json" --destination "$STAGE/app0/share/kodi" \
  --system-addons-source "$ROOT/addons")"
ADDON_ELFS=()
if [ -n "$ADDON_ELF_LIST" ]; then
  mapfile -t ADDON_ELFS <<< "$ADDON_ELF_LIST"
fi

echo "==> 2. app directory from the demo template"
APP="$STAGE/app"
mkdir -p "$APP"
for entry in Makefile assets runtime sce_sys tooling tools include; do
  [ -e "$APP_TEMPLATE/$entry" ] && cp -a "$APP_TEMPLATE/$entry" "$APP/"
done
cp -a "$APP_TEMPLATE/.deps" "$APP/.deps"        # SDK copy incl. libSceAgc/AgcDriver/VideoOut stubs
# Keep the original plaintext import tables mapped for the in-process resolver.
# The converter still builds its separate Sony metadata for the system loader.
patch --batch --forward -d "$APP" -p1 < "$ROOT/tools/ps5/patches/native-app-preserve-addon-metadata.patch"
patch --batch --forward -d "$APP" -p1 < "$ROOT/tools/ps5/patches/native-app-host-runtime-exports.patch"
patch --batch --forward -d "$APP" -p1 < "$ROOT/tools/ps5/patches/native-app-reserve-process-parameters.patch"
cat >> "$APP/tooling/native/ps5-pie.ld" <<'LD'

/* The native title has no mapped ELF header; expose its actual image bounds. */
PROVIDE_HIDDEN(__ps5_addon_image_base = ADDR(.text));
PROVIDE_HIDDEN(__ps5_addon_image_end = ADDR(.bss) + SIZEOF(.bss));
PROVIDE_HIDDEN(__ps5_addon_dynsym_end = ADDR(.dynsym) + SIZEOF(.dynsym));
ASSERT(ADDR(.text) == 0, "PS5 add-on resolver requires zero-based title text")
LD
rm -rf "$APP/src" "$APP/vendor" "$APP/build" "$APP/dist"
mkdir -p "$APP/src" "$APP/vendor"
if [ -n "${PS5_ADDON_ELFS:-}" ]; then
  IFS=: read -r -a SELECTED_ADDON_ELFS <<< "$PS5_ADDON_ELFS"
  ADDON_ELFS+=("${SELECTED_ADDON_ELFS[@]}")
fi
if [ "$PS5_TEST_BINARY_ADDON" = "1" ]; then
  ADDON_ELFS+=("$TEST_ADDON/test_binary_ps5.so")
fi
python3 "$ROOT/tools/ps5/prepare-addon-exports.py" --sdk "$PS5_PAYLOAD_SDK" \
  --output "$APP/vendor" "${ADDON_ELFS[@]}"
"$PS5_PAYLOAD_SDK/bin/prospero-clang" --target=x86_64-unknown-freebsd \
  -c "$APP/vendor/addon-imports.S" -o "$APP/vendor/addon-imports.o"
cp "$APP_TEMPLATE/src/runtime_shims.c" "$APP/src/"
# The GL template's shims stub mkstemps/openlog; shims/native-app/libc_posix.c
# has real ones -> drop the stubs, keep the rest (GL TLS hook, pclose).
python3 - "$APP/src/runtime_shims.c" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
for fn in ("mkstemps", "openlog"):
    s2 = re.sub(r"\n[a-z][^\n]*\b" + fn + r"\([^)]*\)\s*\{.*?\n\}\n", "\n", s, count=1, flags=re.S)
    if s2 == s: sys.exit(f"could not remove {fn} from runtime_shims.c")
    s = s2
open(p, "w").write(s)
PY
cp "$APP_TEMPLATE/src/app_heap.c" "$APP/src/"
cp "$HERE/shims/native-app/weak_shims.c" "$APP/src/"   # weak refs the converter must not see
cp "$HERE/shims/native-app/libc_extras.c" "$APP/src/"  # libc bits the system libc lacks
cp "$HERE/shims/native-app/libc_posix.c" "$APP/src/"   # POSIX/BSD functions (time, tempfiles, tsearch...)
cp "$HERE/shims/native-app/libc_locale.c" "$APP/src/"  # C-locale xlocale layer for libc++
cp "$HERE/shims/native-app/libc_net.c" "$APP/src/"     # getaddrinfo & co on Sony's resolve
cp "$HERE/shims/native-app/libc_socket.c" "$APP/src/"  # Python socket redirects, ahead of SDK libkodishim.a
cp "$HERE/shims/native-app/thread_stack.c" "$APP/src/" # >= 4 MiB default stacks; explicit attrs unchanged
cp "$HERE/shims/native-app/pthread_tls.c" "$APP/src/"
cp "$HERE/shims/native-app/pipe_fallback.c" "$APP/src/" # pipe() via socketpair, fcntl F_SETFD as success (--wrap=pipe, --wrap=fcntl)
cp "$HERE/shims/native-app/stdio_tee.c" "$APP/src/"    # fd 1/2 -> klog (--wrap=write): Python fatal init errors, aborts
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
# app-owned malloc heap: the demo's 128 MiB is far too small for Kodi
sed -i "s/#define PS5_OPENGL_HEAP_SIZE (128u \* 1024u \* 1024u)/#define PS5_OPENGL_HEAP_SIZE (${HEAP_MIB}ull * 1024ull * 1024ull)/" "$APP/src/app_heap.c"
grep -q "PS5_OPENGL_HEAP_SIZE (${HEAP_MIB}ull" "$APP/src/app_heap.c" || { echo "!! could not raise the heap size in app_heap.c"; exit 1; }
# ...and back it with direct memory: mmap() draws from the title's small
# flexible-memory budget (448 MiB), so a Kodi-sized heap cannot be mmap'ed.
cp "$HERE/shims/native-app/heap_dmem.c" "$APP/src/"
python3 - "$APP/src/app_heap.c" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
mmap_call = """mmap(NULL, PS5_OPENGL_HEAP_SIZE, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANON, -1, 0);"""
if mmap_call not in s or "munmap(base, PS5_OPENGL_HEAP_SIZE);" not in s:
    sys.exit("app_heap.c: unexpected heap setup code")
s = s.replace(mmap_call, "ps5_heap_map(PS5_OPENGL_HEAP_SIZE);")
s = s.replace("munmap(base, PS5_OPENGL_HEAP_SIZE);", "ps5_heap_unmap(base, PS5_OPENGL_HEAP_SIZE);")
s = s.replace("#define PS5_OPENGL_HEAP_SIZE", "void *ps5_heap_map(size_t size);\nint ps5_heap_unmap(void *address, size_t size);\n#define PS5_OPENGL_HEAP_SIZE", 1)
s = s.replace('size=%u\\n",', 'size=%llu\\n",')
open(p, "w").write(s)
PY
grep -q -- "--wrap=malloc" "$APP/tools/build.sh" || { echo "!! template build.sh has no malloc wraps"; exit 1; }

echo "==> 3. Kodi link inputs"
python3 - "$BUILD" "$APP/vendor" "$HB" "$PS5_PAYLOAD_SDK" "$PS5_OPENGL_PREFIX" <<'PY'
import os
import re, sys, os, shlex
build, vendor, hb, sdk, gl = sys.argv[1:6]
ninja = open(os.path.join(build, "build.ninja")).read()
m = re.search(r"^build kodi\.bin:.*?\n(?:  .*\n)*", ninja, re.M)
blk = m.group(0)
libs = re.search(r"^  LINK_LIBRARIES = (.*)$", blk, re.M).group(1)
toks = shlex.split(libs)
whole, deps, mode = [], [], None
for t in toks:
    if t == "-Wl,--whole-archive": mode = "whole"; continue
    if t == "-Wl,--no-whole-archive": mode = "deps"; continue
    if mode == "whole":
        whole.append(t if os.path.isabs(t) else os.path.join(build, t))
    elif mode == "deps":
        if t.endswith(".a"):
            deps.append(t if os.path.isabs(t) else os.path.join(build, t))
        elif t.startswith("-l"):
            name = t[2:]
            for d in (hb + "/lib", gl + "/lib"):
                p = f"{d}/lib{name}.a"
                if os.path.exists(p):
                    deps.append(p); break
            # -l of a Sony stub (.so only) is covered by the boilerplate's --as-needed target/lib/*.so
# The payload SDK's own libc.a/libpthread.a belong to the *payload* runtime (they
# call into crt1.o: kernel_*, __dlopen...). In a native title the clean-room
# libc.prx provides libc and pthread, as in ps5-opengl's and ProsperoLight's
# builds; the C++ runtime archives are added explicitly by the caller.
payload_lib = os.path.realpath(sdk + "/target/lib")
deps = [d for d in deps if os.path.dirname(os.path.realpath(d)) != payload_lib]
# de-duplicate, keep orde
seen = set(); deps = [d for d in deps if not (d in seen or seen.add(d))]
open(os.path.join(vendor, "kodi-whole.txt"), "w").write("\n".join(whole) + "\n")
open(os.path.join(vendor, "kodi-deps.txt"), "w").write("\n".join(deps) + "\n")
print(f"    {len(whole)} Kodi archives, {len(deps)} external libraries")
PY
MAIN_O="$BUILD/CMakeFiles/kodi.dir/xbmc/platform/ps5/main.cpp.o"
[ -f "$MAIN_O" ] || { echo "!! $MAIN_O missing"; exit 1; }
# Kodi's own libraries must be linked whole (self-registering factories live in
# otherwise-unreferenced objects). A relocatable pre-link is not an option
# (lld refuses the multi-relocation-section object that produces), so the
# archives go straight into the boilerplate's lld invocation via a response
# file, injected the same way ps5-opengl's builder injects its malloc wraps.
# Host definitions are published by LLVM's dynsym, not a generated pointer
# binding table. The small import-anchor object retains real system imports
# requested only by add-ons; the converter now translates their relocations.
{ echo "$MAIN_O"; echo "$APP/vendor/addon-imports.o"; cat "$APP/vendor/kodi-whole.txt"; } > "$APP/vendor/kodi-whole.rsp"
LINK_SCRIPT="$APP/tools/build.sh"
[ "$(grep -c -- '--wrap=malloc_usable_size \\$' "$LINK_SCRIPT")" = 1 ] || { echo "!! unexpected link line in $LINK_SCRIPT"; exit 1; }
sed -i "/--wrap=malloc_usable_size \\\\$/a\\    --error-limit=0 --wrap=pthread_create --wrap=pthread_setspecific --wrap=pipe --wrap=fcntl --wrap=chdir --wrap=write --wrap=fopen --whole-archive @$APP/vendor/kodi-whole.rsp --no-whole-archive \\\\" "$LINK_SCRIPT"
sed -i "s|--version-script \"\\\$native/app-symbols.map\"|--undefined-version --version-script \"$APP/vendor/addon-symbols.map\" --export-dynamic-symbol-list=\"$APP/vendor/addon-dynamic.list\" @$APP/vendor/addon-roots.rsp|" "$LINK_SCRIPT"
grep -q 'addon-dynamic.list' "$LINK_SCRIPT" || { echo "!! failed to inject add-on export policy"; exit 1; }
grep -q "kodi-whole.rsp" "$LINK_SCRIPT" || { echo "!! failed to inject the whole-archive list"; exit 1; }

# External libraries as a linker GROUP (circular deps resolve inside a group),
# followed by the C++ runtime (unwind, c++abi, c++) and clang's builtins,
# exactly the set ps5-opengl's native builder adds. No libc.a: libc and
# pthread come from the title's clean-room runtime (libc.prx).
BUILTINS="$(clang-18 --print-resource-dir)/lib/linux/libclang_rt.builtins-x86_64.a"
[ -f "$BUILTINS" ] || { echo "!! $BUILTINS not found"; exit 1; }
{
  printf 'SEARCH_DIR("%s")\n' "$APP/.deps/native/ps5-payload-sdk/target/lib"
  printf 'EXTERN(ps5_agc_gate2_run)\n'
  # Pull the unwinder's dynamic-frame registration from libunwind.a: the binary
  # add-on loader calls the whole-section API (ElfLoader.cpp) to
  # register a loaded add-on's .eh_frame, but nothing else in a static eboot
  # references them, so without EXTERN the archive members are left out and the
  # FSELF converter reports them undefined.
  printf 'EXTERN(__unw_add_dynamic_eh_frame_section)\n'
  printf 'EXTERN(__unw_remove_dynamic_eh_frame_section)\n'
  printf 'GROUP (\n'
  while read -r lib; do [ -n "$lib" ] && printf '  "%s"\n' "$lib"; done < "$APP/vendor/kodi-deps.txt"
  for lib in "$PS5_PAYLOAD_SDK/target/lib/libunwind.a" "$PS5_PAYLOAD_SDK/target/lib/libc++abi.a" \
             "$PS5_PAYLOAD_SDK/target/lib/libc++.a" "$BUILTINS"; do
    printf '  "%s"\n' "$lib"
  done
  printf ')\n'
} > "$APP/vendor/libkodi_group.a"
printf 'APP_STATIC_ARCHIVES = vendor/libkodi_group.a\n' > "$APP/.env"

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
# (|| true: with pipefail a failed make would end the script before the diagnostic below)
make -C "$APP" --no-print-directory app 2>&1 | tee "$STAGE/app-build.log" | grep -v "^\[" | tail -30 || true
DIST="$APP/dist/$TITLE_ID"
if [ ! -s "$DIST/eboot.bin" ]; then
  echo "!! no eboot.bin produced; see $STAGE/app-build.log"
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

python3 "$ROOT/tools/ps5/verify-addon-exports.py" "$APP/build/llvm-pie.elf" "${ADDON_ELFS[@]}"
python3 - "$ROOT/tools/ps5" "$APP/build/llvm-pie.elf" <<'PY'
import hashlib
import json
import mmap
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from addon_catalogue import load_catalogue

elf = Path(sys.argv[2])
stable_origin = (
    b"https://raw.githubusercontent.com/Notedop/kodi-ps5-binary-addons/"
    b"main/repo/piers-ps5-1/stable/")
with elf.open("rb") as stream:
    with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image:
        if image.find(b"repository.ps5\0") < 0 or image.find(stable_origin + b"\0") < 0:
            sys.exit("PS5 title lacks the official stable repository registration; regenerate "
                     "CompileInfo.cpp and rebuild Kodi before packaging a runtime receipt")
digest = hashlib.sha256()
with elf.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
receipt = dict(schema=1, runtime=load_catalogue()["runtime"],
               title_sha256=digest.hexdigest())
elf.with_name("ps5-addon-runtime.json").write_text(json.dumps(receipt, indent=2) + "\n")
PY

echo "    imports per system module (first provider in NEEDED order, as the converter binds):"
python3 - "$APP/build/llvm-pie.elf" "$APP/.deps/native/ps5-payload-sdk/target/lib" "$PS5_PAYLOAD_SDK/bin/llvm-nm" "$APP_TEMPLATE/build/llvm-pie.elf" <<'PY'
import subprocess, sys, os, re
elf, stubs, nm, demo = sys.argv[1:5]
def needed(path):
    if not os.path.exists(path): return []
    out = subprocess.run(["readelf", "-dW", path], capture_output=True, text=True).stdout
    return re.findall(r"\(NEEDED\).*\[(.*)\]", out)
def syms(args):
    out = subprocess.run([nm] + args, capture_output=True, text=True).stdout
    return {l.split()[-1].split("@")[0] for l in out.splitlines() if l.strip()}
imports = syms(["-D", "-u", elf])
left = set(imports)
demo_needed = set(needed(demo))
for mod in needed(elf):
    stub = os.path.join(stubs, re.sub(r"\.s?prx$", ".so", mod))
    got = left & syms(["-D", "--defined-only", stub]) if os.path.exists(stub) else set()
    left -= got
    note = "" if mod in demo_needed or mod == "libc.prx" else "   <- not needed by the working GL demo"
    print(f"      {mod:28s} {len(got):4d}{note}")
    if note and got:
        print("        " + " ".join(sorted(got)))
if left:
    print("      unresolved: " + " ".join(sorted(left)))
PY

echo "==> 6. Kodi data into the title folder"
mkdir -p "$DIST/share"
cp -a "$STAGE/app0/share/kodi" "$DIST/share/"
if [ "$PS5_ADAPTIVE_TEST" = "1" ]; then
  echo "==> For adaptive diagnostics, create /data/homebrew/kodi-psmc/kodi-addon-debug on the console."
fi
if [ "$PS5_TEST_BINARY_ADDON" = "1" ]; then
  (cd "$DIST/share/kodi/addons/test.binary.ps5" &&
    sha256sum --check --status "$TEST_ADDON/artifacts.sha256")
  {
    printf 'build-id: %s\n' "$(<"$TEST_ADDON/build-id.txt")"
    cat "$TEST_ADDON/artifacts.sha256"
  } > "$DIST/test-binary-addon-build.txt"
fi
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

if [ -n "${PS5_HOST:-}" ]; then
   PS5_DEPLOY_JOBS="${PS5_DEPLOY_JOBS:-8}"
   [[ "$PS5_DEPLOY_JOBS" =~ ^[1-8]$ ]] || {
     echo "!! PS5_DEPLOY_JOBS must be between 1 and 8" >&2; exit 1; }
   command -v lftp >/dev/null || {
     echo "!! lftp is required for deployment; install lftp or copy the folder manually" >&2; exit 1; }
   echo "==> uploading to ftp://$PS5_HOST:${PS5_FTP_PORT:-2121}/data/homebrew/$TITLE_ID"
   # mirror skips files whose size matches and whose remote time is newer. The
   # console clock can run ahead, and a relayout can keep eboot.bin's size, so
   # always re-send the eboot explicitly.
   lftp -e "set cmd:fail-exit yes; mirror -R --delete --no-perms --parallel=$PS5_DEPLOY_JOBS --verbose '$DIST' '/data/homebrew/$TITLE_ID'; put -O '/data/homebrew/$TITLE_ID' '$DIST/eboot.bin'; quit" -p "${PS5_FTP_PORT:-2121}" "$PS5_HOST"
fi
echo
echo "Title folder: $DIST"
echo "Copy it to /data/homebrew/$TITLE_ID, restart ShadowMountPlus, launch Kodi."
echo "Log: KODI_LOG_TO_CONSOLE is not set in a title, so read /download0/.kodi/temp/kodi.log over FTP;"
echo "     klog (nc \$PS5_HOST 3232) shows loader messages and Kodi's stdout."
