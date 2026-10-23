# PS5 native SPRX architecture and qualification

The normal PS5 build requires the shared native architecture:

```text
system-only EBOOT
  -> load shared core (libkodi-runtime.sprx)
  -> load shared graphics (libkodi-gl.sprx)
  -> load private application (libkodi-app.sprx)
  -> enter Kodi through its private startup interface
```

Independently built add-ons are signed native SPRX modules loaded with the
public native module APIs. No software ELF mapper, candidate-specific title
exports, GOT patcher, private syscall or late-binding fallback is used.
The [build guide](README.PS5.md) and [catalogue/publication workflow](PS5-BINARY-ADDONS.md)
are the authoritative commands. The fresh standard title and all 34 supported
add-ons passed local build/package qualification; the native catalogue was
published on 2026-10-27. See the workflow's qualification record for exact title,
SDK and publication identities. This does not establish console acceptance of
the rebuilt bytes.

Console testing confirmed that installing a native add-on without execute bits
causes native load error `0x8002000d` (`EACCES`); setting its SPRX to `0755`
allowed the same Greynetic binary to load. Native installs now preserve executable
mode, and the PS5 loader repairs execute bits lost during Kodi's ZIP-copy install.
Host filesystem/install/package regressions cover this repair, but the updated
title and the remaining add-ons still require user-operated console qualification.

## Shared-runtime title profile

The launcher links only against system contracts. It loads the providers before
loading the private application so the native loader can bind application imports
before constructors execute. Loading providers after executable startup did not
retroactively rebind the executable's zero-valued imports; that path is not used.
The launcher performs no symbol lookup.

`title_native_startup.h` defines a synchronous, versioned private handoff:
the application validates request size/version/output address before constructors,
then publishes run/finalize callbacks. Both records are 16 bytes on x86-64.
Failed initialization leaves callbacks null. Neither side retains caller-owned
request/output storage. This is not an add-on symbol bridge.

The application and shared providers live until process teardown. Application
finalization is idempotent and distinct from unloading; its native stop function
rejects unloading. The exact hidden `__ps5_native_process_lifetime_v1` marker is
required for its limited shared-TLS-destructor exemption. Generic unloadable
add-ons do not get that exemption.

Firmware additionally requires the app-local `sce_module/libc.prx` loader
companion. It registers the loader heap API using native system allocators, owns
no independent heap/TLS/C++ runtime and exports only its version marker.
It refuses unloading because the system retains its table until process teardown.

## Provider ownership and compatibility

| Owner | Required responsibility |
|---|---|
| Shared core | libc++, libc++abi, unwinder, shared heap, emulated TLS and shared compatibility services |
| Shared graphics | GL/EGL/GLAPI, Mesa/compiler and native driver state shared by Kodi and add-ons |
| System contracts | Declared module/library identities, versions and native C/kernel/platform imports |
| Private application | Kodi implementation and private startup/finalize interface, not public add-on exports |
| Add-on module | Its own Kodi callbacks, CRT/lifecycle and declared provider requirements |

The core's `ps5-libc-net-v1` DNS capability owns `getaddrinfo`, `freeaddrinfo`,
`getnameinfo`, `gai_strerror`, `gethostname`, `gethostbyname` and `__h_errno`.
Clients must not compile private copies of that implementation.

All participating clients use the same allocator wraps: `malloc`, `calloc`,
`realloc`, `free`, `posix_memalign`, `malloc_usable_size`, `aligned_alloc`,
`memalign` and `reallocf`. The core initializes one direct-memory mspace before
constructors and exports the `__wrap_*` implementations. Foreign-pointer
operations use native libc through `__real_*`; failed initialization does not
silently select a second heap. The common contract also wraps `chdir`, `fopen`,
`pthread_create` and `exit`, preserving shared working-directory state, stack
policy and thread cleanup.

`shared_tls.c` owns emulated storage and destructor callbacks. The thread
trampoline drains them on normal return. Wrapped `exit` drains calling-thread
TLS before native global finalization; immediate `_exit`/`_Exit` and abnormal
termination are not interposed. Wraps do not redirect internal system-module calls.

ABI identity is `ps5-sdk-native-frontend-v1`: native PS5 frontend, 16-bit
`wchar_t`, FreeBSD 9/Sony defines and native alignment. `native-clang.py`
preserves that frontend while using the LLVM object backend for modern
constructor arrays. Generic FreeBSD source compilation is not an ABI substitute.
The native stdio overlay keeps `FILE` opaque and uses 24-byte, 8-byte-aligned
`fpos_t`. Dependencies built with exposed FreeBSD stdio macros need rebuilding;
symbol resolution cannot repair their inlined field accesses.

`build-native-stdio-depends.py` creates isolated replacements for audited curl,
OpenSSL crypto, gettext, SMB2, fmt, PNG and both bzip2 archive inputs.
The map hash-binds original/rebuilt archives and the SDK overlay. Normal CMake
library targets retain link order while selecting those replacements; baseline
prefixes/headers are not overwritten. New dependencies still need ABI assessment.

Add-on compatibility uses provider module/library identities, versions and
capabilities, not a particular Kodi executable hash. Exact provenance hashes
identify build inputs without making every compatible rebuild a new ABI.
Current system contract defaults use module version 257 and library version 1;
preserve declared values rather than guessing from filenames.
`resource.ps5.addon-runtime` version 2.0.0 requires the upgraded native title.
Metadata alone cannot protect an old client whose runtime predates this contract.

## Preparation, conversion and package proof

