# PS5 Kodi: migration to an official Kodi fork

Date: 2026-10-04

## Objective

Create a genuine fork of `https://github.com/xbmc/xbmc` and implement PS5 as a Kodi platform in its existing source and build structure. Use the existing PS5 port as a source of requirements, implementation examples, and observed failure cases. Preserve useful behavior while choosing maintainable implementations. Do not assume its dependency patches or workarounds are still necessary.

The final checkout must contain the actual Kodi source compiled for PS5. Configuration must not restore tracked files, copy an overlay into the checkout, or apply Kodi patches at build time.

This document is an implementation handoff. No remote fork has been created as part of writing this plan.

## Sources and evidence

- Official Kodi: https://github.com/xbmc/xbmc
- Kodi CMake documentation: https://github.com/xbmc/xbmc/blob/master/cmake/README.md
- Kodi dependencies documentation: https://github.com/xbmc/xbmc/blob/master/tools/depends/README.md
- Existing PS5 reference: https://github.com/Notedop/kodi-ps5
- Reference revision inspected for this plan: `3cbafb95fdf4d0a921843475f971c62b65fa5ce9`.

The reference README points to `VivaLaVent/kodi-ps5` for releases. Resolve the relationship between repositories, branches, and releases before selecting a migration baseline. Do not assume Notedop's default branch is the release baseline or contains every recent contribution.

Direct inspection of the reference revision found:

- `scripts/20-configure-kodi.sh` restores patch-touched files from Git, copies `overlay/`, changes `BuildStamp.h`, and applies 19 Kodi patches.
- The toolchain includes the open-source SDK's `prospero.cmake`. CMake's OS is FreeBSD-derived, while Kodi's `CORE_SYSTEM_NAME` and `CORE_PLATFORM_NAME` are `ps5`.
- The overlay includes PS5 platform and windowing implementations, platform CMake integration, and replacements for shared files such as `cmake/modules/FindIconv.cmake` and `system/advancedsettings.xml`.
- Supporting scripts build host tools, shims, FFmpeg, dav1d, graphics, Python, and SDK stubs; `scripts/30-deploy.sh` handles staging and title packaging as well as deployment.
- Useful reference documents include `docs/ARCHITECTURE-LESSONS.md`, `docs/upstream-comparison.md`, `docs/python-addons-status.md`, and `docs/validation.md`.

These findings describe source content, not independently verified PS5 runtime behavior. Reinspect the pinned baseline during execution.

## Working rules for the implementing LLM

1. Read applicable `AGENTS.md` and repository contribution instructions before editing.
2. Inspect implementations and registration points in the selected Kodi revision. Class names, hooks, and suggested locations below are starting points, not permission to invent APIs.
3. Preserve functionality as a validation goal, not a requirement to reproduce legacy mechanisms. Qualify dependencies from clean source before adopting workarounds. Separate dependency qualification, Kodi integration, unrelated features, and later upgrades into reviewable changes.
4. Keep the existing port and any developer's locally modified Kodi checkout intact. Use separate checkouts/worktrees. Never run the legacy configure script in the new fork or a checkout with valuable local edits.
5. Use the existing Kodi interfaces for platform, windowing, audio, codecs, renderers, networking, storage, and text input where available. Small platform guards at selection points can be appropriate; avoid broad abstractions without a demonstrated need.
6. Preserve author attribution, license headers, and dependency notices. Reference the original revision and patch/file in migration commit messages or the migration ledger.
7. Use one monolithic Kodi fork. Track PS5-specific dependency source, customizations, build recipes, and title packaging in that repository. Keep installed compilers/SDKs, sysroots, downloaded standard dependency caches, binaries, credentials, and generated outputs outside tracked source. See the dependency policy below.
8. Do not expand sandbox privileges or implement a new loader protocol during migration. Preserve the reference's existing optional integration and its failure behavior; document it as a runtime dependency.
9. Continue through local work and available checks. If blocked by missing credentials, an unknown working Kodi revision, unavailable dependencies, or console access, record the precise blocker and complete independent work. Never report an unexecuted test as passed.

## Follow Kodi's platform and CMake conventions

