# Building Kodi for PS5

This guide covers repository setup, dependencies, configuration, building and
packaging on Linux or WSL. Run commands from the Kodi source root unless noted.
The conventional `tools/buildsteps/ps5` entry points cover Setup -> Configure ->
Compile -> Package -> Deploy; each step can also be rerun independently.
PS5 integration is tracked in the source tree; configuration does not copy
overlays or apply Kodi patch files.

## 1. Prerequisites

### Build location

Keep the checkout, dependencies and build output on a native Linux filesystem,
such as `/home/you/kodi-fork/xbmc-ps5-fork`. Building under `/mnt/c` in WSL can
cause clock-skew warnings and unreliable incremental builds. Editing through a
Windows IDE using the `\\wsl.localhost\...` path is fine; run builds in Linux.

### Bootstrap

```bash
export BUILDTHREADS=4
tools/buildsteps/ps5/prepare-depends
```

The script installs host packages, prepares the payload SDK and required system
import stubs, invokes the `tools/depends/target/ps5-opengl` recipe before
preparing the native-app template,
and builds the bundled one-shot elevation helper, then bootstraps `tools/depends`.
Budget 1-3 hours for the first run; it requests `sudo`. Re-running resumes an
incomplete setup. To repeat one step:

```bash
STEP=opengl tools/buildsteps/ps5/prepare-depends  # packages | sdk | stubs | opengl | template | lapy
```

The equivalent argument form is `bash tools/ps5/bootstrap.sh STEP=opengl`.
That compatibility step delegates to the same Make recipe used by
`make-depends`; bootstrap does not maintain a second graphics build.
SDK setup (`STEP=sdk`) builds the pinned SDK using `tools/ps5/sdk/PKGBUILD` and
installs its package with pacman. The recipe applies Kodi's compiler-wrapper
response-file fixes and builds the sources in `shims/sce_stubs`, including the
additional system libraries and VideoOut VRR export. Neither a local `~/sdk`
checkout nor firmware dumps are required.

The installed `.kodi-sdk.json` receipt binds the recipe, patches, stub sources and
installed SDK files. Matching installations are verified and reused; recipe
updates rebuild only the SDK package and update a qualified pacman-owned
installation without deleting homebrew libraries. Changed installed files or an
installation without a receipt stop setup rather than being overwritten. For an
older or manually customized SDK, choose a new empty absolute
`PS5_PAYLOAD_SDK` prefix; keep the old installation until its replacement works.
`STEP=stubs` now verifies the packaged stubs rather than modifying the SDK.

Pacbrew separately supplies the homebrew libraries and cannot replace the pinned
SDK. Setup applies checked-in source-location fixes to its recipes without
changing their archive checksums, and uses a UTF-8 locale for source extraction.
Pacbrew and native-app template revisions come from
`tools/ps5/dependency-manifest.json`; there is no fallback to a moving branch.
Setup preserves differently pinned cached checkouts and stops rather than
overwriting them. Use a separate `WORK` directory when changing these pins.
Host tools (`STEP=packages`) use the host package manager. Setup can require
sudo; empty, relative and unsafe system-directory SDK prefixes are rejected.
Buildsteps include `$HOME/.local/bin` on `PATH` so Python-installed host tools
prepared by Setup remain available in subsequent steps.
Subsequent dependency, Kodi and package steps verify the SDK but never install it.
To build only the elevation helper with an existing payload SDK, use
`bash tools/ps5/bootstrap.sh STEP=lapy`; this does not rebuild OpenGL.

External sources default to `build/ps5-external`. To keep these expensive builds
outside a disposable build directory:

```bash
export WORK=~/ps5-external
tools/buildsteps/ps5/prepare-depends
```

Keep the same exported choices for subsequent buildsteps. Only `prepare-depends`
installs host packages or runs host provisioning; configure, compile, package and
deploy never silently run it. The lower-level `tools/ps5/bootstrap.sh` remains
available when only external setup is wanted.

### Manual prerequisites and SDK paths

If not using bootstrap, install the host tools:

```bash
sudo apt-get install -y \
  git build-essential autoconf automake autopoint libtool libtool-bin \
  pkg-config cmake ninja-build meson nasm gperf bison flex gawk \
  python3 python3-dev python3-pip default-jre swig \
  libcurl4-openssl-dev zip unzip rsync xxd
```

`libcurl4-openssl-dev` is required by the host-native CMake build; installing
only the runtime libcurl package is insufficient.

The default SDK locations are:

