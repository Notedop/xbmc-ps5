# PS5 binary add-on status

Last updated: 2026-10-15.

This is the maintained compatibility record for PS5 binary add-ons. Update it
with each build batch and console test, including failures and regressions.
Keep build success, loader eligibility and observed console behavior separate:
a successful compile or host check is not a console pass. Console results below
are smoke tests on firmware 9.40, not exhaustive compatibility claims.

Build and loader details: [ELF loader runbook](../xbmc/platform/ps5/elf/README.md).
Persistent source fixes: [add-on patch series](../tools/ps5/patches/addons/README.md).

## Automatic installed-add-on packaging

Normal `ps5-title` / `ps5-package` packaging discovers installed binary add-ons;
no audio, adaptive or screensaver selector is required, and deployment does not
compile the binary-addons `all` target. From the repository root in Linux/WSL:

```sh
PS5_HOST=192.168.178.44 PS5_FTP_PORT=2121 \
  cmake --build build/ps5-release --target ps5-title
```

Build/install add-ons separately before packaging. The default prefix list is:

1. `build/ps5-depends-test/x86_64-unknown-freebsd-debug` (adaptive);
2. `build/ps5-audiodecoders/install`;
3. `build/ps5-screensavers/install`.

Only `share/kodi/addons/*/addon.xml` and the corresponding
`lib/kodi/addons/<installed-directory>/<library_freebsd>` are inspected in each
prefix. This does **not** recursively scan all depends libraries. The manifest
ID determines the staged directory, even when the installed directory differs
(e.g. `screensaver.microcosm.biof` in `screensaver.rsxs.microcosm`). Complete
manifest data directories, including shaders, language files and other resources,
are copied to `app0/share/kodi/addons/<manifest-id>`, beside the primary ELF and
authorized helpers.

Set one generic, colon-separated `PS5_ADDON_PREFIXES` to replace these defaults
with your own install roots, for example:

```sh
PS5_ADDON_PREFIXES="$PWD/build/my-addons/install:$PWD/build/other-addons/install" \
  PS5_HOST=192.168.178.44 cmake --build build/ps5-release --target ps5-title
```

An explicitly empty `PS5_ADDON_PREFIXES` disables installed-add-on discovery.
Missing default prefixes are reported and tolerated; explicitly listed missing
prefixes fail. Duplicate manifest IDs across directories/prefixes fail, rather
than choosing a version by prefix order. Repeated identical prefix paths are
deduplicated. Unknown invalid manifests/ELFs, missing binaries, unsafe resource
paths, forged helper manifests, unrecognized installed helper files and staging
collisions fail before title linking/upload. Unsupported imports still fail the
existing final `verify-addon-exports.py` check; structural staging alone is not
proof of import resolution or console compatibility.

`build/ps5-release/installed-addons-report.json` records preflight selection;
`build/ps5-stage/installed-addons-report.json` records actual staging, with
exclusion reasons and every selected primary/helper path. Diagnostics also print
the selection, resource-only manifests and missing default prefixes.
`tools/ps5/addon-exclusions.json` is the maintained exclusion registry:

| Excluded installed IDs | Reason |
|---|---|
| `audiodecoder.dumb`, `audiodecoder.modplug` | Intentional format-selection policy: OpenMPT owns tracker playback/metadata |
| `audiodecoder.timidity`, `audiodecoder.upse`, `audiodecoder.vgmstream` | Known ELF/emulated TLS blockers, including partial installed helper trees; not loader-eligible |
| `screensaver.rsxs.colorfire`, `screensaver.rsxs.hufotunnel` | Unsupported `__emutls_v._ThreadRuneLocale` imports; no TLS-loader workaround |

With the current installed trees, automatic preflight selects 46 add-ons:
adaptive, 16 audio decoders and 29 screensavers, plus six audio helper ELFs.
The other 12 screensavers that failed the **old staged-title** export audit
have GL-only export misses, not demonstrated TLS incompatibility. Their GL
imports now participate in normal export-policy/anchor generation; real
definitions exist in the installed `libglapi_bridge.a`. Linking extracts these
definitions from the normal GL dependency group and publishes the requested
names. No placeholder implementations, fake exports or relaxed loader policy
are added. A new full title link/export preflight and console tests remain
the user's next step; none of these 29 screensavers has a console pass.

