/*
 *  Stack policy for threads created with default attributes.
 *
 *  Kodi (std::thread), FFmpeg and libc++ create threads with default
 *  attributes, and a title's default pthread stack is far smaller than on a
 *  desktop OS - the first thumbnail encode overflowed it. Every
 *  pthread_create() linked into the title is routed here by --wrap.
 *  Explicit caller attributes, including supplied stack memory, are untouched.
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */
#include <pthread.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

/* 4 MiB has worked for software decoding; 1 MiB previously overflowed.
 * This is an application default, not a firmware requirement. */
#define PS5_DEFAULT_THREAD_STACK (4u * 1024u * 1024u)

int __real_pthread_create(pthread_t* thread, const pthread_attr_t* attr,
                          void* (*start)(void*), void* arg);
int sceKernelDebugOutText(int channel, const char* text);
int sceKernelAvailableFlexibleMemorySize(size_t* size);

/* TEMPORARY DIAGNOSTICS: identify allocation failures without lowering stacks. */
static void log_failure(const char* stage, int result, size_t size, int stack_rc,
                        int explicit_attr, void* (*start)(void*))
{
  size_t available = 0;
  int memory_rc = sceKernelAvailableFlexibleMemorySize(&available);
  char msg[448];
  snprintf(msg, sizeof(msg),
           "[kodi-ps5] DIAGNOSTIC thread-create: stage=%s pthread_rc=%d "
           "attempted_stack_bytes=%zu stack_attr_rc=%d explicit_attr=%d "
           "creator_thread=%p entry=%p flexible_memory_rc=%d flexible_memory_valid=%d "
           "flexible_memory_bytes=%zu\n",
           stage, result, size, stack_rc, explicit_attr, (void*)(uintptr_t)pthread_self(),
           (void*)start, memory_rc, memory_rc == 0, available);
  sceKernelDebugOutText(0, msg);
}

int __wrap_pthread_create(pthread_t* thread, const pthread_attr_t* attr,
                          void* (*start)(void*), void* arg)
{
  const int saved_errno = errno;
  pthread_attr_t local;
  const pthread_attr_t* use = attr;
  int own = 0;
  size_t size = 0;
  int result = 0;
  const char* stage = "attr-init";

  if (!attr)
  {
    result = pthread_attr_init(&local);
    if (result != 0)
      goto attr_failure;
    own = 1;
    use = &local;
    stage = "attr-getstacksize";
    result = pthread_attr_getstacksize(&local, &size);
    if (result != 0)
      goto attr_failure;
    if (size < PS5_DEFAULT_THREAD_STACK)
    {
      size = PS5_DEFAULT_THREAD_STACK;
      stage = "attr-setstacksize";
      result = pthread_attr_setstacksize(&local, size);
      if (result != 0)
        goto attr_failure;
    }
  }

  result = __real_pthread_create(thread, use, start, arg);
  if (result != 0)
  {
    int stack_rc = attr ? pthread_attr_getstacksize(attr, &size) : 0;
    log_failure("pthread_create", result, size, stack_rc, attr != NULL, start);
  }
  goto cleanup;

attr_failure:
  log_failure(stage, result, size, result, 0, start);

cleanup:
  if (own)
  {
    int destroy_rc = pthread_attr_destroy(&local);
    if (destroy_rc != 0)
      log_failure("attr-destroy", destroy_rc, size, 0, 0, start);
  }
  errno = saved_errno;
  return result;
}
