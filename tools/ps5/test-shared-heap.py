#!/usr/bin/env python3
"""Host ASan/UBSan routing tests and actual SDK FreeBSD PIC/import probes.

Run: python3 tools/ps5/test-shared-heap.py
PS5_PAYLOAD_SDK selects the compiler and linker-only system import libraries.
No firmware-analysis inputs or console calls.
All scratch files stay below the repository and are removed on completion.
"""
# SPDX-License-Identifier: GPL-3.0-or-later
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from native_c_link import Elf

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "shims/native-app/shared_heap.c"
SDK = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
WRAPPERS = ("malloc", "calloc", "realloc", "free", "posix_memalign",
            "malloc_usable_size", "aligned_alloc", "memalign", "reallocf")

HARNESS = r"""
#define _GNU_SOURCE
#include "shared_heap.h"
#include <assert.h>
#include <errno.h>
#include <malloc.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static _Alignas(16384) unsigned char arena[1024 * 1024];
static size_t cursor;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static atomic_int get_calls, allocate_calls, map_calls, create_calls, unmap_calls, release_calls;
static atomic_int pool_malloc, pool_calloc, pool_realloc, pool_free, pool_aligned, pool_usable;
static atomic_int native_malloc, native_calloc, native_realloc, native_free, native_aligned;
static atomic_int native_usable, logs, once_calls;
static int mode, fail_alloc, fail_realloc, aligned_error, live_mapping;
static int native_fail_realloc;
static const size_t expected_size = (size_t)2048 * 1024 * 1024;
enum {
  SUCCESS, CONCURRENT, MISSING, TOO_SMALL, ALLOC_FAIL, MAP_FAIL, TYPE_FALLBACK,
  PROT_FALLBACK, CREATE_FAIL, UNMAP_FAIL, RELEASE_FAIL, BAD_RANGE, NULL_MAP,
  ONCE_FAIL, INVALID_SIZE
};

int mock_pthread_once(pthread_once_t* once, void (*function)(void))
{
  atomic_fetch_add(&once_calls, 1);
  if (mode == ONCE_FAIL)
    return EAGAIN;
  return pthread_once(once, function);
}

int sceKernelDebugOutText(int channel, const char* message)
{
  assert(channel == 0 && strstr(message, "shared heap:"));
  atomic_fetch_add(&logs, 1);
  return 0;
}
int64_t sceKernelGetDirectMemorySize(void)
{
  atomic_fetch_add(&get_calls, 1);
  if (mode == MISSING) return -1;
  if (mode == TOO_SMALL) return 1024;
  return INT64_C(8) * 1024 * 1024 * 1024;
}
int sceKernelAllocateDirectMemory(int64_t begin, int64_t end, size_t size,
                                 size_t alignment, int type, int64_t* start)
{
  int call = atomic_fetch_add(&allocate_calls, 1);
  assert(begin == 0 && end == INT64_C(8) * 1024 * 1024 * 1024);
  assert(size == expected_size && alignment == 0x4000);
  assert(type == (call ? 0 : 12));
  if (mode == ALLOC_FAIL || (mode == TYPE_FALLBACK && !call)) return -12;
  *start = 0x4000;
  return 0;
}
int sceKernelMapDirectMemory(void** address, size_t size, int prot, int flags,
                            int64_t start, size_t alignment)
{
  int call = atomic_fetch_add(&map_calls, 1);
  assert(size == expected_size && flags == 0 && start == 0x4000 && alignment == 0x4000);
  assert(prot == ((call % 2) ? 0x33 : 0x03));
  if (mode == MAP_FAIL || mode == RELEASE_FAIL || (mode == PROT_FALLBACK && !call))
    return -13;
  live_mapping = 1;
  *address = mode == NULL_MAP ? NULL : mode == BAD_RANGE ? (void*)(UINTPTR_MAX - 8) : arena;
  return 0;
}
int sceKernelMunmap(void* base, size_t size)
{
  atomic_fetch_add(&unmap_calls, 1);
  assert(live_mapping && size == expected_size);
  assert(base == arena || mode == BAD_RANGE || mode == NULL_MAP);
  if (mode == UNMAP_FAIL) return -16;
  live_mapping = 0;
  return 0;
}
int sceKernelReleaseDirectMemory(int64_t start, size_t size)
{
  assert(!live_mapping && start == 0x4000 && size == expected_size);
  atomic_fetch_add(&release_calls, 1);
  return mode == RELEASE_FAIL ? -16 : 0;
}
void* sceLibcMspaceCreate(const char* name, void* base, size_t size, unsigned flags)
{
  atomic_fetch_add(&create_calls, 1);
  assert(strcmp(name, "KodiShared") == 0 && base == arena && size == expected_size && !flags);
  return mode == CREATE_FAIL || mode == UNMAP_FAIL ? NULL : arena;
}
static void* pool_allocate(size_t size, size_t alignment)
{
  if (fail_alloc) return NULL;
  assert(size && size < sizeof(arena) / 2);
  assert(pthread_mutex_lock(&lock) == 0);
  size_t begin = (cursor + sizeof(size_t) + alignment - 1) & ~(alignment - 1);
  assert(begin + size <= sizeof(arena));
  void* result = arena + begin;
  memcpy((unsigned char*)result - sizeof(size_t), &size, sizeof(size_t));
  cursor = begin + size;
  assert(pthread_mutex_unlock(&lock) == 0);
  return result;
}
static size_t pool_size(const void* address)
{
  size_t size;
  assert((const unsigned char*)address >= arena &&
         (const unsigned char*)address < arena + sizeof(arena));
  memcpy(&size, (const unsigned char*)address - sizeof(size_t), sizeof(size_t));
  return size;
}
void* sceLibcMspaceMalloc(void* pool, size_t size)
{
  assert(pool == arena); atomic_fetch_add(&pool_malloc, 1);
  return pool_allocate(size, 16);
}
void* sceLibcMspaceCalloc(void* pool, size_t count, size_t size)
{
  assert(pool == arena); atomic_fetch_add(&pool_calloc, 1);
  void* result = pool_allocate(count * size, 16);
  if (result) memset(result, 0, count * size);
  return result;
}
void* sceLibcMspaceRealloc(void* pool, void* address, size_t size)
{
  assert(pool == arena && address && size); atomic_fetch_add(&pool_realloc, 1);
  if (fail_realloc) return NULL;
  size_t before = pool_size(address);
  void* result = pool_allocate(size, 16);
  if (result) memcpy(result, address, before < size ? before : size);
  return result;
}
void sceLibcMspaceFree(void* pool, void* address)
{
  assert(pool == arena); (void)pool_size(address); atomic_fetch_add(&pool_free, 1);
}
int sceLibcMspacePosixMemalign(void* pool, void** address, size_t alignment, size_t size)
{
  assert(pool == arena); atomic_fetch_add(&pool_aligned, 1);
  if (aligned_error) return aligned_error;
  *address = pool_allocate(size, alignment);
  return *address ? 0 : ENOMEM;
}
size_t sceLibcMspaceMallocUsableSize(const void* address)
{
  atomic_fetch_add(&pool_usable, 1); return pool_size(address);
}

/* These are ordinary host allocator calls, never global --wrap overrides. */
void* __real_malloc(size_t size) { ++native_malloc; return malloc(size); }
void* __real_calloc(size_t count, size_t size) { ++native_calloc; return calloc(count, size); }
void* __real_realloc(void* address, size_t size)
{
  assert(address && size); ++native_realloc;
  if (native_fail_realloc) { errno = ENOMEM; return NULL; }
  return realloc(address, size);
}
void __real_free(void* address) { assert(address); ++native_free; free(address); }
int __real_posix_memalign(void** address, size_t alignment, size_t size)
{
  ++native_aligned; return posix_memalign(address, alignment, size);
}
size_t __real_malloc_usable_size(const void* address)
{
  ++native_usable; return malloc_usable_size((void*)address);
}

static void foreign_probe(void)
{
  unsigned char* p = malloc(32); assert(p); p[0] = 123;
  assert(__wrap_malloc_usable_size(p) >= 32 && native_usable == 1);
  p = __wrap_realloc(p, 64); assert(p && p[0] == 123 && native_realloc == 1);
  __wrap_free(p); assert(native_free == 1);
  p = malloc(8); assert(p);
  assert(!__wrap_realloc(p, 0) && native_free == 2 && native_realloc == 1);
  p = malloc(8); assert(p);
  assert(!__wrap_reallocf(p, 0) && native_free == 3 && native_realloc == 1);
  p = malloc(8); assert(p); native_fail_realloc = 1;
  errno = 0; assert(!__wrap_realloc(p, 64) && errno == ENOMEM && native_free == 3);
  __wrap_free(p); assert(native_free == 4);
  p = malloc(8); assert(p);
  errno = 0; assert(!__wrap_reallocf(p, 64) && errno == ENOMEM && native_free == 5);
  native_fail_realloc = 0;
}
static void success_probe(void)
{
  foreign_probe();
  assert(get_calls == 0 && once_calls == 0);
  assert(ps5_shared_heap_initialize() == 0);
  assert(get_calls == 1 && create_calls == 1 && release_calls == 0 && unmap_calls == 0);
  int m = pool_malloc, c = pool_calloc, r = pool_realloc, f = pool_free, a = pool_aligned;
  unsigned char* p = __wrap_malloc(32); assert(p && pool_malloc == m + 1); p[0] = 42;
  assert(__wrap_malloc_usable_size(p) == 32 && pool_usable == 1);
  p = __wrap_realloc(p, 64); assert(p && p[0] == 42 && pool_realloc == r + 1);
  fail_realloc = 1;
  errno = 0; assert(!__wrap_realloc(p, 128) && errno == ENOMEM && p[0] == 42);
  assert(pool_free == f);
  assert(!__wrap_reallocf(p, 128) && pool_free == ++f);
  fail_realloc = 0;
  p = __wrap_calloc(8, 4); assert(p && pool_calloc == c + 1);
  for (int i = 0; i < 32; ++i) assert(!p[i]);
  __wrap_free(p); assert(pool_free == ++f);
  p = __wrap_realloc(NULL, 0); assert(p && pool_size(p) == 1);
  assert(!__wrap_realloc(p, 0) && pool_free == ++f);
  p = __wrap_reallocf(NULL, 10); assert(p);
  assert(!__wrap_reallocf(p, 0) && pool_free == ++f);
  p = __wrap_calloc(0, SIZE_MAX); assert(p && pool_size(p) == 1); __wrap_free(p);
  p = __wrap_calloc(SIZE_MAX, 0); assert(p && pool_size(p) == 1); __wrap_free(p);
  errno = 0; c = pool_calloc;
  assert(!__wrap_calloc(SIZE_MAX, 2) && errno == ENOMEM && pool_calloc == c);
  void* sentinel = (void*)(uintptr_t)1;
  p = sentinel; errno = EDOM;
  assert(__wrap_posix_memalign((void**)&p, 3, 10) == EINVAL && p == sentinel && errno == EDOM);
  assert(__wrap_posix_memalign((void**)&p, 4, 10) == EINVAL && p == sentinel);
  assert(__wrap_posix_memalign(NULL, 64, 10) == EINVAL);
  assert(__wrap_posix_memalign((void**)&p, 64, SIZE_MAX) == ENOMEM && p == sentinel);
  assert(pool_aligned == a);
  assert(!__wrap_posix_memalign((void**)&p, 64, 100) && !((uintptr_t)p % 64));
  assert(errno == EDOM && pool_aligned == a + 1); __wrap_free(p);
  assert(!__wrap_posix_memalign((void**)&p, 64, 0) && pool_size(p) == 1); __wrap_free(p);
  aligned_error = EAGAIN; p = sentinel;
  assert(__wrap_posix_memalign((void**)&p, 64, 16) == EAGAIN && errno == EDOM && p == sentinel);
  aligned_error = 0;
  p = __wrap_aligned_alloc(256, 512); assert(p && !((uintptr_t)p % 256)); __wrap_free(p);
  p = __wrap_aligned_alloc(1, 1); assert(p); __wrap_free(p);
  errno = 0; assert(!__wrap_aligned_alloc(64, 65) && errno == EINVAL);
  assert(!__wrap_aligned_alloc(0, 0) && errno == EINVAL);
  assert(!__wrap_aligned_alloc(3, 6) && errno == EINVAL);
  p = __wrap_memalign(128, 19); assert(p && !((uintptr_t)p % 128)); __wrap_free(p);
  p = __wrap_memalign(1, 0); assert(p && pool_size(p) == 1); __wrap_free(p);
  assert(!__wrap_memalign(0, 10) && errno == EINVAL);
  assert(!__wrap_memalign(6, 10) && errno == EINVAL);
  assert(!__wrap_memalign(64, SIZE_MAX) && errno == ENOMEM);
  fail_alloc = 1;
  errno = 0; assert(!__wrap_malloc(10) && errno == ENOMEM);
  errno = 0; assert(!__wrap_calloc(2, 3) && errno == ENOMEM);
  errno = EDOM; p = sentinel;
  assert(__wrap_posix_memalign((void**)&p, 64, 10) == ENOMEM && p == sentinel && errno == EDOM);
  assert(!__wrap_aligned_alloc(64, 64) && errno == ENOMEM);
  assert(!__wrap_memalign(64, 64) && errno == ENOMEM);
  int nf = native_free; f = pool_free;
  __wrap_free(NULL); assert(native_free == nf && pool_free == f);
  assert(__wrap_malloc_usable_size(NULL) == 0);
  assert(create_calls == 1 && !unmap_calls && !release_calls);
}
static void* worker(void* unused)
{
  (void)unused;
  assert(ps5_shared_heap_initialize() == 0);
  for (int i = 0; i < 50; ++i) {
    void* p = __wrap_malloc(8); assert(p); __wrap_free(p);
  }
  return NULL;
}
int main(int argc, char** argv)
{
  assert(argc == 2); mode = atoi(argv[1]);
  if (mode == SUCCESS) success_probe();
  else if (mode == CONCURRENT) {
    pthread_t threads[24];
    for (int i = 0; i < 24; ++i) assert(!pthread_create(&threads[i], NULL, worker, NULL));
    for (int i = 0; i < 24; ++i) assert(!pthread_join(threads[i], NULL));
    assert(get_calls == 1 && allocate_calls == 1 && map_calls == 1 && create_calls == 1);
    assert(pool_malloc == 1200 && pool_free == 1200 && !unmap_calls && !release_calls);
  } else if (mode == TYPE_FALLBACK || mode == PROT_FALLBACK) {
    assert(!ps5_shared_heap_initialize());
    assert(allocate_calls == (mode == TYPE_FALLBACK ? 2 : 1));
    assert(map_calls == (mode == PROT_FALLBACK ? 2 : 1));
    assert(create_calls == 1 && !unmap_calls && !release_calls && logs);
  } else {
    int expected_error = mode == ONCE_FAIL ? EAGAIN : mode == INVALID_SIZE ? EINVAL : ENOMEM;
    errno = 0; assert(ps5_shared_heap_initialize() == -1 && errno == expected_error);
    int calls = get_calls;
    errno = 0; assert(!__wrap_malloc(10) && errno == expected_error);
    assert(!__wrap_calloc(2, 3) && errno == expected_error);
    assert(!__wrap_realloc(NULL, 5) && errno == expected_error);
    void* p = (void*)(uintptr_t)1; errno = EDOM;
    assert(__wrap_posix_memalign(&p, 64, 10) == expected_error && errno == EDOM);
    assert(p == (void*)(uintptr_t)1);
    assert(!__wrap_memalign(64, 10) && errno == expected_error);
    assert(!__wrap_aligned_alloc(64, 64) && errno == expected_error);
    assert(get_calls == calls && !pool_malloc && !pool_calloc && !pool_aligned);
    foreign_probe();
    assert(logs);
    if (mode == MISSING || mode == TOO_SMALL) assert(!allocate_calls && !map_calls);
    if (mode == ALLOC_FAIL) assert(allocate_calls == 2 && !map_calls && !release_calls);
    if (mode == MAP_FAIL || mode == RELEASE_FAIL)
      assert(allocate_calls == 2 && map_calls == 4 && release_calls == 2 && !unmap_calls);
    if (mode == CREATE_FAIL || mode == BAD_RANGE || mode == NULL_MAP)
      assert(unmap_calls == 1 && release_calls == 1 && !live_mapping);
    if (mode == UNMAP_FAIL) assert(unmap_calls == 1 && !release_calls && live_mapping);
    if (mode == ONCE_FAIL || mode == INVALID_SIZE) assert(!get_calls);
  }
  assert(!native_malloc && !native_calloc && !native_aligned);
  return 0;
}
"""


class SharedHeapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix=".shared-heap-probe-", dir=ROOT)
        cls.addClassCleanup(cls.directory.cleanup)
        cls.work = Path(cls.directory.name)
        cls.host = shutil.which("clang-18") or shutil.which("clang")
        if not cls.host:
            raise RuntimeError("host clang is required")
        cls.harness = cls.work / "harness.c"
        cls.harness.write_text(HARNESS)
        cls.executable = cls.build_host()

    @staticmethod
    def run_command(command, **kwargs):
        result = subprocess.run([str(item) for item in command], text=True,
                                capture_output=True, **kwargs)
        if result.returncode:
            raise AssertionError(f"{command}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    @classmethod
    def build_host(cls, mib=None):
        suffix = "default" if mib is None else str(mib)
        obj = cls.work / f"heap-{suffix}.o"
        exe = cls.work / f"heap-{suffix}"
        flags = ["-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-pthread",
                 "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        command = [cls.host, *flags, "-Dpthread_once=mock_pthread_once"]
        if mib is not None:
            command += [f"-DPS5_SHARED_HEAP_MIB={mib}"]
        cls.run_command([*command, "-c", SOURCE, "-o", obj])
        cls.run_command([cls.host, *flags, "-I", SOURCE.parent, cls.harness, obj, "-o", exe])
        return exe

    def test_host_routing_concurrency_and_failures(self):
        cases = ("success", "concurrent", "missing", "too-small", "allocate-failure",
                 "map-failure", "type-fallback", "protection-fallback", "mspace-failure",
                 "unmap-failure", "release-failure", "invalid-range", "null-mapping",
                 "pthread-once-failure")
        for number, name in enumerate(cases):
            with self.subTest(case=name):
                self.run_command([self.executable, number],
                                 env={**os.environ, "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1",
                                      "UBSAN_OPTIONS": "halt_on_error=1"})

    def test_invalid_heap_configuration(self):
        for mib in ("0", "-1", "17592186044416ULL"):
            with self.subTest(mib=mib):
                self.run_command([self.build_host(mib), 14])

    def test_native_freebsd_pic_exports_and_sdk_provider_imports(self):
        compiler = SDK / "bin/prospero-clang"
        self.assertTrue(compiler.is_file(), "actual SDK compiler required")
        obj = self.work / "heap-native.o"
        self.run_command([compiler, "--target=x86_64-unknown-freebsd9",
                          "-march=znver2", "-fshort-wchar", "-D__PROSPERO__=1", "-D__SCE__=1",
                          "-std=c11",
                          "-O2", "-Wall", "-Wextra", "-Werror", "-fPIC",
                          "-fvisibility=hidden", "-c", SOURCE, "-o", obj])
        symbols = self.run_command([SDK / "bin/llvm-nm", "--undefined-only", obj])
        imports = set(re.findall(r"\bU (\S+)", symbols))
        expected_libc = {"snprintf", "realloc", "free", "malloc_usable_size",
                         "sceLibcMspaceCreate", "sceLibcMspaceMalloc", "sceLibcMspaceCalloc",
                         "sceLibcMspaceRealloc", "sceLibcMspaceFree",
                         "sceLibcMspacePosixMemalign", "sceLibcMspaceMallocUsableSize"}
        expected_kernel = {"__error", "pthread_once", "sceKernelGetDirectMemorySize",
                           "sceKernelAllocateDirectMemory", "sceKernelMapDirectMemory",
                           "sceKernelMunmap", "sceKernelReleaseDirectMemory",
                           "sceKernelDebugOutText"}
        # Final shared linking, unlike ld -r, applies __real_* relocation wrapping.
        linked = self.work / "heap-wrapped.so"
        self.run_command([SDK / "bin/ld.lld", "--shared", "--no-undefined", obj, "-o", linked,
                          "-L", SDK / "target/lib", "-lSceLibcInternal", "-lkernel",
                          *("--wrap=" + name for name in WRAPPERS)])
        symbols = self.run_command([SDK / "bin/llvm-nm", "--undefined-only", linked])
        resolved_imports = set(re.findall(r"\bU (\S+)", symbols))
        self.assertEqual(resolved_imports, expected_libc | expected_kernel)
        self.assertEqual(imports - resolved_imports,
                         {"__real_realloc", "__real_free", "__real_malloc_usable_size"})
        readelf = shutil.which("llvm-readelf-18") or shutil.which("readelf")
        self.assertIsNotNone(readelf, "ELF symbol inspection tool required")
        table = self.run_command([readelf, "--symbols", linked])
        for name in ("ps5_shared_heap_initialize", *("__wrap_" + n for n in WRAPPERS)):
            self.assertRegex(table, rf"GLOBAL\s+DEFAULT\s+\d+\s+{name}\b")
        for library, names in (
            ("libSceLibcInternal", expected_libc),
            ("libkernel", expected_kernel),
        ):
            provider = SDK / "target/lib" / (library + ".so")
            exports = {name for name, row in Elf(provider.read_bytes()).symbols(".dynsym")
                       if row[3]}
            for name in names:
                self.assertIn(name, exports, f"missing SDK import {name}")
            if library == "libSceLibcInternal":
                for name in WRAPPERS:
                    self.assertIn(name, exports, f"unsupported allocator family member {name}")
                self.assertNotIn("reallocarray", exports,
                                 "update the public allocator family for native reallocarray")


if __name__ == "__main__":
    unittest.main()
