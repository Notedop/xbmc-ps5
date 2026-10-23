#!/usr/bin/env python3
"""Exercise native CRT lifecycle on the host, with mocked unwind registration."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CXX = os.environ.get("NATIVE_CRT_HOST_CXX") or shutil.which("clang++-18")

HARNESS = r"""
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

extern "C" {
int module_start(size_t, const void*) noexcept;
int module_stop(size_t, const void*) noexcept;
void __ps5_finalize_globals();
extern void* __ps5_test_dso;
int __cxa_atexit(void (*)(void*), void*, void*);
void __real___cxa_finalize(void*);
extern const unsigned char __ps5_eh_frame_start[4] = {0, 0, 0, 0};
}

static int events[128];
static unsigned count;
static bool registered;
static bool failConstructor;
static bool failFinalizer;
static bool reenter;

static void record(int event) { assert(count < 128); events[count++] = event; }
#ifdef PS5_SHARED_RUNTIME_CORE
static bool failHeap;
extern "C" int ps5_shared_heap_initialize()
{
  record(10);
  return failHeap ? -1 : 0;
}
#endif

static void globalDestructor(void*)
{
  assert(registered);
  record(8);
}
static void constructorOne()
{
  assert(registered);
  record(2);
  assert(__cxa_atexit(globalDestructor, nullptr, __ps5_test_dso) == 0);
}
static void constructorTwo()
{
  record(3);
  if (reenter)
  {
    assert(module_start(0, nullptr) == -1);
    assert(module_stop(0, nullptr) == -1);
  }
}
static void constructorThree()
{
  record(4);
  if (failConstructor)
    throw 42;
}
static void finalizerOne() { assert(registered); record(6); }
static void finalizerTwo()
{
  assert(registered);
  record(5);
  if (reenter)
    assert(module_start(0, nullptr) == -1);
  if (failFinalizer)
    throw 43;
}

extern "C" {
using Function = void (*)(void);
Function __ps5_init_array_start[] = {constructorOne, constructorTwo, constructorThree};
Function __ps5_fini_array_start[] = {__ps5_finalize_globals, finalizerOne, finalizerTwo};
asm(".globl __ps5_init_array_end\n"
    ".set __ps5_init_array_end, __ps5_init_array_start + 24\n"
    ".globl __ps5_fini_array_end\n"
    ".set __ps5_fini_array_end, __ps5_fini_array_start + 24\n");
void __unw_add_dynamic_eh_frame_section(uintptr_t frames)
{
  assert(frames == reinterpret_cast<uintptr_t>(__ps5_eh_frame_start));
  assert(!registered);
  registered = true;
  record(1);
}
void __unw_remove_dynamic_eh_frame_section(uintptr_t frames)
{
  assert(frames == reinterpret_cast<uintptr_t>(__ps5_eh_frame_start));
  assert(registered);
  record(9);
  registered = false;
}
void __wrap___cxa_finalize(void* dso)
{
  if (dso == __ps5_test_dso)
    record(7);
  __real___cxa_finalize(dso);
}
}

static void check(const int* expected, unsigned size)
{
  assert(count == size);
  for (unsigned i = 0; i < size; ++i)
    assert(events[i] == expected[i]);
}

int main(int argc, char** argv)
{
  assert(argc == 2);
  assert(module_stop(0, nullptr) == 0);
  assert(count == 0);
#ifdef PS5_SHARED_RUNTIME_CORE
  failHeap = true;
  assert(module_start(0, nullptr) == -1);
  const int failedHeap[] = {10};
  check(failedHeap, 1);
  assert(!registered);
  assert(module_stop(0, nullptr) == 0);
  check(failedHeap, 1);
  count = 0;
  failHeap = false;
  assert(module_start(0, nullptr) == 0);
  const int startedCore[] = {10, 1, 2, 3, 4};
  check(startedCore, 5);
  assert(module_stop(0, nullptr) == 0);
  const int stoppedCore[] = {10, 1, 2, 3, 4, 5, 6, 7, 8, 9};
  check(stoppedCore, 10);
  return 0;
#endif
  if (strcmp(argv[1], "constructor-failure") == 0)
  {
    failConstructor = true;
    assert(module_start(0, nullptr) == -1);
    const int expected[] = {1, 2, 3, 4, 7, 8, 9};
    check(expected, 7);
    assert(!registered);
    assert(module_stop(0, nullptr) == 0);
    failConstructor = false;
    count = 0;
  }
  if (strcmp(argv[1], "finalizer-failure") == 0)
  {
    failFinalizer = true;
    assert(module_start(0, nullptr) == 0);
    assert(module_stop(0, nullptr) == -1);
    const int expected[] = {1, 2, 3, 4, 5};
    check(expected, 5);
    assert(registered);
    assert(module_stop(0, nullptr) == -1);
    assert(module_start(0, nullptr) == -1);
    check(expected, 5);
    return 0;
  }
  reenter = strcmp(argv[1], "reentrant") == 0;
  for (unsigned cycle = 0; cycle < 2; ++cycle)
  {
    assert(module_start(0, nullptr) == 0);
    assert(module_start(0, nullptr) == -1);
    assert(module_stop(0, nullptr) == 0);
    assert(module_stop(0, nullptr) == 0);
    const int expected[] = {1, 2, 3, 4, 5, 6, 7, 8, 9};
    check(expected, 9);
    assert(!registered);
    count = 0;
  }
}
"""


@unittest.skipUnless(CXX, "clang++-18 required for host CRT checks")
class NativeModuleCrtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="native-module-crt-")
        cls.directory = Path(cls.temp.name)
        harness = cls.directory / "harness.cpp"
        harness.write_text(HARNESS)
        obj = cls.directory / "crt.o"
        core_obj = cls.directory / "core-crt.o"
        cls.binary = cls.directory / "crt-test"
        cls.core_binary = cls.directory / "core-crt-test"
        flags = ["-std=c++20", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                 "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        commands = [
            [CXX, *flags, "-D__dso_handle=__ps5_test_dso", "-c",
             str(ROOT / "tools/ps5/native_module_crt.cpp"), "-o", str(obj)],
            [CXX, *flags, str(obj), str(harness), "-Wl,--wrap=__cxa_finalize",
             "-o", str(cls.binary)],
            [CXX, *flags, "-DPS5_SHARED_RUNTIME_CORE=1", "-D__dso_handle=__ps5_test_dso",
             "-c", str(ROOT / "tools/ps5/native_module_crt.cpp"), "-o", str(core_obj)],
            [CXX, *flags, "-DPS5_SHARED_RUNTIME_CORE=1", str(core_obj), str(harness),
             "-Wl,--wrap=__cxa_finalize", "-o", str(cls.core_binary)],
        ]
        try:
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
        except BaseException:
            cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def exercise(self, scenario):
        result = subprocess.run([str(self.binary), scenario], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_initialization_finalization_order_and_reload(self):
        self.exercise("normal")

    def test_failed_constructor_cleans_registered_globals_and_can_retry(self):
        self.exercise("constructor-failure")

    def test_failed_finalizer_retains_unwind_registry_and_rejects_restart(self):
        self.exercise("finalizer-failure")

    def test_reentrant_lifecycle_calls_rejected(self):
        self.exercise("reentrant")

    def test_core_initializes_heap_before_unwind_and_constructors_and_rejects_heap_failure(self):
        result = subprocess.run([str(self.core_binary), "core"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
