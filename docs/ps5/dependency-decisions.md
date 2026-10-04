# PS5 dependency decisions

Tracks every legacy patch, configure cache override, forced include, symbol
wrapper, disabled feature, or shim encountered while qualifying the PS5
dependency graph against the patch decision procedure in
`PS5-Kodi-Fork-Migration-Plan.md` ("Patch decision procedure" under
Phase 1A). Each entry records: failure + evidence, upstream check, clean-
build attempt, capture of the real error, chosen solution with alternatives
and scope, regression check, and final status.

Status vocabulary (per the migration plan): **unnecessary**,
**configuration**, **integration adapter**, **SDK/runtime fix**,
**local patch**, or **unresolved**.

---

## `libuuid` — e2fsprogs `tst_uuid`/`uuid_time` link failure

1. **Failure + evidence**: building the stock `tools/depends/target/libuuid`
   recipe (e2fsprogs 1.46.5) against the PS5 cross toolchain failed at link
   time: `undefined symbol: execl` in `tst_uuid`/`uuid_time`, two test/debug
   helper executables that e2fsprogs' default `lib/uuid` `all::` target
   builds alongside the static library. Evidence: direct build log from
   `make` in the recipe directory (`/tmp/build-libuuid.log` style capture).
2. **Upstream check**: inspected `e2fsprogs-1.46.5/lib/uuid/gen_uuid.c`
   directly (extracted the real tarball from
   `build/ps5-depends-test/xbmc-tarballs/e2fsprogs-1.46.5.tar.xz`). The
   *only* `execl` reference in the entire `lib/uuid` tree is inside
   `get_uuid_via_daemon()`, gated by
   `#if defined(USE_UUIDD) && defined(HAVE_SYS_UN_H)`. `USE_UUIDD` is
   controlled by the top-level `configure.ac`'s own `--disable-uuidd` flag
   (`AC_ARG_ENABLE([uuidd], ...)`), enabled by default upstream. This is an
   existing, first-class, supported configuration option — not something
   requiring a patch.
3. **Build and exercise the clean implementation**: reconfigured a scratch
   copy of the real e2fsprogs source for the PS5 cross host
   (`--host=x86_64-unknown-freebsd ... --disable-uuidd`) and ran the
   *default* `lib/uuid` `all` target (no target restriction). Confirmed via
   `prospero-nm` that `gen_uuid.o` now carries no `execl` symbol at all, and
   `tst_uuid` links and produces a real executable
   (`/tmp/libuuid-poc-build.log`).
4. **Distinguishing root cause**: this was build detection/an upstream
   default, not a missing ABI/API behavior. The PS5 SDK libc's lack of a
   full `exec*()` family (only `execv`) is real and would still matter if
   anything genuinely needed `execl`, but nothing in Kodi's consumption of
   `libuuid.a` does — `uuidd` is a standalone system daemon irrelevant to a
   static-archive consumer in any `tools/depends` sysroot on any platform.
5. **Alternatives considered, in the plan's stated preference order**:
   - *Supported configuration* (chosen): add `--disable-uuidd` to the
     existing `./configure` invocation.
   - *Narrowly scoped platform adapter*: an earlier pass patched the
     Makefile to build only `libuuid.a`/`uuid.h`/`uuid.pc` directly, instead
     of the default `all`/`install` targets, to dodge `tst_uuid` without
     removing it from the source. Technically worked but was unnecessary
     scope (touched every platform's recipe for a problem that, per the
     configure check, has an upstream on/off switch) and left `uuidd`'s
     dead `execl` reference compiled into the object file regardless.
     **Superseded** by the configuration fix.
   - *Local patch / shim*: the PS5 reference port
     (`kodi-ps5-src/shims/libuuid/uuid.c`) replaces libuuid entirely with a
     ~110-line random-v4-only reimplementation, bypassing e2fsprogs. Initially
     adopted, then **superseded** after root-causing the actual failure: a
     one-line configure flag fully resolves the real problem with the real
     upstream library, which is strictly preferable to a reduced
     reimplementation (full API including `uuid_time`/`uuid_type`/
     `uuid_variant`, which the shim lacks; upstream-authored `uuid.pc`;
     zero platform-specific branching in the recipe).
