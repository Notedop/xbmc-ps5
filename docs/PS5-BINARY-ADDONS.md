# PS5 native binary add-ons: build and publish

This guide covers building, packaging, staging and publishing PS5 binary add-ons.
The catalogue in `tools/ps5/addon-catalogue.json` defines the supported targets,
package revisions, bundled add-ons and release channels.

**Use a compatible Kodi PS5 title.** Native packages require the title-owned
`resource.ps5.addon-runtime` version **2.0.0**. Upgrade Kodi rather than copying
the runtime marker into an older installation.

Run commands from the Kodi source root in Linux/WSL on a native Linux filesystem.
Prepare dependencies and the native runtime following [README.PS5.md](README.PS5.md).
The [native architecture reference](PS5-NATIVE-SPRX.md) describes runtime
compatibility requirements.

## Normal workflow

| Step | Command | Result |
|---|---|---|
| Prepare native inputs | `python3 tools/ps5/prepare-native-runtime.py --jobs 4` | Fingerprinted shared SDK and dependency map |
| Configure/build Kodi | `bash tools/ps5/configure.sh`, then `cmake --build build/ps5-release -j4` | Normal native Kodi build |
| Compile catalogue | `python3 tools/ps5/addon-repository.py build --jobs 4` | Independent signed SPRX installs and receipts |
| Package title | `cmake --build build/ps5-release --target ps5-title` | Fresh title stage with bundled add-ons and system repositories |
| Package feeds | `python3 tools/ps5/addon-repository.py package --title-app <stage>/app` | Native ZIPs, feeds, hashes and publication inventory |
| Review publication | `python3 tools/ps5/addon-repository.py publish --checkout PATH --dry-run` | Planned additions and scoped obsolete-file removals |

`list` shows the catalogue without compiling. `build` compiles add-ons without
relinking Kodi. `ps5-title` neither compiles add-ons nor publishes anything.
`package` validates existing add-on installs and the matching title, then
generates the ZIPs and feeds without compiling. Publication requires the
complete qualified catalogue, not a partial selection.

These are separate steps: do not run `build` just to publish already prepared
packages. Reuse existing ZIPs unchanged for channel promotions, and keep the
catalogue and feed metadata in sync.

Packaging never deploys, including when `PS5_HOST` is inherited. The user
performs any console installation separately.

## Build inputs and cache reuse

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
Keep the workspace, source locks and dependency cache between runs. Unchanged
targets and dependencies are reused when their recorded inputs and outputs
still match. Failed builds retain matching completed work for subsequent runs.
A channel, bundling or package-revision change alone does not recompile sources.
Source, dependency or SDK changes can require rebuilding affected targets.

Select individual source targets instead of rebuilding the entire catalogue:

```sh
# Select one source target; repeat --only when needed.
python3 tools/ps5/addon-repository.py build --only inputstream.adaptive --jobs 4

# Explicitly update that source lock rather than refreshing all upstream refs.
python3 tools/ps5/addon-repository.py build \
  --only inputstream.adaptive --refresh-sources --jobs 4
```

A multi-output target builds all of its catalogued outputs together. Use the
target names shown by `list` with `--only`.

### Native network and archive readers

The catalogue includes these nonbundled testing targets:

| Target | Upstream version | Private dependencies |
|---|---|---|
| `inputstream.rtmp` | 22.1.3 | RTMPDump 2.6, OpenSSL 3.5.8, zlib 1.3.2 |
| `vfs.rar` | 22.0.5 | bundled Unrar 5.91, TinyXML 2.6.2 |
| `vfs.sftp` | 22.0.6 | libssh 0.12.2, OpenSSL 3.5.8, zlib 1.3.2 |
| `vfs.libarchive` | 22.0.3 | libarchive 3.8.9, bz2 1.0.8, LZ4 1.10.0, LZO2 2.10, XZ 5.8.4, OpenSSL 3.5.8, zlib 1.3.2 |
| `vfs.pdf` | 22.0.2 | Poppler 26.10.0, FreeType 2.15, iconv 1.19, JPEG-turbo 3.2, PNG 1.6.58, OpenJPEG 2.5.4, zlib 1.3.2 |

Build without packaging or contacting the console:

```sh
env -u PS5_HOST MAKEFLAGS=-j4 python3 tools/ps5/addon-repository.py build \
  --only inputstream.rtmp --only vfs.rar --only vfs.sftp \
  --only vfs.libarchive --only vfs.pdf --jobs 4
```

`MAKEFLAGS` also budgets nested raw-Make dependency recipes. For concurrent
build commands, use separate `--work` directories and divide the aggregate job
budget between them; a workspace permits only one repository command at a time.
Preserve each workspace's source locks, build record, installed SPRX files and
adjacent `.sprx.json` validation receipts.