PS5 is another target platform within Kodi. Follow [Kodi's CMake README](https://github.com/xbmc/xbmc/blob/master/cmake/README.md), its linked build guides, and the corresponding documentation and actual CMake code at the selected Kodi revision. Current master documentation provides guidance; version-specific source determines the available hooks and options.

- Configure the existing top-level Kodi `CMakeLists.txt` with a PS5 cross toolchain and an out-of-source build directory. Use the normal CMake build and install commands.
- Integrate PS5 platform selection, source lists, compile definitions, dependency requirements and installation through Kodi's existing platform mechanisms. Inspect adjacent FreeBSD/Linux and other cross-compiled targets for relevant patterns, without copying assumptions specific to their runtimes.
- Use the documented `CMAKE_TOOLCHAIN_FILE`, `CMAKE_BUILD_TYPE` and feature-option conventions. Platform definitions such as `TARGET_PS5` should come from the platform configuration rather than arbitrary flags scattered through scripts.
- Retain Kodi's `ENABLE_<OPTION>=ON/OFF/AUTO` behavior for existing features. Required enabled dependencies must fail configuration when unavailable; explicitly disable unsupported facilities and record the selected release configuration.
- Reuse Kodi's dependency targets/discovery and `tools/depends` conventions where compatible. Vendoring source does not require adding every project with `add_subdirectory`: Meson, Make and configure-based dependencies can retain their native builders behind the dependency preparation rules.
- Extend the existing platform install/package mechanisms for PS5 resources and native title output. PS5's startup/link/conversion requirements are platform integration responsibilities inside the shared build, with declared inputs and outputs.
- Register suitable portable tests through Kodi's Google Test/CTest conventions. Keep console-only validation separate and report its execution status.
- A script wrapper or preset may simplify configuration; the same build must remain usable through documented CMake commands. Do not create a parallel root application build system.

The intended developer interface is standard CMake configure/build/install plus a documented PS5 packaging target. Exact toolchain paths and target names must be implemented and verified before publishing runnable commands. One repository and one application build graph remain the goal, even when preparing dependencies invokes several native build systems.

## Monolithic repository and dependency policy

This section reflects the user's preference and supersedes any earlier suggestion to maintain the custom graphics implementation in a separate working repository.

### What belongs in the fork

| Component | Repository policy |
| --- | --- |
| Kodi and PS5 platform code | Normal tracked Kodi source |
| Custom ps5-opengl implementation | Vendor the pinned upstream source under `tools/depends/target/ps5-opengl/vendor/` or another upstream-consistent dedicated location |
| PS5 native application boilerplate/runtime | Vendor the required open-source source and packaging tooling, with its own provenance |
| PS5 shims and clean-room link stubs | Track source and build integration in the fork |
| dav1d, FFmpeg, Python, Mesa and other standard dependency inputs | Track exact versions, checksums, recipes and required patches; fetch into a build cache unless an actively maintained custom fork needs vendoring |
| Compiler and installed open-source SDK | Pinned host prerequisite or automated bootstrap recipe; installed output stays untracked |
| Title metadata, icons, conversion/staging scripts | Track in Kodi's PS5 packaging area |
| Built libraries, ELF, eboot.bin, staging folders, release ZIPs | Generated build outputs and release artifacts; never source commits |

A single source repository does not automatically mean an offline build. Audit ps5-opengl's own fetches, nested dependency manifests and application-template downloads. Every transitive input must be pinned. After preparation, allow builds from a populated cache without network access. A completely offline source distribution can be a separate release bundle if needed.

### Import and update ps5-opengl

Prefer a Git subtree import: source is physically present in a normal clone, while maintainers can synchronize it with its original repository. Git submodules do not meet the desired single-checkout workflow. A provenance-preserving vendor import is acceptable if subtree tooling proves unsuitable.

1. Start from clean ps5-opengl upstream source. Select and pin a maintained revision after checking its API, toolchain, and Kodi requirements. The legacy revision `122aa899f9255e37d776b2a5317c86e5e9679907` is historical comparison evidence, not the prescribed new dependency baseline.
2. Record the upstream URL, SHA, import method, applicable licenses, and nested dependency inputs.
3. Do not run `kodi-additions.py` against the new graphics source. Audit each old transformation as a requirement or bug hypothesis. First test whether clean upstream already supplies the needed behavior. Prefer supported APIs or changes in Kodi's graphics integration. Only introduce a focused vendor change for a demonstrated gap, with a reproducer and recorded alternatives.
4. Invoke the graphics project's existing build system from Kodi's orchestration; it need not be rewritten into CMake. Use an isolated build/install prefix and expose the resulting package to Kodi as `PS5OpenGL::OpenGL`.
5. Model real archive outputs as dependencies, including archives referenced by linker-script GROUP inputs. A modified graphics source or configuration must rebuild graphics and relink the final PS5 executable.
6. Update graphics independently on a task branch: import/merge a selected upstream revision, resolve conflicts with local commits, run graphics checks, build Kodi, and test on the console.

Preserve each component's licenses and notices. Inspect the licenses at the exact imported revisions, including static-link and redistribution terms; the current graphics repository advertises GPL-3.0 and must not simply inherit Kodi's license label.

### One final application build

Make Kodi's standard CMake configure/build/install interface the canonical application build entry point. Expose PS5 title packaging through that build system. An optional `tools/ps5/build.sh` may orchestrate prerequisite preparation and invoke those commands, but must remain a thin convenience wrapper; it must not become a second implementation of Kodi's build logic.

The underlying graph must distinguish:

1. Host tools and prepared SDK/toolchain prerequisites.
2. Target dependencies, including graphics and shims, built into a configuration-specific local prefix.
3. Kodi compilation and the actual native-title executable link.
4. ELF import/layout validation and conversion to `eboot.bin`.
5. Staging of modules, title metadata/artwork, Kodi resources, certificates, and optional Python assets.
6. Package validation, release ZIP creation, checksums, and a revision/configuration manifest.

Expose explicit build-system targets, for example `ps5-title` and `ps5-package`, through the selected CMake/Ninja integration. These names are proposals. Packaging must depend on successful completion of the final executable and required assets. Propagate tool failures; an old eboot.bin must never make a failed link look successful.

The current reference first links `kodi.bin`, then extracts link inputs and relinks through a prepared OpenGL demo template. Preserve those link semantics for parity, but turn them into declared build rules. The preferred end state is a direct native-title link target; if Kodi needs an intermediate link check, retain it as an explicit internal target. Never rely on parsing a generated Ninja link line or a previously built demo directory as the permanent interface.

The vendored application runtime/template must be usable directly, without building an ImGui example. Preserve startup code, heap/memory behavior, linker script, RELRO handling, system imports and converter behavior during the transition.

Write artifacts beneath a build directory, for example `build/ps5-release/dist/PPSA99420/` and `build/ps5-release/artifacts/`. The release ZIP should contain the folder title expected by the chosen loader; do not claim it is a conventional installable PKG unless that format is separately implemented and verified.

Deployment is a separate command/target requiring an explicit console destination. The default build/package command must never upload automatically. CI and developer builds must call the same packaging pipeline.

## Incremental builds and reusable preparation

The first preparation may take hours; normal development must reuse it. This is an acceptance requirement, not a later optimization. Starting dependency qualification from clean upstream source means an intentional initial baseline build, not cleaning source or dependency outputs on every invocation.

### Separate lifetimes

| State | Lifetime and invalidation |
| --- | --- |
| Host packages/compiler/open-source SDK | Prepare once for a pinned host/toolchain configuration; verify prerequisites on later calls without reinstalling |
| Download cache | Share immutable checksum-verified inputs across configurations and worktrees |
| Dependency build trees | Persist per dependency/configuration; invoke the native incremental builder when inputs change |
| Installed dependency artifacts | Reuse only for matching source, options, toolchain/SDK and dependency ABI inputs |
| Kodi build tree | Persistent out-of-source CMake/Ninja build; ordinary source/header dependency tracking |
| Stage/release output | Update from successful build outputs and resource changes; package only when requested |

Keep these locations untracked and configurable. Prefer a reusable dependency workspace outside an individual Kodi build tree so replacing that tree does not discard hours of dependency work. Share immutable completed installations or downloads safely; do not let concurrent worktrees mutate the same dependency build/install directories without locking.

### Preparation and invalidation rules

- Bootstrap, fetch and dependency preparation must be resumable and idempotent. A failed FFmpeg or Python step must not restart completed graphics, dav1d or host-tool steps.
- Use a per-dependency signature covering source revision/content, retained patch content, recipe/configuration, target ABI/CPU options, compiler/SDK identity, and relevant dependency inputs. Do not include the entire Kodi Git SHA in every dependency key.
- Separate host and target artifacts. Distinguish incompatible profiles, including graphics display/build options, and record each dependency's actual build mode. A Kodi Debug/Release change need not rebuild dependencies that deliberately use a shared compatible configuration.
- Completion metadata is written only after successful build/install verification. A stamp alone is insufficient if required artifacts are absent or incompatible.
- Detect local edits in vendored source. A pinned upstream SHA does not describe a developer's modified graphics files. Native dependency builders must observe those changes and regenerate outputs correctly.
- Do not use default unconditional cleanup, source re-extraction, force reinstall, git reset, global cache clearing, or deletion of the dependency prefix. Clean/rebuild operations must be explicit and scoped to a selected component.
- Keep unchanged generated headers, wrappers and metadata unchanged on disk. Avoid embedding the current time during every configure, which would force recompilation on otherwise identical inputs.
- Track real headers/archives/resources and generated outputs in the build graph, including linker-script archive members. If using CMake ExternalProject or custom commands, account for local source changes, step dependencies and byproducts; default step stamps are not proof that incremental behavior is correct.
- Report why preparation is reused or invalidated. Unexpected expensive rebuilds must be diagnosable from logs.
- Compiler caching may accelerate repeated compilation but supplements correct dependency tracking; it does not replace it. Cache misses must still produce a correct build.

### Expected developer workflow

Prepare the environment and dependency prefix initially, then configure Kodi against it. Daily work normally invokes `cmake --build <existing-build-directory>`. CMake may regenerate its build files when needed; that must not trigger a full dependency setup. A unified convenience command may check all prerequisites each time and skip completed compatible work.

Packaging is an explicit target that depends on the application and runtime assets. Invoking it may build changed inputs first, but must not repeat bootstrap. An unchanged package can be reused when its full inputs match. Deployment remains separate.

### Required verification

| Change/action | Expected work |
| --- | --- |
| Repeat preparation with identical inputs | Reuse verified completed steps; no dependency recompilation |
| Repeat Kodi build without changes | No compile or relink; quick build-system checks only |
| Edit one PS5 implementation file | Recompile affected Kodi objects and relink the native title |
| Edit a shared header | Recompile its actual dependants |
| Edit vendored graphics implementation | Incremental graphics rebuild, then relink title; unrelated dependencies reused |
| Change dav1d revision/configuration | Rebuild dav1d; rebuild/reconfigure FFmpeg only as required by its affected inputs; relink affected consumers |
| Change Python patch/configuration | Rebuild affected Python inputs and consumers; unrelated graphics/decoder dependencies reused |
| Change icon or title metadata | Restage/repackage affected assets; no unrelated Kodi/dependency compilation |
| Interrupt dependency preparation | Resume unfinished steps while retaining verified completed components |
| Change SDK/compiler/target ABI | Select or rebuild compatible affected artifacts; never reuse incompatible binaries |

Measure reference and new no-op/edit/relink timings on the same host, recording which steps ran. Seconds are a goal for small edits, not a promised threshold: linking and packaging may take longer. Acceptance requires correct rebuild scope, resumability, and no unexplained full rebuilds.

## Phase 0 — establish a reproducible baseline

### Work

- Inspect the reference repository's current branches, tags, release metadata, scripts, patches, and overlay. Select one revision and record its full SHA.
- Find the exact upstream Kodi SHA used for a known working reference build. Inspect build manifests, release notes, CI, and the maintainer's actual Kodi checkout. A version label such as “Kodi 22” is insufficient.
- Do not automatically use current upstream master. Reproduce the reference on its original Kodi base first. If that base cannot be recovered, label any chosen compatibility base as provisional and validate it explicitly.
- Record the build host, tool versions, SDK revision, pacbrew revision/packages, graphics revision and modifications, FFmpeg configuration, Python version and patches, shim/stub revisions, packaging template, and feature flags.
- Capture a reference build, configure/build logs, and available console test results. Preserve symbol files for diagnosing regressions.
- Inventory all overlay files, every patch in `patches/kodi/manifest.txt`, script-driven source modifications, and dependency modifications. Compare the overlay against pristine upstream: an overlay path may replace a shared file rather than add a PS5 file.
- If a known working prepared source tree exists, capture its complete difference against pristine upstream, including untracked additions. Reconcile that difference with the declared inventory to catch manual changes.

### Deliverables

- `docs/ps5/migration-baseline.md`: source SHAs, environment, exact commands, known behavior, unresolved questions.
- `docs/ps5/migration-ledger.md`: one row per source modification, plus dependency/build/packaging entries.
- A machine-readable dependency manifest under `tools/ps5/`, with immutable revisions and effective build options. Choose its format after inspecting existing conventions.

Ledger columns: original path/patch, purpose, destination, dependencies, disposition, implementing commit, verification, status. Dispositions: migrate unchanged, adapt to native integration, superseded with evidence, vendored dependency, pinned downloaded dependency, or deferred with reason. Nothing disappears silently.

### Exit gate

The reference and upstream base are identified, or the unresolved base is clearly marked as a blocker. Every source transformation is accounted for.

## Phase 1 — create the fork and branch layout

### Work

- Create a new GitHub fork of `xbmc/xbmc` under the user's chosen account or organization. Use a new repository name if `kodi-ps5` is already occupied. Preserve the existing wrapper repository.
- Verify that the resulting repository is in the official Kodi fork network and retains upstream history. Importing the wrapper repository into an empty repository does not meet this requirement.
- Clone the fork separately. Configure `origin` as the fork and `upstream` as `https://github.com/xbmc/xbmc.git`.
- Create a long-lived `ps5` branch from the exact Kodi baseline SHA. Keep the inherited upstream branch free of PS5 commits.
- Use task branches and reviewable commits for migration. Confirm before any remote mutation whose destination is ambiguous. Do not rewrite existing shared branches.

### Exit gate

The new repository retains Kodi ancestry, remotes are correct, and `ps5` starts from the recorded baseline. If GitHub access is unavailable, prepare the local upstream-based branch and document the pending remote creation step.

## Phase 1A — qualify the complete dependency graph

This is a required stage before implementing dependency-sensitive Kodi changes. The legacy build can be reproduced separately as a comparison tool; it must not determine the new build's implementation by default.

### Inventory and dependency ordering

Cover dav1d, FFmpeg, CPython/python3, ps5-opengl, the application runtime, SDK/stubs, compatibility libraries, and their transitive inputs. Also audit curl, OpenSSL, libsmb2, libiconv, Brotli, tinyxml, libuuid and other packages discovered in the actual link/build graph. This list is a starting point, not a complete dependency declaration.

Record source version/SHA and checksum, license, host or target role, consumers, enabled features, build options, required artifacts and staging rules. Distinguish host Python/SWIG/Java generators from target CPython. Dependency versions must satisfy the selected Kodi revision; “latest” is not a compatibility policy.

Establish each dependency initially from clean source in an isolated compatible configuration, then retain its build tree and verified installation for incremental reuse. Avoid silently consuming legacy libraries from a globally modified SDK/sysroot. The build graph must place dav1d before FFmpeg when FFmpeg enables libdav1d, and must declare other library relationships from the actual chosen configuration.

### Patch decision procedure

Create `docs/ps5/dependency-decisions.md`. For every legacy patch, configure cache override, forced include, symbol wrapper, disabled feature, or shim:

1. State the exact failure it was intended to address and identify its source evidence.
2. Check whether the chosen upstream source, SDK, or runtime has already addressed it.
3. Build and exercise the clean implementation. A successful compile does not establish runtime compatibility.
4. If it fails, capture the error/log and a focused reproducer. Distinguish missing ABI/API behavior from build detection, incorrect paths, integration mistakes, and obsolete assumptions.
5. Prefer a supported configuration or embedding API, then a narrowly scoped platform adapter or correct SDK/runtime implementation. Use an upstreamable dependency change where appropriate. Keep a local patch only when the simpler approaches cannot meet the requirement.
6. Record the chosen solution, alternatives, scope, regression check, owner/update implications, and status: unnecessary, configuration, integration adapter, SDK/runtime fix, local patch, or unresolved.

Do not replace an honest unsupported operation with a successful no-op merely to satisfy the linker. Configure cache answers must have evidence for the target; do not force OpenSSL or process/thread probes to pass without verifying the resulting behavior.

### Dependency-specific qualification

| Dependency | Required investigation and checks |
| --- | --- |
| ps5-opengl | Clean upstream SDK build and minimal title first; GL/EGL package/import compatibility, context/startup, framebuffer/texture requirements, VideoOut access, display sizing, HDR/VRR hooks, presentation and teardown. Map every legacy customization to an existing API or demonstrated missing capability. Use a graphics example as a test, not a packaging prerequisite. |
| dav1d | Supported Meson cross configuration, target CPU/assembly options, host assembler, threading, static archive/pkg-config exports; exercise representative AV1 decode on target. |
| FFmpeg | Kodi-compatible version and required components; cross configuration and link dependencies, libdav1d wiring, software decode, demux/seek and threading. Determine which hardware decoding functions belong in Kodi's PS5 codec implementation rather than FFmpeg patches. |
| CPython | Kodi-compatible version; embedding initialization/path configuration, static module registration, cross-build versus host generators, file I/O and errno, threading/TLS, sockets/selectors, SSL, standard-library staging and unsupported process operations. Evaluate each existing patch separately. |

For Python, explicitly audit `cross-freebsd.patch`, `ps5-socket-libscenet.patch`, `ps5-stdio-nomacros.patch`, `static-compile.patch`, `no-subprocess.patch`, forced headers, configure overrides, and the associated shim/wrapper code. These names come from the inspected reference; recheck the chosen baseline.

Investigate whether documented CPython embedding configuration can handle path/startup issues, whether supported static-module configuration can replace source edits, and whether the SDK/runtime can correctly implement an ABI gap shared by multiple consumers. A CPython-only networking adaptation must remain local to CPython: process-wide socket wrapping can affect Kodi's curl and SMB clients. Validate behavior before choosing any replacement.

Python console checks must cover interpreter startup and failure reporting, repeated execution/shutdown behavior, Unicode/file operations, imports, socket connection and timeout behavior, HTTPS certificate verification, and a representative pure-Python Kodi add-on. Test unsupported functionality for clear failure. Record optional compiled-extension support separately.

### Exit gate

The manifest covers the complete discovered dependency graph. Each carried workaround has evidence and a maintenance rationale. Clean ps5-opengl has been evaluated without the legacy mutation script. Dependencies are either qualified on the target or explicitly awaiting named runtime tests. Unverified alternatives remain proposals rather than being described as proven fixes.

## Phase 2 — make the source explicit

### Work

- In a disposable reference checkout at the baseline SHA, reproduce the complete source transformations in their original order: overlay first, then patches, plus any recorded script edits. Check every patch application; reject failed or fuzzy application that has not been reviewed.
- Compare the resulting tree against pristine upstream. This effective tree is the behavioral reference for migration.
- Move useful platform implementations into upstream-consistent locations in the fork. Review each Kodi patch against the selected source and qualified dependencies; migrate its necessary behavior as tracked edits, or record why it is obsolete or replaced. Account for every patch without requiring every patch to survive.
- Treat replacement shared files as diffs: retain upstream behavior and scope the PS5 additions. Pay particular attention to `FindIconv.cmake`, shared settings, renderer shaders, filesystem factories, refresh selection, Python diagnostics, and curl changes.
- Where intermediate changes depend on later patches, group them into coherent commits. Build at meaningful boundaries rather than pretending every original patch is independent.
- Compare the new fork against the prepared reference tree. Document every intentional difference. No build-time Kodi patch stack or overlay copy step remains.

### Patch groups to account for

| Reference patches | Migration concern |
| --- | --- |
| 0001–0005 | Host TexturePacker packaging, monotonic clock, Neptune/BSD integration, logging, UTF-16 wchar behavior |
| 0006 | libsmb2 integration with Kodi's SMB file and directory factories |
| 0007–0014 | PBO/rectangle texture constraints, refresh/VRR policy, HDR framebuffer and HLG shader behavior; inspect their cumulative effect |
| 0015 | Native PS5 keyboard integration and lifecycle |
| 0016 and 0019 | curl connection closure and multi-wait behavior; preserve thread and latency assumptions |
| 0017 | Experimental in-process binary add-on loading; retain its feature status and verify whether it is active |
| 0018 | Python initialization diagnostics |

### Exit gate

The ledger accounts for all 19 patches and all overlay/script changes. The fork contains the selected PS5 implementations, with reviewed explanations for retained, replaced, and omitted legacy changes.

## Phase 3 — integrate the native Kodi build

### Proposed placement

Validate every location against the chosen upstream revision.

| Existing reference area | Intended destination/responsibility |
| --- | --- |
| `overlay/xbmc/platform/ps5/` | `xbmc/platform/ps5/`: startup, platform services, input/IME, audio, storage, network, SMB2, video, compatibility code, SCE declarations |
| `overlay/xbmc/windowing/ps5/` | `xbmc/windowing/ps5/`: EGL/GL context, output information, framebuffer, sync, HDR |
| `overlay/cmake/platform/ps5/` | `cmake/platform/ps5/`: platform selection and requirements |
| `overlay/cmake/scripts/ps5/` | `cmake/scripts/ps5/`: architecture/path setup, macros, installation |
| `overlay/cmake/treedata/ps5/` | `cmake/treedata/ps5/`: source and test selection |
| `overlay/cmake/installdata/ps5/` | `cmake/installdata/ps5/`: runtime data installation |
| `toolchain/ps5-kodi.cmake` | Kodi-owned cross-compilation integration at a location consistent with upstream conventions |
| Host/dependency/package scripts and title metadata | `tools/ps5/` or established Kodi packaging locations; separate responsibilities |
| PS5 runtime defaults | Platform-specific installation/configuration; shared defaults stay suitable for other targets |

### Work

- Preserve the distinction between the SDK's CMake OS and Kodi's PS5 platform. Do not replace all FreeBSD/POSIX handling with PS5 conditionals or label Kodi's target as ordinary Linux.
- Wire source registration and platform factories through Kodi's existing CMake/platform mechanisms. Check entry point, platform initialization, teardown, logging, and all factory registrations.
- Separate host tools from target libraries. TexturePacker, JsonSchemaBuilder, bindings generators, and native Python tooling must execute on the host.
- Prevent accidental use of host libraries or pacbrew's software GL. Preserve deliberate PS5OpenGL/EGL package selection and audit include/link paths.
- Adapt the configure script into a thin wrapper around CMake. It can generate build-directory support files, but cannot mutate tracked source.
- Generate build/version metadata in the binary directory with explicit build dependencies. Remove configure-time rewriting of tracked `BuildStamp.h`.
- Use the qualified dependency recipes from Phase 1A. Reuse compatible build machinery, but do not inherit patch sets, forced configure answers, or global sysroot mutations without validation. Prefer Kodi's existing dependency mechanisms where they fit; preserve other native build systems behind the unified orchestrator.
- Add proper dependencies on static archives, shim/stub libraries, generated headers, and export tables. Library changes must trigger relinking without deleting `kodi.bin` manually.
- Make optional features explicit and record their effective values. Avoid silently changing Python/add-on support because a cached artifact happens to exist.

### Exit gate

A clean PS5 configure/build works with pinned dependencies; repeated configuration leaves tracked source untouched; compiler and linker inputs are target-correct. Missing required prerequisites produce actionable failures.

## Phase 4 — preserve packaging and runtime paths

### Work

- Separate staging/packaging from transfer to the console. Packaging must run without a console or FTP credentials.
- Preserve entry point and executable conversion/link requirements, including packaging template and RELRO details.
- Stage the title metadata, executable, Kodi runtime resources, CA bundle, Python standard library, and required add-on assets. Validate staging against the reference inventory.
- Retain current path semantics, including `/app0` resources and `/download0/.kodi` user data, after confirming them in source. Avoid changing title identity or save-data mapping during migration without a specific reason.
- Keep sandbox-dependent USB/local access optional and observable. Network sources must remain usable when optional daemon integration is unavailable.
- Produce a build manifest containing source and dependency revisions, feature configuration, and artifact hashes. Keep debug/symbol output alongside release artifacts.

### Exit gate

Packaging creates a complete installable folder title and release ZIP from the fork, with no reference-port checkout or prebuilt graphics demo required. Console deployment remains a separate explicit action.

## Phase 5 — demonstrate behavior and build parity

Record firmware, loader/HEN, optional daemon, display settings, media sample, source, logs, and pass/fail for each console test. Use the same configuration for reference and migrated builds.

| Area | Minimum verification |
| --- | --- |
| Startup/lifecycle | Launch, navigation, clean exit, relaunch, settings persistence |
| Input/IME | DualSense navigation; keyboard accept/cancel, repeated opening, Unicode text and shutdown cleanup |
| Storage/network | SMB2/3 and available NFS/HTTPS sources; writable user data; USB with optional integration; usable behavior without it |
| Playback | Representative H.264, HEVC 8/10-bit, VP9, and software-decoded media; pause, seek, stop, next video, repeated playback |
| Audio | Stereo and available surround channel mapping, synchronization; passthrough stays unconfirmed unless actually tested |
| Display | SDR/HDR and available VRR behavior, OSD, refresh policy; no new display promises |
| Add-ons | Python initialization, representative pure-Python add-on, installation/update; experimental binary loader status documented |
| Build correctness | Clean build, incremental edit, static-library relink, deterministic dependency selection, tracked tree clean after configure/build/package |
| Other platforms | Configure/build an available supported upstream target, preferably Linux on the build host; exercise generic files touched by PS5 changes |

Reuse the reference's detailed validation matrix. Compilation is not proof of console parity. If hardware testing is unavailable, mark the build as awaiting runtime validation.

### Exit gate

Available reference behavior is retained, regressions are resolved or explicitly accepted, and unverified areas are visible. Relevant non-PS5 checks pass or have precise environment blockers.

## Phase 6 — improve architecture after parity

- Review shared-code changes one subsystem at a time. Use existing capability queries or interfaces where they fit; add a generic hook only when it solves a concrete integration problem.
- Keep PS5-specific SCE calls and compatibility logic in the platform implementation. Audit process-wide symbol wrapping for unintended effects on curl, SMB, Python, and other clients.
- Isolate experimental JIT/binary-loader work from the stable configuration. Preserve feature status rather than promoting a host-tested experiment to supported console functionality.
- Keep only proven necessary PS5 graphics customizations as ordinary commits in the vendored subtree. Standard downloaded dependencies may retain tracked, version-specific patches; build-time transformations of Kodi source or vendored PS5 graphics source may not.
- Preserve the readable upstream diff. Do not reformat unrelated source or reorganize Kodi's existing interfaces during migration.

### Exit gate

Cleanup changes have their own rationale and tests. The PS5 implementation fits the selected Kodi architecture without unnecessary core churn.

## Phase 7 — maintenance and contributor handoff

- Document host setup, pinned dependency preparation, configure/build/package commands, console installation, logging, known limitations, and feature flags in `docs/README.PS5.md` or the upstream-consistent equivalent.
- Add CI using the verified toolchain: build checks and packaging validation where dependencies are available, plus meaningful host tests for portable logic. A configure-only check must be labeled as such.
- Default to merging upstream updates into the shared `ps5` branch. Rebase unpublished task branches as useful; do not force-push a rebased public integration branch by default.
- Perform an upstream update on a disposable integration branch/worktree: inspect the diff, merge the selected upstream revision, resolve conflicts, rebuild, and rerun affected console tests before merging into `ps5`.
- Keep dependency upgrades separate from Kodi source upgrades. Pin CI inputs and retain release manifests.
- Retire the wrapper workflow only after parity is demonstrated. Preserve it as historical reference with a clear pointer to the new fork; do not delete its history.

## Definition of done

- A genuine official-Kodi fork retains upstream ancestry and a documented PS5 branch.
- The compiler reads tracked source directly from that fork.
- All original modifications are accounted for in the migration ledger.
- Configure/build/package do not restore or patch tracked Kodi files or copy an overlay over them.
- Dependency versions, feature flags, and runtime assets are reproducible and documented. Every retained dependency workaround has a recorded failure, solution rationale, and validation; obsolete patches are omitted.
- Clean builds and meaningful incremental rebuilds work; a dependency archive change triggers relinking. Repeated preparation and no-op builds reuse completed work, dependency edits invalidate only affected consumers, and failed/interrupted setup is resumable.
- PS5 behavior is validated against the reference; unavailable tests and existing limitations are labeled honestly.
- Relevant non-PS5 behavior remains intact.
- Contributors can build from the new fork without the legacy wrapper checkout.

## First message to give the implementing LLM

> Implement the attached PS5 Kodi fork migration plan. Inspect both repositories and applicable instructions first. Follow Kodi's cmake/README.md, linked build guides, and platform conventions at the selected revision; integrate PS5 as another platform in the existing top-level CMake build. Begin with Phase 0: identify the exact working upstream Kodi SHA, pin the PS5 reference revision, and create the baseline manifest and complete migration ledger. Preserve all existing checkouts and local edits. Then create or use a genuine fork of xbmc/xbmc, create a ps5 branch from the recorded baseline, and migrate the effective source into Kodi's platform structure. Use the monolithic dependency policy: vendor clean selected upstream PS5 graphics and application runtime source, then qualify the complete dependency graph including dav1d, FFmpeg and CPython. Do not automatically apply the legacy graphics or Python patches. Record evidence for every retained workaround and prefer supported APIs/configuration or narrowly scoped platform fixes. Do not mix migration with an upstream upgrade, add features, or run the legacy configure script in the new fork. Implement reusable, resumable prerequisite/dependency preparation and persistent incremental builds from the start; normal builds must not repeat full setup. Verify the no-op and scoped-rebuild matrix. Proceed in reviewable stages, update the ledger and validation results as you go, and report actual blockers precisely. Completion requires a build from tracked fork source with no Kodi overlay or patch application at configure time, followed by documented parity checks. Start doing the work rather than returning another high-level plan.
