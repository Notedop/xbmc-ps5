# In-process ELF loader (binary add-ons)

A PS5 title cannot use Kodi's normal `dlopen` path. `DllLoaderContainer`
selects `CPS5AddonLoader`, which reads an ELF64 x86-64 shared object, maps its
segments writable, applies relocations, changes code pages to executable
(never RWX), runs constructors and resolves the `ADDON_*` entry points.
Kodi's ordinary add-on API checks and instance lifecycle still apply.

Supported relocations are `RELATIVE`, `64`, `GLOB_DAT` and `JUMP_SLOT`.
TLS and other relocation types are counted but not applied. There is no general
dependency loader. Use only inspected, trusted test binaries: this is not a
complete system ELF loader.

Add-ons call Kodi through the official dev-kit function tables. Their named
imports are runtime functions/data, not arbitrary Kodi APIs. The host resolver
uses the running title's relocated imports; having a library in the SDK does
not make all its symbols available to an add-on. Native title conversion
encodes dynamic import names as NIDs, unlike the plaintext symbols in the probe.
Packaging preserves the original LLVM symbol/string/relocation/dynamic tables
in readable segments for the add-on resolver. The converter still creates its
separate Sony metadata for the system loader; its process parameters must not
overwrite the preserved tables. Packaging derives an export policy from the
SDK's actual libc++/libc++abi/libunwind archive definitions and automatically
discovered installed add-on imports, including their authorized helpers.
`PS5_ADDON_ELFS` additionally accepts trusted colon-separated `.so` paths for
exports only; it is not needed for automatically staged binaries. Requested runtime
symbols are rooted at the final native link so unused archive members are not
silently omitted. The original dynsym exposes linked functions and data; the
link also creates ordinary relocated import slots for selected non-archive
imports that Kodi itself might not otherwise reference (`R_X86_64_64`, zero
addend). These are not a custom export pointer table: the resolver reads their
original ELF relocations just like imported GOT entries. The
resolver uses `image base + st_value`, rejecting undefined, reserved-section,
hidden, local, TLS and IFUNC entries and preferring strong definitions.
The 26 hidden C++ pointer bindings have been removed. Allocator/POSIX imports
prefer the title's exported `__wrap_*` definitions, preserving Kodi's heap and
wrapper policies rather than bypassing them through libc's GOT.
`pthread_setspecific` must resolve to the title's `__wrap_pthread_setspecific`.
Its non-inlined, non-tail-call boundary keeps the system call's return address
inside the title, whose module is registered with the system loader. This
preserves the system's real TSD allocation, values, errors and exit destructors;
it does not implement ELF TLS or register manually mapped images as system modules.
Packaging fails if a selected add-on imports this function without the wrapper.
The converter retains these definitions only in the original LLVM tables. Its
separate Sony tables contain compacted system imports; their relocations are
reindexed, and relocations to host definitions become local `RELATIVE` entries.
The old generated `g_host_exports` table and link-time assembly-table design
are retired.

The native title does not map its ELF file header. Packaging supplies hidden
image-boundary linker symbols for resolver initialization instead of searching
backward through memory. The preserved LLVM `_DYNAMIC` metadata uses link-time
virtual addresses, to which the runtime image base is added. Repackage the title
after changing this setup; a missing boundary reports a loader error.

On PS5 the loader uses SDK libunwind's `__unw_add_dynamic_eh_frame_section`
before constructors and `__unw_remove_dynamic_eh_frame_section` on unload.
Its `__register_frame` registers only a single FDE, not an entire section.
The Linux/libgcc host path retains `__register_frame`. Emitting `.eh_frame` does not by itself prove
console exception support: check actual registration and throw/catch results.
`__cxa_atexit` and `__cxa_finalize` imports bind to the loader's module-owned
registry, never the process CRT. Registration uses the mapped DSO handle;
callbacks are removed before invocation, run LIFO, and may register more
callbacks without a registry lock being held. Explicit DSO finalization is
idempotent. Unload runs `DT_FINI_ARRAY` in reverse order and `DT_FINI`, then
drains remaining registrations before removing unwind data and unmapping.
Uninitialized images do not run finalizers, and constructor exceptions clean
up the partially initialized image. Finalizer arrays must fit a readable
segment and their functions must be in executable segments.
**Before unload, Kodi must destroy all instances and quiesce all add-on
threads/callbacks.** This is not a mechanism for unloading executing code.
Process-wide `__cxa_finalize(nullptr)` is deliberately not forwarded to the
host. Imports of ordinary `atexit` or thread-local destructor registration are
rejected rather than leaving dangling process/thread-exit callbacks.

