/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "shared_heap.h"

#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>

#ifndef PS5_SHARED_HEAP_MIB
#define PS5_SHARED_HEAP_MIB 2048
#endif

#define HEAP_PAGE_SIZE ((size_t)0x4000)

void* __real_realloc(void* address, size_t size);
void __real_free(void* address);
size_t __real_malloc_usable_size(const void* address);

void* sceLibcMspaceCreate(const char* name, void* base, size_t size, unsigned flags);
void* sceLibcMspaceMalloc(void* mspace, size_t size);
void* sceLibcMspaceCalloc(void* mspace, size_t count, size_t size);
void* sceLibcMspaceRealloc(void* mspace, void* address, size_t size);
void sceLibcMspaceFree(void* mspace, void* address);
int sceLibcMspacePosixMemalign(void* mspace, void** address, size_t alignment, size_t size);
size_t sceLibcMspaceMallocUsableSize(const void* address);

int64_t sceKernelGetDirectMemorySize(void);
int sceKernelAllocateDirectMemory(int64_t begin, int64_t end, size_t size,
                                 size_t alignment, int type, int64_t* start);
int sceKernelMapDirectMemory(void** address, size_t size, int protection, int flags,
                            int64_t start, size_t alignment);
int sceKernelMunmap(void* address, size_t size);
int sceKernelReleaseDirectMemory(int64_t start, size_t size);
int sceKernelDebugOutText(int channel, const char* text);

static pthread_once_t heap_once = PTHREAD_ONCE_INIT;
static atomic_int heap_ready;
static int heap_error = ENOMEM;
static void* heap_base;
static size_t heap_size;
static void* heap_mspace;

static void diagnose(const char* operation, size_t size, int error)
{
  char text[192];
  snprintf(text, sizeof(text), "[kodi-ps5] shared heap: %s size=%zu error=0x%x\n",
           operation, size, (unsigned)error);
  sceKernelDebugOutText(0, text);
}

static void release_backing(int64_t start, size_t size)
{
  int error = sceKernelReleaseDirectMemory(start, size);
  if (error)
    diagnose("release direct memory failed (backing retained)", size, error);
}

static void discard_mapping(void* base, int64_t start, size_t size)
{
  int error = sceKernelMunmap(base, size);
  if (error)
    diagnose("unmap failed (backing retained)", size, error);
  else
    release_backing(start, size);
}

static void initialize_heap(void)
{
  const uintmax_t mib = PS5_SHARED_HEAP_MIB;
  static const int types[] = {12, 0};
  static const int protections[] = {0x03, 0x33};
  if (PS5_SHARED_HEAP_MIB <= 0 || mib > SIZE_MAX / (1024u * 1024u) ||
      mib > INT64_MAX / (1024u * 1024u))
  {
    heap_error = EINVAL;
    diagnose("invalid PS5_SHARED_HEAP_MIB", 0, heap_error);
    return;
  }
  size_t size = (size_t)mib * 1024u * 1024u;
  if (size % HEAP_PAGE_SIZE)
  {
    heap_error = EINVAL;
    diagnose("heap size is not page aligned", size, heap_error);
    return;
  }
  int64_t limit = sceKernelGetDirectMemorySize();
  if (limit <= 0 || (uint64_t)limit < size)
  {
    diagnose("direct memory unavailable", size, ENOMEM);
    return;
  }
  for (size_t t = 0; t < sizeof(types) / sizeof(types[0]); ++t)
  {
    int64_t start = -1;
    int error = sceKernelAllocateDirectMemory(0, limit, size, HEAP_PAGE_SIZE, types[t], &start);
    if (error)
    {
      diagnose(types[t] == 12 ? "allocate type 12 failed" : "allocate type 0 failed", size, error);
      continue;
    }
    for (size_t p = 0; p < sizeof(protections) / sizeof(protections[0]); ++p)
    {
      void* base = NULL;
      error = sceKernelMapDirectMemory(&base, size, protections[p], 0, start, HEAP_PAGE_SIZE);
      if (error)
      {
        diagnose(protections[p] == 0x03 ? "map prot 0x03 failed" : "map prot 0x33 failed",
                 size, error);
        continue;
      }
      if (!base || (uintptr_t)base > UINTPTR_MAX - size)
      {
        diagnose("invalid mapping range", size, ENOMEM);
        discard_mapping(base, start, size);
        return;
      }
      void* mspace = sceLibcMspaceCreate("KodiShared", base, size, 0);
      if (!mspace)
      {
        diagnose("sceLibcMspaceCreate failed", size, ENOMEM);
        discard_mapping(base, start, size);
        return;
      }
      heap_base = base;
      heap_size = size;
      heap_mspace = mspace;
      heap_error = 0;
      atomic_store_explicit(&heap_ready, 1, memory_order_release);
      return;
    }
    release_backing(start, size);
  }
  diagnose("initialization exhausted direct memory options", size, ENOMEM);
}

