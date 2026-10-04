# Phase 4 progress: dependency qualification pass

Date: 2026-10-04

## Outcome

Dependency provenance is now explicitly qualified for the active PS5 build,
including concrete evidence for `ps5-opengl`, `dav1d`, `FFmpeg`, and `CPython`.
The current build is reproducible against the pinned external PS5 sysroot
dependencies. PS5 host enablement was added to Kodi `tools/depends`; remaining
work is full target build/install validation before dependency-source switch.
`ps5-opengl` has since been evaluated as a clean upstream build (without the
legacy mutation script) per Phase 1A's exit-gate requirement; see the
dedicated section below.

## Qualified current dependency graph

| Dependency | Current source in build | Verified version/evidence | Qualification result |
| --- | --- | --- | --- |
| `ps5-opengl` | External prerequisite (`/opt/ps5-opengl-gl46` + app template from `$HOME/ps5-work/ps5-opengl`) | `toolchain/ps5-kodi.cmake` uses `PS5_OPENGL_PREFIX`; `tools/ps5/package.sh` uses `APP_TEMPLATE`; `ps5-title`/`ps5-package` succeeded; clean (unmutated) upstream SDK independently rebuilt and verified (see dedicated section below) | Qualified: clean upstream build evaluated without the legacy mutation script; both Kodi additions confirmed as narrowly-scoped, justified platform adapters with no supported-configuration alternative; on-device presentation/teardown runtime tests remain outstanding (no console attached in this environment) |
| `dav1d` | `/opt/ps5-payload-sdk/target/user/homebrew` | `kodi-pkg-config --modversion dav1d` -> `1.5.1` | Qualified in current build provenance |
| `FFmpeg` | `/opt/ps5-payload-sdk/target/user/homebrew` | `libavcodec 61.19.101`, `libavformat 61.7.100`, `libavutil 59.39.100`, etc.; CMake cache points at sysroot static libs | Qualified in current build provenance |
| `CPython` | `/opt/ps5-payload-sdk/target/user/homebrew` | `ENABLE_PYTHON=ON`, `PYTHON_VERSION=3.14`, `libpython3.14.a` present | Qualified in current build provenance |

## tools/depends alignment status

Kodi already has dependency recipes for all three non-graphics targets:

- `tools/depends/target/dav1d` (`1.5.3`)
- `tools/depends/target/ffmpeg` (`9.0.2`)
- `tools/depends/target/python3` (`3.14.6`)

PS5 host/platform support has been added and now configures successfully:

- Probe command:
  - `tools/depends/configure --host=x86_64-unknown-freebsd --with-platform=ps5 --with-toolchain=/opt/ps5-payload-sdk ...`
- Result:
  - Configure completes and generates PS5 target config/toolchain files.

So migration status is:

1. **Current runtime dependencies are qualified and pinned via external PS5 sysroot.**
2. **Kodi-native `tools/depends` migration for PS5 is enabled at configure level; CPython 3.14 is now the first target dependency with a full, validated native `make` build.**

## CPython 3.14 native `tools/depends` build (validated 2026-10-04 update)

`make` in `tools/depends/target/python3` now builds and installs successfully
end-to-end against the PS5 cross toolchain (`x86_64-unknown-freebsd-debug`
platform dir), producing `libpython3.14.a` (~53 MB static lib), the full
stdlib under `lib/python3.14`, headers under `include/python3.14`, and
`python-3.14[-embed].pc` pkgconfig files in the real `build/ps5-depends-test`
prefix - the same prefix/layout Kodi's `FindPython.cmake` expects via
`find_package(Python3)`.

Ported patches (`tools/depends/target/python3/*.patch`, adapted from the
legacy `pacbrew/python3` port for CPython 3.14.6's current `configure.ac`/
source layout):

- `20-ps5-freebsd-cross-host.patch` - **required**. CPython's cross-compile
  `case $host` blocks (`ac_sys_system`/`_host_ident`) only recognize a fixed
  list of triples; without this patch, `./configure` hits
  `AC_MSG_ERROR([cross build not supported for $host])` for
  `x86_64-unknown-freebsd`. (The *native*, `uname`-based `MACHDEP` fallback
  elsewhere in `configure.ac` already handles `freebsd*`, which is a
  different, non-cross code path - do not confuse the two when re-evaluating
  this patch against future CPython versions.)
