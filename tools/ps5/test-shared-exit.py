#!/usr/bin/env python3
"""Check main-thread TLS cleanup precedes native exit/global finalization."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CC = shutil.which("clang-18")
SDK = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
HARNESS = r"""
#include <assert.h>
#include <setjmp.h>
#include <stdint.h>
#include "shared_tls.h"

_Noreturn void __wrap_exit(int);
static jmp_buf continuation;
static int dso;
static int events;
static int exit_status;
int sceKernelDebugOutText(int channel, const char* text)
{
  (void)channel;
  (void)text;
  assert(!"unexpected TLS runtime error");
  return 0;
}
static void destroy(void* value) { events = events * 10 + (int)(intptr_t)value; }
_Noreturn void __real_exit(int status)
{
  assert(events == 21);
  assert(ps5_tls_dso_pending(&dso) == 0);
  exit_status = status;
  longjmp(continuation, 1);
}
int main(void)
{
  const int statuses[] = {0, 27, -2};
  for (unsigned i = 0; i < sizeof(statuses) / sizeof(statuses[0]); ++i)
  {
    events = 0;
    assert(__cxa_thread_atexit_impl(destroy, (void*)(intptr_t)1, &dso) == 0);
    assert(__cxa_thread_atexit_impl(destroy, (void*)(intptr_t)2, &dso) == 0);
    if (setjmp(continuation) == 0)
      __wrap_exit(statuses[i]);
    assert(exit_status == statuses[i]);
    assert(events == 21);
  }
}
"""


@unittest.skipUnless(CC, "clang-18 required")
class SharedExitTest(unittest.TestCase):
    def test_real_tls_destructors_run_before_native_exit_with_status_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="shared-exit-") as temporary:
            directory = Path(temporary)
            source = directory / "harness.c"
            source.write_text(HARNESS)
            binary = directory / "exit-test"
            commands = [
                [CC, "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                 "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-pthread",
                 "-I" + str(ROOT / "shims/native-app"), str(source),
                 str(ROOT / "shims/native-app/shared_tls.c"),
                 str(ROOT / "shims/native-app/shared_exit.c"), "-o", str(binary)],
                [str(binary)],
            ]
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless((SDK / "bin/prospero-clang").is_file(), "native SDK required")
    def test_native_sdk_compilation(self):
        with tempfile.TemporaryDirectory(prefix="shared-exit-native-") as temporary:
            result = subprocess.run([
                str(SDK / "bin/prospero-clang"), "-std=c11", "-O2", "-Wall", "-Wextra",
                "-Werror", "-fPIC", "-c", str(ROOT / "shims/native-app/shared_exit.c"),
                "-o", str(Path(temporary) / "exit.o"),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
