# PS5 native binary add-ons: build and publish

**Upgrade Kodi before installing these native packages.** The repository IDs and
`piers-ps5-1` feed URLs are retained, but native packages require the title's
`resource.ps5.addon-runtime` version **2.0.0**. Do not copy the marker into an
older title to bypass this requirement. Feed metadata cannot make a legacy
installed client safe. There is no software-SO-loader fallback or parallel feed.

The integrated workflow passed local qualification and the in-place public
repository migration on 2026-10-27: all 34 supported add-ons across 27 source
targets built and packaged against the verified standard title. All binary
packages remain in testing. Compilation, signing and publication are not console
acceptance of the rebuilt bytes.

Run commands from the Kodi source root in Linux/WSL on a native Linux filesystem.
Prepare dependencies and the native runtime following [README.PS5.md](README.PS5.md).
The [native architecture reference](PS5-NATIVE-SPRX.md) describes provider
ownership, ABI limits and the distinction between local and console evidence.

## Normal workflow

| Step | Command | Result |
|---|---|---|
| Prepare native inputs | `python3 tools/ps5/prepare-native-runtime.py --jobs 4` | Fingerprinted shared SDK and dependency map |
| Configure/build Kodi | `bash tools/ps5/configure.sh`, then `cmake --build build/ps5-release -j4` | Normal native Kodi build |
| Compile catalogue | `python3 tools/ps5/addon-repository.py build --jobs 4` | Independent signed SPRX installs and receipts |
| Package title | `cmake --build build/ps5-release --target ps5-title` | Fresh title stage with bundled add-ons and system repositories |
| Package feeds | `python3 tools/ps5/addon-repository.py package --title-app <stage>/app` | Native ZIPs, feeds, hashes and publication inventory |
| Review publication | `python3 tools/ps5/addon-repository.py publish --checkout PATH --dry-run` | Planned additions and scoped obsolete-file removals |

`list` shows the catalogue without compiling. `build` uses the ordinary
`cmake/addons` superbuild with `toolchain/ps5-native-addon.cmake`; it does not
relink Kodi. `ps5-title` neither compiles add-ons nor publishes anything.
`package` validates the complete catalogue and the matching standard native
title, including signed-module extraction and provider requirements. A partial
catalogue cannot qualify for publication.

Packaging never deploys, including when `PS5_HOST` is inherited. The user
performs any console installation separately.

## PVR lifecycle stability

PVR client updates serialize the complete state snapshot and manager
stop/client-change/restart sequence, so rapid add-on events cannot execute
stale lifecycle plans concurrently. GUI channel checks retain and check group
snapshots during shutdown. The `pvr.hts` source patch joins the HTSP receive
thread (and its registration worker) before demuxers are destroyed. Deploy both
the rebuilt application module and rebuilt `pvr.hts` module for these fixes.
Unloading PVR components also clears the known-client cache, so restarting
after an aborted initialization reloads channel groups instead of treating
discarded data as already loaded.
Run `python3 tools/ps5/test-pvr-lifecycle.py --addon-source <patched-pvr.hts-source>`
to exercise transaction serialization, callback teardown ordering, and aborted
startup followed by re-enabling the same client.

## Inputs, isolation and caching

The default prepared inputs are `build/ps5-native/sdk` and
`build/ps5-native/depends/archive-map.json`. Global repository options precede
the subcommand:

```sh
python3 tools/ps5/addon-repository.py \
  --runtime-sdk /path/to/sdk --archive-map /path/to/archive-map.json \
  --depends /path/to/depends --work /path/to/work --output /path/to/output \
  build --jobs 4
```

Use the same inputs for building, validation and feed generation. Payload SDK
and dependency overrides also accept `PS5_PAYLOAD_SDK` and `PS5_DEPENDS_PREFIX`;
native inputs accept `PS5_NATIVE_RUNTIME_SDK` and `PS5_NATIVE_DEPENDS_MAP`.
Firmware-analysis files are not SDK/build/validation inputs and must not be
shipped or committed. Ordinary SDK import libraries are linker inputs, not
runtime binaries.