Compatibility overrides remain available:

- Unset `PS5_AUDIO_DECODERS` means automatic audio discovery. An explicit
  space-separated value restricts only audio decoders; an empty value excludes
  all audio decoders. Explicitly naming DUMB/ModPlug overrides their selection
  exclusions, but cannot override structural/TLS validation.
- `PS5_AUDIO_DECODER_PREFIX` / `PS5_ADAPTIVE_PREFIX` replace their respective
  default roots when `PS5_ADDON_PREFIXES` is unset.
- `PS5_ADAPTIVE_TEST=1` now requires the automatically discovered adaptive and
  adds the DASH test plugin/debug marker, not permission to package adaptive.
- `PS5_TEST_BINARY_ADDON=1` remains the separate, opt-in built loader probe.
- `PS5_ADDON_ELFS` remains a trusted **exports-only** override, not a stager.

Focused checks (no Kodi build, console execution or upload):

```sh
python3 tools/ps5/test-stage-installed-addons.py
python3 tools/ps5/test-stage-audio-decoders.py
bash -n tools/ps5/package.sh
```

Implementation validation on the current installed trees additionally copied
and compared 4,151 resource files and all 52 selected ELF files, generated the
real SDK export policy, assembled its import anchors and performed a relocatable
GL archive link: all 84 selected GL imports resolved to real archive definitions.
This was **not** a final title link/conversion, console run or upload.

## Accepted foundation

| Add-on | Build / loader status | Console evidence |
|---|---|---|
| `test.binary.ps5` | Built and packaged | Official ABI, exceptions, threads/TSD, unload/reload, restart and diagnostics-off accepted |
| `inputstream.adaptive` | Built and packaged with PS5 worker-shutdown patch | Non-DRM DASH audio/video through hardware-decoded 4K; seek, pause/resume, repeated stop/replay, restart and diagnostics-off accepted; Arte +7 also played |

Adaptive acceptance does not establish DRM/Widevine support. YouTube's local
manifest HTTP-server failure remains separate.

## Audio decoders

All 18 decoders below compiled and passed staging preflight. The selected
deployment set contains 16: DUMB and ModPlug are deliberately excluded so
OpenMPT handles tracker formats consistently.

| Add-on | Selected | Console evidence / limitations |
|---|---|---|
| `audiodecoder.2sf` | Yes | Not tested; sample needed |
| `audiodecoder.asap` | Yes | SAP playback passed; large folder scans caused thousands of load/unload cycles and an earlier crash, unresolved |
| `audiodecoder.dumb` | No | Earlier tracker playback worked, but loop termination/seek behavior conflicted with displayed OpenMPT metadata |
| `audiodecoder.gme` | Yes | NSF, NSFE, GYM, VGM, GBS, KSS and HES playback passed; NSFE names/lengths/track switching passed on 2026-10-15 |
| `audiodecoder.gsf` | Yes | Advance Wars playback and about 10 track skips passed after leak fixes |
| `audiodecoder.hvl` | Yes | AHX/HVL playback passed |
| `audiodecoder.modplug` | No | Earlier tracker playback worked; excluded to avoid format-selection and metadata mismatch |
| `audiodecoder.ncsf` | Yes | Not tested; sample needed |
| `audiodecoder.nosefart` | Yes | NSF playback, stop/replay and track switching passed with manifest-authorized helpers |
| `audiodecoder.openmpt` | Yes | MOD/IT/XM/S3M length, natural ending and seek reported good on 2026-10-15 |
| `audiodecoder.organya` | Yes | Not tested; sample needed |
| `audiodecoder.qsf` | Yes | `Capcom/01 - title.miniqsf` failed initialization on 2026-10-15; parent-library dependency has not been checked, so not a confirmed decoder defect |
| `audiodecoder.sidplay` | Yes | Not tested; sample needed |
| `audiodecoder.snesapu` | Yes | SPC playback passed; F-Zero Big Blue playback/stop reported good again on 2026-10-15; exact decoder selection requires log confirmation |
| `audiodecoder.ssf` | Yes | Not tested; sample needed |
| `audiodecoder.stsound` | Yes | YM playback passed, including three files after fixes |
| `audiodecoder.usf` | Yes | Super Mario 64 title, previously failing tracks and 10-15 skips passed after leak/allocation fixes |
| `audiodecoder.wsr` | Yes | WSR playback, stop/replay and track switching passed with manifest-authorized helpers |

