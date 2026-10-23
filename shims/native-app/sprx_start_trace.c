/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "native_start_trace.h"

#include <stddef.h>
#include <string.h>

extern void sceKernelDebugOutText(int channel, const char* text);
extern int __real_sceKernelLoadStartModule(
    const char*, size_t, const void*, unsigned int, const void*, int*);

static void emit(int stage)
{
  const char* message = stage == PS5_SPRX_TRACE_ENTER
                            ? "[sprx-start-trace] initializer-enter\n"
                        : stage == PS5_SPRX_TRACE_LEAVE
                            ? "[sprx-start-trace] initializer-leave\n"
                            : "[sprx-start-trace] invalid-stage\n";
  sceKernelDebugOutText(0, message);
}

int __wrap_sceKernelLoadStartModule(const char* path,
                                  size_t size,
                                  const void* args,
                                  unsigned int flags,
                                  const void* options,
                                  int* result)
{
  const char* basename = path ? strrchr(path, '/') : NULL;
  basename = basename ? basename + 1 : path;
  if (!basename || strcmp(basename, "screensaver.ps5.sprx-test.sprx") != 0)
    return __real_sceKernelLoadStartModule(path, size, args, flags, options, result);

  if (size || args || flags || options || !result)
  {
    sceKernelDebugOutText(0, "[sprx-start-trace] unsupported caller arguments\n");
    return -1;
  }

  const PS5SprxStartTrace trace = {.version = 1, .emit = emit};
  sceKernelDebugOutText(0, "[sprx-start-trace] host-ready\n");
  const int handle =
      __real_sceKernelLoadStartModule(path, sizeof(trace), &trace, flags, options, result);
  sceKernelDebugOutText(0, "[sprx-start-trace] public-api-return\n");
  return handle;
}
