# PS5 binary add-ons handover

Updated: 2026-10-14, following console acceptance and build-integration cleanup.

## Current result and blocker

The tiny official-ABI screensaver probe passed console acceptance, including
C++ exceptions, repeated unload/reload, a full restart and diagnostics-off use.

The real `inputstream.adaptive` add-on now builds in a loader-compatible ELF
format. Console file-log evidence confirms loading, relocation, constructors,
API compatibility, add-on/instance creation and clean teardown on a failed
YouTube manifest download.

Direct non-DRM DASH playback reproducibly crashed before the adaptive worker
body ran. Matching firmware 9.40 disassembly establishes a null module-record
dereference during the libc++ proxy's first `pthread_setspecific` call.
A title-side non-tail-calling wrapper is implemented and **console verified**:
probe 0.0.5 passed thread/TSD and destruction checks. The user confirmed picture
and sound; logs show 1080p playback and an adaptive transition to hardware-decoded
3840x2160 video.

**Resolved blocker: intermittent freeze when stopping playback.** The first run
stopped after `Closing stream player 2`, followed by a video output-buffer timeout.
A second run on the same deployed build stopped successfully: both threads exited,
decoder memory was released, the instance/add-on were destroyed, unwind metadata
was deregistered and the image unmapped. Stop-to-GUI return took about 124 ms.
The buffer-timeout warning occurred in this successful run too, so it alone does
not establish the freeze's cause. An instrumented freeze now localizes the hang
to adaptive's download-worker join. A synchronization fix is implemented;
repeated-stop, longer playback, restart and diagnostics-off console validation
now pass. The earlier successful retry was not caused by
the new logging.

### Latest stop investigation and next run

- Cleanup at user time 18:32: stopped the TCP capture; no capture remains running.
  Consolidated the shutdown fix and opt-in diagnostics into
  `tools/ps5/patches/inputstream-adaptive-ps5.patch`. PS5 binary-addons configure
  applies it automatically before configuring adaptive; other platforms and
  add-ons are unchanged. `build-adaptive-addon.sh` uses the same CMake applicator
  for an existing configured build. Conflicts and mixed patch states fail
  explicitly. The installed/console-accepted binary is unchanged by cleanup.
  Firmware, console/file logs and the accepted playback binary/source snapshot
  are preserved; temporary validation build directories are removed.
- At user time 18:28 the user reported successful playback of some streams in
  the real Arte +7 add-on. Other selections reported "stream not available";
  their cause is not established (do not assume regional restrictions or a
  platform defect without corresponding logs). This extends manual playback
  evidence beyond the direct DASH test plugin. The user also confirmed having
  already tested seeking, pause/resume and stop within Arte +7 successfully.
- Diagnostics-off acceptance passed at user time 18:20. Saved log:
  `build/ps5-adaptive-debug-off-20261014-1820.kodi.log`. It contains no
  PS5AddonDebug or PS5AdaptiveStop markers, shows 1080p -> hardware-decoded 4K
  playback and clean adaptive destruction / GUI return at 18:53:38.
  The user reports normal behavior. This establishes that the observed stop
  recovery does not require diagnostic logging. Non-DRM adaptive playback is
  accepted for the tested PS5 scenarios; YouTube's local manifest HTTP-server
  failure and upstream/cross-platform validation remain separate work.
- At user time 18:16 the user reported both requested minute-long play/stop/
  close/restart runs completed without freezing. The live capture confirms
  clean video exits and adaptive unmaps at 18:47:52.186 and 18:49:15.583.
  Retrieved `build/ps5-adaptive-restart-20261014-1816.kodi.old.log` contains
  the second run, both worker joins completed, 4K decoding and normal
  `Application stopped` / `Exiting the application` at 18:49:22.
  The adjacent `.kodi.log` snapshot is the subsequent session, not the playback
  evidence. Core non-DRM playback/stop/replay/restart acceptance now passes with
  diagnostics enabled. At that point diagnostics-off acceptance was pending;
  it subsequently passed (see above). Wider upstream scenarios
  (including live streams and other platforms) are not yet established.
- At user time 18:17 the user also reported fast-forward, seeking, backward
  navigation and pause working in the direct adaptive test. These are manual
  smoke-test results on this PS5 build, not exhaustive upstream validation.