| Input | Default location |
|---|---|
| Payload SDK and homebrew sysroot | `/opt/ps5-payload-sdk` |
| Patched OpenGL/EGL SDK | `/opt/ps5-opengl-gl46` |
| External sources | `build/ps5-external` |
| Graphics sources (`PS5_OPENGL_SOURCE`) | `$WORK/ps5-opengl` |
| Native-app template (`APP_TEMPLATE`) | `$PS5_OPENGL_SOURCE/build/native-app/PPSA99005` |

Use the revisions and patches selected by bootstrap, including the system import
stubs. A stock upstream OpenGL SDK is not a drop-in replacement. Bootstrap will
not overwrite a differently pinned source checkout; use a fresh `WORK` directory
for an SDK upgrade.

For a custom graphics installation, export `PS5_OPENGL_PREFIX` before setup.
Select its matching pinned source with `PS5_OPENGL_SOURCE`. If using a fresh
source checkout with an existing built template, set `APP_TEMPLATE` explicitly;
`PS5_NATIVE_TOOL` can separately select its host `ps5-native-tool`. Keep SDK,
graphics and template choices consistent through configuration and packaging.

## 2. Build dependencies

Build the host-native tools and PS5-target dependencies once. Repeat the
affected recipes after dependency or recipe changes.

```bash
tools/buildsteps/ps5/configure-depends
tools/buildsteps/ps5/make-native-depends
tools/buildsteps/ps5/make-depends
```

The PS5 Make graph owns the pinned OpenGL SDK and the port's patched libraries
and shims, orders their
prerequisites (including brotli and nghttp2 before curl), and reuses the remaining
pacbrew libraries. It does not build the generic all-platform dependency set. Native
tools are built separately before target dependencies.
The `ps5-opengl` recipe reuses the specialized graphics helper and existing
source/build/install caches. Every invocation validates the installed graphics
against the pin, patches, display profile and built files; a qualified unchanged
installation is reused without rebuilding. Bootstrap delegates to this recipe
early because preparing its app template requires graphics first.

Resolve any failed recipe before continuing; failures stop immediately, without
blind retries or blanket cleanup. Resume with the failed step, or select a
target recipe, for example `tools/buildsteps/ps5/make-depends curl`.
Use `tools/buildsteps/ps5/make-depends ps5-opengl` to validate or update graphics
alone. The recipe's `clean`/`distclean` preserve external graphics caches.
Installed-marker files skip
completed recipes; changing extracted dependency sources alone may not
invalidate those markers.

## 3. Prepare the native runtime and configure

```bash
tools/buildsteps/ps5/prepare-xbmc
tools/buildsteps/ps5/configure-xbmc
```

Preparation uses the installed SDKs, graphics sources and cached dependency
archives. Export `PS5_HOMEBREW_SOURCES=/path/to/pacbrew-repo` for a pacbrew source
cache outside `$WORK/pacbrew-repo`, or pass `--homebrew-sources` to `prepare-xbmc`.
Use `--graphics-imports` when the graphics prefix lacks its system import
libraries. Check the selected inputs without compiling the native runtime first:

```bash
tools/buildsteps/ps5/prepare-xbmc --check-inputs
```

Validated outputs become `build/ps5-native/sdk` and `build/ps5-native/depends`;
preparation does not overwrite the original SDK installations. Firmware dumps
and analysis files are not build inputs and must not be packaged or committed.

Shared buildstep overrides:

| Variable | Default |
|---|---|
| `WORKSPACE` | Kodi source root |
| `Configuration` | `Release` (`Debug` for development) |
| `BUILDTHREADS` | `1`; exported to the existing helpers as `JOBS` |
| `BUILD` | `build/ps5-release` |
| `BUILD_TYPE` | `$Configuration` |
| `XBMC_DEPENDS_ROOT` | `tools/depends/xbmc-depends` |
| `TARBALLS` | `/opt/xbmc-tarballs`; choose a writable download cache |
| `NATIVE` | `$XBMC_DEPENDS_ROOT/x86_64-linux-gnu-native` |
| `PS5_PAYLOAD_SDK` | `/opt/ps5-payload-sdk` |
| `PS5_OPENGL_PREFIX` | `/opt/ps5-opengl-gl46` |
| `PS5_NATIVE_RUNTIME_SDK` | `build/ps5-native/sdk` |
| `PS5_NATIVE_DEPENDS_MAP` | `build/ps5-native/depends/archive-map.json` |
| `PS5_DEPENDS_PREFIX` | `$XBMC_DEPENDS_ROOT/x86_64-unknown-freebsd-debug` |
| `PS5_NATIVE_OUTPUT` | `build/ps5-native` |
| `PS5_HOMEBREW_SOURCES` | `$WORK/pacbrew-repo` (existing pacbrew source cache) |

