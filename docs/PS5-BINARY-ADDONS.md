# PS5 binary add-ons: build and publish

Run all commands from the Kodi source root in Linux/WSL, on a native Linux
filesystem. The PS5 SDK, cross-compiled depends prefix and configured Kodi
build must already exist; follow [the PS5 build guide](README.PS5.md) first.
This workflow handles compiled binary add-ons, not ordinary Python plugins.

## The four separate steps

| Step | Command | Result |
|---|---|---|
| Compile add-ons | `python3 tools/ps5/addon-repository.py build` | Installed binaries/resources and per-target build records |
| Build/package Kodi | `cmake --build build/ps5-release --target ps5-title` | Deployable title, candidate exports and runtime receipt |
| Package repository | `python3 tools/ps5/addon-repository.py package` | Add-on ZIPs, testing/stable feeds and publication inventory |
| Publish | `python3 tools/ps5/addon-repository.py publish --checkout PATH` | Copies generated files to a separate Git checkout |

These steps are not interchangeable. `build` does not generate repository ZIPs.
`ps5-title` does not compile add-ons. `publish` does not build or package anything.
The title must support the candidate binaries before repository packaging can
succeed. A successful build or host check is not console acceptance.

## First build or full refresh after shared build changes

```sh
# Show the current selection without downloading or compiling.
python3 tools/ps5/addon-repository.py list

# Compile/install the complete catalogue.
python3 tools/ps5/addon-repository.py build --jobs "$(nproc)"

# Build Kodi, then package the title using the installed candidate inventory.
cmake --build build/ps5-release -j"$(nproc)"
cmake --build build/ps5-release --target ps5-title

# Validate candidates against that title and generate both repository feeds.
python3 tools/ps5/addon-repository.py package
```

Lower `--jobs` if the machine runs out of memory. Some targets build substantial
dependencies as well as their small Kodi wrapper.

Title packaging normally reads install prefixes from
`build/ps5-addon-repository/work/build-record.json`. All catalogue candidates
contribute imports to title export generation, but only entries with
`bundled: true` are copied into the title. Do not use a partial candidate
inventory when preparing a title for publication.

`ps5-title` uploads when `PS5_HOST` is set in the environment. Leave it unset
when you only want local packaging. Rebuilding the title does not deploy a
repository feed.

## Publishing to GitHub

Use a separate checkout of `Notedop/kodi-ps5-binary-addons`. Its working tree must
be clean, its local branch must be `main`, and `origin` must point to that
repository. Download URLs explicitly use remote `main`, not the remote's default
branch.

```sh
git -C ~/kodi-ps5-binary-addons status --short
git -C ~/kodi-ps5-binary-addons branch -vv
git -C ~/kodi-ps5-binary-addons remote get-url origin

# Copy generated files only; no commit or push.
python3 tools/ps5/addon-repository.py publish --checkout ~/kodi-ps5-binary-addons

# Inspect and commit only the intended generated changes in that checkout.
git -C ~/kodi-ps5-binary-addons diff --stat
# After committing, explicitly push to remote main and set its upstream.
git -C ~/kodi-ps5-binary-addons push --set-upstream origin main
```

Alternatively, let the publisher stage the generated paths and commit them:

```sh
python3 tools/ps5/addon-repository.py publish \
  --checkout ~/kodi-ps5-binary-addons --commit --push
```

Choose one approach. After copy-only publication, commit or otherwise resolve
those changes before invoking the publisher again: it refuses a dirty checkout.
`--commit` alone does not push; `--push` requires `--commit` and explicitly pushes
`HEAD:main`. It does not repair an existing upstream tracking `origin/master`.

If local `main` still tracks `origin/master`, the explicit push above updates
tracking without deleting `master`. If local `main` does not exist, switch to
it when available; rename `master` to `main` only if that is the intended release
branch. Do not force-push to bypass divergent history. Set GitHub's default
branch to `main` separately if needed.

Publication preserves older package versions and rejects changed contents at
an existing version. There is no automatic remote cleanup or forced push.
Raw GitHub URLs serve the packages; GitHub Pages is not required.

## Common scenarios

### Repeat an unchanged build

Run the same `build` command. Valid targets report `cached - skipping build`;
they are not compiled again. Source refs are pinned on first use and are not
refreshed just because the branch moved upstream.

The cache is per source target, not per whole catalogue:

| Change | Compilation impact |
|---|---|
| Add a new source target to the catalogue | New target only |
| Change one target's source, definition or PS5 patches | That target |
| Add another output ID to a multi-output target | That shared target |
| Change channel, acceptance, bundled policy or package revision | No recompilation by itself |
| Change shared builder/configuration, API headers, SDK or depends inputs | Can invalidate all targets |

An invalidated target gets a fresh fingerprint-specific build/install tree,
including its private dependencies. This is not an in-place incremental rebuild
of the old dependency tree. Preserve the workspace and source lock; deleting
them or refreshing upstream unnecessarily makes repeat builds more expensive.