- Fixed-build console run at user time 18:10: repeated playback/stop without a
  reported freeze. Saved full log: `build/ps5-stop-fixed-20261014-1810.kodi.log`.
  It contains 28 worker join entries and 28 exits, 28 worker-body exits and
  14 successful adaptive image unmaps. Exit requests precede segment reset,
  confirming the fixed binary is active; playback includes 3840x2160 decoding.
  Capture `build/ps5-console-fw9.40-20261014-1808.log` is preserved and stopped.
- Capture: `build/ps5-console-fw9.40-20261014-1740.log`.
- Full file log retrieved before closure:
  `build/ps5-stop-freeze-20261014-1747.kodi.log`.
- Successful retry file log retrieved over FTP:
  `build/ps5-stop-success-20261014-1826.kodi.log`. Stop at 18:26:11.901,
  `unmap-exit result=ok` at 18:26:12.021 and GUI `finished waiting` at
  18:26:12.025. This run used the earlier deployed title, not the new diagnostics.
- Latest capture: `build/ps5-console-fw9.40-20261014-1757.log`.
  Several instrumented play/stop cycles succeeded, then froze on a quick stop.
  File log: `build/ps5-stop-freeze-instrumented-20261014-1759.kodi.log`.
  At 18:33:13.072, audio stream 1002 completed the download mutex, reader wait
  and buffer reset, then reached `worker-join-enter` without worker-body/ join
  exit. Video close had not started. The exact worker waiting location is not
  captured, but the blocking join is established.
- The user closed Kodi. The TCP 3232 `nc` capture was stopped at the user's
  request; **no capture is running**.
- Core disables the demux stream before calling the video player's CloseStream.
  Adaptive disable waits for download locking, an asynchronous sample reader,
  and worker disposal/join. The new markers distinguish these phases.
- New PS5-only core markers bracket `enable_stream` and video close/drain.
  `tools/ps5/patches/inputstream-adaptive-ps5.patch` adds markers
  around adaptive download locking, reader completion, buffer reset, worker exit
  notification/join and reader destruction. All use `KODI_PS5_ADDON_DEBUG`;
  they are lifecycle-only, not per-frame logs.
- The same consolidated patch fixes concrete synchronization
  defects: the exit flag was a plain bool read across threads; shutdown notified
  cvState without its wait mutex; buffer Reset woke the segment waiter before
  exit was published. RequestExit now stops downloads, publishes an atomic exit
  flag under mutexWorker and notifies cvState. Dispose requests exit before
  Reset wakes the segment waiter. The worker rechecks exit after acquiring
  mutexWorker before accessing buffers. Joining still precedes deletion/unload;
  there are no detached threads or timeout-based success fallbacks.
- `bash tools/ps5/build-adaptive-addon.sh` applies the consolidated patch to the
  existing selected source tree, rebuilds only adaptive and installs it into the
  configured prefix. Clean PS5 binary-addons configurations now apply the patch
  automatically; downloaded source edits alone are not persistent.
  Override `PS5_ADDON_BUILD` only for a different configured wrapper directory.
- The fixed and instrumented adaptive is already rebuilt and installed locally.
  The original accepted playback binary and source snapshots are preserved in
  `build/ps5-stop-trace-originals`. No new full Kodi build or deployment was run.
  Both changed core translation units compile with the existing PS5 flags.
  Native import/export and conversion checks pass for the instrumented adaptive
  (233 strong imports), preserving original LLVM metadata. Patch checks cover
  exact source reproduction, repeat detection and rejection of mixed patch states.
- `python3 tools/ps5/test-adaptive-worker-shutdown.py` extracts the real worker
  wait/predicate checks, THREADDATA exit/destructor, Dispose and segment wait/reset
  code into a host fixture. Downloads and Kodi logging are replaced; the fixed
  synchronization is not. It checks mutex-protected publication, both waits,
  1000 joined workers and exit between the fast check and download-lock acquisition.
- Next work is to be discussed: the user wants to explore building other Kodi
  binary add-ons. Do not start an all-addons build/deployment without agreeing
  scope. The current non-DRM adaptive milestone is accepted, not universal TLS,
  shared-library dependency or DRM support.

No commits were made. The working tree contains all changes described below.
Preserve the existing dirty work. The user performs full Kodi builds,
packaging, deployments and console tests.

## 2026-10-14 diagnosis and implementation

