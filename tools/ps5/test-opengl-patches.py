#!/usr/bin/env python3
"""Test conventional patches: python3 tools/ps5/test-opengl-patches.py.

Read clean pinned sources from the local 1.0.2 checkout with git show, apply
with git apply in isolated repository-local fixtures, and compile small probes
from the patched C functions. No Python rewriting of SDK sources, SDK build,
network access or changes to either dependency checkout are involved.
"""
import os
import re
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
PIN = "a519561626933257213d64a4a8bd5e02b55dceb2"
UPSTREAM = REPO / "build/ps5-external/opengl-1.0.2/ps5-opengl"
PATCHES = sorted((REPO / "tools/ps5/patches/ps5-opengl").glob("*.patch"))
RUNTIME_PATH = "src/platform/ps5_agc_native_runtime.c"
SCREEN_PATH = "src/gallium/ps5/ps5_screen.c"
EGL_PATH = "src/egl/ps5_egl.c"
PRESENT_PATH = "tests/ps5/test_present_shutdown.py"
FILES = (RUNTIME_PATH, SCREEN_PATH, EGL_PATH, PRESENT_PATH)

RUNTIME = """#include <stdint.h>
#include <stddef.h>
#define AGC_RUNTIME_PACKAGES
#define VIDEO_OUT_PIXEL_FORMAT UINT64_C(0x8000000000000000)
static unsigned active_width = 1920, active_height = 1080;
#define DISPLAY_WIDTH active_width
#define DISPLAY_HEIGHT active_height
static int runtime_video_handle = -1;
static int runtime_video_registered;
typedef struct { uint64_t words[16]; } video_attribute_t;
static unsigned attribute_width, attribute_height, submissions;
static uint64_t attribute_format;
static int submit_result;
static void set_attribute(video_attribute_t *a, uint64_t f, uint32_t t,
                          uint32_t w, uint32_t h, uint32_t x, uint32_t y, uint32_t z)
{
    (void)a; (void)t; (void)x; (void)y; (void)z;
    attribute_width = w; attribute_height = h; attribute_format = f;
}
static struct { void (*set_attribute2)(video_attribute_t *, uint64_t, uint32_t,
    uint32_t, uint32_t, uint32_t, uint32_t, uint32_t); } runtime_video_api = {set_attribute};
int sceVideoOutSubmitChangeBufferAttribute2(int32_t h, int32_t s, const void *a,
                                           int32_t c, void *o)
{
    (void)h; (void)s; (void)a; (void)c; (void)o;
    ++submissions;
    return submit_result;
}
"""

SCREEN_TYPES = """#include <stdint.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#define PS5_DEFERRED_DRAW_BATCH 1
#define PIPE_MAX_TEXTURE_LEVELS 16
#define PS5_MAX_TEXTURE_2D_SIZE 16384u
enum pipe_format { PIPE_FORMAT_NONE, PIPE_FORMAT_R8_UNORM, PIPE_FORMAT_R8G8_UNORM,
                  PIPE_FORMAT_R16_UNORM, PIPE_FORMAT_R16G16_UNORM };
enum { PIPE_BUFFER = 0, PIPE_TEXTURE_2D = 2, PIPE_BIND_SAMPLER_VIEW = 8,
       PIPE_BIND_DISPLAY_TARGET = 16, PIPE_USAGE_DEFAULT = 0 };
struct pipe_screen { int unused; };
struct pipe_resource {
   unsigned target, width0, depth0, array_size, last_level, bind, usage;
   uint16_t height0;
   enum pipe_format format;
   struct { unsigned count; } reference;
   struct pipe_screen *screen;
};
struct ps5_screen {
   struct pipe_screen base;
   int resource_mutex;
   struct pipe_resource *render_pool;
   unsigned render_arena_bitmap[1];
};
"""
SCREEN_SUPPORT = """
static bool ps5_linear_sampled_layout(const struct pipe_resource *r)
{
   return r->target == PIPE_TEXTURE_2D && r->bind == PIPE_BIND_SAMPLER_VIEW;
}
static bool ps5_resource_layout(const struct pipe_resource *r, size_t *size, unsigned *stride)
{
   const unsigned bpp = r->format == PIPE_FORMAT_R8_UNORM ? 1 :
                        r->format == PIPE_FORMAT_R16G16_UNORM ? 4 : 2;
   *stride = (r->width0 * bpp + 255u) & ~255u;
   *size = (size_t)*stride * r->height0;
   return true;
}
static unsigned releases, publications;
static void *decoder_memory = (void *)(uintptr_t)0x10000;
static void ps5_release_resource_memory(void *data, size_t size, int64_t direct)
{
   (void)size; (void)direct;
   if (data == decoder_memory)
      abort();
   if (data)
      ++releases;
}
static void simple_mtx_lock(int *mutex) { (void)mutex; }
static void simple_mtx_unlock(int *mutex) { (void)mutex; }
static void ps5_render_arena_mark(struct ps5_screen *s, unsigned first, unsigned count, bool used)
{
   (void)s; (void)first; (void)count; (void)used;
}
static void pipe_resource_reference(struct pipe_resource **dst, struct pipe_resource *src)
{
   *dst = src;
}
struct ps5_batch_flush_cache { int unused; };
static uint64_t ps5_texture_publication_epoch = 1;
static void ps5_flush_batch_backing(struct ps5_batch_flush_cache *batch, unsigned slot,
                                    const void *data, size_t bytes)
{
   (void)batch; (void)slot; (void)data; (void)bytes;
   ++publications;
}
"""

