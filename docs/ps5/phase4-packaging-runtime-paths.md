# Phase 4 progress: preserve packaging and runtime paths

Date: 2026-10-04

## Outcome

Phase 4's "Work" checklist was audited bullet-by-bullet against the fork as
already built up through Phase 3, plus one concrete gap (build manifest) that
was closed in this pass.

## Checklist

1. **Separate staging/packaging from transfer to the console.** Already true:
   `tools/ps5/package.sh` stages (`cmake --install` + native-app link) and
   zips entirely on the host; `PS5_HOST`/FTP upload is opt-in and only runs
   after a complete local artifact exists.
2. **Preserve entry point/executable conversion/RELRO.** Already true: the
   script reuses `ps5-opengl`'s native-app boilerplate (its linker script,
   ELF -> FSELF converter, clean-room `libc.prx`) rather than re-deriving
   these from scratch; see `docs/ps5/phase3-native-configure-build.md`'s
   `ps5-title`/`ps5-package` evidence.
3. **Stage title metadata, executable, runtime resources, CA bundle, Python
   stdlib, add-on assets.**
   - Title metadata/executable/runtime resources: already staged (`param.json`
     generation, `icon0.png`, `share/kodi` copy - see `package.sh` steps 1-6).
   - CA bundle: confirmed staged automatically. `cmake/installdata/common/
     certificates.txt` installs `system/certs/*` as part of every platform's
     ordinary `share/kodi` tree (not PS5-specific, not excluded for PS5), so
     `cacert.pem` ships without any extra packaging step.
   - Python standard library: confirmed staged. `cmake/scripts/ps5/
     Install.cmake` installs `${CMAKE_SYSROOT}/user/homebrew/lib/python3.14`
     into `share/kodi/python/lib` whenever `ENABLE_PYTHON` is on.
   - Add-on assets: covered by the same `cmake --install` step as any other
     Kodi platform (no PS5-specific exclusion found).
4. **Retain `/app0` and `/download0/.kodi` path semantics.** Already true and
   explicitly commented in `package.sh` (`downloadDataSize` sizing, "/download0
   = Kodi's home"); unchanged in this pass.
5. **Keep sandbox-dependent USB/local access optional and observable.**
   Already true: `xbmc/platform/ps5/storage/PS5StorageProvider.cpp` polls a
   fixed `/mnt/usbN` list, logs each mount/removal via `CLog`, and simply
   finds nothing if no USB sandbox access is granted - no hard dependency,
   no crash/blocking path. Network sources (SMB2, UPnP, curl/HTTP) do not
   depend on USB or any optional daemon.
6. **Build manifest with source/dependency revisions, feature config, artifact
   hashes.** **Gap found and closed this pass.** Previously `package.sh` only
   wrote the release zip's own `sha256sum`. Added a `<TITLE_ID>-BUILD-
   MANIFEST.json` written next to the zip in `$ARTIFACT_DIR`, containing:
   - `kodi_source_revision` (`git describe --always --dirty` against the
     fork's own worktree - not the reference port's).
   - `feature_config`: `ENABLE_PYTHON`/`ENABLE_UPNP`/`ENABLE_OPTICAL`/
     `ENABLE_DVDCSS`/`ENABLE_AIRTUNES` read from the build's own
     `CMakeCache.txt` (so the manifest reflects what was *actually* built,
     not just the platform defaults).
   - `dependency_versions`: best-effort, parsed from each qualified
     `tools/depends/target/*/​*-VERSION` file (`dav1d`, `ffmpeg`, `python3`,
     `curl`, `libsmb2`, `brotli`, `libiconv`) when present; `null` if a
     recipe was never built via `tools/depends` in this environment (i.e.
     still sourced from the external pacbrew sysroot - honestly reported,
     not guessed).
   - `artifact`: the zip's filename and the same sha256 already computed.
   - Debug/symbol output: unchanged in this pass - `kodi.bin` already
     carries full `-g` symbols in the existing release build configuration;
     no separate stripped/symbols split exists yet and none was requested.

   Validated by running the new manifest-generation step directly (without a
   full rebuild) against the existing `build/ps5-release` + `build/ps5-stage/
   artifacts` from Phase 3's prior successful build; produced a well-formed
   JSON with real values (`ENABLE_PYTHON: "ON"`, real dependency versions,
   real zip sha256). Confirmed via `bash -n` and `python3 -m py_compile` that
   the new script section and its embedded Python block are syntactically
   valid before relying on the runtime test.

## Exit gate status

"Packaging creates a complete installable folder title and release ZIP from
the fork, with no reference-port checkout or prebuilt graphics demo required
[other than the documented `ps5-opengl` app template, which Phase 1A already
qualified as an external prerequisite, not a reference-port dependency]."
This was already satisfied as of Phase 3; this pass adds the one remaining
unmet "Work" bullet (build manifest) and confirms the rest. **Phase 4 is
complete** except for on-device verification (console deployment is called
out explicitly as a later, separate action, consistent with Phase 5's own
console-dependent scope).