### Outstanding observations

- Opening the SMB `.m3u` shows names but selecting an entry does not start
  playback; opening the `.nsf` directly works. Cause is not established.
  Extended NEZplug/GME playlists are not supported by Kodi's ordinary playlist
  parser; do not assume a PS5 loader defect or treat names alone as proof that
  the decoder's sidecar-playlist support was exercised.
- Some GME formats show the default 150-second duration when no duration
  metadata is available. KSS and HES showed this during the 2026-10-15 tests;
  the user accepted this limitation and requested no implementation change.
- The QSF sample's initialization failure is not yet distinguished from an
  incomplete miniQSF dependency set.
- `audiodecoder.timidity`, `audiodecoder.upse` and `audiodecoder.vgmstream`
  remain blocked by ELF TLS requirements. GENH needs vgmstream. SACD is deferred.

Direct console capture for the 2026-10-15 tests was saved on the workstation as
`D:\PS5\kodi-binary-addon-test-20261015.log`; that monitor was stopped.
These tests preceded the later upstream-master merge and do not validate the
newly merged title.

## Screensavers

### Console observations

After discussion, all 13 reported failures below are excluded individually
from automatic packaging via `tools/ps5/addon-exclusions.json`. Twelve are
effects from the shared RSXS repository; the other is standalone Ping Pong.
The RSXS build target remains enabled, and other RSXS effects remain eligible.
No repository-wide exclusion was applied. These exclusions take effect on the
next package/deploy, not on the existing console installation.

The 2026-10-15 test round ended at user time 22:41; the direct kernel monitor
was stopped. Capture: `D:\PS5\kodi-screensavers-20261015-2221.log`.
Thirteen screensavers were reported: ten slow/barely visible, two black-screen
only, and Hyperspace freezing Kodi. Cyclone also had an earlier SIGBUS before
a slow-running retry. None of these thirteen passed acceptance. The detailed
observations below supersede the batch table's initial "Not tested" labels;
no result is inferred for screensavers not reported by the user.

- 2026-10-15: `screensaver.rsxs.sundancer2` renders slowly; performance smoke
  test failed. Cause not investigated.

- 2026-10-15: `screensaver.rsxs.spirographx` renders slowly; performance smoke
  test failed. Cause not investigated.

- 2026-10-15: `screensaver.rsxs.solarwinds` renders slowly; performance smoke
  test failed. Cause not investigated.

- 2026-10-15: `screensaver.rsxs.skyrocket` renders slowly; performance smoke
  test failed. Cause not investigated.

- 2026-10-15: `screensaver.pingpong` shows only a black screen; rendering
  smoke test failed. Cause not investigated.

- 2026-10-15: `screensaver.rsxs.matrixview` renders slowly; performance smoke
  test failed. Cause not investigated.

- 2026-10-15: `screensaver.rsxs.lattice` renders slowly; performance smoke
  test failed. Cause not investigated.

- 2026-10-15: `screensaver.rsxs.hyperspace` freezes Kodi during the console
  smoke test. Runtime test failed; cause not investigated.

- 2026-10-15: `screensaver.rsxs.hufosmoke` runs slowly with barely visible
  output; some rendering is visible. Visual/performance smoke test failed;
  cause not investigated.

- 2026-10-15: `screensaver.rsxs.flocks` renders slowly; performance smoke
  test failed. Cause not investigated.

- 2026-10-15: `screensaver.rsxs.euphoria` shows only a black screen.
  Rendering smoke test failed; cause not investigated.

