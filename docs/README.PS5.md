# PS5 build entrypoint (migration worktree)

This branch keeps PS5 integration in tracked Kodi source. Configure does **not**
copy overlays or apply patch files. This document covers everything needed to
go from a bare Linux (or WSL) machine to a running PS5 package.

For binary add-on compilation, title integration, testing/stable promotion and
publication, follow the [PS5 binary add-on workflow](PS5-BINARY-ADDONS.md).

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
  `tools/ps5/patches/ps5-opengl/*.patch` applied, which add the
  driver entry points Kodi needs (video out handle, HDR scanout format,
  zero-copy EGL images). A stock upstream build does **not** export these, so
  a release archive cannot be substituted as-is.

  The current pin is SDK 1.0.2 preparation commit
  `a519561626933257213d64a4a8bd5e02b55dceb2` from
  `https://github.com/Notedop/ps5-opengl`, which preserves the original
  upstream history. It is not known whether this was upstream's final revision.
  Bootstrap refuses to reuse a different source revision rather than replacing
  an existing patched checkout. For an upgrade, use a new `WORK` directory.
  `PS5_OPENGL_PREFIX` may point to a user-writable directory to avoid changing
  an existing system installation. Configure Kodi with both
  `-DPS5_OPENGL_PREFIX=<prefix>` and
  `-DPS5OpenGL_DIR=<prefix>/lib/cmake/PS5OpenGL`, and pass the same
  `PS5_OPENGL_PREFIX` when packaging. If only the graphics SDK is rebuilt,
  keep `APP_TEMPLATE` pointing to the existing native-app template; a fresh
  `WORK` used for the SDK alone does not contain a built template.
  The existing fixed scanout profile remains the default; runtime display-mode
  selection is not enabled by this upgrade.

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

Packaging compiles `shims/native-app/libc_socket.c` directly into the title,
ahead of the SDK's archived copy in `libkodishim.a`. Socket-shim-only changes
therefore require re-packaging, not rebuilding Python. Changes to Python's
`ps5_pysocket.h` redirection still require rebuilding the Python dependency.
Run `bash tools/ps5/test-socket-shim.sh` for the socket regression tests and
PS5 SDK compile check.

Socket diagnostics use `[kodi-ps5] pysock`: creation logs include the socket ID,
family, type and protocol; failed connects log the captured raw sceNet errno,
then the socket type and numeric destination. IPv4 UDP connect to destination
port zero is rejected by the FreeBSD-style stack with `EADDRNOTAVAIL` (49,
`0x31`), not `EIO`. The shim preserves the rejection; a caught probe failure
alone does not establish that the add-on's HTTP server failed to start.

Both targets discover installed binary add-ons using the repository builder's
`build/ps5-addon-repository/work/build-record.json` when present. They copy only
catalogue-bundled outputs, including resources/helpers, but use all candidates
for title export generation. Neither target compiles add-ons or publishes feeds.
The stable repository is shipped and enabled; testing is shipped but disabled
on first discovery. Compatibility exclusions remain authoritative.

Use colon-separated `PS5_ADDON_PREFIXES` to override install roots; an explicitly
empty value disables discovery. Historical depends/audio/screensaver prefixes
are used only when no override or repository build record exists. Missing
explicit roots, duplicate IDs and invalid/unsafe installed files fail.
Selection and exclusions are recorded in
`build/ps5-stage/installed-addons-report.json` (preflight copy in the Kodi build
directory). Follow the [binary add-on workflow](PS5-BINARY-ADDONS.md) for build
order, release scenarios and publication commands. `PS5_ADAPTIVE_TEST=1` is only
needed for direct DASH test/debug content, not ordinary packaging.

Set `PS5_HOST` to upload the packaged title over FTP using `lftp mirror`.
Install `lftp` on the build host. Deployment uses three concurrent transfers
by default and prints mirror activity.

```bash
PS5_HOST=192.168.1.100 PS5_DEPLOY_JOBS=3 \
  cmake --build build/ps5-release --target ps5-title
```

`PS5_DEPLOY_JOBS` accepts `1..8`; use `1` to restore serial transfers if the
console's FTP service struggles with parallel uploads. `PS5_FTP_PORT` defaults
to `2121`. Normal lftp size/timestamp comparison is retained: unchanged files
can be skipped, but regenerated files with newer timestamps may be uploaded
again even when their contents match. Size-only comparison is deliberately
not enabled because it can miss same-size executable or asset changes.

