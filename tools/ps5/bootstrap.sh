#!/usr/bin/env bash
# Prepare a bare Linux (or WSL2 Ubuntu 24.04) host for building Kodi for PS5.
#
# This installs the external prerequisites that the Kodi build itself does not
# produce - everything in docs/README.PS5.md section 0.3:
#
#   1. host packages           the toolchain and build tooling
#   2. ps5-payload-sdk         -> /opt/ps5-payload-sdk  (via pacbrew/pacman)
#   3. pacbrew-repo            ~100 ported libraries built into the SDK sysroot
#   4. Sony link stubs         libSceVideodec2 + VRR-extended libSceVideoOut
#   5. ps5-opengl              OpenGL 4.6 / EGL SDK -> /opt/ps5-opengl-gl46
#   6. native-app boilerplate  + imgui demo, the app template package.sh uses
#
# Everything is idempotent; re-run after a failure. Budget 1-3 hours of build
# time for pacbrew and ps5-opengl (Mesa) on an 8-core machine.
#
# After this, continue with docs/README.PS5.md section 1 (tools/depends).
#
#   WORK   checkout/build dir for the external sources (ps5-opengl, pacbrew,
#          the app boilerplate)                      default build/ps5-external
#   JOBS   parallelism                                 default $(nproc)
#   STEP   run a single step only: packages | sdk | stubs | opengl | template
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"        # tools/ps5
ROOT="$(cd "$HERE/../.." && pwd)"
export PS5_PAYLOAD_SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
export PS5_OPENGL_PREFIX="${PS5_OPENGL_PREFIX:-/opt/ps5-opengl-gl46}"
WORK="${WORK:-$ROOT/build/ps5-external}"
JOBS="${JOBS:-$(nproc)}"
STEP="${STEP:-all}"
export MAKEFLAGS="${MAKEFLAGS:--j$JOBS}"
mkdir -p "$WORK"

want() { [ "$STEP" = all ] || [ "$STEP" = "$1" ]; }

# Keep sudo's credential cache alive for the whole run: the pacbrew and Mesa
# builds run far longer than sudo's 15-minute timeout, and both end in
# `sudo pacman -U` / `sudo cp`. Same tty as the build, so the refresh counts.
sudo -v
( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 60; done ) &
SUDO_KEEPALIVE=$!
trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null' EXIT

if want packages; then
  echo "==> host packages"
  sudo apt-get update
  # SDK + pacbrew prerequisites (from their READMEs), what ps5-opengl's
  # docs/building.md asks for, and what Kodi's own configure step and native
  # tools need on the host (see docs/README.PS5.md section 0.2).
  sudo apt-get install -y \
    bash clang clang-18 lld lld-18 llvm llvm-18 wget curl git unzip socat rsync zip \
    cmake ninja-build meson pkg-config pkgconf python3 python3-dev python3-pip \
    python3-pyelftools python3-mako python3-glad python3-yaml python3-ply \
    python3-setuptools python3-packaging python3-venv \
    build-essential autoconf automake libtool libtool-bin yasm nasm bison flex gperf gawk \
    libarchive-tools autopoint po4a doxygen makepkg pacman-package-manager \
    gettext tcl flatbuffers-compiler default-jre-headless swig \
    glslang-tools spirv-tools \
    liblzo2-dev libpng-dev libgif-dev libjpeg-dev zlib1g-dev libcurl4-openssl-dev

  # Mesa 26 (ps5-opengl) needs a newer Meson than the 1.3 Ubuntu 24.04 ships.
  if ! dpkg --compare-versions "$(meson --version 2>/dev/null || echo 0)" ge 1.10; then
    pip3 install --user --break-system-packages 'meson>=1.10'
  fi
fi
export PATH="$HOME/.local/bin:$PATH"

if want sdk; then
  echo "==> ps5-payload-sdk + pacbrew-repo (ported libraries)"
  # pacbrew installs the SDK itself as its first package (ps5-payload-sdk, via
  # pacman) and libc++ as its third (libcxx). Do NOT unpack the GitHub release
  # ZIP into /opt first: pacman refuses to overwrite files it does not own
  # ("conflicting files"). If a manual copy is there, remove it.
  if [ -d "$PS5_PAYLOAD_SDK" ] && ! pacman -Qq ps5-payload-sdk >/dev/null 2>&1; then
    echo "    removing manually unpacked SDK at $PS5_PAYLOAD_SDK (pacbrew reinstalls it as a package)"
    sudo rm -rf "$PS5_PAYLOAD_SDK"
  fi
  if [ ! -d "$WORK/pacbrew-repo" ]; then
    git clone --depth 1 https://github.com/ps5-payload-dev/pacbrew-repo.git "$WORK/pacbrew-repo"
  fi
  if [ ! -f "$PS5_PAYLOAD_SDK/target/user/homebrew/lib/pkgconfig/fontconfig.pc" ]; then
    # Builds and installs every package (pacman -U) into the sysroot, SDK
    # first, in the order of upstream's ci-libs.sh - but through our own loop,
    # because upstream's list has glu/glew before the mesa they depend on, and
    # because Kodi does not use pacbrew's GL stack at all (ps5-opengl builds
    # its own compiler and Mesa). Skipping llvm+mesa alone saves 1-2 hours.
    # Set PACBREW_SKIP="" to build everything (then build glu/glew after mesa).
    SKIP="${PACBREW_SKIP-glu glew llvm mesa love}" WORK="$WORK" \
      bash "$HERE/bootstrap/pacbrew.sh" sdk
  else
    echo "    sysroot already populated (fontconfig.pc present)"
  fi