int ps5_shared_heap_initialize(void)
{
  int error = pthread_once(&heap_once, initialize_heap);
  if (error)
    diagnose("pthread_once failed", 0, error);
  else
    error = heap_error;
  if (!error)
    return 0;
  errno = error;
  return -1;
}

static int owns(const void* address)
{
  if (!atomic_load_explicit(&heap_ready, memory_order_acquire))
    return 0;
  uintptr_t value = (uintptr_t)address;
  uintptr_t base = (uintptr_t)heap_base;
  return value >= base && value - base < heap_size;
}

static void* allocation_result(void* address, size_t size, const char* operation)
{
  if (!address)
  {
    diagnose(operation, size, ENOMEM);
    errno = ENOMEM;
  }
  return address;
}

void* __wrap_malloc(size_t size)
{
  if (ps5_shared_heap_initialize())
    return NULL;
  return allocation_result(sceLibcMspaceMalloc(heap_mspace, size ? size : 1), size, "malloc failed");
}

void* __wrap_calloc(size_t count, size_t size)
{
  if (size && count > SIZE_MAX / size)
    return allocation_result(NULL, size, "calloc overflow");
  if (ps5_shared_heap_initialize())
    return NULL;
  if (!count || !size)
    count = size = 1;
  return allocation_result(sceLibcMspaceCalloc(heap_mspace, count, size), count * size,
                           "calloc failed");
}

void __wrap_free(void* address)
{
  if (!address)
    return;
  if (owns(address))
    sceLibcMspaceFree(heap_mspace, address);
  else
    __real_free(address);
}

void* __wrap_realloc(void* address, size_t size)
{
  if (!address)
    return __wrap_malloc(size);
  if (!size)
  {
    __wrap_free(address);
    return NULL;
  }
  void* result = owns(address) ? sceLibcMspaceRealloc(heap_mspace, address, size)
                               : __real_realloc(address, size);
  return allocation_result(result, size, "realloc failed");
}

size_t __wrap_malloc_usable_size(const void* address)
{
  if (!address)
    return 0;
  return owns(address) ? sceLibcMspaceMallocUsableSize(address)
                       : __real_malloc_usable_size(address);
}

int __wrap_posix_memalign(void** address, size_t alignment, size_t size)
{
  int saved_errno = errno;
  int error;
  void* result = NULL;
  if (!address || alignment < sizeof(void*) || (alignment & (alignment - 1)))
    error = EINVAL;
  else if (size > SIZE_MAX - (alignment - 1))
    error = ENOMEM;
  else if (ps5_shared_heap_initialize())
    error = errno;
  else
  {
    error = sceLibcMspacePosixMemalign(heap_mspace, &result, alignment, size ? size : 1);
    if (!error && !result)
      error = ENOMEM;
  }
  if (error)
    diagnose("posix_memalign failed", size, error);
  else
    *address = result;
  errno = saved_errno;
  return error;
}

static void* aligned_allocation(size_t alignment, size_t size)
{
  /* C aligned_alloc/memalign also permit power-of-two alignments below sizeof(void*). */
  if (!alignment || (alignment & (alignment - 1)))
  {
    diagnose("invalid alignment", size, EINVAL);
    errno = EINVAL;
    return NULL;
  }
  void* result = NULL;
  int error = __wrap_posix_memalign(&result, alignment < sizeof(void*) ? sizeof(void*) : alignment,
                                   size);
  if (error)
    errno = error;
  return result;
}

void* __wrap_aligned_alloc(size_t alignment, size_t size)
{
  if (!alignment || size % alignment)
  {
    diagnose("aligned_alloc size/alignment mismatch", size, EINVAL);
    errno = EINVAL;
    return NULL;
  }
  return aligned_allocation(alignment, size);
}

void* __wrap_memalign(size_t alignment, size_t size)
{
  return aligned_allocation(alignment, size);
}

void* __wrap_reallocf(void* address, size_t size)
{
  void* result = __wrap_realloc(address, size);
  if (!result && address && size)
  {
    int error = errno;
    __wrap_free(address);
    errno = error;
  }
  return result;
}
