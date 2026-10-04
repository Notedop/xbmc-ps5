# Phase 3 progress: native configure/build from tracked fork source

Date: 2026-10-04

## Outcome

Tracked-source PS5 configure/build now runs directly from the migrated Kodi
fork checkout, with no overlay copy or Kodi patch application at configure
time.

Fork checkout location:

- `/home/raoul/kodi-fork/xbmc-ps5-fork`
- branch `ps5`
- baseline `28ea2eac1eb7af8fdcbd2672d933ce87f594be79`

## Implemented

In fork source:

- `toolchain/ps5-kodi.cmake` (tracked in fork)
- `tools/ps5/configure.sh` (native configure entrypoint)
- `docs/README.PS5.md`

The new configure entrypoint:

1. Uses tracked source directly.
2. Uses a tracked toolchain file in the fork.
3. Creates build-local pkg-config wrapper in the build directory.
4. Does not run any source mutation step (`git checkout`, overlay copy, patch apply, or BuildStamp rewrite).

## Build evidence

Commands run against tracked fork source:

1. First configure:
   - `bash tools/ps5/configure.sh`
   - Result: success.
2. Second configure (same inputs):
   - `bash tools/ps5/configure.sh`
   - Result: success.
3. First full build:
   - `cmake --build .../build/ps5-release -j$(nproc)`
   - Result: success (`ninja` completed from `1/1865` to completion).
4. No-op build:
   - Same build command without source changes
   - Result: success, reduced work (`1/8 ... 8/8`).
5. Scoped rebuild check:
   - Touch active PS5 source `xbmc/platform/ps5/audio/AESinkPS5.cpp`
   - Rebuild result: success with scoped work (`1/7 ... 7/7`).

## Ledger updates in this stage

- `SCRIPT-001`: done
- `SCRIPT-002`: done
- `SCRIPT-003`: done (BuildStamp generated under build dir and preferred via include order)
- `SCRIPT-004`: done
- `SCRIPT-005`: done (`ps5-title` CMake target integrated and executed)
- `SCRIPT-006`: done (`ps5-package` CMake target integrated and executed)

## Packaging target integration (follow-on in same workstream)

Packaging is now integrated into the top-level CMake build as PS5-specific
targets in fork source:

- `cmake/scripts/ps5/ExtraTargets.cmake`
- `tools/ps5/package.sh`
- vendored runtime assets under `shims/` and `title/`

Executed build targets:

1. `cmake --build .../build/ps5-release --target ps5-title`
   - Result: success.
   - Produced title folder:
     - `/home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-stage/app/dist/PPSA99420`
2. `cmake --build .../build/ps5-release --target ps5-package`
   - Result: success.
   - Produced package artifact:
     - `/home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-stage/artifacts/PPSA99420.zip`
     - `/home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-stage/artifacts/PPSA99420.zip.sha256`

Notable blocker encountered and resolved:

- Initial `ps5-package` run failed due missing host `zip` binary.
- Resolution: `tools/ps5/package.sh` now falls back to `cmake -E tar --format=zip`
  when `zip` is unavailable.