Fully close Kodi before deploying and wait for deployment to finish before
launching it. As with the original mirror deployment, files are updated in
place and files no longer in the local title are removed remotely. No hash
manifest or extra verification scans are used. The mirror also removes the
previous Python uploader's manifest and temporary files if they remain in
the remote title directory.

## 5. Runtime debugging

Startup requires sandbox escape. Writable data lives under
`/data/homebrew/kodi-psmc/.kodi`; the escape check creates the HOME directory
if missing and requires that it can be opened. Startup retains `/app0` before
escape, then finds its mounted path under `/mnt/sandbox/*/app0` by matching
device and inode. Missing or ambiguous matches stop startup; no title ID,
sandbox instance suffix or physical installation directory is assumed.
Packaged resources, Python and system add-ons use `<resolved app0>/share/kodi`.
The video decoder and native keyboard modules are preloaded before requesting
sandbox escape; later initialization reuses the cached results, since loading
modules after escape can fail with `0x80020063`.
Startup supplies `HOME`,
`KODI_HOME` and `KODI_BIN_HOME` from these separate locations; the shared POSIX
settings initialization registers the special paths and creates user directories.
Python's environment, temporary directory and CA bundle are configured separately.

Marker files are read from `/data/homebrew/kodi-psmc` (the writable HOME root):
`kodi-debug`,
`kodi-video-debug` and `kodi-addon-debug` remain in place.
`kodi-uninstall` removes that tree and exits.
Reset/uninstall markers are removed only after successful deletion. Neither
operation deletes the packaged title or its resources.
Markers stay outside `.kodi`, so resetting data does not delete them prematurely.
The application mount does not need to be writable.

Optional empty marker files in the writable HOME root enable
diagnostics at startup. Restart Kodi after adding or removing them.

| Marker | Effect |
|---|---|
| `kodi-debug` | General Kodi debug logging. |
| `kodi-video-debug` | Detailed video decoding, frame-content, presentation timing and HDR diagnostics, independently of `kodi-debug`. |

For example, create this empty file on the console over FTP:

```text
/data/homebrew/kodi-psmc/kodi-video-debug
```

The application mount is resolved at runtime and logged as `app directory`,
but is not used for markers. Diagnostic output goes to
the kernel log alongside Kodi messages. Detailed video diagnostics add work
to the playback path; leave these markers absent for normal playback. Remove
debug markers from the HOME root when finished. Packaging and deployment do not
create or remove these user-controlled markers.

### Network streaming

PS5 curl connections request a 1 MiB socket receive buffer before connecting;
the console may clamp this (512 KiB was observed). The default 64 KiB buffer
limited a high-bitrate HTTPS stream to roughly 45 Mbit/s; enlarging it allowed
roughly 180 Mbit/s and sustained playback. This is separate from Kodi's media
cache size. Buffer-setting failures are logged and leave the connection usable
with its existing buffer. The callback applies to other curl transfers too,
including artwork and add-on downloads, not Python's separate socket layer.

After changing this policy, restart Kodi to avoid reusing existing connections.
Check high-bitrate playback, forward/backward seeks, stop/reopen, concurrent
artwork loading and add-on downloads. Repeatedly opening and closing streams
should not exhaust socket resources.

### Thread stacks and decoder allocation failures

`shims/native-app/thread_stack.c` raises the native default to at least **4 MiB**
only when `pthread_create()` receives a null attributes pointer. This covers
title-linked callers such as `std::thread` without prescribing a size for every
workload. Explicit caller attributes are passed through unchanged, including
smaller stacks, guard sizes, scheduling options and caller-supplied stack memory.
The caller remains responsible for choosing and allocating a sufficient stack.
The built CPython FreeBSD implementation already explicitly requests 4 MiB by
default; an add-on's `threading.stack_size()` override remains respected.

4 MiB is the working application default observed on the console, not a firmware
requirement or a proven maximum stack usage. A global 8 MiB policy increases
per-thread resource demand; the previous automatic fallback could then run
workers on 1 MiB stacks, a size that previously overflowed during software
thumbnail decoding. There is no longer a smaller-stack retry. Attribute errors
and thread-creation failures are returned to the caller without changing
`errno`; a cleanup error after successful creation is logged, not reported as
creation failure for a thread that already exists.