RUNTIME_PROBE = """
#include <assert.h>
int main(void)
{
    const uint64_t hdr = UINT64_C(0x8100070422000000);
    int32_t results[4];
    assert(ps5_opengl_video_out_handle() == -1);
    assert(ps5_opengl_set_scanout_format(hdr, results) == -1);
    runtime_video_handle = 7;
    runtime_video_acquire();
    /* Changing requested dimensions must not change the registered layout. */
    active_width = 2560; active_height = 1440;
    assert(ps5_opengl_set_scanout_format(hdr, results) == 0);
    assert(attribute_width == 1920 && attribute_height == 1080);
    assert(attribute_format == hdr && submissions == 1);
    assert(results[0] == 0 && results[1] == 0x7fffffff);
    assert(ps5_opengl_set_scanout_format(hdr, results) == 0 && submissions == 1);
    /* New registration is SDR even if the OS reuses the same video handle. */
    runtime_video_acquire();
    assert(ps5_opengl_set_scanout_format(hdr, results) == 0);
    assert(attribute_width == 2560 && attribute_height == 1440 && submissions == 2);
    submit_result = -23;
    assert(ps5_opengl_set_scanout_format(VIDEO_OUT_PIXEL_FORMAT, results) == -23);
    assert(results[0] == -23 && results[1] == 0x7fffffff && submissions == 3);
    assert(ps5_opengl_set_scanout_format(VIDEO_OUT_PIXEL_FORMAT, results) == -23);
    assert(submissions == 4); /* A refused switch did not update tracked state. */
    assert(ps5_opengl_set_scanout_format(42, results) == -1 && submissions == 4);
    assert(ps5_opengl_set_scanout_format(hdr, NULL) == -1);
    return 0;
}
"""