Focused Linux/WSL validation (no Kodi build or console):

```sh
bash tools/ps5/test-elf-loader.sh
```

It checks repeated init, reverse finalization, two simultaneous modules,
explicit finalization without double destruction, concurrent registrations
with threads joined before unload, throwing/catching inside global destructors,
uninitialized unload, rejected malformed finalizers, constructor-failure
cleanup after exception destruction, and reload. It uses installed Clang/LLVM, matching the
loader's LLVM ELF layout (GNU ld's `-nostdlib` output may lack the final
`.eh_frame` terminator required by libgcc's whole-section registration).

### Worker-thread TSD crash (firmware 9.40)

The direct DASH crash at `libkernel + 0x40fcb` is a null module-record read,
not an invalid TLS relocation or an established bad pthread key. Matching
firmware disassembly shows `pthread_setspecific` (`+0x40de0`) calling its
first-use TSD allocator (`+0x40d10`), which calls `+0x40fa0`. That helper walks
two caller frames to obtain the add-on return address, looks up its registered
system module (`+0x34080`), and reads flags at offset `0x70` without checking
for null. The crash's `rax=0`, fault address `0x70`, and saved add-on frame
`+0x13ac4a` match this path exactly: the module is manually mapped and unknown
to the system registry. The libc++ proxy has not called `AdaptiveStream::worker`
yet. Adjacent key-create/delete and get-specific paths do not use this helper.

The title wrapper fixes the caller boundary using ordinary named imports;
there are no firmware addresses, module-record offsets or kernel patches in
the runtime fix. The offsets above document only the diagnosed 9.40 capture.
Decrypted firmware artifacts remain local under Git-ignored `build/ps5-firmware`.

```sh
bash tools/ps5/test-pthread-tls.sh
```

This optimized host regression loads an import-only module with `ElfLoader`.
It rejects the unwrapped caller as an unregistered image, accepts the wrapped
caller in the host executable, checks per-thread values, clear/set, unchanged
error returns, exit destructors and repeated unload/reload, and checks export
preflight rejection without the wrapper. It also compiles the shim with the
PS5 SDK. It models caller validation; it does not execute Sony's kernel.
Console playback remains unproven until a repackaged title is tested.
Probe version `0.0.5` additionally starts and joins a `std::thread`, checks TSD
values and its exit destructor before allowing the screensaver PASS marker.

## Isolated test add-on

`addons/test.binary.ps5` is a log-only screensaver called **PS5 Binary Loader
Test**. It uses the current checkout's `CAddonBase`, `CInstanceScreensaver` and
`ADDONCREATOR`, not handwritten copies of ABI structures.

Build from the repository root in Linux/WSL:

```sh
bash tools/ps5/build-test-addon.sh
```

`PS5_PAYLOAD_SDK` and `BUILD` override the SDK and Kodi build directories.
The default output is `build/ps5-release/test-addons/test.binary.ps5`.
No Kodi configure, bootstrap or dependency build is needed for this command.

The script uses the existing SDK compiler and headers with the FreeBSD ABI,
then links a standard import-only `ET_DYN` using the SDK-selected LLVM linker.
The SCE compiler driver's legacy `.ctors` and executable linker script are
not suitable for this loader's `.init_array` shared-object contract.
No second C++ runtime is embedded.

Preflight checks exports, manifest/API version, constructors, unwind header,
relocation types, dynamic dependencies and 16 KiB page-rounded segment layout.
The script prints the runtime imports that must resolve on-console. It retains
an unstripped `.so`, `elf-report.txt`, input/artifact checksums and a build ID.
An ELF preflight PASS is not a console runtime PASS.

The probe exercises a static constructor, two-way Kodi callbacks, heap
allocation/deallocation, virtual dispatch, a C++ exception caught inside the
add-on, a joined `std::thread` with TSD values and an exit destructor, and the
real Start/Render/Stop/destruction lifecycle. Noinline boundaries
keep the heap/vtable/exception checks from being folded away by optimization.
There are no external assets or graphics calls and no per-frame log spam.

## Opt-in packaging and deployment

After rebuilding Kodi for any loader/diagnostic changes:

```sh
cmake --build build/ps5-release -j"$(nproc)"
env -u PS5_HOST PS5_TEST_BINARY_ADDON=1 \
  cmake --build build/ps5-release --target ps5-title
touch build/ps5-stage/app/dist/PPSA99420/kodi-addon-debug
```

