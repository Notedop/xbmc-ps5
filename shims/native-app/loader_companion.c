/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stddef.h>

extern void* malloc(size_t);
extern void free(void*);
extern int posix_memalign(void**, size_t, size_t);
extern void sceKernelDebugOutText(int, const char*);

struct application_heap_api
{
  void* (*allocate)(size_t);
  void (*release)(void*);
  void* reserved[4];
  int (*aligned_allocate)(void**, size_t, size_t);
  void* reserved_tail[2];
};

extern void _sceKernelRtldSetApplicationHeapAPI(const struct application_heap_api*);

_Static_assert(sizeof(struct application_heap_api) == 0x48, "native loader heap API size");
_Static_assert(offsetof(struct application_heap_api, aligned_allocate) == 0x30,
               "native loader aligned allocation slot");

const unsigned int ps5_native_loader_companion_v1 = 1;
static const struct application_heap_api heap_api = {
    .allocate = malloc,
    .release = free,
    .aligned_allocate = posix_memalign,
};
static int started;

#ifdef __PROSPERO__
__asm__(".hidden module_start\n.hidden module_stop\n");
#endif

__attribute__((visibility("hidden"))) int module_start(size_t size, const void* arguments)
{
  (void)size;
  (void)arguments;
  if (started)
  {
    sceKernelDebugOutText(0, "loader_companion: duplicate initialization rejected\n");
    return -1;
  }
  _sceKernelRtldSetApplicationHeapAPI(&heap_api);
  started = 1;
  sceKernelDebugOutText(0, "loader_companion: native loader heap registered\n");
  return 0;
}

__attribute__((visibility("hidden"))) int module_stop(size_t size, const void* arguments)
{
  (void)size;
  (void)arguments;
  sceKernelDebugOutText(0, "loader_companion: process-lifetime module cannot be unloaded\n");
  return -1;
}
