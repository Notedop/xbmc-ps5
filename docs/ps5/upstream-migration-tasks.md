# Upstream migration tasks: removing the local patches and shims

`tools/ps5/bootstrap.sh` currently prepares the two external prerequisites by
**patching them locally**. That works and is reproducible, but it pins us to
one revision of each project and makes every upstream bump a merge exercise.

None of these changes are Kodi-specific in nature - they are generally useful
entry points and missing link stubs. The goal is therefore to upstream all of
them, after which `bootstrap.sh` only has to *download* pinned releases and
every `patches/` file below disappears.

This document tracks that work. It is **not** a prerequisite for building:
the bootstrap as it stands is the supported path until these land.

## Status overview

| # | Target project | Change | Local artifact to delete afterwards |
|---|---|---|---|
| 1 | `ps5-payload-dev/sdk` | Ship a `libSceVideodec2` link stub | `shims/sce_stubs/libSceVideodec2.c` |
| 2 | `ps5-payload-dev/sdk` | Add `sceVideoOutVrrUnpegFromFixedRate` to the `libSceVideoOut` stub | the `libSceVideoOut` half of `tools/ps5/bootstrap/sce-stubs.sh` |
| 3 | `blackbearreloaded/ps5-opengl` | Export the video out handle | part of `tools/ps5/patches/ps5-opengl/kodi-additions.py` |
| 4 | `blackbearreloaded/ps5-opengl` | Scanout pixel format switch (HDR output) | part of the same |
| 5 | `blackbearreloaded/ps5-opengl` | EGL image over foreign memory (zero-copy video) | part of the same |
| 6 | this repo | Make the remaining externs weak + feature-detected | hard link dependency on a patched SDK |
| 7 | `blackbearreloaded/ps5-opengl` | Fix the failing `test_descriptor_snapshot.py` self-test | the `test-compiler` skip in `tools/ps5/bootstrap/ps5-opengl.sh` |

---

## 1 + 2. `ps5-payload-dev/sdk`: two missing link stubs

Both are pure symbol-name stubs in the style of the SDK's own `sce_stubs`,
containing no logic whatsoever - the payload runtime linker resolves them
against the real module at load time. They are missing simply because nothing
upstream has needed them yet.

**`libSceVideodec2`** - the hardware video decoder. The SDK ships no stub at
all for this module. Nine symbols, used by `xbmc/platform/ps5/video`:

```
sceVideodec2QueryComputeMemoryInfo   sceVideodec2AllocateComputeQueue
sceVideodec2ReleaseComputeQueue      sceVideodec2QueryDecoderMemoryInfo
sceVideodec2CreateDecoder            sceVideodec2DeleteDecoder
sceVideodec2Decode                   sceVideodec2Flush
sceVideodec2Reset
```

The full file is `shims/sce_stubs/libSceVideodec2.c` (18 lines) and can be
submitted as-is.

**`sceVideoOutVrrUnpegFromFixedRate`** - one symbol missing from the SDK's
existing `libSceVideoOut` stub, needed to leave a fixed refresh rate when VRR
is active (`xbmc/platform/ps5/VideoOutInfo.cpp`). Today
`tools/ps5/bootstrap/sce-stubs.sh` regenerates the whole stub from the SDK's
own (keeping the original as `libSceVideoOut.so.sdk`) purely to append this one
entry - a one-line upstream addition removes all of that machinery.

**Effort:** trivial, no API design needed. These are the highest-value, lowest-risk
items; do them first.

**After landing:** bump the pacbrew `ps5-payload-sdk` package version in
`bootstrap.sh`, delete `shims/sce_stubs/` and
`tools/ps5/bootstrap/sce-stubs.sh`, and drop the stub-presence checks from
`tools/ps5/package.sh` (lines around the `sce_stubs` loop).

---

## 3-5. `blackbearreloaded/ps5-opengl`: three driver entry points

Applied by `tools/ps5/patches/ps5-opengl/kodi-additions.py`, written against
the revision pinned in `tools/ps5/patches/ps5-opengl/PS5-OPENGL-COMMIT`.

The script is already written with upstreaming in mind: it inserts at named
anchors, reports `skipped, anchor differs` instead of corrupting a file, and
*removes* older diagnostic additions so the tree converges on "upstream plus
these functions".

### 3. `ps5_opengl_video_out_handle()`

Four lines in `src/platform/ps5_agc_native_runtime.c` returning the existing
`static int runtime_video_handle`. Any application that wants the display's
refresh rate, a vblank clock or VRR control needs the handle the driver
already opened; there is currently no way to obtain it.

Already generic - no renaming needed, submit as is.

### 4. `ps5_opengl_set_scanout_format()`

Re-registers the scanout buffers with a different pixel format (the HDR 10-bit
BT.2020 PQ format, or back to SDR) via
`sceVideoOutSubmitChangeBufferAttribute2` - the same call games use to toggle
HDR. Both formats are 32bpp so the buffers themselves are unchanged.

