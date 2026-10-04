# Phase 2 progress: effective source tree established

Date: 2026-10-04

## Outcome

An effective PS5 Kodi source tree is now reproduced in a clean upstream-based
checkout without using the legacy configure script:

- Worktree: `/home/raoul/kodi-fork/xbmc-ps5-fork`
- Branch: `ps5`
- Base SHA: `28ea2eac1eb7af8fdcbd2672d933ce87f594be79`

## Applied transformations

1. Overlay imported from reference:
   - Source: `/home/raoul/kodi-ps5-src/overlay`
   - Imported file count: 87
2. Kodi patch stack applied from manifest:
   - Source: `/home/raoul/kodi-ps5-src/patches/kodi/manifest.txt`
   - Applied: 19 / 19
   - Failed: 0
   - Evidence log: `docs/ps5/phase2-patch-apply.log`

## Effective-tree change footprint

Git status in `xbmc-ps5-fork` after overlay import + patch application:

- 25 modified tracked files
- 7 added platform paths
- 32 changed paths total

This matches the reference working-tree footprint, except for one additional
local-only file in `/home/raoul/kodi`:

- `xbmc/addons/AddonInstaller.cpp` is modified only in the local working tree
  and is **not** part of the overlay+patch manifest-derived effective source.

## Ledger synchronization

`tools/ps5/migration-ledger.csv` was updated:

- `PATCH-*` rows: set to `status=done`, `disposition=adapt-native-integration`
- `OVERLAY-*` rows: set to `status=done`, `disposition=adapt-native-integration`
- `SCRIPT-*` rows remain pending (these represent configure-time mutation paths
  that still need native build-graph replacement in migration commits)

## Blocking items discovered at this stage

1. The local working Kodi tree contains at least one non-manifest divergence
   (`xbmc/addons/AddonInstaller.cpp`) that requires explicit triage before
   parity claims can be made against a single canonical reference source.