Use the same `BUILD` override for the probe and packaging if using a nondefault
build directory. Packaging requires a complete Kodi build and regenerates
staging; it is not a data-only incremental upload. `PS5_TEST_BINARY_ADDON`
defaults to `0`. When set to `1`, packaging rejects missing/modified/stale probe
outputs before clearing staging, then copies the verified manifest and `.so`
after Kodi installation. Ordinary packaging does not opt into this hook.

The final title contains:

```text
PPSA99420/
  kodi-addon-debug
  test-binary-addon-build.txt
  share/kodi/addons/test.binary.ps5/
    addon.xml
    test_binary_ps5.so
```

`test-binary-addon-build.txt` records the build ID and artifact hashes.
The console sandbox library path is
`/mnt/sandbox/PPSA99420_000/app0/share/kodi/addons/test.binary.ps5/test_binary_ps5.so`.

Close Kodi completely, upload the finished title using the existing FTP/manual
workflow to `/data/homebrew/PPSA99420`, restart ShadowMountPlus as usual, then
launch Kodi. Wait for upload completion before launch.

The marker must be created **after** packaging because staging is regenerated.
If using `PS5_HOST=...` on the packaging target, its automatic FTP upload happens
before the `touch` above: upload the marker separately after that upload.
Mirroring can delete console-only markers. Do not assume that an old marker
survived, or that placing it in staging before packaging will work.

## Console smoke test

`kodi-addon-debug` enables `KODI_PS5_ADDON_DEBUG` at startup independently of
`kodi-debug` and video diagnostics. Restart after adding/removing it. Leave
video diagnostic/comparison switches off for this test.

Capture full startup-to-teardown kernel output and Kodi's file log. The latter
is `$HOME/.kodi/temp/kodi.log`; use the mounted HOME shown in startup output
rather than guessing its FTP path. The regular kernel sink is asynchronous and
bounded, so queued lines may be missing after a crash. Crash-sensitive direct
diagnostic phase markers help distinguish entry from successful return.

1. Confirm the diagnostic switch and intended title/probe identity.
2. Find/enable **PS5 Binary Loader Test** in Kodi's screensaver/add-on settings.
   If absent, inspect the deployed files, dependencies/platform, disabled state
   and any user-installed shadow copy. Do not reset save data or add-on databases.
3. Select it under **Settings -> Interface -> Screensaver** and use **Preview**.
   Discovery and selection alone do not load or execute the binary.
4. Confirm load/constructor/export/API/create phases and every probe PASS,
   followed by `first Render` and `STAGE 2 RUNTIME PASS` with the matching build
   ID. A log-only screensaver has no test image; callbacks are the evidence.
5. Exit Preview with the controller. Confirm `Stop`, instance/add-on destruction
   and loader release/unmapping when the reference lifecycle permits it.
6. Repeat Preview three times, then restart Kodi and repeat once. Look for
   stable teardown and no accumulating live images.
7. Remove the marker from both local title and console copy, restart, repeat a
   preview with diagnostics off, and restore the previous screensaver setting.

A failed mandatory probe reports FAIL and returns a real Create/Start failure.
Runtime PASS is emitted only after the mandatory checks and first Render;
it does not replace the later teardown/repeat/restart acceptance criteria.
After a crash, collect the fault report and full logs before trying again.
Preserve separate run logs rather than overwriting the only comparison copy.

## Failure triage

| Last successful phase / symptom | Inspect first |
| --- | --- |
| Not listed | Final/deployed files, manifest/dependencies, disabled state, shadow copy |
| Cannot locate/read library | Resolved Kodi path, filename/size/permissions |
| Mapping/protection failure | errno, actual page size, segment overlap, W^X transition |
| Unresolved import/relocation | Exact symbol/type/offset, host import encoding/GOT, dependency assumptions |
| Constructor call does not return | Fault address, module base/load bias, matching unstripped `.so` |
| API/export rejection | Dev-kit exports and matching global/instance versions |
| Create/Start failure | Function-table boundary/status and first probe FAIL |
| Exception terminates | Unwinder imports and actual frame-registration outcome |
| Exit/repeated-preview failure | Instance lifetime, references/callbacks and unmapping |

For an address in the add-on, subtract the logged load bias and symbolize it
against that run's unstripped `test_binary_ps5.so`, not a newer rebuilt binary.
Host/title faults require the matching unstripped native title ELF instead.
The last asynchronously queued log message alone is not an exact fault site.