SCREEN_PROBE = """
#include <assert.h>
int main(void)
{
   struct ps5_screen screen = {0};
   for (enum pipe_format f = PIPE_FORMAT_R8_UNORM; f <= PIPE_FORMAT_R16G16_UNORM; ++f) {
      unsigned bpp = f == PIPE_FORMAT_R8_UNORM ? 1 : f == PIPE_FORMAT_R16G16_UNORM ? 4 : 2;
      unsigned stride = (1920 * bpp + 255u) & ~255u;
      struct pipe_resource *base = ps5_kodi_resource_from_memory(
         &screen.base, f, 1920, 1080, stride, decoder_memory);
      assert(base);
      struct ps5_resource *r = (struct ps5_resource *)base;
      assert(r->size == (size_t)stride * 1080 && r->layer_stride == r->size);
      assert(r->level_offset[0] == 0 && r->level_stride[0] == stride);
      ps5_flush_texture_backing(NULL, 0, r, r->size, false);
      assert(publications == 0);
      ps5_resource_destroy(&screen.base, base);
      assert(releases == 0);
   }
   assert(!ps5_kodi_resource_from_memory(&screen.base, PIPE_FORMAT_NONE, 1920, 1080, 2048, decoder_memory));
   assert(!ps5_kodi_resource_from_memory(&screen.base, PIPE_FORMAT_R8_UNORM, UINT32_MAX, 1080, 2048, decoder_memory));
   assert(!ps5_kodi_resource_from_memory(&screen.base, PIPE_FORMAT_R8_UNORM, 1920, 65536, 2048, decoder_memory));
   assert(!ps5_kodi_resource_from_memory(&screen.base, PIPE_FORMAT_R8_UNORM, 1920, 1080, 1920, decoder_memory));
   assert(!ps5_kodi_resource_from_memory(&screen.base, PIPE_FORMAT_R8_UNORM, 1920, 1080, 2048, (void *)1));
   assert(!ps5_kodi_resource_from_memory(&screen.base, PIPE_FORMAT_R8_UNORM, 1920, 1080, 2048, (void *)(uintptr_t)UINT64_C(0x1000000000000)));
   assert(!ps5_kodi_resource_from_memory(&screen.base, PIPE_FORMAT_R8_UNORM, 1920, 1080, 2048, (void *)(uintptr_t)UINT64_C(0xffffffffff00)));
   struct ps5_resource *owned = calloc(1, sizeof(*owned));
   owned->data = (void *)(uintptr_t)0x20000;
   ps5_flush_texture_backing(NULL, 0, owned, 0, false);
   assert(publications == 1);
   ps5_resource_destroy(&screen.base, &owned->base);
   assert(releases == 1);
   return 0;
}
"""


def function(source, name):
    """Extract a definition verbatim for a C probe, never rewrite SDK sources."""
    match = re.search(r"\b" + re.escape(name) + r"\([^;{}]*\)\s*\{", source)
    if not match:
        raise AssertionError(f"missing definition: {name}")
    start = source.rfind("\n", 0, match.start()) + 1
    if not source[start:match.start()].strip():
        start = source.rfind("\n", 0, start - 1) + 1
    depth = 1
    for end in range(match.end(), len(source)):
        depth += (source[end] == "{") - (source[end] == "}")
        if not depth:
            return source[start:end + 1] + "\n"
    raise AssertionError(f"unterminated definition: {name}")


class PatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PATCHES:
            raise AssertionError("no conventional ps5-opengl patches found")
        cls.clean = {
            rel: subprocess.run(["git", "-C", str(UPSTREAM), "show", PIN + ":" + rel],
                                check=True, capture_output=True).stdout
            for rel in FILES
        }

    def setUp(self):
        self.work = REPO / (".test-opengl-patches-" + uuid.uuid4().hex)
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)
        subprocess.run(["git", "-C", str(self.work), "init", "--quiet"], check=True)
        self.write_sources(self.clean)

    def write_sources(self, sources):
        for rel, source in sources.items():
            path = self.work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source)

    def read_sources(self):
        return {rel: (self.work / rel).read_bytes() for rel in FILES}

    def apply(self, *flags, success=True):
        result = subprocess.run(["git", "-C", str(self.work), "apply", *flags,
                                 *map(str, PATCHES)], capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def patched(self):
        self.apply("--check")
        self.apply()
        return {rel: data.decode() for rel, data in self.read_sources().items()}

    def compile_run(self, name, source):
        if not shutil.which("cc"):
            self.fail("cc is required for the small foreign-memory/HDR runtime probes")
        path = self.work / (name + ".c")
        executable = self.work / name
        path.write_text(source)
        for command in (
            ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(path), "-o", str(executable)],
            [str(executable)],
        ):
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_full_application_and_idempotence(self):
        self.apply("--check")
        self.assertEqual(self.read_sources(), self.clean)
        updated = self.patched()
        self.assertIn("ps5_opengl_video_out_handle", updated[RUNTIME_PATH])
        self.assertIn("ps5_opengl_set_scanout_format", updated[RUNTIME_PATH])
        self.assertIn("kodi_foreign_memory", updated[SCREEN_PATH])
        before = self.read_sources()
        times = {rel: (self.work / rel).stat().st_mtime_ns for rel in FILES}
        self.apply("--check", success=False)
        self.apply("--reverse", "--check")
        self.assertEqual(self.read_sources(), before)
        self.assertEqual(times, {rel: (self.work / rel).stat().st_mtime_ns for rel in FILES})
        self.apply("--reverse")
        self.assertEqual(self.read_sources(), self.clean)

    def test_mismatched_required_hunks_fail_without_any_partial_writes(self):
        anchors = (
            (SCREEN_PATH, b"   unsigned render_arena_slot_count;\n"),
            (SCREEN_PATH, b"   uint64_t *epoch = stencil ? &texture->stencil_publication_epoch : &texture->texture_publication_epoch;\n"),
            (SCREEN_PATH, b"      memset(ps5->render_arena_bitmap, 0,\n"),
            (RUNTIME_PATH, b"static int runtime_video_handle = -1;\n"),
            (RUNTIME_PATH, b"    runtime_video_registered = 1;\n"),
            (PRESENT_PATH, b"#define VIDEO_OUT_PIXEL_FORMAT 1\n"),
        )
        for rel, anchor in anchors:
            with self.subTest(anchor=anchor):
                damaged = self.clean.copy()
                self.assertEqual(damaged[rel].count(anchor), 1)
                damaged[rel] = damaged[rel].replace(anchor, b"/* incompatible source edit */\n")
                self.write_sources(damaged)
                self.apply("--check", success=False)
                self.assertEqual(self.read_sources(), damaged)
                self.apply(success=False)
                self.assertEqual(self.read_sources(), damaged)

    def test_modified_kodi_code_is_not_overwritten(self):
        self.patched()
        damaged = self.read_sources()
        damaged[SCREEN_PATH] = damaged[SCREEN_PATH].replace(
            b"resource->kodi_foreign_memory = true;", b"resource->kodi_foreign_memory = false;")
        self.write_sources(damaged)
        self.apply("--check", success=False)
        self.apply("--reverse", "--check", success=False)
        self.assertEqual(self.read_sources(), damaged)

    def test_unrelated_edits_are_preserved(self):
        sources = self.clean.copy()
        sources[EGL_PATH] = b"/* unrelated local edit */\n" + sources[EGL_PATH]
        self.write_sources(sources)
        self.patched()
        self.apply("--reverse", "--check")
        self.apply("--reverse")
        self.assertEqual(self.read_sources(), sources)

    def test_hdr_uses_registered_runtime_dimensions_and_resets_on_registration(self):
        runtime = self.patched()[RUNTIME_PATH]
        state = "\n".join(re.findall(r"^static .*kodi_scanout_.*$", runtime, re.M)) + "\n"
        acquire = function(runtime, "runtime_video_acquire")
        reset = acquire[acquire.index("    runtime_video_registered = 1;"):
                        acquire.index("    runtime_scanout_flush_needed = 1;")]
        probe = (RUNTIME + state + function(runtime, "ps5_opengl_video_out_handle") +
                 "static int runtime_video_acquire(void)\n{\n" + reset + "return 0;\n}\n" +
                 function(runtime, "ps5_opengl_set_scanout_format") + RUNTIME_PROBE)
        self.compile_run("hdr-probe", probe)

    def test_foreign_destroy_never_releases_decoder_memory_or_publishes_cpu_cache(self):
        screen = self.patched()[SCREEN_PATH]
        resource = re.search(r"struct ps5_resource \{.*?\n\};", screen, re.S).group()
        code = SCREEN_TYPES + resource + "\n" + SCREEN_SUPPORT
        for name in ("ps5_resource_destroy", "ps5_flush_texture_backing", "ps5_kodi_resource_from_memory"):
            code += function(screen, name)
        self.compile_run("memory-probe", code + SCREEN_PROBE)

    def test_egl_validates_display_and_preserves_resource_reference(self):
        code = self.patched()[EGL_PATH]
        self.assertIn("img->resource->screen == ps5_display.screen", code)
        self.assertIn("if (!out || !ps5_kodi_validate_egl_image", code)
        self.assertIn("pipe_resource_reference(&out->texture, img->resource)", code)
        self.assertIn("pipe_resource_reference(&img->resource, NULL)", code)

    def test_upstream_present_shutdown_all_four_configurations(self):
        self.patched()
        scratch = self.work / "scratch"
        scratch.mkdir()
        result = subprocess.run([sys.executable, str(self.work / PRESENT_PATH)],
                                cwd=self.work, env={**os.environ, "TMPDIR": str(scratch)},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("present-shutdown: PASS"), 2)
        self.assertEqual(result.stdout.count("present-HFR-reopen: PASS"), 2)
        self.assertFalse(list(scratch.iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