- The preserved root `kodi.old.log` is the repeat crash run: thread 102169,
  manifest parsed successfully at 00:00:12.379, then stream 1001 opening.
- The user supplied firmware version **9.40** and authorized decrypted FTP
  retrieval. The module is saved locally in
  `build/ps5-firmware/9.40/libkernel.sprx` (545800 bytes), with firmware/source/
  SHA-256 in adjacent `libkernel.metadata.json`. It is Git-ignored and not a
  packaging input. Do not redistribute it or infer other firmware compatibility.
- SDK NIDs identify `pthread_setspecific` at module offset `0x40de0`.
  The add-on instruction immediately before saved frame `0x13ac4a` calls this
  import; its `AdaptiveStream::worker` invocation comes later.
- `pthread_setspecific` calls the first-use TSD allocator at `0x40d10`;
  that calls `0x40fa0`, which walks caller frames and obtains the add-on return
  address. The module lookup at `0x34080` searches registered system-module
  segments and returns null for the manually mapped add-on.
  At `0x40fcb`, `movl 0x70(%rax), %eax` faults with `rax=0`, exactly matching
  the capture and both saved kernel frames.
- New `shims/native-app/pthread_tls.c` provides
  `__wrap_pthread_setspecific`, marked `noinline, disable_tail_calls`.
  It forwards unchanged arguments/results to the real system function but
  keeps its return address in registered title text. No firmware addresses,
  kernel modification, synthetic TLS or new dependencies are used.
- `package.sh` copies/links the shim with `--wrap=pthread_setspecific`;
  export generation publishes it, preflight requires it for selected imports,
  and resolver diagnostics now report `phase=host-resolver-wrapped`.
- `test-pthread-tls.sh` passes an optimized manually mapped add-on regression
  with an unwrapped negative control, per-thread values, clear/reset,
  unchanged error forwarding, exit destructors and unload/reload. This models
  system caller validation on Linux, not execution of Sony's kernel.
  SDK disassembly confirms a real call/return rather than a tail jump.
- Existing loader lifecycle checks and isolated PS5 HostExports compilation pass.
  Native export/conversion checks pass with both adaptive and the new probe
  selected (276 strong imports), retaining original LLVM metadata.
- The screensaver probe is now **0.0.5**, adding a joined `std::thread` and
  TSD value/exit-destructor checks before the PASS marker. An isolated build
  produced build ID `40a9d9e9e22d2ff4` and was subsequently accepted on console.
  The temporary isolated probe build was removed during cleanup; the current
  normal build and deployed probe are preserved.
- The user subsequently built and deployed the title and completed probe 0.0.5
  and successful adaptive playback; see the latest stop investigation above.

## Environment and evidence locations

- Windows workspace: `\\wsl.localhost\Ubuntu-24.04\home\raoul\kodi-fork`.
- Actual repository: child directory `xbmc-ps5-fork`, branch `ps5`.
- Linux repository: `/home/raoul/kodi-fork/xbmc-ps5-fork`.
- Use WSL Ubuntu-24.04 for SDK/compiler/build commands.
- SDK: `/opt/ps5-payload-sdk`, LLVM 18.
- Kodi build: `build/ps5-release`.
- Final title: `build/ps5-stage/app/dist/PPSA99420`.
- Native unconverted ELF: `build/ps5-stage/app/build/llvm-pie.elf`.
- Console FTP title: `/data/homebrew/PPSA99420`.
- Console mounted title: `/mnt/sandbox/PPSA99420_000/app0`.
- Writable Kodi home shown in logs:
  `/mnt/sandbox/PPSA99420_000/download0/.kodi`.
- Historical kernel capture: `D:\PS5\output.log`. The latest saved capture is
  `build/ps5-console-fw9.40-20261014-1740.log`; neither capture is running.
- The workspace-root `kodi.log` is an earlier file-log copy, 352,011 bytes,
  copied at Windows time 23:10:11 and ending at console time 23:41:53. It
  contains the YouTube failure and clean adaptive teardown, not the direct
  DASH crash. Do not mistake it for the latest crash-run file log.
- At the last inspection there was no `D:\PS5\kodi.log`.
- Console timestamps run roughly 33 minutes ahead of the user's wall clock.
  Correlate by attempt/marker, not wall-clock equality.

