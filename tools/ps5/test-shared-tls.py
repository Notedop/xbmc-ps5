#!/usr/bin/env python3
"""Qualify the standalone TLS owner, not native module loading/unloading."""
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "shims/native-app/shared_tls.c"
SDK = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))

C_OBJECTS = r"""
#include <assert.h>
#include <stdint.h>
_Thread_local int c_initialized = 37;
_Thread_local unsigned char c_zero[97];
_Alignas(4096) _Thread_local unsigned char c_aligned[8192];
void check_c_tls(int value)
{
  assert(c_initialized == 37);
  assert(c_zero[96] == 0);
  assert((uintptr_t)c_aligned % 4096 == 0);
  c_initialized = value;
  c_zero[96] = 23;
  c_aligned[8191] = 45;
}
void check_c_tls_again(int value)
{
  assert(c_initialized == value && c_zero[96] == 23 && c_aligned[8191] == 45);
}
"""

HOST_PROBE = r"""
#include "shared_tls.h"
#include <assert.h>
#include <atomic>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sched.h>
#include <stdlib.h>

extern "C" void check_c_tls(int);
extern "C" void check_c_tls_again(int);
extern "C" void tls_test_fault(int, int);
extern "C" void tls_test_exhaust_index(void);
extern "C" void tls_test_exhaust_pending(void*);
extern "C" void* __dso_handle;
extern "C" void sceKernelDebugOutText(int, const char* message)
{
  fputs(message, stderr);
}

static int tags[4];
static std::atomic<int> completed{0}, entered{0}, release_callback{0}, ready{0}, start{0};
static thread_local int value = 11;
static thread_local int sequence = 0;
static thread_local int identity = 0;

static void recursive(void*)
{
  assert(sequence++ == 1);
  assert(value == 99);
  ps5_tls_thread_exit(); // Reentrant cleanup must not release storage.
}
static void last(void*)
{
  assert(sequence++ == 2);
  assert(value == 99);
  check_c_tls_again(identity);
  ++completed;
}
static void first(void*)
{
  assert(sequence++ == 0);
  assert(value == 99);
  assert(ps5_tls_dso_pending(&tags[0]) >= 2);
  assert(__cxa_thread_atexit_impl(recursive, nullptr, &tags[0]) == 0);
}
struct Object
{
  int member = 72;
  ~Object()
  {
    assert(member == 72 && value == 99);
    check_c_tls_again(identity);
    assert(sequence == 3);
    ++completed;
  }
};
static thread_local Object object;

static void* worker(void* argument)
{
  ++start;
  while (start.load() != 24)
    sched_yield();
  identity = (int)(intptr_t)argument;
  assert(value == 11);
  check_c_tls(identity);
  assert(object.member == 72);
  assert(ps5_tls_dso_pending(&__dso_handle) > 0);
  value = 99;
  assert(__cxa_thread_atexit_impl(last, nullptr, &tags[0]) == 0);
  assert(__cxa_thread_atexit_impl(first, nullptr, &tags[0]) == 0);
  ++ready;
  while (ready.load() != 24)
    sched_yield();
  if (identity & 1)
    pthread_exit(nullptr);
  return nullptr;
}

static void noop(void*) {}
static void blocking(void*)
{
  entered = 1;
  while (!release_callback.load())
    sched_yield();
  assert(ps5_tls_dso_pending(&tags[1]) == 1);
}
static void* blocked_worker(void*)
{
  assert(__cxa_thread_atexit_impl(blocking, nullptr, &tags[1]) == 0);
  return nullptr;
}
static void* canceled_worker(void*)
{
  assert(__cxa_thread_atexit_impl(noop, nullptr, &tags[2]) == 0);
  entered = 1;
  for (;;)
    pthread_testcancel();
}
static void* race_worker(void*)
{
  for (int i = 0; i < 1000; ++i)
  {
    int result = __cxa_thread_atexit_impl(noop, nullptr, &tags[3]);
    assert(result == 0 || (result == -1 && errno == EBUSY));
    ps5_tls_thread_exit();
  }
  return nullptr;
}

int main(int argc, char** argv)
{
  if (argc > 1)
  {
    int mode = atoi(argv[1]);
    ps5_emutls_control control = {16, 16, {0}, nullptr};
    if (mode <= 9)
    {
      // Fault each allocation/key operation, before any runtime state exists.
      const int kinds[] = {0, 1, 2, 3, 3, 4, 4, 0, 4, 4};
      const int after[] = {0, 0, 0, 0, 0, 0, 1, 0, 0, 1};
      if (mode == 7)
      {
        tls_test_exhaust_pending(&tags[0]);
        assert(__cxa_thread_atexit_impl(noop, nullptr, &tags[0]) == -1);
        assert(errno == EOVERFLOW);
        return 0;
      }
      tls_test_fault(kinds[mode], after[mode]);
      if (mode == 4 || mode >= 8)
        __emutls_get_address(&control);
      else
      {
        assert(__cxa_thread_atexit_impl(noop, nullptr, &tags[0]) == -1);
        assert(errno == (mode < 2 ? EAGAIN : mode == 2 ? EINVAL : ENOMEM));
        assert(ps5_tls_dso_pending(&tags[0]) == 0);
        return 0;
      }
    }
    else if (mode == 10) { control.align = 3; __emutls_get_address(&control); }
    else if (mode == 11) { control.size = SIZE_MAX; __emutls_get_address(&control); }
    else if (mode == 12) { control.loc.index = SIZE_MAX; __emutls_get_address(&control); }
    else if (mode == 13) { control.size = 0; __emutls_get_address(&control); }
    else if (mode == 14) { tls_test_exhaust_index(); __emutls_get_address(&control); }
    else if (mode == 15) { __emutls_get_address(nullptr); }
    else if (mode == 16)
    {
      assert(__cxa_thread_atexit_impl(nullptr, nullptr, nullptr) == -1);
      assert(errno == EINVAL);
      return 0;
    }
    else if (mode == 17)
    {
      tls_test_fault(4, 0);
      assert(ps5_tls_begin_unload(&tags[0]) == -1 && errno == ENOMEM);
      return 0;
    }
    else if (mode == 18)
    {
      tls_test_fault(3, 0);
      __emutls_get_address(&control); // Storage malloc after context and slots.
    }
    else if (mode == 19 || mode == 20)
    {
      assert(__cxa_thread_atexit_impl(noop, nullptr, &tags[0]) == 0);
      tls_test_fault(2, mode - 19);
      ps5_tls_thread_exit(); // Restore/clear key failures are fatal.
    }
    else if (mode == 21) { control.align = 0; __emutls_get_address(&control); }
    abort(); // Every non-returning case above must fail inside the runtime.
  }

  pthread_t threads[24];
  for (intptr_t i = 0; i < 24; ++i)
    assert(pthread_create(&threads[i], nullptr, worker, (void*)i) == 0);
  for (auto thread : threads)
    assert(pthread_join(thread, nullptr) == 0);
  assert(completed == 48 && ps5_tls_dso_pending(&tags[0]) == 0);
  assert(ps5_tls_dso_pending(&__dso_handle) == 0);
  identity = 100;
  check_c_tls(identity);
  assert(object.member == 72);
  value = 99;
  assert(__cxa_thread_atexit_impl(last, nullptr, &tags[0]) == 0);
  assert(__cxa_thread_atexit_impl(first, nullptr, &tags[0]) == 0);
  ps5_tls_thread_exit();
  assert(completed == 50 && ps5_tls_dso_pending(&__dso_handle) == 0);

  pthread_t thread;
  assert(pthread_create(&thread, nullptr, blocked_worker, nullptr) == 0);
  while (!entered.load())
    sched_yield();
  assert(ps5_tls_begin_unload(&tags[1]) == -1 && errno == EBUSY);
  assert(ps5_tls_reopen_dso(&tags[1]) == -1 && errno == EBUSY);
  release_callback = 1;
  assert(pthread_join(thread, nullptr) == 0);
  assert(ps5_tls_begin_unload(&tags[1]) == 0);
  assert(__cxa_thread_atexit_impl(noop, nullptr, &tags[1]) == -1 && errno == EBUSY);
  assert(ps5_tls_reopen_dso(&tags[1]) == 0);
  assert(__cxa_thread_atexit_impl(noop, nullptr, &tags[1]) == 0);
  ps5_tls_thread_exit();
  assert(ps5_tls_dso_pending(&tags[1]) == 0);
  ps5_tls_thread_exit();

  entered = 0;
  assert(pthread_create(&thread, nullptr, canceled_worker, nullptr) == 0);
  while (!entered.load())
    sched_yield();
  assert(pthread_cancel(thread) == 0);
  void* result;
  assert(pthread_join(thread, &result) == 0 && result == PTHREAD_CANCELED);
  assert(ps5_tls_dso_pending(&tags[2]) == 0);

  assert(pthread_create(&thread, nullptr, race_worker, nullptr) == 0);
  for (int i = 0; i < 1000; ++i)
  {
    int status = ps5_tls_begin_unload(&tags[3]);
    assert(status == 0 || errno == EBUSY);
    if (status == 0)
      assert(ps5_tls_dso_pending(&tags[3]) == 0);
    status = ps5_tls_reopen_dso(&tags[3]);
    assert(status == 0 || errno == EBUSY);
  }
  assert(pthread_join(thread, nullptr) == 0);
  assert(ps5_tls_dso_pending(&tags[3]) == 0);
  assert(ps5_tls_reopen_dso(&tags[3]) == 0);
  assert(ps5_tls_begin_unload(nullptr) == 0);
  assert(__cxa_thread_atexit_impl(noop, nullptr, nullptr) == -1 && errno == EBUSY);
  assert(ps5_tls_reopen_dso(nullptr) == 0);
  return 0;
}
"""