fi

if want stubs; then
  echo "==> Sony link stubs"
  WORK="$WORK" bash "$HERE/bootstrap/sce-stubs.sh"
fi

if want opengl; then
  echo "==> ps5-opengl -> $PS5_OPENGL_PREFIX"
  WORK="$WORK" JOBS="$JOBS" bash "$HERE/bootstrap/ps5-opengl.sh"
fi

if want template; then
  echo "==> native-app boilerplate (app template for tools/ps5/package.sh)"
  # The boilerplate supplies the ELF -> folder-title tooling that the imgui
  # demo uses, and that tools/ps5/package.sh reuses as its app template.
  # ps5-opengl's pinned boilerplate commit is no longer in upstream history;
  # the next best is 81d4235 (the RELRO fix, same build layout as before).
  # Newer main restructured the build (Ninja), which this tooling has not been
  # validated against.
  if [ ! -d "$WORK/ps5-native-app-boilerplate" ]; then
    git clone https://github.com/blackbearreloaded/ps5-native-app-boilerplate.git "$WORK/ps5-native-app-boilerplate"
    ( cd "$WORK/ps5-native-app-boilerplate" \
        && { git checkout -q 4e1d1277dd0531a9a9df8c780e446b9cc26534dd 2>/dev/null \
             || git checkout -q 81d4235346afc7490e98866af4020e94f255b204 2>/dev/null \
             || echo "    known boilerplate commits not found upstream; staying on main"; } )
  fi
  # Older boilerplate revisions wrote the RELRO LOAD segment at the wrong file
  # offset and the console refused the title (CE-107750-0). Upstream fixed it
  # in 81d4235 (relro_origin); for revisions before that, apply the fix here.
  WRITER="$WORK/ps5-native-app-boilerplate/tooling/native/sce_module_writer.cpp"
  if grep -q "relro_origin\|RELRO mapping must begin" "$WRITER"; then
    echo "    boilerplate RELRO fix present"
  else
    ( cd "$WORK/ps5-native-app-boilerplate" \
        && patch -p1 < "$HERE/patches/ps5-native-app-boilerplate-relro.patch" )
  fi
  if [ ! -d "$WORK/ps5-native-app-boilerplate/.deps/native" ]; then
    # Downloads the SDK release zip + zlib into the boilerplate's own .deps
    # (used only for packaging apps; the GL libraries use our sysroot).
    ( cd "$WORK/ps5-native-app-boilerplate" && bash tools/setup-native-dependencies.sh )
  fi
  # package.sh copies its app skeleton (.deps, runtime_shims.c, app_heap.c,
  # linker script, sce_sys) out of this demo's prepared build directory, and
  # checks for exactly these two entries.
  TEMPLATE="$WORK/ps5-opengl/build/native-app/PPSA99005"
  if [ ! -f "$TEMPLATE/Makefile" ] || [ ! -d "$TEMPLATE/.deps/native" ]; then
    echo "    building ps5-opengl's imgui demo (the app template)"
    make -C "$WORK/ps5-opengl" imgui-demo
  else
    echo "    app template present"
  fi
fi

cat <<MSG

Done. External sources and their builds live in
  $WORK
so a "git clean -fdx -- build" discards them along with everything else; set
WORK= to keep them elsewhere.

Add to ~/.bashrc:
  export PS5_PAYLOAD_SDK=$PS5_PAYLOAD_SDK
  export PS5_OPENGL_PREFIX=$PS5_OPENGL_PREFIX
  export PS5_HOST=<your console IP>

Sanity check before building Kodi - confirm GL works on YOUR firmware: upload
  $WORK/ps5-opengl/build/native-app/PPSA99005/dist/PPSA99005/
to /data/homebrew/PPSA99005 (FTP, port 2121) and launch it from the home
screen.

Next: docs/README.PS5.md section 1 (tools/depends), then tools/ps5/configure.sh.
MSG