The tail was started using `Get-Content D:\PS5\output.log -Tail 30 -Wait`,
session ID `ps5-adaptive-live`, and stopped at the user's request at approximately
23:29 Europe/Amsterdam. No live tail is left running. Start a fresh tail only
when requested. No new retry is required to establish reproducibility.

Kernel logging is asynchronous and can overflow. The last queued resolver
message is not necessarily the last executed operation or the fault site.

## Work completed

### 1. Official ABI probe and diagnostics

Replaced the old handwritten test ABI with official Kodi dev-kit
`CAddonBase`, `CInstanceScreensaver` and `ADDONCREATOR`.
The probe is `addons/test.binary.ps5`, manifest version `0.0.4`.

Added:

- `tools/ps5/build-test-addon.sh`: isolated SDK compile/link and ELF preflight.
- `tools/ps5/stage-test-addon.sh`: freshness/hash verification and staging.
- Opt-in `PS5_TEST_BINARY_ADDON=1` packaging, off by default.
- `kodi-addon-debug` startup marker sets `KODI_PS5_ADDON_DEBUG`.
- Loader/API/lifecycle diagnostics and direct constructor phase markers.

Console acceptance completed: heap/virtual dispatch, Kodi callbacks,
throw/catch, Start/Render/Stop, destruction, unwind removal and successful
unmapping; three previews, restart, and diagnostics-off restart.

Probe build ID: `8f4369782362ef0a`.

### 2. Host metadata and unwind fixes

Three distinct failures were fixed during probe testing:

1. Backward ELF-header scanning dereferenced unmapped memory. Native title
   headers are not mapped. Packaging now supplies hidden linker image bounds.
2. Native conversion dropped/zeroed original LLVM dynamic metadata.
   `native-app-preserve-addon-metadata.patch` keeps it mapped, bounds dynsym,
   and reserves RELRO space so process parameters cannot overwrite it.
3. SDK libunwind `__register_frame` registers one FDE, not a whole section.
   PS5 now calls `__unw_add_dynamic_eh_frame_section` and its removal counterpart.
   Packaging retains those archive APIs. Linux/libgcc keeps its existing path.

Preserved metadata addresses are link-time virtual addresses: add the title
image base. Title load bias in these runs is `0x400000`; subtract it only for
addresses within the title before symbolizing against `llvm-pie.elf`.
Do not subtract it from system-library PCs.

### 3. Existing real-add-on builder fixed for PS5

The existing builder remains:

```sh
make -j"$(nproc)" -C tools/depends/target/binary-addons ADDONS=inputstream.adaptive
```

Changes:

- `tools/depends/target/Toolchain_binaddons.cmake.in` has a PS5 branch:
  `CMAKE_SYSTEM_NAME=FreeBSD`, `CORE_SYSTEM_NAME=freebsd`,
  `CORE_PLATFORM_NAME=ps5`.
- SDK compiler uses `--target=x86_64-unknown-freebsd`, SDK C headers after
  libc++ headers, and `-fuse-init-array`.
- Shared/module links use LLVM `ld.lld`, `-nostdlib`, both hash tables,
  unwind header and 16 KiB segment alignment.
- No second C++ runtime embedded and no SDK executable linker script.
- `cmake/addons/CMakeLists.txt` disables add-on test executables on PS5.
- Regenerated local toolchain files and rebuilt/installed adaptive only.
  An older local `config.status` left CPPFLAGS placeholders unsubstituted;
  the ignored generated toolchain was corrected locally. The authoritative
  template change is tracked; do not rely on an old generated toolchain.

Builder caveats: per-platform `.installed-*` stamp does not track an `ADDONS`
selection change; `.success` alone is not compatibility evidence; the wrapper
configure recipe cleans untracked files under `cmake/addons`. Avoid blindly
running its destructive clean/reconfigure path.

Installed prefix:
`build/ps5-depends-test/x86_64-unknown-freebsd-debug`.

Manifest/resources:
`share/kodi/addons/inputstream.adaptive`.

Binary:
`lib/kodi/addons/inputstream.adaptive/inputstream.adaptive.so.22.3.23`.

Version: `22.3.23`; SONAME `.so.23.0`.
Manifest uses `library_freebsd` and platform `freebsd`.