### Build or update one target

```sh
python3 tools/ps5/addon-repository.py build --only inputstream.adaptive --jobs 4

# Deliberately resolve this target's upstream ref again.
python3 tools/ps5/addon-repository.py build \
  --only inputstream.adaptive --refresh-sources --jobs 4
```

`--only` may be repeated. A multi-output source target compiles once for all
its catalogued outputs; selecting one output selects that source target.
Omit `--only` to process the whole catalogue. Without it, `--refresh-sources`
refreshes every selected target's upstream ref.

After a binary change, rebuild/package the title when its exports or runtime
need updating, then run `package` and `publish`. `package` always validates the
complete catalogue, even after a focused build. Once the title is already
compatible, `build --jobs 4 --package` can combine compilation and repository
packaging; it does not rebuild the title for you.

### Add a new binary add-on

Edit `tools/ps5/addon-catalogue.json`, adding a source under `targets`:

```json
"visualization.example": {
  "url": "https://github.com/OWNER/visualization.example",
  "ref": "Piers",
  "addons": {
    "visualization.example": {
      "bundled": false,
      "channel": "testing",
      "revision": 1
    }
  }
}
```

This is a template, not an existing supported add-on. Use a public GitHub source
URL, an existing compatible branch/tag or commit, and exact manifest output IDs.
List multiple output policies under one target when one build produces several
add-ons. Keep new candidates in testing and normally leave `bundled` false.
The catalogue must not conflict with `tools/ps5/addon-exclusions.json`.

The source target also needs Kodi build/platform metadata under
`cmake/addons/addons/<target>/`; existing Kodi targets normally already have it.
Ensure the metadata allows FreeBSD, the build platform used for PS5, and the
source has a supported CMake build root. Catalogue inclusion does not port an
add-on: dependencies, TLS, imports or runtime behavior may still need work.
Persistent PS5 patches belong in the
[add-on patch series](../tools/ps5/patches/addons/README.md).

Run `list`, build the new target with `--only`, then build/package the title with
the full installed inventory and generate/publish packages. Existing unrelated
targets remain cached unless shared build inputs also changed.

### Promote testing to stable

Install and console-test the exact generated testing ZIP. Then update its nested
catalogue policy, keeping `revision` unchanged for promotion of the same ZIP:

```json
{
  "bundled": false,
  "channel": "stable",
  "revision": 1,
  "accepted_sha256": "<64-character SHA-256 of the tested ZIP>",
  "acceptance": "Tested on title <build ID>, firmware <version>, <date>: <scenarios and results>."
}
```

Get the hash from `repo/piers-ps5-1/testing/build-manifest.json` inside the output
directory, or run `sha256sum` on the tested ZIP. Record the actual tested title
and behavior, not just successful compilation. Historical acceptance of another
binary is not approval of this ZIP.

Run `package`, then `publish` and commit/push. Promotion alone requires neither
compilation nor title packaging. Stable candidates also remain in testing.
Packaging rejects a stable entry if the generated ZIP differs from its accepted
hash. To demote, set `channel` back to `testing` and remove the acceptance fields;
regenerate/publish the feeds. Older ZIPs remain archived, and installed versions
are not automatically downgraded.

### Release changed package contents

If the upstream version stays the same but binaries, resources or helpers change,
increment that output's `revision`, for example from `1` to `2`. An upstream
version `1.2.3` becomes `1.2.3+ps5.2`. Never overwrite different contents at an
already generated/published package version.

Start the changed release in testing and remove stale acceptance fields.
Build if compilation inputs changed, package against a compatible title, then
publish. After console testing, promote the exact new ZIP. Bundled and
downloadable copies use the same revision policy; changing a bundled version
also requires title repackaging to update the shipped copy.

## What Kodi ships and trusts

| Component | Title behavior |
|---|---|
| `repository.ps5` | Shipped, enabled and official in PS5 builds; stable feed |
| `repository.ps5.testing` | Shipped, disabled on first discovery; manual opt-in |
| `resource.ps5.addon-runtime` | Required system marker; never published in repository feeds |
| Catalogue entry with `bundled: true` | Shipped binary add-on, independent of testing/stable channel |

Enable testing through **My add-ons > Add-on repository > Kodi PS5 Testing
Add-ons**. Existing user enable/disable choices for testing are preserved.
Repository ZIPs are also generated for manual installation.

Title inclusion is configured by `stage_system_addons()` in
`tools/ps5/stage-installed-addons.py`. Stable and the runtime marker are registered
in the packaged `system/addon-manifest.xml`; testing is deliberately absent.
Even `optional="true"` manifest entries are enabled on first discovery.

Official-source status is separate: `CCompileInfo::LoadOfficialRepoInfos()` in
`xbmc/CompileInfo.cpp.in` adds only the PS5 stable repository and its exact URL
prefix, guarded by `TARGET_PS5`. Testing is not official simply because it ships.
**Update official add-ons from: Official repositories only** permits stable PS5
updates without letting testing automatically replace official add-ons. Explicit
testing installation is still possible. Keep Kodi's ordinary official repository
for non-PS5 add-ons. Changing official-source configuration requires a Kodi
rebuild, not just repackaging.