Already generic. The implementation deliberately avoids the
unregister/re-register fallback, which is documented in the source as hanging
the next present on hardware; that rationale should travel with the PR.

### 5. `ps5_opengl_memory_image_create()` + EGL image hooks

A sampled 2D texture over memory the driver does not own (the hardware video
decoder's frames), exposed to GL as an EGL image. Touches
`src/gallium/ps5/ps5_screen.c` (a `kodi_foreign_memory` resource flag plus
matching skips in the unmap and cache-flush paths) and `src/egl/ps5_egl.c`
(`validate_egl_image` / `get_egl_image` frontend hooks).

**Needs a rename before submitting.** The public entry point is already
neutrally named, but the internals carry Kodi's name:

| now | suggested |
|---|---|
| `resource->kodi_foreign_memory` | `resource->foreign_memory` |
| `ps5_kodi_resource_from_memory` | `ps5_resource_from_memory` |
| `ps5_kodi_validate_egl_image` | `ps5_validate_egl_image` |
| `ps5_kodi_get_egl_image` | `ps5_get_egl_image` |
| `struct ps5_kodi_image` / `PS5_KODI_IMAGE_MAGIC` | `struct ps5_memory_image` / `PS5_MEMORY_IMAGE_MAGIC` |
| `/* KODI-PS5: ... */` comments | plain descriptive comments |

**Rebase first.** The pinned commit is well behind `main` (the project has since
tagged releases), so the anchors these insertions target may have moved. Rebase
`kodi-additions.py` onto the current tag, confirm every anchor still reports
applied rather than `skipped, anchor differs`, and rebuild before opening PRs.

**After landing:** delete `tools/ps5/patches/ps5-opengl/`, and reduce
`tools/ps5/bootstrap/ps5-opengl.sh` to downloading and unpacking a pinned
release archive (they are relocatable prefixes with headers, static libs and
pkgconfig/cmake metadata - the same layout `$PS5_OPENGL_PREFIX` already has).

---

## 6. This repo: weaken the remaining hard dependency

Independent of the above, and worth doing **first** because it decouples this
repo from the patch status of the SDK it is built against.

`xbmc/platform/ps5/VideoOutInfo.cpp` declares:

- `ps5_opengl_set_scanout_format` as `__attribute__((weak))`, with a null check
  and a `ScanoutFormatSwitchAvailable()` accessor - the right pattern;
- `ps5_opengl_video_out_handle` as a **hard** `extern "C"` symbol - so linking
  against an unpatched `/opt/ps5-opengl-gl46` fails at link time rather than
  degrading.

Making the latter weak as well, guarded the same way, means Kodi links against
both a stock and a patched SDK. With the symbol absent the existing
`handle < 0` guards already take over and the degradation is graceful:

| area | without the symbol |
|---|---|
| `WinSystemPS5.cpp`, `WinSystemPS5GLContext.cpp` | no automatic refresh-rate matching (e.g. 24p film) |
| `HdrOutputPS5.cpp` (`IsDisplayHdr()`) | no HDR display detection |
| `VideoSyncPS5.cpp` | vblank sync falls back to the software timer |

No crashes, no fallback code to write - only a weak declaration and a null
check. The zero-copy video path should get the same treatment where it
resolves `ps5_opengl_memory_image_create`.

---

## Suggested order

1. **Item 6** - weak symbols here. Small, self-contained, and removes the hard
   coupling that makes everything else urgent.
2. **Items 1-2** - the two SDK stub PRs. Trivial, clearly generic, and they
   delete a whole bootstrap step plus a vendored source directory.
3. **Items 3-5** - rebase `kodi-additions.py` onto current `ps5-opengl`,
   de-Kodi-ify the zero-copy internals, then open the PRs (3 and 4 can go
   first; 5 is the larger review).
4. Shrink `tools/ps5/bootstrap/ps5-opengl.sh` to a download step and delete
   `tools/ps5/patches/`.

---

## 7. `blackbearreloaded/ps5-opengl`: a failing host self-test at the pin

`make sdk-gl46` runs `make test-compiler` between building Mesa and installing
the SDK. At the pinned revision `122aa89`, `tests/ps5/test_descriptor_snapshot.py`
fails on an **unmodified** checkout:

```
assert 'ps5_flush_gpu_data(storage->data, ps5_descriptor_snapshot_size(context, slot));' in publish
AssertionError
```

The test slices `src/gallium/ps5/ps5_screen.c` by source text and asserts the
exact statement appears inside `ps5_prepare_constant()`. It is not caused by
`kodi-additions.py` - this was confirmed by stashing our three modified files
and re-running the test on the clean tree.

Because these self-tests only check the shader compiler's own sources and have
no effect on the SDK that gets produced, `tools/ps5/bootstrap/ps5-opengl.sh`
drives the individual `sdk-gl46` toolchain steps and skips `test-compiler`.
Run `make -C <ps5-opengl> test-compiler` by hand when changing the compiler.

**Action:** report upstream, or confirm the test is stale and expected to be
rebased. Once fixed, the bootstrap script can call `make sdk-gl46` again.