Failure-only kernel-log entries prefixed `[kodi-ps5] DIAGNOSTIC thread-create:`
include the operation, native pthread return code, attempted stack size, explicit
attribute flag, creating thread, entry point and available flexible memory.
`stack_attr_rc` records stack-attribute inspection or setup errors;
`flexible_memory_valid=0` means the memory query failed and its byte count must
not be interpreted as available memory. These temporary diagnostics do not
require a marker or change cache settings. Memory availability alone does not
establish why thread creation failed.

On PS5, software video filter graphs use a six-thread limit rather than an automatically
sized worker pool. This includes software thumbnail and HDR-metadata extraction
and software-playback filters. The scale filter inherits the graph's thread
limit. Hardware decoding, decoder worker counts, concurrent Kodi jobs and stack
sizes are unchanged. Debug logs report
`[kodi-ps5] FFmpeg software filter graph: threads=6` when a graph is created.

Temporary SMB debug diagnostics report the first 32 entries of each share/path
listing with `[kodi-ps5] SMB entry:`. Each line includes the redacted parent URL
(limited to 256 characters), entry name (160 characters), raw libsmb2 type,
mapped directory flag, final Kodi item folder flag and size. Raw types are
`0` (file), `1` (directory) and `2` (link/reparse point). A
`[kodi-ps5] SMB listing:` summary includes total entries, directory and zero-size
non-directory counts, and the number of omitted entry logs. Classification is
based on the server's directory attribute: the local libsmb2 patch preserves
`DIRECTORY` even when `REPARSE_POINT` is also set, consistently for listing, stat
and fstat. Non-directory reparse points remain links/files, not folders. The
patch changes no public libsmb2 layout or ABI. These diagnostics add no directory
timings or traversal fallback.

The Pictures dialog "Loading media information from files..." is a separate,
sequential picture-tag lookup phase, not the video filter graph. Kodi's built-in
metadata parser reads each uncached image into memory before parsing its tags,
so remote image reads can delay the dialog even without video decoding.
PS5 debug logs prefixed `[kodi-ps5] Picture metadata:` report phase begin/end and
each actual tag lookup's `read_begin`/`read_end`, redacted path (256 characters),
listed size, result and elapsed milliseconds. The listed size is not a measured
transfer byte count; the timing includes both I/O and metadata parsing. A
`read_begin` without its matching `read_end` identifies an in-progress lookup.
Cached/skipped items do not produce read logs. These diagnostics do not change
metadata loading, caching or cancellation.

FFmpeg decoder-open failures log the returned error code and description.
If reopening fails, picture retrieval reports a fatal decoder error instead
of accessing the freed context; VideoPlayer aborts that playback through its
existing `PLAYER_ABORT` path. A successful reopen waits for fresh input.
TLS destructor registration also reports failures instead of claiming success
after failed key creation, allocation or thread-specific storage.

Run `bash tools/ps5/test-thread-stack.sh`, `bash tools/ps5/test-tls-dtors.sh`
and `bash tools/ps5/test-pthread-tls.sh` for host regressions and SDK compile
checks. Decoder lifecycle regressions are in `TestDVDVideoCodecFFmpeg`.
The stack and TLS shims are copied into the native application during packaging;
changing only those shims needs re-packaging. Native SMB diagnostics and
decoder/player changes require a Kodi rebuild followed by packaging. Neither
requires rebuilding Python.
The reparse-directory fix additionally requires rebuilding/installing the
`tools/depends/target/libsmb2` dependency before rebuilding Kodi and packaging.
Run `bash tools/ps5/test-smb2-file-type.sh` against the patched dependency source
for directory/reparse mapping regressions and an SDK compile check.
Packaging resolves bare `-l` inputs using Kodi's actual linker search paths
before SDK/OpenGL fallbacks, so the native title uses the rebuilt dependency
rather than an older SDK archive. `vendor/kodi-deps.txt` in the staged native
application records the selected archives. Run
`python3 tools/ps5/test-kodi-link-inputs.py` for archive-selection regressions.