## Existing full binary-add-on builder (follow-on)

See the maintained [PS5 binary add-on status](../../../../docs/PS5-BINARY-ADDONS.md)
for per-add-on build results, console acceptance and unresolved limitations.

### Automatic installed binary add-on deployment

Ordinary `ps5-title` packaging automatically discovers binary manifests in
`share/kodi/addons` and primary libraries in `lib/kodi/addons` under these roots:
`build/ps5-depends-test/x86_64-unknown-freebsd-debug`,
`build/ps5-audiodecoders/install`, and `build/ps5-screensavers/install`.
`PS5_ADDON_PREFIXES` replaces that list with colon-separated installed prefixes;
an explicitly empty value disables discovery. There is no recursive scan of
unrelated depends libraries and no compilation of the binary-addons `all` target.
Missing default roots are reported, but missing explicit roots fail.
The manifest ID names the destination; its full resource tree and validated
primary/helpers are copied together. Duplicate IDs (including across prefixes),
invalid manifests/ELFs, resource escapes and destination collisions fail.

`tools/ps5/addon-exclusions.json` explicitly records tracker-selection exclusions
(DUMB/ModPlug), known TLS-blocked decoders (Timidity/UPSE/vgmstream), and emutls
screensavers (RSXS Colorfire/Hufotunnel). Selection and exclusions are printed
and recorded in `<BUILD>/installed-addons-report.json` before staging, then
`<STAGE>/installed-addons-report.json` after staging.
The 29 other screensavers are packaging candidates only, not console passes.
Old staged-title GL export misses are not blanket exclusions: the selected GL
imports produce real link anchors and export names, extracting existing
`libglapi_bridge.a` definitions through the normal GL dependency group.
The final original-title import preflight remains mandatory and unchanged.

Unset `PS5_AUDIO_DECODERS` means automatic discovery. An explicit value restricts
only audio to those space-separated IDs; an empty value excludes all audio.
Explicit DUMB/ModPlug selection overrides tracker policy, never ELF validation.
`PS5_AUDIO_DECODER_PREFIX` / `PS5_ADAPTIVE_PREFIX` replace their default roots
only when the generic prefix list is unset.
See [the packaging/status record](../../../../docs/PS5-BINARY-ADDONS.md) for details.
This stages installed artifacts; it does not build add-ons or prove playback.

Installed `lib*.so` companions (excluding symlinks) are staged beside each
primary and included in the import/export preflight. Staging rejects any
primary or companion that is not an x86-64 shared object, contains `PT_TLS` or
`DT_NEEDED`, or uses relocations other than `RELATIVE`, `64`, `GLOB_DAT` and
`JUMP_SLOT`. When companions exist, staging generates `ps5-auxiliaries.list`:

```text
format 1
primary audiodecoder.wsr.so.22.0.3
helper libin_wsr_0.so
helper libin_wsr_1.so
helper libin_wsr_track.so
```

At load, `CPS5AddonLoader` reads this file from the primary's directory. Its
`primary` entry must equal the loaded file name; helper entries are exact
basenames (max 64). Malformed manifests fail the add-on load; absent manifests
grant no helper scope. Installed add-on data may not supply its own manifest.
The manifest is part of the packaged title, so the package is the trust root,
as for the primary binary. No add-on names are compiled into Kodi: a new
decoder following this layout needs only building and packaging, though new
imports still require repackaging the title and console validation.
Per-image executable import thunks route `dlopen`, `dlsym`, `dlclose` and
`dlerror` without changing native title calls or relying on caller return
addresses. Helpers bind their defined symbols to their own image; separate
basenames and separate parents retain independent emulator state. Repeated
opens within one parent are refcounted. Parent finalizers run while helpers
remain available, then helpers are finalized before the parent is unmapped.
Instances and worker threads must still be quiescent before unloading.
ELF TLS, general `DT_NEEDED` dependencies and helper-to-helper loading remain
unsupported. WSR and Nosefart playback passed on console (firmware 9.40) with
manifest-authorized helpers.
Their bundled extended `.m3u` files (`file::TYPE,track,...`) are NEZplug/GME
syntax that Kodi's playlist parser does not support on any platform.