Current binary: 39,080,200 bytes, logged FNV-1a64 `b78aa08670d7747a`.
Four LOAD segments, no RWX, 16 KiB alignment, GNU_EH_FRAME,
18 init-array entries, no DT_NEEDED, no TLS relocations.
232 undefined runtime symbols.
Relocation counts: RELATIVE 3393, 64 4558, GLOB_DAT 1390, JUMP_SLOT 148.

### 4. Runtime exports and scoped finalization

The previous probe-only set of 26 manual C++ runtime references was replaced
by real defined symbols published through preserved LLVM dynsym metadata.

Packaging takes trusted `.so` inputs through `PS5_ADDON_ELFS`, roots needed
archive members, publishes definitions, and creates relocated anchors for
system imports not otherwise referenced by Kodi. It preserves title allocator
and POSIX wrapper policies. This does not fabricate missing implementations.

`native-app-host-runtime-exports.patch` allows the converter to retain original
definitions while producing separate compact Sony import metadata, reindexing
relocations and converting references to host definitions to local relocations.

Host resolver initialization uses `std::call_once`; concurrent lookups cannot
observe partially initialized state.

Loader-owned `__cxa_atexit`/`__cxa_finalize` handles module global destructors.
Unload runs finalizers while unwind data is still registered, then removes
unwind metadata and unmaps. Caller must first quiesce worker threads/callbacks.
Unsupported process/thread-lifetime destructor mechanisms remain rejected.
No general TLS/dependency loader or Widevine support was added.

New helper/test files:

- `tools/ps5/prepare-addon-exports.py`
- `tools/ps5/verify-addon-exports.py`
- `tools/ps5/test-native-addon-exports.sh`
- `tools/ps5/test-elf-loader.sh`
- `tools/ps5/tests/`

Targeted SDK object compiles, isolated export/converter checks and focused
lifecycle tests passed during implementation. Isolated linking exposed all
232 adaptive imports and conversion retained original metadata.
These checks did not establish console streaming/thread compatibility.

### 5. Direct DASH test and automatic staging

Created `addons/plugin.video.ps5.adaptive.test`, version `0.0.1`.
It explicitly sets `inputstream=inputstream.adaptive`, DASH MIME type and
disables content lookup, resolving:

`https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd`

Public unencrypted Big Buck Bunny, H.264/AAC; no credentials, local HTTP server
or YouTube. Log marker: `[PS5AdaptiveTest]`.

ZIP:
`build/ps5-release/test-addons/plugin.video.ps5.adaptive.test-0.0.1.zip`.
The user installed it successfully and reproduced the crash twice.

`package.sh` now supports opt-in `PS5_ADAPTIVE_TEST=1`: selects adaptive
runtime exports, stages its manifest/resources/binary and this Python test,
then adds `kodi-addon-debug` **before the normal automatic upload**.
Optional `PS5_ADAPTIVE_PREFIX` overrides the installed prefix.

The earlier handoff deliberately used `env -u PS5_HOST`, causing a painfully
slow manual full-title transfer. The user explicitly wants future automatic
deployment preserved. Do not repeat manual merge-after-upload instructions
when this opt-in staging hook can do the work before upload.
The new hook has syntax/routing/manifest checks but has not yet been exercised
in a full packaging/deployment run.

## Console findings

### YouTube: complete load/create/cleanup, failed manifest connection

YouTube v7.4.4 had account/API errors, plus repeated
`http_server.get_connect_address()` EIO. Its local server at PS5 IP
`192.168.178.44:50152` refused connections.
Restarting the PS5 did not clear that error.

The copied Kodi file log shows at console time 23:41:23-25:

- All relocations applied; TLS=0, other=0.
- Segment protections successful, load result `ok`.
- Unwind registration and all 18 constructors returned.
- Official ADDON exports found and used API versions compatible.
- Add-on creation `status=0`; inputstream instance creation `status=0`.
- Adaptive entered its Open path and failed downloading YouTube's local MPD:
  `Could not connect to server(7)`, `CURLOpen failed`.
- Instance/add-on destruction returned, finalizers returned, unwind removed,
  unmap result `ok errno=0`.

This proves useful binary lifecycle behavior on that failure path, not adaptive
streaming. The subsequent Apple MOV screensaver used ordinary video playback,
not adaptive.

Other user-tried plugins failed before adaptive:

- WRAL and WSJ call removed `HTMLParser().unescape` (Python 3.14).
- WABC website regex found no match, then called `.group(1)`.
- Popcornflix API response failed JSON decoding.