# Include the actual runtime with intercepted libc boundaries, not fake TLS.
FAULT_WRAPPER = r"""
#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
static int fault_kind = -1, fault_after;
void tls_test_fault(int kind, int after) { fault_kind = kind; fault_after = after; }
static int failing(int kind)
{
  if (kind != fault_kind) return 0;
  if (fault_after-- != 0) return 0;
  fault_kind = -1;
  return 1;
}
static int test_once(pthread_once_t* once, void (*fn)(void))
{ return failing(0) ? EAGAIN : pthread_once(once, fn); }
static int test_key(pthread_key_t* key, void (*fn)(void*))
{ return failing(1) ? EAGAIN : pthread_key_create(key, fn); }
static int test_set(pthread_key_t key, const void* value)
{ return failing(2) ? EINVAL : pthread_setspecific(key, value); }
static void* test_malloc(size_t size)
{ return failing(3) ? NULL : malloc(size); }
static void* test_calloc(size_t count, size_t size)
{ return failing(4) ? NULL : calloc(count, size); }
#define pthread_once test_once
#define pthread_key_create test_key
#define pthread_setspecific test_set
#define malloc test_malloc
#define calloc test_calloc
#include "shared_tls.c"
void tls_test_exhaust_index(void) { last_index = SIZE_MAX / sizeof(struct tls_slot); }
void tls_test_exhaust_pending(void* tag) { get_dso(tag)->pending = SIZE_MAX; }
"""


class SharedTlsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        # Keep all generated files inside the checkout, never system temp paths.
        cls.directory = tempfile.TemporaryDirectory(prefix=".shared-tls-test-", dir=ROOT)
        cls.work = Path(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_command(self, command, **kwargs):
        return subprocess.run([str(arg) for arg in command], check=True,
                              capture_output=True, text=True, timeout=120, **kwargs)

    @unittest.skipUnless(shutil.which("clang-18") and shutil.which("clang++-18"),
                         "Clang 18 C/C++ compilers required")
    def test_actual_compiler_tls_and_failures(self):
        flags = ["-g", "-O1", "-femulated-tls", "-fsanitize=address,undefined",
                 "-fno-omit-frame-pointer", "-pthread", "-Wall", "-Wextra", "-Werror",
                 "-I" + str(SOURCE.parent)]
        objects = []
        for name, content, compiler in [
                ("runtime.c", FAULT_WRAPPER, "clang-18"),
                ("objects.c", C_OBJECTS, "clang-18"),
                ("probe.cpp", HOST_PROBE, "clang++-18")]:
            source = self.work / name
            source.write_text(content)
            output = source.with_suffix(".o")
            self.run_command([compiler, *flags, "-c", source, "-o", output])
            objects.append(output)
        executable = self.work / "probe"
        self.run_command(["clang++-18", *flags, *objects, "-o", executable])
        environment = dict(os.environ, ASAN_OPTIONS="detect_leaks=1:abort_on_error=1",
                           UBSAN_OPTIONS="halt_on_error=1")
        self.run_command([executable], env=environment)
        for mode in range(22):
            with self.subTest(mode=mode):
                result = subprocess.run([str(executable), str(mode)], env=environment,
                                        capture_output=True, text=True, timeout=30)
                fatal = mode in {4, 8, 9, 10, 11, 12, 13, 14, 15, 18, 19, 20, 21}
                self.assertEqual(result.returncode, -6 if fatal else 0, result.stderr)
                self.assertIn("shared_tls:", result.stderr)
                self.assertNotIn("ERROR: AddressSanitizer", result.stderr)
                self.assertNotIn("runtime error:", result.stderr)

    @unittest.skipUnless((SDK / "bin/prospero-clang").is_file(), "PS5 SDK required")
    def test_target_compiler_abi_and_no_native_tls(self):
        flags = ["--target=x86_64-unknown-freebsd", "-fPIC", "-femulated-tls",
                 "-Wall", "-Wextra", "-Werror", "-I" + str(SOURCE.parent)]
        compiler = SDK / "bin/prospero-clang"
        runtime = self.work / "target-runtime.o"
        self.run_command([compiler, *flags, "-std=c11", "-c", SOURCE, "-o", runtime])
        generated = self.work / "target-objects.c"
        generated.write_text(C_OBJECTS + r"""
#include "shared_tls.h"
_Static_assert(sizeof(struct ps5_emutls_control) == 32, "control size");
_Static_assert(offsetof(struct ps5_emutls_control, size) == 0, "size offset");
_Static_assert(offsetof(struct ps5_emutls_control, align) == 8, "align offset");
_Static_assert(offsetof(struct ps5_emutls_control, loc) == 16, "index offset");
_Static_assert(offsetof(struct ps5_emutls_control, template_value) == 24, "template offset");
""")
        llvm = self.work / "target.ll"
        self.run_command([compiler, *flags, "-S", "-emit-llvm", generated, "-o", llvm])
        # Emutls lowering happens during code generation, after LLVM IR emission.
        objects = self.work / "target-objects.o"
        self.run_command([compiler, *flags, "-c", generated, "-o", objects])
        assembly = self.work / "target-objects.s"
        self.run_command([compiler, *flags, "-S", generated, "-o", assembly])
        text = assembly.read_text()
        self.assertIn("__emutls_get_address", text)
        self.assertRegex(text, r"(?s)__emutls_v.c_initialized:.*?\.quad\s+4.*?"
                         r"\.quad\s+4.*?\.quad\s+0.*?\.quad\s+__emutls_t.c_initialized")
        cpp = self.work / "target-object.cpp"
        cpp.write_text("""
thread_local int other = 27;
struct Object { int value = 42; ~Object(); };
thread_local Object object;
Object::~Object() { other += value; }
int touch() { return object.value + other; }
""")
        cpp_object = self.work / "target-cxx.o"
        self.run_command([SDK / "bin/prospero-clang++", *flags,
                          "-std=c++17", "-c", cpp, "-o", cpp_object])
        cpp_undefined = self.run_command(["nm", "-u", cpp_object]).stdout
        self.assertIn("__cxa_thread_atexit", cpp_undefined)
        self.assertIn("__emutls_get_address", cpp_undefined)
        for obj in [runtime, objects, cpp_object]:
            symbols = self.run_command(["readelf", "-sW", obj]).stdout
            self.assertNotRegex(symbols, r"\bTLS\b")
            relocations = self.run_command(["readelf", "-rW", obj]).stdout
            self.assertNotRegex(relocations, r"R_X86_64_(?:TPOFF|DTPOFF|DTPMOD|TLS|GOTTPOFF)")
        undefined = self.run_command(["nm", "-u", runtime]).stdout
        self.assertNotIn("__emutls_get_address", undefined)
        self.assertNotIn("__cxa_thread_atexit", undefined)


if __name__ == "__main__":
    unittest.main()