Source refs are pinned in `work/source-lock.json`; unchanged targets reuse
validated fingerprint-specific installs. SDK/provider/ABI/builder changes
invalidate affected targets, including their private dependencies. A channel,
bundling or package-revision change alone does not recompile sources. Legacy SO
build records cannot satisfy native cache validation.

```sh
# Select one source target; repeat --only when needed.
python3 tools/ps5/addon-repository.py build --only inputstream.adaptive --jobs 4

# Explicitly update that source lock rather than refreshing all upstream refs.
python3 tools/ps5/addon-repository.py build \
  --only inputstream.adaptive --refresh-sources --jobs 4
```

A multi-output target builds once for all its catalogued outputs. Source-target
and output-specific patch series both participate in the target fingerprint.
Microcosm retains its published `screensaver.microcosm.biof` identity in its
native build, resource directory and installed package. Preserve the
workspace/source locks instead of deleting caches. Another add-on using existing
provider capabilities does not require a title rebuild or new title exports.
A new runtime requirement needs a compatible upgraded provider/title.

Private Autoconf dependencies use the same native compiler launcher, forced ABI
header and opaque-stdio overlay as CMake dependencies. Do not bypass the generated
`CONFIG_SITE` or reuse dependencies compiled against the exposed FreeBSD `FILE`.
The launcher supports Autoconf's implicit single-object outputs using the Clang
driver's output plan, while retaining native frontend and init-array lowering.
After changing SDK tools, prepare fresh fingerprinted inputs rather than editing
an existing SDK artifact.
Nosefart and WSR embed separately namespaced tag/track and playback engines in
their SPRX; they do not ship ordinary ELF helper libraries or merge engine state.

## Title staging and runtime trust

Native CMake installs and repository ZIPs give SPRX files executable permissions
(`0755`); their JSON receipts remain non-executable (`0644`). Kodi's ordinary ZIP
installation copies file contents without preserving execute bits. Before native
loading, the PS5 loader restores missing execute bits on regular SPRX files,
preserving their read/write permissions. It skips permission changes for already
executable modules, including read-only title files, and reports filesystem errors
instead of calling the native loader after a failed repair. This also repairs
previously installed `0644` modules without reinstalling them.

Title packaging reads `build/ps5-addon-repository/work/build-record.json`, or
explicit colon-separated `PS5_ADDON_PREFIXES`. An explicitly empty override
disables discovery. A custom builder `--work` is not discovered automatically:
pass its recorded install prefixes. Invalid explicit roots, duplicate IDs and
unsafe binaries fail.

Only catalogue entries with `bundled: true` enter the title. Adaptive is bundled;
the Bunny DASH diagnostic is opt-in via `PS5_BUNNY_DIAGNOSTIC=1` and requires
staged native Adaptive. It is not a catalogue binary. The native
loader smoke sample remains opt-in via `PS5_TEST_BINARY_ADDON=1`.
Legacy `PS5_ADDON_ELFS`, `PS5_AUDIO_DECODERS` and `PS5_ADAPTIVE_TEST` are rejected.

Stages are fresh `BUILD/title-<UTC timestamp>-<pid>` directories by default.
For a reproducible chosen path, use a previously nonexistent `STAGE` with
`package.sh`, or configure `PS5_PACKAGE_STAGE`. Pass that stage's `app` directory
explicitly to `package --title-app`; do not reuse an old `ps5-stage`.
`native-addons.json` records staging selection.

| Component | Title behavior |
|---|---|
| `repository.ps5` | Shipped, registered, enabled and official; stable feed |
| `repository.ps5.testing` | Shipped but not enabled on first discovery; opt-in testing feed |
| `resource.ps5.addon-runtime` | Required title-owned system marker; not a downloadable runtime upgrade |

Enable testing through **My add-ons > Add-on repository > Kodi PS5 Testing
Add-ons**. Existing enable/disable choices are preserved. Testing does not become
official merely because it ships. Kodi's normal official-source policy still
applies; retain its ordinary repository for non-PS5 add-ons.

## In-place native repository migration

Use a separate clean checkout of `Notedop/kodi-ps5-binary-addons`, on local
`main`, with the correct `origin`. Feed URLs explicitly use remote `main`.
The publisher does not repair branches/upstreams or force-push.

