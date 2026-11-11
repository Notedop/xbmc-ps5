# Building Kodi for PS5

This guide covers repository setup, dependencies, configuration, building and
packaging on Linux or WSL. Run commands from the Kodi source root unless noted.
PS5 integration is tracked in the source tree; configuration does not copy
overlays or apply Kodi patch files.

For binary add-on builds and publication, see
[PS5 binary add-ons](PS5-BINARY-ADDONS.md). For the shared-runtime architecture,
see [PS5 native SPRX](PS5-NATIVE-SPRX.md#shared-runtime-title-profile).

## 1. Prerequisites

### Build location

Keep the checkout, dependencies and build output on a native Linux filesystem,
such as `/home/you/kodi-fork/xbmc-ps5-fork`. Building under `/mnt/c` in WSL can
cause clock-skew warnings and unreliable incremental builds. Editing through a
Windows IDE using the `\\wsl.localhost\...` path is fine; run builds in Linux.

### Bootstrap

```bash
tools/ps5/bootstrap.sh
```

The script installs host packages, prepares the payload SDK and required system
import stubs, builds the patched OpenGL SDK, prepares the native-app template,
and builds the bundled one-shot elevation helper.
Budget 1-3 hours for the first run; it requests `sudo`. Re-running resumes an
incomplete setup. To repeat one step:

```bash
STEP=opengl tools/ps5/bootstrap.sh  # packages | sdk | stubs | opengl | template | lapy
```

The equivalent argument form is `bash tools/ps5/bootstrap.sh STEP=opengl`.
To build only the elevation helper with an existing payload SDK, use
`bash tools/ps5/bootstrap.sh STEP=lapy`; this does not rebuild OpenGL.

External sources default to `build/ps5-external`. To keep these expensive builds
outside a disposable build directory:

```bash
WORK=~/ps5-external tools/ps5/bootstrap.sh
```

Pass the same `WORK` to `tools/ps5/package.sh` when overriding it.

### Manual prerequisites and SDK paths

If not using bootstrap, install the host tools:

```bash
sudo apt-get install -y \
  git build-essential autoconf automake autopoint libtool libtool-bin \
  pkg-config cmake ninja-build meson nasm gperf bison flex gawk \
  python3 python3-dev python3-pip default-jre swig \
  libcurl4-openssl-dev zip unzip rsync
```

`libcurl4-openssl-dev` is required by the host-native CMake build; installing
only the runtime libcurl package is insufficient.

The default SDK locations are:

| Input | Default location |
|---|---|
| Payload SDK and homebrew sysroot | `/opt/ps5-payload-sdk` |
| Patched OpenGL/EGL SDK | `/opt/ps5-opengl-gl46` |
| External sources | `build/ps5-external` |
| Native-app template | `$WORK/ps5-opengl/build/native-app/PPSA99005` |

Use the revisions and patches selected by bootstrap, including the system import
stubs. A stock upstream OpenGL SDK is not a drop-in replacement. Bootstrap will
not overwrite a differently pinned source checkout; use a fresh `WORK` directory
for an SDK upgrade.

For a custom graphics installation, pass `PS5_OPENGL_PREFIX` to bootstrap and
packaging, and configure Kodi with `-DPS5_OPENGL_PREFIX=<prefix>` and
`-DPS5OpenGL_DIR=<prefix>/lib/cmake/PS5OpenGL`. If only rebuilding the graphics
SDK in a fresh `WORK`, keep `APP_TEMPLATE` pointing to an existing built template.

## 2. Build dependencies

Build the host-native tools and PS5-target dependencies once. Repeat the
affected recipes after dependency or recipe changes.

```bash
ROOT="$(pwd)"
cd tools/depends
./bootstrap
./configure --host=x86_64-unknown-freebsd --with-platform=ps5 \
  --with-toolchain=/opt/ps5-payload-sdk \
  --prefix="$ROOT/build/ps5-depends-test" \
  --with-rendersystem=gl

make -C native

# Preserve this order: curl requires brotli to be installed first.
for recipe in bzip2 expat gettext libffi libxml2 openssl sqlite3 xz zlib \
              dav1d ffmpeg python3 brotli curl libiconv tinyxml tinyxml2 \
              fstrcmp libuuid libprocstat libkodishim libsmb2; do
  make -C "target/$recipe" || break
done

cd "$ROOT"
```

Resolve any failed recipe before continuing. Installed-marker files skip
completed recipes; changing extracted dependency sources alone may not
invalidate those markers.

## 3. Prepare the native runtime and configure

```bash
python3 tools/ps5/prepare-native-runtime.py --jobs 4
bash tools/ps5/configure.sh
```

Preparation uses the installed SDKs, graphics sources and cached dependency
archives. If necessary, pass `--homebrew-sources` for a pacbrew source cache
outside `build/ps5-external/pacbrew-repo`, or `--graphics-imports` when the graphics
prefix lacks its system import libraries.

Validated outputs become `build/ps5-native/sdk` and `build/ps5-native/depends`;
preparation does not overwrite the original SDK installations. Firmware dumps
and analysis files are not build inputs and must not be packaged or committed.

Configuration overrides:

| Variable | Default |
|---|---|
| `BUILD` | `build/ps5-release` |
| `BUILD_TYPE` | `Release` (use `Debug` for development) |
| `NATIVE` | `build/ps5-depends-test/x86_64-linux-gnu-native` |
| `PS5_PAYLOAD_SDK` | `/opt/ps5-payload-sdk` |
| `PS5_OPENGL_PREFIX` | `/opt/ps5-opengl-gl46` |
| `PS5_NATIVE_RUNTIME_SDK` | `build/ps5-native/sdk` |
| `PS5_NATIVE_DEPENDS_MAP` | `build/ps5-native/depends/archive-map.json` |
| `PS5_DEPENDS_PREFIX` | `build/ps5-depends-test/x86_64-unknown-freebsd-debug` |

Use absolute paths for custom prefixes. The prepared SDK and dependency map used
for packaging must match the configured Kodi build.

## 4. Build

```bash
cmake --build build/ps5-release -j"$(nproc)"
```

Use the configured `BUILD` directory if overridden.

## 5. Package

Build required binary add-ons through the
[catalogue workflow](PS5-BINARY-ADDONS.md) before packaging. In particular,
`peripheral.joystick` is a required bundled provider; packaging rejects a title
without it. The packaging targets discover installed catalogue outputs but do
not compile add-ons or publish feeds.

Title metadata is owned by `title/sce_sys/param.json`. Increment `contentVersion`
there before packaging, for example from `22.000.000` to `22.000.001`.
`TITLE_ID`, `KODI_CATEGORY` and `KODI_HDR_TITLE` environment overrides are rejected.

**Leave `PS5_HOST` unset for build/package-only work.** The packaging script can
upload over FTP when that variable is set; installation should be an explicit,
separate user action.

```bash
unset PS5_HOST

# Create a fresh deployable title stage.
cmake --build build/ps5-release --target ps5-title

# Create a fresh title stage and its ZIP artifact.
cmake --build build/ps5-release --target ps5-package
```

Both targets call `tools/ps5/package.sh` and print their output paths. Each stage
is a fresh `title-<UTC timestamp>-<pid>` directory under `BUILD`; existing stages
are never overwritten. Set `STAGE` when calling the script directly, or configure
`PS5_PACKAGE_STAGE` for the CMake targets, to choose a fresh output location.
Use the resulting `<stage>/app` for add-on tooling's `--title-app` option.

Packaging options:

| Variable | Purpose |
|---|---|
| `BUILD` | Configured Kodi build directory |
| `WORK` | External source root used by bootstrap |
| `APP_TEMPLATE` | Override the built native-app template |
| `STAGE` | Fresh output directory when invoking `package.sh` directly |
| `PS5_ADDON_PREFIXES` | Colon-separated installed add-on roots; empty disables discovery |

By default, installed add-ons come from
`build/ps5-addon-repository/work/build-record.json`. Explicit missing roots,
duplicate IDs and invalid files fail packaging. The stage records add-on
selection in `native-addons.json`. Local build-proof files are retained in the
stage but excluded from the distributable ZIP.

After manually installing the title, restart ShadowMountPlus and launch Kodi.

## 6. Troubleshooting and rebuild scope

- **Bootstrap or SDK failures:** check the configured prefixes and pinned source
  revision. Repeat only the failed bootstrap step, using the same `WORK`.
- **Missing host tools:** complete `make -C native` and ensure `NATIVE` points to
  its installed prefix.
- **Stale dependencies:** rebuild/install the affected recipe, then repeat native
  runtime preparation, configuration, building and packaging as appropriate.
  Do not assume an installed marker proves changed sources were rebuilt.
- **Rebuild versus repackage:** Kodi C++ changes require a rebuild and packaging.
  Native-app shim-only changes require packaging. Python dependency/header
  changes require rebuilding Python first; dependency changes require rebuilding
  the affected library.

Writable Kodi data is under `/data/homebrew/kodi-psmc/.kodi`; the file log is
`/data/homebrew/kodi-psmc/.kodi/temp/kodi.log`.
For startup troubleshooting, create an empty `kodi-debug` file in
`/data/homebrew/kodi-psmc` and restart Kodi. `kodi-video-debug` enables detailed
video diagnostics independently. Remove diagnostic markers and restart when
finished; packaging does not create or remove them.
