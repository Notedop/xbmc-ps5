/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "native_start_trace.h"

#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

int module_start(size_t, const void*);
int __wrap_sceKernelLoadStartModule(
    const char*, size_t, const void*, unsigned int, const void*, int*);

static const char* target = "/app0/screensaver.ps5.sprx-test.sprx";
static const char* messages[8];
static unsigned int count;
static unsigned int calls;
static int mode;
static int original_args;

void sceKernelDebugOutText(int channel, const char* text)
{
  assert(channel == 0 && count < 8);
  messages[count++] = text;
}

int __real_sceKernelLoadStartModule(const char* path,
                                  size_t size,
                                  const void* args,
                                  unsigned int flags,
                                  const void* options,
                                  int* result)
{
  ++calls;
  if (mode == 3)
  {
    assert(strcmp(path, "/system/other.sprx") == 0);
    assert(size == sizeof(original_args) && args == &original_args);
    assert(flags == 7 && options == &original_args);
    *result = 77;
    return 42;
  }
  assert(strcmp(path, target) == 0 && flags == 0 && options == NULL);
  assert(size == sizeof(PS5SprxStartTrace) && args);
  const PS5SprxStartTrace* trace = args;
  assert(trace->version == 1 && trace->emit);
  if (mode == 1)
    return -7;
  *result = mode == 2 ? -9 : module_start(size, args);
  return 41;
}

static void unexpected_emit(int stage)
{
  (void)stage;
  assert(!"invalid trace arguments must not invoke a callback");
}

int main(void)
{
  PS5SprxStartTrace invalid = {.version = 1, .emit = unexpected_emit};
  assert(module_start(0, NULL) == -1);
  assert(module_start(sizeof(invalid), NULL) == -1);
  assert(module_start(sizeof(invalid) - 1, &invalid) == -1);
  invalid.version = 2;
  assert(module_start(sizeof(invalid), &invalid) == -1);
  invalid.version = 1;
  invalid.emit = NULL;
  assert(module_start(sizeof(invalid), &invalid) == -1);

  for (mode = 0; mode < 3; ++mode)
  {
    count = calls = 0;
    int result = 0;
    assert(__wrap_sceKernelLoadStartModule(target, 0, NULL, 0, NULL, &result) ==
           (mode == 1 ? -7 : 41));
    assert(calls == 1);
    assert(strcmp(messages[0], "[sprx-start-trace] host-ready\n") == 0);
    if (mode == 0)
    {
      assert(count == 4 && result == 0);
      assert(strcmp(messages[1], "[sprx-start-trace] initializer-enter\n") == 0);
      assert(strcmp(messages[2], "[sprx-start-trace] initializer-leave\n") == 0);
    }
    else
      assert(count == 2 && result == (mode == 1 ? 0 : -9));
    assert(strcmp(messages[count - 1], "[sprx-start-trace] public-api-return\n") == 0);
  }

  mode = 3;
  count = calls = 0;
  int result = 0;
  assert(__wrap_sceKernelLoadStartModule("/system/other.sprx", sizeof(original_args),
                                       &original_args, 7, &original_args, &result) == 42);
  assert(calls == 1 && count == 0 && result == 77);

  count = calls = 0;
  assert(__wrap_sceKernelLoadStartModule(target, 1, NULL, 0, NULL, &result) == -1);
  assert(__wrap_sceKernelLoadStartModule(target, 0, &original_args, 0, NULL, &result) == -1);
  assert(__wrap_sceKernelLoadStartModule(target, 0, NULL, 1, NULL, &result) == -1);
  assert(__wrap_sceKernelLoadStartModule(target, 0, NULL, 0, &original_args, &result) == -1);
  assert(__wrap_sceKernelLoadStartModule(target, 0, NULL, 0, NULL, NULL) == -1);
  assert(calls == 0 && count == 5);
  for (unsigned int i = 0; i < count; ++i)
    assert(strcmp(messages[i], "[sprx-start-trace] unsupported caller arguments\n") == 0);
  puts("PASS: actual diagnostic initializer, callback order, errors and unchanged non-target calls");
}
