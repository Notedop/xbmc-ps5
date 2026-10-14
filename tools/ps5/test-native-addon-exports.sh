#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="${BUILD:-$ROOT/build/ps5-native-export-tests}"
APP_TEMPLATE="${APP_TEMPLATE:-$ROOT/build/ps5-external/ps5-opengl/build/native-app/PPSA99005}"
STAGED_APP="${STAGED_APP:-$ROOT/build/ps5-stage/app}"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
mkdir -p "$BUILD/tooling"
cp -a "$APP_TEMPLATE/tooling/native" "$BUILD/tooling/"
patch --batch --forward -d "$BUILD" -p1 < "$ROOT/tools/ps5/patches/native-app-preserve-addon-metadata.patch"
patch --batch --forward -d "$BUILD" -p1 < "$ROOT/tools/ps5/patches/native-app-host-runtime-exports.patch"
native="$BUILD/tooling/native"
zlib_root="$APP_TEMPLATE/.deps/native/zlib/root"
zlib="$(find "$zlib_root" -name libz.a -print -quit)"
"${CXX:-clang++-18}" -std=c++20 -O2 -Wall -Wextra -Werror \
  -I"$zlib_root/usr/include" "$native/native_app_builder.cpp" \
  "$native/self_container.cpp" "$native/elf_object.cpp" "$native/sce_module_writer.cpp" \
  "$zlib" -o "$BUILD/ps5-native-tool"
"$SDK/bin/prospero-clang" --target=x86_64-unknown-freebsd -fPIC -fplt \
  -c "$ROOT/tools/ps5/tests/native-export-fixture.c" -o "$BUILD/fixture.o"
stubs="$STAGED_APP/.deps/native/ps5-payload-sdk/target/lib"
cd "$BUILD"
ld.lld-18 -T tooling/native/ps5-pie.ld -shared --eh-frame-hdr \
  -z max-page-size=0x4000 -e _start -o fixture.elf fixture.o "$stubs/libc_stub_weak.so"
convert()
{
  ./ps5-native-tool link --in "$1.elf" --out "$1.sce.elf" --stub-dir "$stubs" \
    --module-sdk 0x09000000 --companion-sdk 0x09000000 --file-name eboot.elf
  python3 "$ROOT/tools/ps5/tests/check-native-metadata.py" "$1.elf" "$1.sce.elf"
}
convert fixture
if [ -n "${PS5_ADDON_ELFS:-}" ]; then
  IFS=: read -r -a addons <<< "$PS5_ADDON_ELFS"
  python3 "$ROOT/tools/ps5/prepare-addon-exports.py" --sdk "$SDK" --output exports "${addons[@]}"
  "$SDK/bin/prospero-clang" --target=x86_64-unknown-freebsd \
    -c exports/addon-imports.S -o exports/addon-imports.o
  builtins="$(clang-18 --print-resource-dir)/lib/linux/libclang_rt.builtins-x86_64.a"
  "$SDK/bin/prospero-clang" --target=x86_64-unknown-freebsd -O2 -Wall -Wextra -Werror \
    -c "$ROOT/shims/native-app/pthread_tls.c" -o pthread_tls.o
  mapfile -t shim_objects < <(find "$STAGED_APP/build/obj" -maxdepth 1 \
    -name 'src_*.o' ! -name 'src_pthread_tls.c.o' -print | sort)
  # Reuse existing native shims only: no Kodi archives, title main or full build.
  "$SDK/bin/prospero-lld" -T tooling/native/ps5-pie.ld -L"$stubs" \
    --eh-frame-hdr -e _start --undefined-version \
    --version-script exports/addon-symbols.map \
    --export-dynamic-symbol-list=exports/addon-dynamic.list @exports/addon-roots.rsp \
    --wrap=malloc --wrap=calloc --wrap=realloc --wrap=free --wrap=posix_memalign \
    --wrap=malloc_usable_size --wrap=pthread_create --wrap=pthread_setspecific --wrap=pipe --wrap=fcntl \
    --wrap=chdir --wrap=write --wrap=fopen \
    -o runtime.elf fixture.o exports/addon-imports.o \
    "${shim_objects[@]}" \
    pthread_tls.o "$STAGED_APP/build/obj/app_cpp_runtime.o" \
    --start-group "$SDK/target/lib/libunwind.a" "$SDK/target/lib/libc++abi.a" \
    "$SDK/target/lib/libc++.a" "$builtins" --end-group --as-needed "$stubs"/*.so
  python3 "$ROOT/tools/ps5/verify-addon-exports.py" runtime.elf "${addons[@]}"
  convert runtime
fi
echo "PASS: native definition exports and imported runtime slots (no console execution)"
