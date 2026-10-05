# PS5 build entrypoint (migration worktree)

This branch keeps PS5 integration in tracked Kodi source. Configure does **not**
copy overlays or apply patch files. This document covers everything needed to
go from a bare Linux (or WSL) machine to a running PS5 package.

## 0. Prerequisites

### 0.1. Build location (WSL users especially)

Keep the checkout and build directories on a native Linux filesystem (e.g.
`/home/you/...`). Building under a Windows-mounted path (`/mnt/c/...`) from
WSL causes DrvFs/9p clock-skew ("Clock skew detected" / "modification time in
the future" warnings from `make`), which can silently corrupt incremental
builds while still appearing to succeed. Editing the checkout from Windows
(e.g. an IDE pointed at the `\\wsl.localhost\...` UNC path) is fine - only the
actual build/compile must run against a native Linux path.

### 0.2. One-command bootstrap

Everything in 0.3 and 0.4 below - host packages, both external SDKs and the
app template - is installed by a single idempotent script:

```bash
tools/ps5/bootstrap.sh
```

Budget 1-3 hours on first run (pacbrew and Mesa are built from source); it
asks for `sudo` once and keeps the credential alive for the whole run.
Re-running it after a failure resumes rather than starting over, and a single
step can be repeated on its own:

```bash
STEP=opengl tools/ps5/bootstrap.sh     # packages | sdk | stubs | opengl | template
```

The external sources it checks out and builds (`ps5-opengl`, `pacbrew-repo`,
the app boilerplate) go to `build/ps5-external/`, so they are covered by
`.gitignore` and by `git clean -fdx -- build`. Set `WORK=` to put them
somewhere else - e.g. to keep them across a full build wipe, since
re-fetching and rebuilding Mesa costs 1-2 hours:

```bash
WORK=~/ps5-external tools/ps5/bootstrap.sh
```

`tools/ps5/package.sh` reads the same `WORK` variable to find its app
template, so pass it there too if you override it.

Read 0.3 and 0.4 if you want to know what it installs or need to do it by
hand; otherwise skip straight to section 1.

### 0.3. Host OS packages

The PS5 build cross-compiles almost everything itself (via `tools/depends`
and the external PS5 SDK sysroot below), so it needs far fewer `-dev` library
packages than a desktop Linux Kodi build - just the toolchain and build
tooling used to build those dependencies and a couple of CMake/SWIG
host-tool prerequisites:

```bash
sudo apt-get install -y \
  git build-essential autoconf automake autopoint libtool libtool-bin \
  pkg-config cmake ninja-build meson nasm gperf bison flex gawk \
  python3 python3-dev python3-pip default-jre swig \
  libcurl4-openssl-dev zip unzip rsync
```

`libcurl4-openssl-dev` in particular is easy to miss: `tools/depends/native`'s
own CMake-based recipes (e.g. `cmake` itself) probe for a host libcurl at
configure time and fail with `CMAKE_USE_SYSTEM_CURL is ON but a curl is not
found!` if only the runtime package (`libcurl4t64`) is installed.

### 0.4. External PS5 SDK prerequisites

These are not part of the Kodi source tree. `tools/ps5/bootstrap.sh` installs
and builds both (steps `sdk`, `stubs`, `opengl`, `template`); this section
describes what they are and why, for anyone installing them by hand or
debugging the bootstrap.

- `/opt/ps5-payload-sdk` - the PS5 homebrew cross-toolchain (`prospero-clang`,
  `prospero-pkg-config`, etc.) plus a prebuilt "homebrew" sysroot
  (`$PS5_PAYLOAD_SDK/target/user/homebrew`). FFmpeg, dav1d and CPython 3.14
  are now built from `tools/depends/target/{ffmpeg,dav1d,python3}` and linked
  from there, not from this sysroot (see
  `docs/ps5/phase4-dependency-qualification.md`). The sysroot is still used
  for a handful of libraries this repo intentionally doesn't build itself -
  fontconfig, freetype, harfbuzz, libass, fribidi, libpng, libfmt and the
  PS5-specific `libSceImeDialog`/`libSceUserService` system libs. Because
  fontconfig's `.pc` file declares a private dependency on `expat`,
  `tools/ps5/configure.sh` patches copies of the SDK's `.pc` files (absolute
  paths, no sysroot prefixing) and routes `pkg-config` lookups so that
  fontconfig's expat requirement resolves to the same
  `tools/depends/target/expat` build Python uses, instead of the SDK's own
  (older, symbol-colliding) expat - avoiding duplicate-symbol link errors
  from having two different expat static libraries in one final link.