These plugins were inspected, not fixed. YouTube's network cause is unresolved:
could be configuration/add-on behavior or PS5 Python/socket compatibility.

### Direct DASH: reproducible worker-thread crash

First direct test, console time 23:56:43:

- SIGSEGV thread 101979.
- Fault address `0x70` (read, page not present).
- PC `0x800040fcb`.

Repeat after relaunch, console time 2026-10-14 00:00:11:

- Direct test marker and URL at 00:00:11.685.
- VideoPlayer selected the test and adaptive binary.
- Load thread 102169; crash thread 102172.
- Same SIGSEGV, same fault address and PC.
- All 18 constructor exit markers visible.

Repeat registers:

```text
rax=0                 rbx=0x8000711d0
rcx=0x100             rdx=0x800089b18
rsi=0x7e7d6bf28       rdi=0x29ad3ac4a
rbp=0x7e7d6bf40       rsp=0x7e7d6bf20
r8=0x100              r9=0x9768
r10=0                 r11=0x800081950
r12=0                 r13=0
r14=9                 r15=0x880f550c0
rip=0x800040fcb
```

Backtrace:

```text
0x800040d2c
0x800040e08
0x29ad3ac4a
0x800007c10
0
```

System module report places libkernel text at `0x800000000..0x800048000`.
Thus the fault is inside libkernel, **not title text**.
Title text starts `0x400000`, ending `0x3f9c000` in this report.

Adaptive map/load bias on the repeat is `0x29ac00000`.
Subtracting it from the add-on backtrace frame gives `0x13ac4a`.
`addr2line -Cfipe` against the installed unstripped adaptive binary maps that
frame to libc++ `__thread_proxy`, inlined `__thread_execute`/`__invoke`,
invoking an `adaptive::AdaptiveStream` member function.

At the original handover this did not establish the precise pthread/TLS
operation. The matching firmware analysis above now establishes the
`pthread_setspecific` caller-module lookup failure and exact instruction.
Do not infer cause from the repeated queued typeinfo-resolver log line.

## Next steps

1. Have the user rebuild and package using the normal upload workflow:

   ```sh
   bash tools/ps5/build-test-addon.sh
   cmake --build build/ps5-release -j"$(nproc)"
   PS5_ADAPTIVE_TEST=1 PS5_TEST_BINARY_ADDON=1 \
     PS5_HOST=192.168.178.44 PS5_FTP_PORT=2121 \
     cmake --build build/ps5-release --target ps5-title
   ```

   Firmware 9.40 console FTP is confirmed at this IP/port (drakmor ftpsrv).
   Packaging is opt-in and stages the tests/marker before normal automatic
   upload. The adaptive binary does not need rebuilding for this host fix.
2. Run the updated screensaver probe; verify thread/TSD PASS and reload,
   plus resolver `host-resolver-wrapped symbol=pthread_setspecific` pointing
   into title text. Then run direct DASH: actual audio/video for 30 seconds,
   stop, replay, clean teardown, then restart/replay. Preserve new logs.
3. Investigate any remaining failure from new evidence; do not call the
   host-side checks a console playback PASS.
4. Only after direct non-DRM playback works, return to YouTube's separate HTTP
   server issue. DRM/Widevine and general binary dependencies are later work.

## Primary source files to read

- `xbmc/platform/ps5/elf/{HostExports.cpp,ElfLoader.cpp,ElfLoader.h,PS5AddonLoader.cpp}`
- `xbmc/platform/ps5/elf/README.md` (detailed implementation/runbook)
- `tools/ps5/package.sh`
- `tools/ps5/patches/native-app-preserve-addon-metadata.patch`
- `tools/ps5/patches/native-app-host-runtime-exports.patch`
- `tools/ps5/{prepare-addon-exports.py,verify-addon-exports.py}`
- `shims/native-app/pthread_tls.c`, `tools/ps5/test-pthread-tls.sh`
- `tools/depends/target/Toolchain_binaddons.cmake.in`
- `cmake/addons/CMakeLists.txt`
- `addons/plugin.video.ps5.adaptive.test/{addon.xml,default.py,README.md}`
- Native template/runtime wrapper and thread shim sources selected by package.sh
- Downloaded adaptive `AdaptiveStream` and libc++ thread paths.

Do not add these debugging instructions to `docs/README.PS5.md`.