- 2026-10-15: `screensaver.rsxs.cyclone` was followed by a main-thread
  SIGBUS shortly after `Dll Destroyed - Cyclone` and opening the add-on settings
  dialog. After restarting Kodi, a second attempt ran very slowly without a
  reported crash. The crash is not consistently reproduced and its cause is
  unestablished; Cyclone is not accepted for normal use. Capture:
  `D:\PS5\kodi-screensavers-20261015-2221.log`.

- 2026-10-15: `screensaver.rsxs.busyspheres` runs but is extremely slow.
  Temporarily excluded from automatic packaging in `addon-exclusions.json`
  pending performance investigation. This takes effect on the next package;
  the current console installation is unchanged.

The 2026-10-15 isolated batch found 11 source definitions. Ten support
FreeBSD/PS5 and all ten built, installing 31 binary add-ons under
`build/ps5-screensavers/install`. `screensaver.cpblobs` excludes this platform
and was not built. No screensaver console acceptance is claimed yet, apart
from the purpose-built `test.binary.ps5` probe above.

| Built add-on IDs | Version | Console |
|---|---|---|
| `screensaver.asterwave`, `screensaver.matrixtrails`, `screensaver.shadertoy`, `screensaver.stars` | 22.0.5 | Not tested |
| `screensaver.asteroids`, `screensaver.biogenesis`, `screensaver.greynetic`, `screensaver.pingpong`, `screensaver.pyro` | 22.0.6 | Not tested |
| `screensaver.rsxs.busyspheres`, `screensaver.rsxs.colorfire`, `screensaver.rsxs.cyclone`, `screensaver.rsxs.drempels`, `screensaver.rsxs.euphoria`, `screensaver.rsxs.feedback`, `screensaver.rsxs.fieldlines`, `screensaver.rsxs.flocks`, `screensaver.rsxs.flux`, `screensaver.rsxs.helios`, `screensaver.rsxs.hufosmoke` | 22.0.7 | Not tested |
| `screensaver.rsxs.hufotunnel`, `screensaver.rsxs.hyperspace`, `screensaver.rsxs.lattice`, `screensaver.rsxs.lorenz`, `screensaver.rsxs.matrixview`, `screensaver.microcosm.biof`, `screensaver.rsxs.plasma`, `screensaver.rsxs.skyrocket`, `screensaver.rsxs.solarwinds`, `screensaver.rsxs.spirographx`, `screensaver.rsxs.sundancer2` | 22.0.7 | Not tested |

All 31 ELFs passed the structural audit (no `PT_TLS`, `DT_NEEDED` or
unsupported relocations). Against the currently staged title, 17 passed the
candidate-only host export preflight; 14 remain import-blocked. Missing
exports include GL entry points such as `glPolygonMode`, `glReadBuffer`,
`glTexStorage*` and `glCompressedTexSubImage*`. RSXS Colorfire and Hufotunnel
also require emutls symbols including `__emutls_v._ThreadRuneLocale`.
The two emutls consumers are explicitly excluded from automatic packaging.
The other 12 export misses have real GL definitions and are now included in
automatic import/export generation as described above. This does not establish
successful final title linking or runtime compatibility, and does not permit
bypassing loader restrictions.

Persistent source patches disable SOIL2's X11/GLX path for Asterwave and
Matrix Trails and replace RSXS `memalign` with `posix_memalign`, retaining
16-byte alignment. The isolated configuration stages PS5 GL headers and an
empty `libGL.a` for CMake discovery only; it supplies no GL implementation.

Local evidence is under `build/ps5-screensavers/audit/`: `results.tsv`
(target outcomes), `addons.tsv` (IDs, versions and installed libraries),
`elf-audit.json` (per-binary blockers), and the full/candidate-only
`export-preflight*.log` files. No full Kodi build or deployment was performed
for this batch.

## Updating this record

For each new result, record the exact add-on ID, test date, build/loader outcome
and the behavior actually observed on console. Preserve unresolved failures
until new evidence resolves them. After an upstream merge or relevant runtime
change, retain historical passes but explicitly distinguish them from retests
of the new title. Do not promote untested add-ons to supported merely because
they compile, stage or share an already-tested file extension.