Use absolute paths for custom prefixes. The prepared SDK and dependency map used
for packaging must match the configured Kodi build.
Target dependencies retain tools/depends' default debug suffix independently of
Kodi's `Configuration`. If passing `--disable-debug` to `configure-depends`,
also export the resulting release `PS5_DEPENDS_PREFIX` for subsequent steps.
`prepare-xbmc` accepts additional native preparation arguments.
`configure-xbmc` accepts CMake arguments; Ninja remains the default generator.
Configuration records SDK, dependency-map, graphics, external-source and
template paths for the package/deploy targets, so another shell need not
reconstruct those choices.

## 4. Build

```bash
tools/buildsteps/ps5/make-xbmc
tools/buildsteps/ps5/make-binary-addons
```

The add-on step builds the required bundled `peripheral.joystick` and
`inputstream.adaptive` providers through the existing catalogue CLI, preserving
its incremental/resume checks. It does not package or publish repository feeds.
Build-local definitions use the locked catalogue sources; no separate global
binary-add-on repository bootstrap is needed.
Use that CLI for other catalogue selections. No step cleans dependency or add-on
caches by default.

## 5. Package

Build required binary add-ons with `tools/buildsteps/ps5/make-binary-addons`
before packaging (section 4). In particular,
`peripheral.joystick` is a required bundled provider; packaging rejects a title
without it. The packaging targets discover installed catalogue outputs but do
not compile add-ons or publish feeds.

Python's standard library is staged from the configured `tools/depends` prefix,
using the Python version selected by CMake, not the SDK's homebrew directory.
The PS5 Python recipe disables both pymalloc and mimalloc to use the title's
allocator and supported module initialization.

Title metadata is owned by `title/sce_sys/param.json`. Increment `contentVersion`
there before packaging, for example from `22.000.000` to `22.000.001`.
`TITLE_ID`, `KODI_CATEGORY` and `KODI_HDR_TITLE` environment overrides are rejected.

Packaging never uploads, even if ambient `PS5_HOST` is set. Deployment is a
separate explicit action.

```bash
# Create a fresh deployable title stage.
cmake --build build/ps5-release --target ps5-title

# Create a fresh title stage and its ZIP artifact.
tools/buildsteps/ps5/package
```

Both targets call `tools/ps5/package.sh` and print their output paths. They share
one reusable `BUILD/title` directory, cleaned before each packaging run so removed
files and optional diagnostics cannot leak into the next title. Set `STAGE` when
calling the script directly, or configure `PS5_PACKAGE_STAGE` for the CMake targets,
to override this location. Concurrent packaging into the same stage is rejected.
Only directories marked by the packaging script are replaced; existing unmarked
directories and symlinks are rejected. Failed-build logs remain until the next run;
copy them elsewhere first if needed. Historical timestamped stages are not removed
automatically. SDK and add-on caches remain separate from this disposable output.
Use the resulting `<stage>/app` for add-on tooling's `--title-app` option.
Packaging rejects unfinished Kodi link work and graph-tool failures rather than
shipping an old binary. Ninja, Ninja Multi-Config (with the selected build
configuration) and Unix Makefiles have freshness checks; other generators are
rejected.

Packaging options:

| Variable | Purpose |
|---|---|
| `BUILD` | Configured Kodi build directory |
| `WORK` | External source root used by bootstrap |
| `APP_TEMPLATE` | Override the built native-app template |
| `STAGE` | Reusable output directory; defaults to `BUILD/title` |
| `PS5_ADDON_PREFIXES` | Colon-separated installed add-on roots; empty disables discovery |

By default, installed add-ons come from
`build/ps5-addon-repository/work/build-record.json`. Explicit missing roots,
duplicate IDs and invalid files fail packaging. The stage records add-on
selection in `native-addons.json`. Local build-proof files are retained in the
stage but excluded from the distributable ZIP.

After installing the title as described below, restart ShadowMountPlus and launch
Kodi manually.