```sh
git -C ~/kodi-ps5-binary-addons status --short
git -C ~/kodi-ps5-binary-addons branch -vv
git -C ~/kodi-ps5-binary-addons remote get-url origin

# Review before applying; does not change the checkout.
python3 tools/ps5/addon-repository.py publish \
  --checkout ~/kodi-ps5-binary-addons --dry-run

# Only after complete native qualification and review of the removal inventory.
python3 tools/ps5/addon-repository.py publish \
  --checkout ~/kodi-ps5-binary-addons --apply-native-migration --commit --push
```

Migration inventories obsolete ZIPs, sidecars, loose binary artifacts and known
runtime trees within the owned publication scope. Unrelated files/settings are
preserved. Additions, replacement indexes and intentional removals form one
publication commit; history is not rewritten. Failed application rolls back
modified paths. An unchanged committed rerun is idempotent.

`--commit` alone does not push; `--push` requires it and pushes `HEAD:main`.
Without `--commit`, review and commit the copied changes before invoking the
publisher again: a dirty checkout is rejected. Publication requires the generated
`output/publication.json`, not arbitrary files copied from an old output tree.

Native revisions must exceed the published legacy versions. Different bytes at
an existing immutable version are rejected, including cross-channel collisions.
Increment the affected catalogue `revision` for changed contents without an
upstream version bump (`1.2.3`, revision 2 becomes `1.2.3+ps5.2`).

## Catalogue changes and stable promotion

### Qualified native publication

Public commit [`56564388e54a0689bcaa657c646694dda5cbe022`](https://github.com/Notedop/kodi-ps5-binary-addons/commit/56564388e54a0689bcaa657c646694dda5cbe022)
replaced the legacy packages with native `+ps5.2` revisions and repository
bootstrap version 2.0.0, retaining the feed URLs and Microcosm's published ID.
All 269 publication files passed public SHA-256 readback; an unchanged migration
dry-run reported no changes or removals.

The matching local title stage is
`build/ps5-native-standard-91dc852b/title-native-refreshed-sdk-c9525854f2/app`,
prepared against SDK fingerprint
`98586060bca1a22017614fb69c8fda24eb11e9ede503c71462a650bf3ccb01dc`.
All 20 exact completed-title checks passed. Earlier user-operated console
acceptance applies to the earlier title bytes, not this refreshed title or the
entire rebuilt catalogue. No console deployment was performed.

### Further changes

Keep new/changed bytes in testing. Add exact upstream output IDs to
`tools/ps5/addon-catalogue.json` and preserve multi-output grouping. The source
also needs ordinary Kodi build/platform definitions under `cmake/addons/addons`.
Adding a catalogue entry does not port its dependencies, TLS or runtime behavior.
Persistent PS5 changes belong in the [add-on patch series](../tools/ps5/patches/addons/README.md).
Review `addon-exclusions.json`; do not silently exclude a failing supported entry.

Promote only an exact console-tested ZIP: set `channel` to `stable`,
`accepted_sha256` to its SHA-256, and `acceptance` to the actual title/version,
scenarios and results. Keep the revision unchanged for a byte-identical promotion.
Packaging rejects mismatched approval hashes. Historical acceptance of another
binary is not approval; rebuilt native packages do not inherit legacy acceptance.
Stable packages also remain available through testing. Demotion changes feed
selection, not automatic downgrading of installed versions.

ZIP order, timestamps and permissions are deterministic.
`*.zip.build.json` records provenance, not console acceptance. Retain source
locks and matching patches, and review license/corresponding-source obligations.

## Failures and focused checks

Missing/stale SDK or dependency-map errors require preparing matching inputs;
do not bypass receipt checks. Wrong platform metadata must be corrected at
generation (`ps5` platform, `library_freebsd` selecting the SPRX), not by renaming
an ELF. Undeclared imports, unsupported TLS/relocations and private Kodi-app
dependencies require fixing the build/provider contract, not weakening policy.
Missing or mismatched title proof requires packaging the matching native title.

```sh
python3 tools/ps5/test.py \
  test-addon-build.py test-addon-repository.py test-stage-installed-addons.py
bash -n tools/ps5/package.sh
```

Use the explicit runner: ordinary unittest discovery does not include these
hyphenated filenames. Functional console acceptance remains a separate,
user-operated step.
