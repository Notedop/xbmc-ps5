# PS5 migration baseline (Phase 0)

Last updated: 2026-10-04

## Scope completed in this stage

1. Inspected migration instructions and repository guidance:
   - `PS5-Kodi-Fork-Migration-Plan.md`
   - `kodi/AGENTS.md`
   - `kodi/cmake/README.md`
   - `kodi/tools/depends/README.md`
2. Pinned the PS5 reference revision used for migration intake.
3. Identified the exact upstream Kodi SHA used by the known local working reference build tree.
4. Created baseline and dependency manifests plus a complete migration ledger.
5. Created a clean local upstream-based `ps5` branch checkout for migration work, without touching existing working trees.

## Recorded baseline revisions

| Item | Revision | Evidence |
| --- | --- | --- |
| PS5 reference revision (migration intake) | `3cbafb95fdf4d0a921843475f971c62b65fa5ce9` | `notedop/kodi-ps5` `origin/main` at local reference checkout; commit message `fix sandbox escape` |
| Local reference workspace HEAD | `773c1bbcfb6a98d0e21f5a08add02c2e66424cc8` | Local `kodi-ps5-src` has one local commit on top of `3cbafb9` |
| Exact upstream Kodi SHA for known working local build | `28ea2eac1eb7af8fdcbd2672d933ce87f594be79` | `/home/raoul/kodi-ps5-build/CMakeCache.txt` has `CMAKE_HOME_DIRECTORY=/home/raoul/kodi`; `/home/raoul/kodi` HEAD is this SHA |
| PS5 OpenGL pin used by reference scripts | `122aa899f9255e37d776b2a5317c86e5e9679907` | `patches/ps5-opengl/PS5-OPENGL-COMMIT` |
| Staged (not yet validated) newer Kodi submodule pin in local ref WIP | `e46bd9cc6cafa0073f386a974e0aa43546a4d531` | Staged gitlink in local `kodi-ps5-src` index (`.gitmodules` migration work) |

## Branch/repository setup done

- Created a separate migration checkout at:
  - `/home/raoul/kodi-fork/xbmc-ps5-fork`
- Branch created from recorded baseline:
  - `ps5` at `28ea2eac1eb7af8fdcbd2672d933ce87f594be79`
- Remotes configured:
  - `upstream -> https://github.com/xbmc/xbmc.git`
  - `origin -> https://github.com/00000080461_ups01/xbmc.git` (placeholder URL; repository not created in this run)

No existing checkout was reset, overwritten, or repurposed.

## Baseline environment evidence

| Component | Value |
| --- | --- |
| Host OS | Ubuntu 24.04.4 LTS (WSL2 kernel `6.6.87.2-microsoft-standard-WSL2`) |
| CMake | `3.28.3` |
| Ninja | `1.11.1` |
| Python (host) | `3.12.3` |
| Meson | `1.12.1` |
| Clang | `18.1.3` |
| NASM | `2.16.01` |
| SWIG | `4.2.0` |
| Java | `21.0.12.1` |
| PS5 SDK path | `/opt/ps5-payload-sdk` (contains `bin/ ldscripts/ samples/ target/ toolchain/`) |
| Active toolchain file in known working build | `/home/raoul/kodi-ps5-src/toolchain/ps5-kodi.cmake` |

## Inventory captured

- Overlay files inventoried: **87**
- Kodi patches inventoried from `patches/kodi/manifest.txt`: **19**
- Script-driven source mutation steps inventoried: **4**
- Total ledger rows generated: **110**

See:
- `tools/ps5/migration-ledger.csv`
- `docs/ps5/migration-ledger.md`

## Phase 0 blockers / uncertainties (precise)

1. **Historical release-to-Kodi-SHA mapping is not pinned in reference releases.**  
   Release/readme flow at `3cbafb9` states "clone Kodi master / 22.x" rather than a fixed commit, so exact historical upstream SHA for earlier public releases cannot be derived purely from release metadata.

2. **A newer Kodi pin (`e46bd9c`) exists only in local in-progress submodule migration work, not in the pinned reference revision (`3cbafb9`).**  
   It is recorded as a candidate but not selected as baseline for this migration stage.

3. **Remote GitHub fork creation was not completed in this run.**  
   A local branch checkout and remotes are prepared, but creating/pushing a real fork repository requires repository creation rights/action against `00000080461_ups01`.

## Next migration stage entry criteria

Proceed to Phase 1A/2 from this baseline:
- Baseline source SHA: `28ea2eac1eb7af8fdcbd2672d933ce87f594be79`
- Reference intake SHA: `3cbafb95fdf4d0a921843475f971c62b65fa5ce9`
- Ledger source of truth: `tools/ps5/migration-ledger.csv`
- Dependency manifest source of truth: `tools/ps5/dependency-manifest.json`