## Paths and advanced overrides

| Path | Purpose |
|---|---|
| `tools/ps5/addon-catalogue.json` | Source targets, output selection and release policy |
| `tools/ps5/addon-exclusions.json` | Explicit compatibility/selection blocks and reasons |
| `build/ps5-addon-repository/work/source-lock.json` | Pinned source commits |
| `build/ps5-addon-repository/work/build-record.json` | Install prefixes, fingerprints and artifact hashes |
| `build/ps5-addon-repository/output/publication.json` | Generated inventory required by `publish` |
| `build/ps5-addon-repository/output/repo/piers-ps5-1/{testing,stable}/` | Feeds, build manifests, add-on ZIPs and hashes |
| `build/ps5-stage/app/build/llvm-pie.elf` | Original pre-conversion title ELF used by `package` |
| Beside that ELF: `ps5-addon-runtime.json` | Runtime generation and matching title hash |
| `build/ps5-release/installed-addons-report.json` | Title preflight selection/exclusion report |
| `build/ps5-stage/installed-addons-report.json` | Actual title staging report |

Global options go **before** the subcommand: `--sdk`, `--depends`, `--work`,
`--output`, `--catalogue`. SDK/depends also accept `PS5_PAYLOAD_SDK` and
`PS5_DEPENDS_PREFIX`. Reuse the same overrides for build, validation and
publication. `package --title-elf PATH` selects another original title ELF; its
matching runtime receipt is still required.

Title packaging does not automatically follow a custom builder `--work` path.
Set colon-separated `PS5_ADDON_PREFIXES` to its recorded install prefixes.
This overrides discovery; an explicitly empty value disables it. Without an
override or repository build record, historical depends/audio/screensaver
prefixes are a compatibility fallback, not the recommended publication workflow.
Missing explicit prefixes, duplicate IDs and invalid binaries fail closed.

`PS5_TEST_BINARY_ADDON=1` includes the separately built loader probe.
`PS5_ADAPTIVE_TEST=1` adds adaptive test content/debug markers, not permission to
ship that add-on. Neither is needed for ordinary repository builds.
`PS5_ADDON_ELFS` is a trusted exports-only override, not an installer.
Loader details and diagnostic procedures are in the
[ELF loader runbook](../xbmc/platform/ps5/elf/README.md).

ZIP ordering, timestamps and permissions are deterministic. Each
`*.zip.build.json` records source revision/build provenance, not console
acceptance. Retain source locks, dependency sources and matching Kodi patches;
review license and corresponding-source obligations before distribution.
Changing the runtime ABI requires coordinated repository URLs, runtime marker
version and feed generation changes, not silently reusing `piers-ps5-1`.

## Troubleshooting

| Message or symptom | Action |
|---|---|
| Missing `output/publication.json` | Run `package` successfully before `publish`; use the same `--output` |
| Publication checkout must be on `main` | Check local branch and upstream; publish/push to remote `main` |
| Publication checkout is dirty | Review and commit/resolve its changes; do not discard unrelated work |
| Build record missing, stale or modified outputs | Rerun `build` with the same SDK/depends/work configuration |
| Missing original title ELF or runtime receipt | Build/package `ps5-title`; do not substitute the converted executable |
| Runtime receipt does not match | Repackage the matching title; do not forge/edit the receipt |
| Missing title exports | Repackage title with the complete candidate inventory; port unsupported imports rather than bypassing checks |
| Incomplete candidate inventory warning | Build all catalogue targets, then rebuild/package title before publishing |
| Existing-version collision | Increment the affected package revision for changed contents |
| Stable approval hash mismatch | Test and approve the exact new ZIP, or return it to testing |
| `DT_NEEDED`, TLS or relocation rejection | Fix the add-on/dependency build; do not weaken the loader policy |
| Every target rebuilds after a shared change | Expected cache invalidation; private dependencies are rebuilt too |

PS5 GL consumers use the generated Kodi CMake header-only interface and leave
GL calls as title imports. Linking SDK `libGL.so` can introduce an OSMesa
`DT_NEEDED`, which the loader intentionally rejects. The builder validates
primary/helper ELF eligibility on fresh builds and cached-result reuse;
repository packaging additionally checks actual title exports.

Focused host checks, without console deployment:

```sh
python3 tools/ps5/test-addon-build.py
python3 tools/ps5/test-addon-repository.py
python3 tools/ps5/test-stage-installed-addons.py
bash -n tools/ps5/package.sh
```

Per-add-on inventories belong in the catalogue and generated manifests, not
duplicated tables here. Release acceptance belongs with the exact ZIP hash and
catalogue policy; earlier console observations remain historical evidence in
`HANDOVER-PS5-BINARY-ADDONS.md`, not blanket approval of current packages.