SFTP stores keys and known hosts in Kodi's writable `special://home/.ssh`.
RAR omits unavailable Unix ownership restoration and symlink timestamps while
retaining file extraction and ordinary timestamps. Libarchive keeps its
upstream format selection, which excludes RAR. PDF retains the upstream
Poppler/font configuration; rendering and font availability need console
acceptance along with network authentication and archive contents. Native
compile/import/signing validation is not console acceptance.

`vfs.smb2` is deliberately not catalogued: Kodi's existing PS5 libsmb2 SMB
implementation remains in use, rather than being overridden by an enabled
binary VFS add-on.

## Title staging

Title packaging reads `build/ps5-addon-repository/work/build-record.json`, or
explicit colon-separated `PS5_ADDON_PREFIXES`. An explicitly empty override
disables discovery. A custom builder `--work` is not discovered automatically:
pass its recorded install prefixes. Invalid explicit roots, duplicate IDs and
unsafe binaries fail.

Only catalogue entries with `bundled: true` enter the title. Adaptive is bundled;
`peripheral.joystick` is also bundled and registered as a required system add-on
in PS5 title stages, so Kodi enables its button-map provider at startup. Title
packaging requires this provider to be built and installed. Build it independently:

```sh
python3 tools/ps5/addon-repository.py build --only peripheral.joystick --jobs 4
```

The Bunny DASH diagnostic is opt-in via `PS5_BUNNY_DIAGNOSTIC=1` and requires
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
| `peripheral.joystick` | Required bundled controller button-map provider |

Enable testing through **My add-ons > Add-on repository > Kodi PS5 Testing
Add-ons**. Existing enable/disable choices are preserved. Testing does not become
official merely because it ships. Kodi's normal official-source policy still
applies; retain its ordinary repository for non-PS5 add-ons.

## Publish to the binary add-ons repository

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

# Apply locally after reviewing the removal inventory; leave changes uncommitted.
python3 tools/ps5/addon-repository.py publish \
  --checkout ~/kodi-ps5-binary-addons --apply-native-migration
```

The dry run lists additions, updated metadata and obsolete files in the managed
feeds. Review removals before passing `--apply-native-migration`. Unrelated
repository files are preserved. Review the resulting checkout changes before
committing or pushing them.

Optional `--commit` creates a publication commit without pushing. `--push`
requires `--commit` and pushes `HEAD:main`; use it only when explicitly intended.
Without `--commit`, review and commit the copied changes before invoking the
publisher again: a dirty checkout is rejected. Publication requires the generated
`output/publication.json`, not arbitrary files copied from an old output tree.

Different bytes at an existing package version are rejected, including
cross-channel collisions.
Increment the affected catalogue `revision` for changed contents without an
upstream version bump (`1.2.3`, revision 2 becomes `1.2.3+ps5.2`).

## Catalogue changes and stable promotion

Keep new/changed bytes in testing. Add exact upstream output IDs to
`tools/ps5/addon-catalogue.json` and preserve multi-output grouping. The source
also needs ordinary Kodi build/platform definitions under `cmake/addons/addons`.
Persistent PS5 changes belong in the [add-on patch series](../tools/ps5/patches/addons/README.md).
Review `addon-exclusions.json`; do not silently exclude a failing supported entry.

For an approved stable promotion, set `channel` to `stable`, `accepted_sha256`
to the existing ZIP's SHA-256, and `acceptance` to the validation evidence and
explicit promotion approval for those exact bytes. Keep the revision unchanged
and reuse the published ZIP, checksum, build receipt and assets without
rebuilding or repackaging. Update the feed indexes, their checksums and build
manifests to match the channel selection. Packaging rejects mismatched approval
hashes; approval of different package bytes does not carry over.
The feed generator also includes stable packages in testing. Demotion changes
feed selection, not automatic downgrading of installed versions.

ZIP order, timestamps and permissions are deterministic.
`*.zip.build.json` records provenance, not console acceptance. Retain source
locks and matching patches, and review license/corresponding-source obligations.

## Workflow checks

```sh
python3 tools/ps5/test.py \
  test-addon-build.py test-addon-repository.py test-stage-installed-addons.py
python3 tools/ps5/test.py \
  test-inputstream-rtmp-ps5.py test-vfs-rar-ps5.py test-vfs-sftp-ps5.py \
  test-vfs-libarchive-ps5.py test-vfs-pdf-ps5.py
bash -n tools/ps5/package.sh
```

Use the explicit runner: ordinary unittest discovery does not include these
hyphenated filenames. Functional console acceptance remains a separate,
user-operated step.
