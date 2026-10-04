# PS5 migration ledger (Phase 0 baseline)

This ledger tracks every source transformation that must be accounted for
during migration from the reference PS5 port to a native Kodi fork branch.

## Source of truth

- Complete row set: `tools/ps5/migration-ledger.csv`
- Row count: **110**
  - `19` Kodi patch rows (`PATCH-*`)
  - `87` overlay file rows (`OVERLAY-*`)
  - `4` script-driven source-mutation rows (`SCRIPT-*`)

CSV columns:

1. `id`
2. `original_path_or_patch`
3. `purpose`
4. `destination`
5. `dependencies`
6. `disposition`
7. `implementing_commit`
8. `verification`
9. `status`
10. `source_kind`

## Disposition values

- `pending-review`: not yet decided (default at Phase 0)
- `migrate-unchanged`
- `adapt-native-integration`
- `supersede-with-evidence`
- `vendored-dependency`
- `pinned-downloaded-dependency`
- `deferred-with-reason`

## Status values

- `pending`
- `in-progress`
- `done`
- `blocked`

## Phase 0 completion statement

The ledger now includes one row for each discovered reference source
modification class and item from:

- `patches/kodi/manifest.txt`
- `overlay/**` file inventory
- configure-time source mutation actions in `scripts/20-configure-kodi.sh`

No entry was dropped silently; all migration decisions remain explicit and will
be updated commit-by-commit in subsequent phases.