The title ships `title/default-config.json` from the repository as
`default-config.json` at its root. To customize elevation or
debug settings, copy it to `config.json` beside `eboot.bin`
and edit that copy. Startup uses `config.json` when present, otherwise
`default-config.json`; an invalid or unreadable selected file is an error, not
a reason to fall back. If neither file exists, the built-in defaults apply.
Packages update only `default-config.json`, and explicit FTP deployment
preserves `config.json`, so upgrades leave user configuration intact.
Configuration is loaded and validated once before sandbox elevation, even when
the sandbox is already open. Missing settings use built-in defaults; the two
files are not merged.

Debug settings replace the old `kodi-debug`, `kodi-video-debug` and
`kodi-addon-debug` marker files, which are no longer read:

```json
{
  "debug": {
    "enabled": false,
    "video": false,
    "addons": false
  }
}
```

Set `debug.enabled` to `true` for Kodi debug logging and the background network
self-test, `debug.video` for detailed video diagnostics, or `debug.addons` for
binary add-on/inputstream diagnostics. All three are independent booleans and
default to `false`. Restart Kodi after changing them. Startup passes the typed
configuration to elevation and debug setup; debug setup exports the existing
`KODI_PS5_*_DEBUG` environment flags for Kodi and external add-on consumers.
The old `kodi-reset` and `kodi-uninstall` markers are no longer read; startup
does not delete userdata based on marker files.
Run `bash tools/ps5/tests/shell/test-config.sh` for host-side configuration regression checks.

## 6. Deploy an existing completed stage

Start the console FTP server, install `lftp` on the host, then explicitly select
the console:

```bash
tools/buildsteps/ps5/deploy <console-host>
```

This only validates and uploads an existing stage. It never compiles, packages,
launches or closes an application. `STAGE` overrides the configured
`PS5_PACKAGE_STAGE` (otherwise `BUILD/title`); keep the matching prepared SDK
and dependency map available for title verification. Missing or invalid title
verification receipts stop deployment. `PS5_HOST` alone never authorizes upload.
`PS5_FTP_PORT` defaults to `2121`, and `PS5_DEPLOY_JOBS` to `8` (range 1–8).
Uploads preserve the console's `config.json` and always resend the launcher and
complete module tree. Transfer failures are reported as failures.

Alternatively, explicitly configure `-DPS5_DEPLOY_HOST=<console-host>` and invoke
`cmake --build <build-dir> --target ps5-deploy`. That target has no compile or
package dependency. For manual transfer, copy the printed title folder into
`/data/homebrew/<titleId>`.

## 7. Host regression tests

```bash
# Focused buildstep, package, native preparation and discovery tests (no console).
tools/buildsteps/ps5/run-tests

# Select existing host suites explicitly.
tools/buildsteps/ps5/run-tests test-prepare-native-runtime.py test-python-discovery.py
```

The runner delegates to `tools/ps5/test.py`, which selects Python tests by
basename from `tools/ps5/tests/python`, not under `tools/buildsteps`. New graph tests use
tiny mocked fixtures, not SDK/dependency builds. PS5 cross-compiled Kodi tests
remain disabled and are never executed on the host. Runtime C++ test sources
belong beside the production code under Kodi's normal test-library conventions.

## 8. Troubleshooting and rebuild scope

- **Bootstrap or SDK failures:** check the configured prefixes and pinned source
  revision. Repeat only the failed bootstrap step, using the same `WORK`.
- **Missing host tools:** complete `tools/buildsteps/ps5/make-native-depends` and ensure `NATIVE` points to
  its installed prefix.
- **Stale dependencies:** rebuild/install the affected recipe, then repeat native
  runtime preparation, configuration, building and packaging as appropriate.
  Do not assume an installed marker proves changed sources were rebuilt.
- **Rebuild versus repackage:** Kodi C++ changes require a rebuild and packaging.
  Native-app shim-only changes require packaging. Python dependency/header
  changes require rebuilding Python first; dependency changes require rebuilding
  the affected library.
- **Selective resume:** after an ordinary Kodi source change, repeat
  `make-xbmc` and `package`; deploy only when wanted. Dependency/native input
  changes require the affected recipe and `prepare-xbmc` first. Rerun
  `configure-xbmc` when changing configured paths/options. Never clear all
  dependency or graphics output just to resume a failed later step.

Writable Kodi data is under `/data/homebrew/kodi-psmc/.kodi`; the file log is
`/data/homebrew/kodi-psmc/.kodi/temp/kodi.log`.
For startup troubleshooting, enable the relevant `debug` settings in the title's
`config.json` and restart Kodi. Set them back to `false` and restart when finished.