`prepare-native-runtime.py` orchestrates existing builders in fingerprinted
isolated trees. It publishes SDK/dependency pointers only after complete
validation and retains failed stages. Source/SDK inputs changing during
preparation prevent publication. Ordinary payload/graphics SDK import libraries
provide declared system contracts, or explicit `--system-sdk` roots can supply
them. They are declarations, not proof of installed firmware implementation or
cross-firmware calling semantics.

Firmware-analysis files are development-analysis-only. Normal preparation,
builds and validation do not read them; neither SDK nor title nor source commits
may include them. Standalone analysis utilities are not part of this build graph.
Validation of our own generated native modules remains required.

LLD links ordinary intermediate ELF images with `native-shared.ld`.
The converter preserves code/data addresses and native symbol indices, supports
the declared relocation profile and binds PLT imports eagerly. Builders select
providers in actual `DT_NEEDED` order; conversion requires exact coverage and
rejects undeclared dependencies. SDK `.so` files contain dummy linker code/data
and must never be shipped as runtime modules.

Native dynamic tables reside inside the dedicated metadata LOAD, with matching
file/virtual offsets and header ordering. Earlier placement in an overlapping
writable LOAD caused a loader rejection; sign/extract equality alone cannot
prove this layout valid. Own-provider proof, SDK assembly and title verification
independently check the mapping.

`native_module_crt.cpp` registers `.eh_frame` with LLVM
`__unw_add_dynamic_eh_frame_section` before constructors, finalizes globals for
its DSO and removes unwind records only after successful cleanup. A failed
finalizer leaves unwind records registered and reports failure.
`prepare_runtime_archive.py` converts the single validated libc++ legacy
constructor into modern arrays in a copy, not the installed SDK archive.
The private application likewise normalizes supported retained Python/microhttpd
tables in outputs only. Malformed lifecycle tables fail.

Native TLS, unsupported relocations, unprepared legacy lifecycle tables, shared
preinit arrays and unloadable add-on TLS-destructor imports remain rejected.
General TLS-destructor unloading requires quiescence guarantees; do not infer
it from basic load/unload observations.

Installed add-ons require signed SPRX, their adjacent `.sprx.json` receipts,
`ps5` platform metadata and correct `library_freebsd` selection. Native install
and package validation rejects ordinary ELF, import stubs, auxiliary lists,
undeclared native companions and private Kodi-app imports.

The title carries `ps5-shared-runtime.json`, `ps5-shared-title-proof.json` and
the runtime marker receipt. `verify-shared-title.py` re-proves native bindings,
shared ownership, archive/runtime hashes and signing/extraction. Own modules
require exact extraction equality. The executable signer omits a 24-byte
nonloadable note; that bounded difference is explicitly recorded, not described
as whole-file equality. Title staging never deploys or contacts the console.

## Retained tools and focused checks

| Tool/group | Caller or supported use |
|---|---|
| `prepare-native-runtime.py` | Normal runtime/dependency preparation entrypoint |
| `build-shared-runtime.py`, `build-shared-graphics*.py`, `prepare-native-addon-sdk.py` | Preparation's isolated component builders |
| `native_contract.py`, `native_addon.py` | Shared SDK and installed/package validation |
| `native_shared_link.py`, `native_runtime_sdk.py` | Conversion and own-provider SDK proof |
| `build-native-kodi-app.py`, `build-native-title-launcher.py`, `native_loader_companion.py` | Normal title packaging |
| `addon-repository.py`, `stage-installed-addons.py` | Native catalogue build/stage/feed/publication |
| `native_system_provider.py`, `native_system_sdk.py` | Standalone implementation analysis, not normal build dependencies |
| `build-sprx-test-addon.sh`, `NativeCAddon.cmake` | Restricted import-free C loader sample; explicit diagnostic only |
| `test.py` | Explicit runner for hyphenated Python test files |

```sh
python3 tools/ps5/test.py \
  test-native-runtime-sdk.py test-native-shared-link.py test-native-module-crt.py \
  test-native-loader-companion.py test-native-title-launcher.py \
  test-build-native-kodi-app.py test-prepare-shared-title.py test-verify-shared-title.py
bash tools/ps5/test-native-loader.sh
```

Host sanitizer/mock tests cover ownership and lifecycle error paths; target
compilation and sign/extract checks cover binary structure. Neither establishes
real native firmware behavior. The restricted C sample can be included with
`PS5_TEST_BINARY_ADDON=1`; it does not qualify arbitrary C++ add-ons.

## Acceptance history and limits

Earlier hash-identified native candidates established native startup and
independent Greynetic/Adaptive operation. The user confirmed startup/shutdown
and TVHeadend on the clean retry; native Greynetic and Adaptive load/unload were
observed and Adaptive streamed the public Bunny DASH sample. Earlier UI reports
from an unverified USB launch were not evidence for the repaired native candidate.
Historical manual manifest correction is not a substitute for generated `ps5`
metadata in this workflow.

The first shutdown incident was a SceShellCore abort after app0 unmount returned
`EBUSY`, following Kodi process termination. The retaining reference is unknown;
a clean retry worked. Neither legacy Adaptive nor FTP is proven to have caused
it. No speculative unload change is justified by that incident.

Those observations do not qualify new rebuilt bytes or the complete catalogue.
Full functional coverage, broader lifecycle/TLS/exception behavior, DNS edge
cases, cross-firmware compatibility and DRM/CDM remain unqualified. Local
catalogue build/package status and user-operated functional acceptance must be
reported separately. No stable promotion without acceptance of the exact ZIP.
