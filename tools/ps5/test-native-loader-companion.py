#!/usr/bin/env python3
"""Check the mandatory loader companion without adding another client runtime."""
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import native_loader_companion as companion

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "shims/native-app/loader_companion.c"
SDK = Path(os.environ.get("PS5_NATIVE_RUNTIME_SDK", str(
    ROOT / "build/ps5-shared-runtime/addon-sdk-unified-tls")))
TOOL = ROOT / "build/ps5-shared-title-stage/app/build/host/ps5-native-tool"

HARNESS = r'''
#define _POSIX_C_SOURCE 200112L
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#define malloc fixture_malloc
#define free fixture_free
#define posix_memalign fixture_posix_memalign
#include "loader_companion.c"
#undef malloc
#undef free
#undef posix_memalign

static const struct application_heap_api* registered;
static int registrations;
static int messages;
static const char* last_message;
void* fixture_malloc(size_t size) { return malloc(size); }
void fixture_free(void* pointer) { free(pointer); }
int fixture_posix_memalign(void** result, size_t alignment, size_t size)
{
  return posix_memalign(result, alignment, size);
}
void _sceKernelRtldSetApplicationHeapAPI(const struct application_heap_api* api)
{
  assert(api != NULL);
  registrations++;
  registered = api;
}
void sceKernelDebugOutText(int channel, const char* message)
{
  assert(channel == 0 && message != NULL);
  messages++;
  last_message = message;
}
int main(void)
{
  assert(ps5_native_loader_companion_v1 == 1);
  assert(module_start(0, NULL) == 0);
  assert(registrations == 1 && messages == 1);
  assert(registered->allocate == fixture_malloc);
  assert(registered->release == fixture_free);
  assert(registered->aligned_allocate == fixture_posix_memalign);
  for (unsigned int i = 0; i < 4; ++i) assert(registered->reserved[i] == NULL);
  for (unsigned int i = 0; i < 2; ++i) assert(registered->reserved_tail[i] == NULL);
  void* memory = registered->allocate(128);
  assert(memory != NULL);
  memset(memory, 0x5a, 128);
  registered->release(memory);
  assert(registered->aligned_allocate(&memory, 64, 128) == 0);
  registered->release(memory);
  assert(module_start(0, NULL) == -1);
  assert(registrations == 1 && strstr(last_message, "duplicate") != NULL);
  assert(module_stop(0, NULL) == -1);
  assert(registrations == 1 && strstr(last_message, "cannot be unloaded") != NULL);
  return 0;
}
'''


class LoaderCompanionTest(unittest.TestCase):
    def test_rejects_placeholder_c_exports(self):
        exports = {companion.MARKER: (), "malloc": ()}
        with patch.object(companion, "prove_provider", return_value=exports):
            with self.assertRaisesRegex(ValueError, "must not export replacement C APIs"):
                companion.verify_native(b"", b"", [])

    @unittest.skipUnless(shutil.which("clang-18"), "clang-18 required")
    def test_native_heap_registration_lifetime_and_explicit_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, executable = root / "test.c", root / "test"
            source.write_text(HARNESS)
            result = subprocess.run([
                "clang-18", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-fsanitize=address,undefined", "-I", str(SOURCE.parent),
                str(source), "-o", str(executable),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run([str(executable)], check=True, capture_output=True)

    def test_rejects_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "output already exists"):
                companion.build(SDK, Path(temporary), TOOL)

    @unittest.skipUnless((SDK / "manifest.json").is_file() and TOOL.is_file(),
                         "completed local SDK and signing tool required")
    def test_actual_native_build_and_tampered_package_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "companion"
            proof = companion.build(SDK, directory, TOOL)
            self.assertEqual(stat.S_IMODE((directory / "libc.prx").stat().st_mode), 0o755)
            self.assertEqual(proof["verification"]["exports"], [companion.MARKER])
            self.assertFalse(proof["verification"]["owns_heap_tls_or_cxx"])
            self.assertFalse(proof["verification"]["replacement_c_exports"])
            self.assertTrue(proof["process_lifetime"])
            self.assertFalse(proof["console_accepted"])
            repeated = Path(temporary) / "repeated-companion"
            companion.build(SDK, repeated, TOOL)
            self.assertEqual(companion.digest(directory / "libc.prx"),
                             companion.digest(repeated / "libc.prx"))
            raw = bytearray((directory / "libc.prx").read_bytes())
            raw[-1] ^= 1
            (directory / "libc.prx").write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                companion.verify_artifacts(directory, SDK, TOOL)


if __name__ == "__main__":
    unittest.main()