- `/opt/ps5-opengl-gl46` - the PS5 OpenGL/EGL shim prefix, plus an app
  template under `build/ps5-external/ps5-opengl` used by
  `tools/ps5/package.sh`.
  Built from the revision pinned in
  `tools/ps5/patches/ps5-opengl/PS5-OPENGL-COMMIT` with
  `tools/ps5/patches/ps5-opengl/kodi-additions.py` applied, which adds the
  driver entry points Kodi needs (video out handle, HDR scanout format,
  zero-copy EGL images). A stock upstream build does **not** export these, so
  a release archive cannot be substituted as-is.

  The SDK also receives two link stubs it does not ship
  (`tools/ps5/bootstrap/sce-stubs.sh`): `libSceVideodec2` for the hardware
  video decoder, and `sceVideoOutVrrUnpegFromFixedRate` added to the existing
  `libSceVideoOut` stub (the original is kept as `libSceVideoOut.so.sdk`).

  All of these local modifications are intended to be upstreamed; see
  `docs/ps5/upstream-migration-tasks.md` for the plan and what it would let us
  delete.

## 1. Build the dependency toolchain (`tools/depends`)

Kodi's native dependency build system builds both the PS5-target static
libraries Kodi links against (libuuid, libsmb2, curl, brotli, zlib, tinyxml,
etc.) and a small set of **host-native** tools used during the build itself
(TexturePacker, SWIG, ...). Do this once (re-run only after a `git clean -fdx
-- tools/depends` or a dependency/recipe change):

```bash
cd /home/raoul/kodi-fork/xbmc-ps5-fork/tools/depends
./bootstrap
./configure --host=x86_64-unknown-freebsd --with-platform=ps5 \
  --with-toolchain=/opt/ps5-payload-sdk \
  --prefix=/home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-depends-test \
  --with-rendersystem=gl

# Host-native build tools (TexturePacker, SWIG, autoconf, cmake, meson, ...)
make -C native

# PS5-target dependency recipes, in this order (curl needs brotli already
# installed - its CMake configure hard-fails otherwise)
for r in bzip2 expat gettext libffi libxml2 openssl sqlite3 xz zlib \
         dav1d ffmpeg python3 brotli curl libiconv tinyxml tinyxml2 \
         fstrcmp libuuid libprocstat libkodishim libsmb2; do
  make -C "target/$r" || break
done
```

This step is idempotent: each recipe's `.installed-*` marker file lets `make`
skip already-built recipes. If you suspect a stale/corrupted build (e.g.
after bumping a dependency version), `git clean -fdx -- tools/depends` first
to force every recipe to reconfigure from scratch - Make's dependency check
only watches a few declared files (`Makefile`, `*-VERSION`, ...), not the
extracted source tree, so it can silently keep reusing a build that would no
longer succeed if actually rebuilt.

## 2. Configure

```bash
cd /home/raoul/kodi-fork/xbmc-ps5-fork
bash tools/ps5/configure.sh
```

Optional overrides:

- `BUILD=/abs/path/to/build-dir`
- `BUILD_TYPE=Debug|Release`
- `NATIVE=/abs/path/to/native-tools-prefix` (default:
  `build/ps5-depends-test/x86_64-linux-gnu-native`, i.e. step 1's
  `make -C native` output - TexturePacker and SWIG are picked up from here;
  JsonSchemaBuilder self-builds into `$NATIVE/bin` on first configure if not
  already present)
- `PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk`
- `PS5_OPENGL_PREFIX=/opt/ps5-opengl-gl46`
- `PS5_DEPENDS_PREFIX=/abs/path/to/tools/depends/target/prefix` (preferred
  dependency pkg-config root when present; default:
  `build/ps5-depends-test/x86_64-unknown-freebsd-debug`, i.e. step 1's
  `make -C target/...` output)

## 3. Build

```bash
cmake --build /home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-release -j$(nproc)
```

## 4. Package targets

`ps5-title` creates the deployable title folder, and `ps5-package` additionally
creates a zip artifact.

```bash
# Build title folder under build/ps5-stage/app/dist/PPSA99420
cmake --build /home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-release --target ps5-title

# Build zip package under build/ps5-stage/artifacts/PPSA99420.zip
cmake --build /home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-release --target ps5-package
```

Both targets call `tools/ps5/package.sh`, which uses:

- `BUILD` (default: `/home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-release`)
- `STAGE` (default: `/home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-stage`)
- `APP_TEMPLATE` (default:
  `build/ps5-external/ps5-opengl/build/native-app/PPSA99005`, i.e.
  `$WORK/ps5-opengl/...` - see 0.2)