6. **Chosen solution**: `--disable-uuidd` added to
   `tools/depends/target/libuuid/Makefile`'s existing `./configure` call,
   applied unconditionally for every platform (not PS5-gated) since it is a
   universally-correct simplification with no behavior change for any
   platform that doesn't use `uuidd` from a `tools/depends` build anyway.
   **Scope**: one line in one Makefile, no new files. **Regression check**:
   full clean `make`/install cycle re-validated end-to-end against the real
   `tools/depends` build system; confirmed installed `libuuid.a` exports
   every expected `uuid_*` symbol via `prospero-nm`, and `uuid.pc` matches
   upstream's own generated pkgconfig content. **Owner/update implications**:
   none — this flag is stable across e2fsprogs releases and does not need
   revisiting on a version bump. **Status: configuration.**

The `shims/libuuid/uuid.c`/`uuid.h` files remain vendored in the tree
(parity with the reference port this fork is migrating from) but are no
longer referenced by any build.

---

## `libprocstat` — exiv2's FreeBSD `libprocstat` API has no PS5 equivalent

1. **Failure + evidence**: exiv2 references the FreeBSD `libprocstat` API
   (`procstat_open_sysctl`/`procstat_getprocs`/etc.) unconditionally on any
   `__FreeBSD__`-defined target, including the PS5 SDK's FreeBSD-derived
   libc (only used by exiv2's own library-info self-diagnostic dump, which
   Kodi never invokes). No such library exists on PS5.
2. **Upstream check**: `libprocstat` is a FreeBSD kernel-ABI introspection
   library built on `sysctl(KERN_PROC...)`/`libkvm`, tied directly to the
   real FreeBSD kernel's process-table layout. PS5 has no FreeBSD kernel
   underneath its libc compatibility shim — there is no configuration flag,
   upstream source, or SDK component that could provide a genuine
   implementation.
3. **Build/exercise clean implementation**: not applicable — no clean
   upstream implementation exists for this target to attempt.
4. **Distinguish root cause**: this is a genuine missing-ABI situation
   (platform fundamentally lacks the kernel interface), not a
   build-detection or configuration problem.
5. **Alternatives considered**: disabling exiv2's library-info feature
   entirely (bigger, more invasive change to a third-party dependency's
   build options, for a feature Kodi never calls) vs. a minimal link-time
   stub satisfying only the symbols exiv2's linker actually needs. The stub
   is smaller in scope and already proven in the reference port.
6. **Chosen solution**: new `tools/depends/target/libprocstat` recipe
   (PS5-only; hard-errors if built for any other `OS`) compiling
   `shims/libprocstat/procstat.c` — every function reports "nothing"/`NULL`,
   matching exiv2's expectations for a library-info dump on a platform with
   no such introspection available. **Scope**: one new recipe, one existing
   vendored source file, no changes to exiv2 itself. **Regression check**:
   validated via `make`; `prospero-nm` confirms the six expected
   `procstat_*` symbols are present. **Owner/update implications**: revisit
   only if exiv2's `__FreeBSD__`-gated code path changes which symbols it
   calls. **Status: local patch** (platform adapter/stub — no supported
   configuration or SDK alternative exists).

---

## `libkodishim` — libpython/libc symbols and PS5 socket routing absent from the SDK

1. **Failure + evidence**: libpython's build and the final Kodi app link
   reference `getentropy`/`explicit_bzero`, which the PS5 SDK's stub
   libraries do not provide at all (not a configuration option — genuinely
   absent symbols). Separately, CPython's `24-ps5-socket-libscenet.patch`
   (see CPython session notes) redirects `Modules/socketmodule.c`'s BSD
   socket calls to a `ps5_socket`/`ps5_connect`/etc. family that must route
   through Sony's `libSceNet`, because the PS5 sandbox denies raw
   `socket()`/`connect()` syscalls (`EACCES`) for a title process.
2. **Upstream check**: neither gap has an upstream or SDK-provided
   alternative. `getentropy`/`explicit_bzero` are not exposed by the PS5
   payload SDK's libc at any configuration; the sandboxed-socket situation
   is a platform policy restriction with no "real" BSD socket path to fall
   back to — `libSceNet` is the only permitted network path on-device.
3. **Build/exercise clean implementation**: not applicable — no clean
   upstream/SDK implementation exists for either gap on this target.
4. **Distinguish root cause**: genuine missing-ABI/platform-policy
   situation, not build detection or an obsolete assumption.
5. **Alternatives considered**: linking the SDK's full `libkernel_sys`/
   `libScePosixForWebKit` stub set that happens to define these symbols
   (rejected — a title never loads those modules at runtime, so calling
   through them would jump to address 0 instead of failing safely) vs. a
   small purpose-built shim providing correct implementations on primitives
   the SDK libc does export. The shim is smaller, already proven in the
   reference port, and avoids linking unrelated unused stub modules.