`python3 tools/ps5/test-stage-installed-addons.py` checks generic discovery,
manifest-ID mapping, full resources, duplicates, invalid manifests/ELFs,
exclusions/overrides, helper policy and import-list/export generation.
`python3 tools/ps5/test-stage-audio-decoders.py` retains the legacy audio checks
for generated manifests, symlink exclusion and TLS/`DT_NEEDED` rejection.
The focused loader suite also checks manifest parsing, mapped helper state/pointer relocations,
optimized tail calls, path restrictions, native handles, concurrent reference
counts, per-thread errors and parent/helper teardown. Host checks do not prove
PS5 playback. The PS5 shim rejects directory-relative `openat` with `ENOSYS`;
the loader then uses the validated absolute path with `O_NOFOLLOW`, checking
the pinned directory's device/inode before and after opening. Packaged add-on
directories must remain trusted and immutable during loading. Regression tests
exercise this fallback and reject directory replacement.
Rebuild Kodi before packaging the new loader; the existing
decoder binaries do not need rebuilding:

```sh
cmake --build build/ps5-release -j"$(nproc)"
```

```sh
PS5_HOST=192.168.178.44 PS5_FTP_PORT=2121 \
  cmake --build build/ps5-release --target ps5-title
```

Kodi already provides the general builder:

```sh
make -j"$(nproc)" -C tools/depends/target/binary-addons
```

Its `ADDONS` default is `all`; an initial selected build can use, for example:

```sh
make -j"$(nproc)" -C tools/depends/target/binary-addons ADDONS=inputstream.adaptive
```

It uses `cmake/addons`, the configured depends toolchain and add-on definitions,
not the local smoke probe, which has no definition entry in that system. In this
workspace the generated depends configuration selects FreeBSD/PS5. That is not
proof every add-on will compile, meet this loader's constraints, be packaged in
the title, or run on-console.