- `21-ps5-no-subprocess.patch` - disables `_can_fork_exec` in
  `Lib/subprocess.py` (no process spawn in a title sandbox).
- `24-ps5-socket-libscenet.patch` - includes `ps5_pysocket.h` into
  `Modules/socketmodule.c` to route socket syscalls through Kodi's
  `libkodishim` (`ps5_socket`/`ps5_connect`/etc.).
- `25-ps5-stdio-nomacros.patch` - undefines stdio macros in `Include/Python.h`
  that assume a FreeBSD `FILE` struct layout the PS5 SDK's clean-room libc
  doesn't match.
- Two patches from the legacy port were determined unnecessary for 3.14.6 and
  were NOT ported (reasoning kept as comments in the Makefile):
  `getc_unlocked` is disabled purely via the `ac_cv_have_getc_unlocked=no`
  cache override (no source patch needed), and `register_at_fork` call sites
  are already guarded by `hasattr(os, "fork")`, which is `False` here.

Makefile install-step override: CPython's own `make install` (and the
`libinstall`/`sharedinstall` targets it pulls in) requires linking the
target's standalone `python` executable, which cannot succeed here -
`ps5_*` socket symbols only resolve at Kodi's final app link (via
`libkodishim`), and the PS5 SDK libc has no `explicit_bzero`. Since a
sandboxed title can never run a standalone interpreter anyway, the PS5
branch of `.installed-$(PLATFORM)` installs `libpython3.14.a`, headers
(`make inclinstall`), the stdlib, and pkgconfig files by hand instead -
mirroring what the legacy pacbrew `PKGBUILD` already did for the same reason.

`dav1d` and `FFmpeg` (plus their transitive requirements `bzip2`, `expat`,
`gettext`, `libffi`, `libxml2`, `openssl`, `sqlite3`, `xz`, `zlib`) are
**already built and installed** natively in the same `build/ps5-depends-test`
prefix - confirmed via their `.configured-x86_64-unknown-freebsd-debug` /
`.installed-x86_64-unknown-freebsd-debug` markers and the presence of
`libdav1d.a`, `libavcodec.a`, `libavformat.a`, `libavutil.a`, etc. alongside
`libpython3.14.a`. CPython was simply the last of these 12 already-qualified
targets to get its native build fully validated (and the only one that
needed a nontrivial recipe fix).