6. **Chosen solution**: new `tools/depends/target/libkodishim` recipe
   (PS5-only; hard-errors if built for any other `OS`) compiling
   `shims/libkodishim/kodishim.c` (getentropy/explicit_bzero plus assorted
   weak POSIX shims consumed by libpython) and
   `shims/native-app/libc_socket.c` (the `ps5_*` socket family, implemented
   over `libSceNet`) into `libkodishim.a`. Linked only at Kodi's final app
   executable link (`cmake/scripts/ps5/ArchSetup.cmake`: `-lkodishim
   -lSceNet`), never by any individual dependency's own build. **Scope**:
   one new recipe, two existing vendored source files, no changes to
   libpython or any other dependency. **Regression check**: validated via
   `make`; `prospero-nm` confirms `getentropy`, `explicit_bzero`, and the
   `ps5_*` socket functions are present in the installed archive.
   **Owner/update implications**: revisit if a future PS5 SDK release adds
   real `getentropy`/`explicit_bzero`, or if CPython's socket-redirection
   patch changes which `ps5_*` symbols it expects. **Status: local patch**
   (platform adapter — no supported configuration or SDK alternative
   exists for either half of this shim).

---

## `gnutls` / `gmp` / `libffi` — not part of this port's dependency graph

1. **"Failure"**: none encountered directly; flagged during Phase 1A
   inventory review as plausible candidates (migration plan's starting list
   mentions auditing "other packages discovered in the actual link/build
   graph").
2. **Upstream/scope check**: cross-referenced the PS5 reference port's
   actual build graph (`kodi-ps5-src/build/orchestrator/build.ninja`'s
   `ps5_deps_core` phony target) and its pacbrew prebuilt-package list
   (`pacbrew-repo/ci-libs.sh`'s `PKGS=(...)`). Neither `gnutls` nor `gmp`
   appears in either — this PS5 port uses OpenSSL exclusively for TLS, with
   no GnuTLS/GMP anywhere in its dependency graph. `libffi` is separately
   confirmed unneeded: CPython's `_ctypes` module is already disabled for
   PS5 (`py_cv_module__ctypes=n/a`, from the CPython qualification pass).
3. **Build/exercise**: not attempted — out of scope, no consumer exists.
4. **Distinguish root cause**: n/a — this is a scope correction, not a
   build problem.
5. **Alternatives considered**: n/a.
6. **Chosen solution**: drop all three from the Phase 1A working list.
   **Status: unnecessary.** `fontconfig`/`freetype2` remain unconfirmed
   (present in pacbrew's package list, supplied as prebuilt binaries in the
   reference port, not yet attempted natively here) — treat as open, not
   excluded, pending confirmation of whether Kodi's PS5 font rendering
   needs them.

---

## CPython 3.14 PS5 patches (20 / 21 / 24 / 25)

See `docs/ps5/phase4-dependency-qualification.md`'s "CPython 3.14 native
`tools/depends` build" section for the full per-patch rationale (carried
over from the prior qualification pass; duplicated here only as an index
entry per this file's tracking purpose).

- `20-ps5-freebsd-cross-host.patch` — **status: local patch** (configure.ac
  cross-host triple recognition; no supported configuration alternative,
  CPython's `AC_MSG_ERROR` path has no override flag).
- `21-ps5-no-subprocess.patch` — **status: local patch** (disables
  `_can_fork_exec`; platform policy restriction, no process spawn permitted
  in a title sandbox, no configuration alternative).
- `24-ps5-socket-libscenet.patch` — **status: local patch** (socket
  redirection to `libkodishim`'s `ps5_*` family; see `libkodishim` entry
  above for the underlying platform-policy justification).
- `25-ps5-stdio-nomacros.patch` — **status: local patch** (PS5 SDK's
  clean-room libc `FILE` struct layout doesn't match the stdio macros
  CPython's `Include/Python.h` assumes; ABI mismatch, no configuration
  alternative).
- `getc_unlocked` — **status: configuration** (disabled via
  `ac_cv_have_getc_unlocked=no` cache override; no source patch needed).
- `register_at_fork` — **status: unnecessary** (call sites already guarded
  by `hasattr(os, "fork")`, which is `False` on PS5; no patch or override
  needed at all).

---

## `libsmb2` — no prior `tools/depends` recipe; authored from scratch

1. **"Failure" + evidence**: not a build failure — `libsmb2` simply had no
   `tools/depends/target/libsmb2` recipe in this fork at all. It is named
   explicitly in the migration plan's Phase 1A dependency list
   (`PS5-Kodi-Fork-Migration-Plan.md` line 212: "curl, OpenSSL, libsmb2,
   libiconv, Brotli, tinyxml, libuuid") and is required by
   `cmake/scripts/ps5/ArchSetup.cmake`'s `SYSTEM_LDFLAGS` (`-lsmb2`) and by
   the already-present `xbmc/platform/ps5/filesystem/{SMB2File,
   SMB2Directory,SMB2Session}.cpp` sources (Kodi's `HAS_FILESYSTEM_SMB2`
   backend, parallel to and independent from the existing
   `HAS_FILESYSTEM_SMB`/`libsmbclient` path — confirmed against
   `kodi-ps5-src/patches/kodi/0006-smb-over-libsmb2-ps5.patch`).
2. **Upstream check**: upstream is `github.com/sahlberg/libsmb2`, tagged
   release `libsmb2-6.2` (the matching tarball is already cached from the
   reference port's own pacbrew mirror,
   `pacbrew-repo/libsmb2/libsmb2-6.2.tar.gz`; sha256 cross-checked against
   GitHub's own tag archive). The reference port's `PKGBUILD` for this
   package uses a **plain, unpatched** CMake build
   (`-DCMAKE_BUILD_TYPE=Release` only) — no source patches anywhere.
   `lib/CMakeLists.txt` confirms a straightforward static-archive
   (`add_library(smb2 ...)`) build with `ENABLE_LIBKRB5`/`ENABLE_GSSAPI` as
   the only optional, disableable features.
3. **Build and exercise the clean implementation**: authored
   `tools/depends/target/libsmb2/{LIBSMB2-VERSION,Makefile}` modeled on the
   existing `libnfs` recipe (same upstream author, same CMake style).
   `CMAKE_OPTIONS` disable `BUILD_SHARED_LIBS`, `ENABLE_EXAMPLES`,
   `ENABLE_LIBKRB5`, `ENABLE_GSSAPI` (none of which the PS5 port needs: no
   Kerberos SMB auth for typical home-network shares, no shared libs on a
   static sandboxed target). Built clean via the real `tools/depends`
   `make` against the PS5 cross toolchain with **zero source
   modifications**. Confirmed `libsmb2.a`, `libsmb2.pc`, and a CMake
   `FindSMB2.cmake`/package-config are installed to the shared `PREFIX`,
   and `prospero-nm` shows all expected public symbols
   (`smb2_init_context`, `smb2_connect_share`, `smb2_read`, `smb2_write`,
   `smb2_opendir`, `smb2_readdir`, etc.).
4. **Distinguish root cause**: n/a — there was no failure to diagnose, only
   a missing recipe. No ABI gap, no platform policy restriction; the PS5
   cross toolchain builds this library exactly as any other POSIX target
   would (it already builds against basic BSD sockets/pthreads, both of
   which the SDK provides).
5. **Alternatives considered**: none needed — a clean, unpatched upstream
   CMake configuration fully satisfies the requirement on the first
   attempt, which is the most-preferred outcome in the plan's preference
   order.
6. **Chosen solution**: new `tools/depends/target/libsmb2` recipe, real
   upstream source, no patches. **Scope**: two new files
   (`LIBSMB2-VERSION`, `Makefile`), no changes to any other dependency or
   to Kodi's own sources (the PS5 filesystem backend consuming it already
   existed in the tree from prior work). **Regression check**: full clean
   `make`/install cycle validated; installed archive carries the complete
   expected public API via `prospero-nm`. **Owner/update implications**:
   revisit only on a libsmb2 version bump (re-verify
   `ENABLE_LIBKRB5`/`ENABLE_GSSAPI` still default-disableable, and re-check
   the sha512 pin). **Status: unnecessary** (no legacy patch/shim/override
   was ever required — a supported, unmodified upstream configuration was
   sufficient from the start).

Note: `cmake/scripts/ps5/ArchSetup.cmake` currently sets `USE_INTERNAL_LIBS
OFF` and expects most dependencies (including `libsmb2`) to already be
present in the pacbrew-repo-derived sysroot; this new `tools/depends`
recipe is the Phase 1A *qualification* build (clean source, isolated
configuration, per the plan's Phase 1A requirement), proving the dependency
builds correctly with no patches under the native build system. Wiring
Kodi's own CMake configuration to consume this `tools/depends`-built copy
instead of (or in addition to) the sysroot copy is Phase 2 scope
("move useful platform implementations... account for every patch") and is
not required to satisfy Phase 1A's exit gate.

---

## `ps5-opengl` — two narrowly-scoped source additions over vanilla upstream Mesa/GL

1. **Failure + evidence**: not a build failure. Phase 1A's exit gate
   explicitly requires "Clean ps5-opengl has been evaluated without the
   legacy mutation script" (`kodi-ps5-src/scripts/18-build-ps5-opengl.sh`,
   which rewrites upstream source in place via
   `patches/ps5-opengl/kodi-additions.py` before building, rather than
   applying a tracked patch file). Prior passes had only recorded
   `ps5-opengl` as an external pinned prerequisite without performing this
   specific evaluation.
2. **Upstream check**: at the pinned commit
   (`122aa899f9255e37d776b2a5317c86e5e9679907`), `git stash` removed the
   mutation entirely (confirmed clean `git status --porcelain` matching
   upstream). The two capabilities the mutation adds -
   `ps5_opengl_video_out_handle()` (video-out handle export) and zero-copy
   EGL-image-from-foreign-memory (`ps5_kodi_resource_from_memory` +
   `validate_egl_image`/`get_egl_image` frontend hooks) - have **no public
   upstream accessor or configuration flag**: confirmed via `llvm-nm` across
   every archive in a from-scratch clean build that
   `ps5_opengl_video_out_handle` is exported by none of them, and Mesa's EGL
   frontend does not populate `validate_egl_image`/`get_egl_image` for any
   caller without this addition.
3. **Build and exercise the clean implementation**: built the stashed
   (unmutated) tree via `toolchain/install-ps5-opengl-gl46.sh` directly
   (bypassing the Makefile's `sdk-gl46`'s bundled `test-compiler` step - see
   point 4). Result: full GL 4.6 Core + EGL headers and archive set
   installed successfully, and `tests/ps5/verify_gl46_link_surface.py`
   reported `gl46-link-surface: PASS commands=657 exported=657` - a genuine
   graphics-API-surface exercise, not merely a packaging check.
4. **Distinguishing root cause**: `make sdk-gl46`'s own `test-compiler`
   prerequisite fails via `tests/ps5/test_descriptor_snapshot.py` (asserts a
   literal source string absent from this pinned commit's
   `ps5_screen.c::ps5_prepare_constant()`) - reproduced identically with the
   mutation stashed and restored, proving this is a pre-existing upstream
   test-suite issue unrelated to the Kodi migration, not a regression it
   introduced. This independently confirms why the reference port's own
   `scripts/18-build-ps5-opengl.sh` already bypasses `make sdk-gl46` and
   calls the installer script directly with the comment "host-side compiler
   self-tests are not needed here."
5. **Alternatives considered, in the plan's stated preference order**:
   - *Supported configuration*: none exists - neither capability is gated
     behind any upstream Mesa/EGL build option; both require new driver-side
     code.
   - *Narrowly scoped platform adapter* (chosen): two additive-only source
     insertions (~190 lines across 3 files per `git diff --stat`, no
     deletions to upstream logic), each solving a capability gap confirmed
     absent from the clean build.
   - *Local patch carried as an unreviewable mutation script* (status quo
     before this pass): functionally works but is less transparent than a
     tracked patch/diff would be; noted as a Phase 2 candidate improvement
     (convert `kodi-additions.py`'s insertions into a reviewable unified
     diff) but out of scope for Phase 1A's qualification requirement itself.
6. **Chosen solution**: retain both additions as-is (`ps5_opengl_video_out_
   handle` and the zero-copy EGL image hooks), now with explicit clean-build
   evidence that neither has a supported-configuration alternative. **Scope**:
   3 files, ~190 lines, additive only, in the separate `ps5-opengl` checkout
   (not part of this Kodi fork's own tree). **Regression check**: clean
   from-scratch build with the mutation stashed, confirmed via `llvm-nm`
   across every installed archive that both additions are absent upstream;
   mutation restored afterward (`git stash pop`) leaving the working tree as
   found. **Owner/update implications**: revisit if a `ps5-opengl` commit
   bump changes the three anchor files' structure (`kodi-additions.py`
   already reports "skipped, anchor differs" rather than failing silently
   in that case). The bundled `make sdk-gl46`'s `test-compiler` failure
   should be reported/tracked upstream separately; it does not block this
   port. **Status: local patch** (platform adapter) for both additions -
   correctly so, unlike `libuuid`'s reverted shim, since no upstream
   configuration or SDK alternative exists for either capability.