The PS5 binary-add-on toolchain uses `CORE_SYSTEM_NAME=freebsd` for supported
add-on metadata (`library_freebsd` and `<platform>freebsd</platform>), while
`CORE_PLATFORM_NAME=ps5` identifies the console-specific build. It selects the
SDK's FreeBSD compiler target and `.init_array` constructors, and links shared
objects with LLVM lld rather than the SDK's executable linker script. Shared
links use `-nostdlib`, 16 KiB segment alignment and an unwind program header;
the add-on imports the title runtime instead of embedding another C++ runtime.
PS5 add-on test executables are disabled because they cannot run on the build
host. These settings are in `tools/depends/target/Toolchain_binaddons.cmake.in`;
an older generated toolchain must be regenerated before rebuilding.

The corrected `inputstream.adaptive` build subsequently passed the console
scenarios recorded below; this is not acceptance of every newly merged title.
The export/unload mechanism addresses its named runtime imports and global
destructor lifetime. Packaging rejects
selected binaries whose strong imports are not safely resolver-visible in
the final original LLVM dynsym/GOT. This is not a playback or DRM test.

### Next user build/package step: selected adaptive, non-DRM only

Console result (firmware 9.40, 2026-10-14): probe 0.0.5 passed, and direct DASH
played with sound, including a transition from 1080p to hardware-decoded 4K.
The first stop froze Kodi after audio shutdown and `Closing stream player 2`;
a retry on the same deployed build completed decoder release, add-on destruction,
unwind deregistration and unmapping in about 124 ms. The freeze is intermittent.
The video buffer-timeout warning occurred in both runs and is not a root cause
by itself. An instrumented quick-stop freeze subsequently reached adaptive's
`worker-join-enter` for audio stream 1002 after reader completion and buffer reset,
without worker exit. Video shutdown had not started.

The worker shutdown patch addresses a cross-thread plain-bool data race and a
missed-wakeup window: exit is now atomic and published under the state-wait mutex,
before Reset wakes the segment wait. A second exit check under the download mutex
prevents buffer access if disposal wins that lock. Worker join and safe unload
are retained. The logs establish the blocked join, not the worker's exact wait.
Console acceptance now passes on firmware 9.40: repeated quick stops/replays,
minute-long runs across Kodi restarts, user-reported seeking/fast-forward/backward
and pause, and a diagnostics-off run with 1080p -> 4K playback and clean stop.
The diagnostics-off file log contains no add-on phase markers and confirms
adaptive destruction and return to the GUI. These are the tested PS5 scenarios,
not exhaustive cross-platform or live-stream validation. YouTube's local
manifest HTTP-server connection failure remains a separate issue.

ARTE +7 additionally passed user-reported playback, seeking, pause/resume and
stop tests. Some programmes reported unavailable streams; their cause has not
been established.

PS5 binary-addons configuration automatically applies
`tools/ps5/patches/inputstream-adaptive-ps5.patch` to extracted adaptive sources
before configuring the add-on. Other platforms/add-ons are untouched. The patch
combines the shutdown fix and opt-in lifecycle diagnostics and rejects
incompatible or partially patched sources instead of silently building unpatched
code. For an existing configured adaptive build, use:

```sh
bash tools/ps5/build-adaptive-addon.sh
```

This uses the same idempotent patch applicator as the automatic build integration
and builds/installs only the existing configured add-on.
It does not reconfigure the binary-addons wrapper or deploy files.
`PS5_ADDON_BUILD` selects an alternate configured wrapper.
`python3 tools/ps5/test-adaptive-worker-shutdown.py` runs a host regression
checking clean patch application/idempotence/conflict rejection and extracting
the patched synchronization code, covering both waits, 1000 joined
workers and the buffer-reset/download-lock race without network downloads.
Both core and adaptive markers use `KODI_PS5_ADDON_DEBUG`, enabled by the
automatic staging marker below. Preserve the complete Kodi file log before
closing a frozen run: `enable-stream-enter/exit` distinguishes the add-on
callback from `video-close-enter` and `video-drain-enter/exit`.
`[PS5AdaptiveStop]` markers distinguish download-mutex acquisition, asynchronous
reader completion, buffer reset, worker join and reader reset. These log only
lifecycle transitions, not frame or download loops. The existing video-buffer
timeout alone is not proof of a decoder teardown failure.

Other add-ons get a patch series: PS5 configuration applies every
`tools/ps5/patches/addons/<addon-id>/NNNN-name.patch` in order through
`tools/ps5/apply-patch-series.cmake`, which records applied patches and their
hashes in `<source>/.ps5-applied-patches` and refuses a tree whose recorded
patches changed. Each patch is applied with `apply-source-patch.cmake` at fuzz
0. [`tools/ps5/patches/addons/README.md`](../../../../tools/ps5/patches/addons/README.md)
lists the series and their upstream status. PS5 configuration also pins
`CMAKE_INSTALL_LIBDIR=lib/kodi`, so add-ons using GNUInstallDirs install to
`lib/kodi/addons/<addon-id>`.

From this repository root in Linux/WSL, with the already installed corrected
adaptive build:

```sh
bash tools/ps5/build-test-addon.sh
cmake --build build/ps5-release -j"$(nproc)"
PS5_ADAPTIVE_TEST=1 PS5_TEST_BINARY_ADDON=1 \
  PS5_HOST=192.168.178.44 PS5_FTP_PORT=2121 \
  cmake --build build/ps5-release --target ps5-title
```

`PS5_ADAPTIVE_TEST=1` requires adaptive (whose installed manifest/resources/binary
and exports are now included automatically), stages the direct DASH Python test, and adds the
`kodi-addon-debug` marker **before automatic upload**. `PS5_ADAPTIVE_PREFIX`
overrides the installed prefix if needed. Do not disable `PS5_HOST` or manually
merge files after uploading: packaging regenerates staging. The title-side
wrapper fix alone needs no adaptive rebuild; the stop diagnostics require the
instrumented adaptive installed by the helper above. `PS5_ADDON_ELFS` remains
an exports-only option for additional trusted binaries, not an automatic stager.

On-console, first run the version `0.0.5` exception/thread/reload probe, then
the direct DASH test: actual audio/video for at least 30 seconds, stop, replay,
clean teardown and restart/replay. Capture
`phase=host-resolver-wrapped symbol=pthread_setspecific` (a title address, not
the raw libkernel address), the thread/TSD PASS marker and `phase=finalizers-*`
diagnostics. Missing
exports, constructors, API mismatch, worker teardown and playback behavior
remain real failures to investigate, not reasons to add placeholder symbols.
`dlopen`/`dlsym`/`dlerror` resolve only to existing title imports; this change
does not provide a Widevine loader. DRM/Widevine, TLS and general dependencies
remain unsupported.

The wrapper uses a per-platform `.installed-*` stamp; changing `ADDONS` after a
completed build does not itself invalidate that stamp. Inspect its generated
configuration/output and `cmake/addons/.success`/`.failure` records before
treating completion as success. Its configure recipe also cleans untracked
files in `cmake/addons`; preserve local work there before using it.

After the smoke test passes on-console, choose one real add-on, audit its
imports/TLS/relocations/dependencies/API versions, and reuse this builder where
compatible. Full TLS/dependency support, inputstream playback and
DRM/Widevine are separate milestones, not consequences of a passing probe.