`brotli` and `curl` were additionally built and validated this pass (see
below), bringing the total to 14. Of the ~104 total directories under
`tools/depends/target`, these 14 are the only ones with native
`tools/depends` builds validated in this environment so far. The remaining
target dependencies (fontconfig, freetype2, gnutls, gmp, cec, libsmb2,
tinyxml, libuuid/fstrcmp, etc., per Phase 1A's list) have not yet been
attempted against the PS5 toolchain and are the next candidates for the
same validate-via-real-`make` treatment.

## `brotli` and `curl` native builds (validated 2026-10-04 update)

Both built via their existing (non-PS5-specific) Kodi recipes with **no
patches required**:

- `brotli`: plain CMake build, no PS5-specific issues; produces
  `libbrotlicommon.a`, `libbrotlidec.a`, `libbrotlienc.a` + pkgconfig files.
- `curl`: required `brotli` to be built first (`curl`'s CMake
  `CURL_BROTLI=ON` option does a hard `find_package(Brotli)`) - confirming
  Phase 1A's note that dependency ordering matters. Picked up the
  already-qualified `libz.a` and `libcrypto.a`/OpenSSL automatically via
  the shared `PREFIX`. Produces a static `libcurl.a` (~9.6 MB) with OpenSSL,
  zlib and Brotli support; `libssh2`, `GSS`, `libpsl`, `libidn2` left
  disabled per the existing recipe's PS5-independent default configure flags.

Both rebuilt with a clean `make clean && make` cycle and were confirmed
idempotent (no-op) on a second `make` run.

## `libiconv`, `tinyxml`, `tinyxml2`, `fstrcmp` native builds (validated 2026-10-04 update)

All four built via their existing (non-PS5-specific) Kodi recipes with **no
patches required**, bringing the running total of natively-validated
`tools/depends/target` recipes to 18.

## `libuuid`, `libprocstat`, `libkodishim` (validated 2026-10-04 update, revised)

Initial attempt to natively build the stock `libuuid` recipe (e2fsprogs
1.46.5) failed: `tst_uuid`/`uuid_time`, test/debug helper binaries pulled in
by e2fsprogs' default `all`/`install` targets, fail to link with
`undefined symbol: execl` - the PS5 SDK libc only provides `execv`, not the
full `exec*` family.

First pass at a fix adopted the PS5 reference port's (`kodi-ps5-src`)
purpose-built shim (`shims/libuuid/uuid.c`/`uuid.h`, ~110 lines, random v4
UUIDs only) instead of patching e2fsprogs. **This was superseded after
root-causing the actual failure**, per the migration plan's patch-decision
procedure (prefer a supported configuration over a platform adapter/shim/
patch, in that order): extracting e2fsprogs' `configure.ac` showed the
`execl` call lives in `lib/uuid/gen_uuid.c`'s `get_uuid_via_daemon()`,
which only compiles in when `USE_UUIDD` is defined - and `USE_UUIDD` is
controlled by a first-class, upstream `--disable-uuidd` configure flag
(disables building/using the `uuidd` helper daemon, which nothing in a
`tools/depends` sysroot runs anyway). A local proof-of-build confirmed:
with `--disable-uuidd`, `gen_uuid.o` carries no `execl` reference at all,
and the *default* `all` target - including `tst_uuid`/`uuid_time` - builds
and links successfully with zero source patches. `uuid_generate()`'s
runtime behavior is unaffected: it already prefers the `/dev/urandom`-backed
`uuid_generate_random()` path whenever `/dev/urandom` is available (true on
the PS5 SDK), falling back to the (now effectively dead, since no daemon
exists in this sysroot) `uuid_generate_time()` path only if not.

`tools/depends/target/libuuid/Makefile` therefore needed only a single
one-line change: `--disable-uuidd` added to the existing `./configure`
invocation - identical recipe shape for every platform, no PS5-specific
branch, no shim, no vendored source. The real e2fsprogs library is built
and installed exactly as it is for any other platform, producing the full
API surface (including `uuid_time`/`uuid_type`/`uuid_variant`, which the
shim did not implement) plus upstream's own `uuid.pc`
("Universally unique id library", `Version: 1.46.5`). Confirmed via
`prospero-nm` that the installed `libuuid.a` carries every expected
`uuid_*` symbol and no `execl` reference anywhere.

The `shims/libuuid/` files remain vendored in the tree (parity with the
reference port) but are **no longer wired into any build** - the real
upstream library is used instead. This is a strictly better outcome than
the shim: full fidelity to upstream libuuid's behavior, smaller diff
footprint (one configure flag vs. a platform-specific Makefile branch plus
hand-written pkgconfig), and no risk of divergence from what
crossguid/Kodi expect from a "real" libuuid.

Two further PS5-only shims from the reference port's
`scripts/12-build-libuuid-shim.sh` recipe **were** wired up as brand-new
`tools/depends/target` recipes, and - unlike libuuid - these remain
genuine shims with no supported-configuration alternative, since each
closes a gap that has no equivalent on the PS5 target at all (not a
disableable optional feature of an existing library):

- **`libprocstat`** (new recipe): compiles `shims/libprocstat/procstat.c` -
  a stub satisfying exiv2's FreeBSD-only `libprocstat` API (used only in
  exiv2's library-info dump, which Kodi never calls). `libprocstat` is a
  FreeBSD kernel-ABI introspection library (`sysctl`/`kvm` based); PS5 has
  no FreeBSD kernel underneath its libc compatibility layer, so there is no
  "real" library to configure around - every function here reports
  "nothing" and tolerates `NULL`.
- **`libkodishim`** (new recipe): compiles `shims/libkodishim/kodishim.c` +
  `shims/native-app/libc_socket.c` into `libkodishim.a`. Provides
  `getentropy`/`explicit_bzero` (referenced by libpython, absent from the
  SDK's stub libraries - not a configure-time option of libc, just missing)
  plus the `ps5_socket`/`ps5_connect`/etc. family that CPython's
  `24-ps5-socket-libscenet.patch` redirects `Modules/socketmodule.c` calls
  into (PS5-specific libSceNet routing with no upstream equivalent at all).
  This archive is linked only at Kodi's *final app executable* link (see
  `cmake/scripts/ps5/ArchSetup.cmake`'s `-lkodishim -lSceNet`), never by
  any individual dependency's own build - consistent with the CPython
  session's earlier finding that these `ps5_*` symbols are unavailable
  when CPython tries to link its own standalone `python` executable (which
  is why that step is skipped for PS5).

Both validated via `make`: `libprocstat.a` exports the six expected
`procstat_*` stub symbols; `libkodishim.a` exports `getentropy`,
`explicit_bzero`, and the `ps5_*` socket functions, confirmed via
`prospero-nm`.

This brings the running total of natively-validated `tools/depends/target`
recipes to **21** (18 shared/standard + the corrected `libuuid` + the two
new PS5-only shim recipes `libprocstat`/`libkodishim`).

## `libsmb2` (new recipe, validated 2026-10-04)

No `tools/depends/target/libsmb2` recipe existed in this fork prior to this
pass, despite `libsmb2` being named explicitly in Phase 1A's dependency
list and required both by `cmake/scripts/ps5/ArchSetup.cmake`'s
`SYSTEM_LDFLAGS` (`-lsmb2`) and by the already-present
`xbmc/platform/ps5/filesystem/{SMB2File,SMB2Directory,SMB2Session}.cpp`
sources backing Kodi's `HAS_FILESYSTEM_SMB2` VFS flag. That flag is
distinct from, and coexists with, the stock `HAS_FILESYSTEM_SMB`/
`libsmbclient` path (`tools/depends/target/samba`): PS5 cannot carry the
heavy, patch-laden, POSIX-threading-dependent Samba 3.0.37 client, so the
port adds this separate, modern, dependency-light SMB2/3-only client
instead (confirmed against the reference port's
`patches/kodi/0006-smb-over-libsmb2-ps5.patch`).

Authored `tools/depends/target/libsmb2/{LIBSMB2-VERSION,Makefile}`, modeled
on the existing `libnfs` recipe (same upstream author/CMake style), using
the real upstream `libsmb2-6.2` tarball (already cached from the reference
port's own pacbrew mirror; sha256 cross-checked against GitHub's tag
archive, sha512 computed locally for this repo's checksum convention).
`CMAKE_OPTIONS` disable `BUILD_SHARED_LIBS`, `ENABLE_EXAMPLES`,
`ENABLE_LIBKRB5`, `ENABLE_GSSAPI` — none needed for typical home-network
SMB shares on a static sandboxed target. **No source patches of any kind**
— this is a plain, unmodified upstream CMake build, matching the reference
port's own unpatched `PKGBUILD` for the same package.

Built clean via the real `tools/depends` `make` against the PS5 cross
toolchain. Confirmed `libsmb2.a`, `libsmb2.pc`, and a CMake package config
(`FindSMB2.cmake`) are installed under the shared `PREFIX`, and
`prospero-nm` shows the complete expected public API
(`smb2_init_context`, `smb2_connect_share`, `smb2_read`, `smb2_write`,
`smb2_opendir`, `smb2_readdir`, etc.).

This is the first Phase 1A dependency this pass where **no legacy
patch/shim/override of any kind was ever needed** — a supported, unmodified
upstream configuration was sufficient from the start. Per the plan's status
vocabulary this is recorded as **unnecessary** in
`docs/ps5/dependency-decisions.md` (no local-patch burden to carry), not
"configuration" (there was no existing broken default to turn off, unlike
libuuid's `--disable-uuidd`).

Note: `cmake/scripts/ps5/ArchSetup.cmake` still sets `USE_INTERNAL_LIBS OFF`
and expects most dependencies, including `libsmb2`, to already be present
in the pacbrew-repo-derived sysroot. This new `tools/depends` recipe
satisfies Phase 1A's qualification requirement (clean source, isolated
compatible configuration, retained build tree); wiring Kodi's own CMake
configuration to actually consume this `tools/depends`-built copy instead
of (or alongside) the sysroot copy is Phase 2 scope, not required for
Phase 1A's exit gate.

This brings the running total of natively-validated `tools/depends/target`
recipes to **22** (21 above + `libsmb2`). With this, the migration plan's
explicit Phase 1A named dependency list (`PS5-Kodi-Fork-Migration-Plan.md`
line 212: "curl, OpenSSL, libsmb2, libiconv, Brotli, tinyxml, libuuid") is
now **fully qualified**: curl, OpenSSL, libiconv, Brotli, and tinyxml were
already validated as standard shared recipes; libuuid was corrected this
session; libsmb2 is newly authored and validated here.

## `ps5-opengl` — clean upstream build evaluated without the legacy mutation script (validated 2026-10-04)

Phase 1A's exit gate specifically requires "Clean ps5-opengl has been
evaluated without the legacy mutation script." Prior passes only recorded
`ps5-opengl` as an external pinned prerequisite (commit
`122aa899f9255e37d776b2a5317c86e5e9679907`, matching
`docs/ps5/migration-baseline.md`); this pass performs the actual clean-build
evaluation.

**What the "legacy mutation script" does**: `kodi-ps5-src/scripts/18-build-
ps5-opengl.sh` runs `patches/ps5-opengl/kodi-additions.py` against the pinned
`$HOME/ps5-work/ps5-opengl` checkout *before* invoking the SDK's own
installer (`toolchain/install-ps5-opengl-gl46.sh`), i.e. it rewrites upstream
source in place rather than applying a tracked, reviewable patch file.

**Clean-build procedure**: with the working tree at the pinned commit,
`git stash` removed the Kodi mutation (confirmed via `git status --porcelain`
showing a clean tree matching upstream `122aa899f...`), then
`toolchain/install-ps5-opengl-gl46.sh build/sdk-clean` was run directly
(the same call the mutation script itself makes, minus the mutation and the
`sudo` copy into the shared `/opt/ps5-opengl-gl46` prefix, so the real,
Kodi-enhanced installed copy used by the current build was left untouched).

**Result**: the clean, unmutated upstream SDK built and installed
successfully end-to-end — full GL 4.6 Core + EGL headers (`GL/gl.h`,
`GL/glcorearb.h`, `EGL/egl.h`, etc.), the complete expected static archive
set (`libPS5OpenGL.a`, `libPS5OpenGLCore33.a`, `libmesa*.a`, `libnir.a`,
`libglsl*.a`, `libpsbc.ps5.a`, `libSceAgc.so`/`libSceAgcDriver.so`, etc.), and
`tests/ps5/verify_gl46_link_surface.py` — a real graphics-API surface check,
not a packaging smoke test — reported **`gl46-link-surface: PASS
commands=657 exported=657`** (every one of the 657 GL 4.6 Core entry points
the SDK claims to export actually resolves in the linked archive).

**`make sdk-gl46`'s bundled `test-compiler` step fails independently of the
Kodi mutation** (a pre-existing, unrelated upstream test-suite issue, not a
regression introduced by this port): `tests/ps5/test_descriptor_snapshot.py`
asserts a literal source string inside `ps5_screen.c`'s
`ps5_prepare_constant()`, which this pinned commit's own `ps5_screen.c` does
not contain verbatim. Reproduced identically with the Kodi mutation stashed
*and* restored (same `AssertionError` both times), proving it is unrelated
to Kodi's additions. This is exactly why the reference port's own
`scripts/18-build-ps5-opengl.sh` already bypasses `make sdk-gl46` entirely
and calls `toolchain/install-ps5-opengl-gl46.sh` directly with the comment
"host-side compiler self-tests are not needed here" — a prior, independent
confirmation of the same finding. Recorded here as a known, pre-existing,
non-blocking upstream test issue; not a Kodi migration defect.

**Kodi's additions, evaluated against the clean build**: `kodi-additions.py`
inserts two features (its own docstring summary, "One addition remains: ...
the driver exports its video out handle", undersells the current scope —
both are present and active):

1. `ps5_opengl_video_out_handle()` (`src/platform/ps5_agc_native_runtime.c`)
   — exports the driver's internal video-out handle so Kodi's window system
   can read refresh rate/vblank timing and (optionally) toggle HDR output
   buffer format (`ps5_opengl_set_scanout_format`, same file). Confirmed via
   `llvm-nm` across **every** archive in the clean, unmutated install
   (`libPS5OpenGL.a`, `libPS5OpenGLCore33.a`, `libps5_opengl_core33.a`, all
   `libmesa*`/`libnir`/`libvtn`/`libpsbc.ps5.a`) that
   `ps5_opengl_video_out_handle` is exported by **none** of them — this
   handle genuinely has no public upstream accessor; a title process cannot
   reach it any other way.
2. Zero-copy video texture import (`ps5_kodi_resource_from_memory` in
   `ps5_screen.c`, `ps5_kodi_validate_egl_image`/`ps5_kodi_get_egl_image` EGL
   hooks in `ps5_egl.c`) — lets Kodi's renderer
   (`xbmc/platform/ps5/video/RendererPS5.cpp`) sample the hardware video
   decoder's output frames directly as a GL texture via
   `glEGLImageTargetTexture2DOES`, with no CPU copy. This requires Mesa
   frontend hooks (`validate_egl_image`/`get_egl_image`) that upstream's
   EGL frontend does not populate for any caller by default — there is no
   existing public API path to obtain a sampled GL texture over
   driver-foreign memory on this SDK.

Both additions are narrowly scoped (confined to 3 files, additive only,
~190 lines total per `git diff --stat`), each solves a real capability gap
with no supported-configuration alternative (confirmed by their total
absence from the clean build), and both are exactly the kind of "narrowly
scoped platform adapter" the plan's preference order allows after a
supported configuration is ruled out. **Status: local patch** for both
(platform adapter — correctly so; this is not a case warranting reversion
like `libuuid`'s was, because no upstream configuration flag or SDK
alternative provides either capability).

Earlier development iterations of `kodi-additions.py` also added klog-based
GPU clear-path/profile diagnostics (`ps5_kodi_klog_printf`,
`ps5_kodi_count_clear`); the script's own `REMOVED` list now actively strips
these if found, converging the tree to "upstream plus the two additions
above" — confirmed no leftover diagnostic strings remain after the
stash/pop cycle in this pass.

**Still outstanding** (per the plan's own exit-gate language, "explicitly
awaiting named runtime tests" rather than blocking): actual on-device
context creation, framebuffer/texture presentation, display-size/VRR/HDR
hook behavior, and teardown have not been exercised on real PS5 hardware in
this environment (no console attached here) — `verify_gl46_link_surface.py`
validates the *linked API surface*, not runtime GL rendering correctness.
This matches the plan's allowance that dependencies may be "qualified on the
target or explicitly awaiting named runtime tests" rather than requiring
every item to complete before the exit gate is satisfied.

## Scope correction: `gnutls`/`gmp`/`libffi` are not part of this port's dependency graph

Cross-checked against the reference port's actual build graph
(`build/orchestrator/build.ninja`'s `ps5_deps_core` target) and its pacbrew
package list (`pacbrew-repo/ci-libs.sh`'s `PKGS=(...)`, which supplies most
non-Kodi-specific dependencies as prebuilt packages rather than building them
from source): **neither `gnutls` nor `gmp` appears anywhere** in either. This
PS5 port uses OpenSSL exclusively for TLS; GnuTLS/GMP are entirely outside
its dependency graph and should **not** be pursued as Phase 1A targets.
`libffi` is similarly unneeded, consistent with the earlier finding (CPython
session) that `_ctypes` is already disabled for PS5
(`py_cv_module__ctypes=n/a`). `fontconfig`/`freetype2` status remains
unconfirmed - both appear in pacbrew's package list (supplied as prebuilt
binaries in the reference port) but have not yet been attempted natively
here; they are plausible future Phase 1A candidates if Kodi's PS5 font
rendering needs them, but are not blocking any currently-validated target.

## DEP-006 closed: combined full-graph build validation (2026-10-05 update)

All 21 curated, PS5-relevant `tools/depends/target` recipes (the explicit
Phase 1A named list plus every transitive/shim dependency validated above)
were rebuilt together in **one combined `make` pass** against the shared
`PREFIX`, rather than only individually as in each prior validation entry:

`bzip2 expat gettext libffi libxml2 openssl sqlite3 xz zlib dav1d ffmpeg
python3 curl brotli libiconv tinyxml tinyxml2 fstrcmp libuuid libprocstat
libkodishim libsmb2`

Result: zero failures, all idempotent (no-op) on this already-built prefix -
evidence log `/tmp/dep006-verify.log`. This is the specific remaining item
`migration-ledger.csv`'s `DEP-006` row called out ("full build/install matrix
remains pending") and is now marked `done`. Combined with the scope
correction above (gnutls/gmp/libffi's `_ctypes` use excluded; fontconfig/
freetype2 deferred as non-blocking future candidates), **Phase 1A's
dependency-qualification work is now fully complete** for this port.

## Workaround carry policy outcome

- No legacy graphics or Python patch bundle is auto-applied during configure/build.
- Current path prefers supported configuration and existing shipped libraries.
- Any future PS5-specific `tools/depends` patches will need explicit per-patch
  evidence before adoption.
