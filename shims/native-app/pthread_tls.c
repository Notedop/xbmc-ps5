/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include <pthread.h>

int __real_pthread_setspecific(pthread_key_t key, const void* value);

/* libkernel's first TSD allocation inspects its caller's module. Manually
 * mapped add-ons are not system modules, so keep a real title return address.
 * noinline alone does not prevent a tail jump back to the add-on's caller. */
__attribute__((noinline, disable_tail_calls))
int __wrap_pthread_setspecific(pthread_key_t key, const void* value)
{
  return __real_pthread_setspecific(key, value);
}
