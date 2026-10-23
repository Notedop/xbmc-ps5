/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef PS5_SHARED_TLS_H
#define PS5_SHARED_TLS_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Clang/LLVM emulated-TLS control ABI; loc.index starts at zero. */
struct ps5_emutls_control
{
  uintptr_t size;
  uintptr_t align;
  union
  {
    uintptr_t index;
    void* address;
  } loc;
  void* template_value;
};

void* __emutls_get_address(struct ps5_emutls_control* control);
int __cxa_thread_atexit_impl(void (*destructor)(void*), void* object, void* dso);

/* Includes callbacks currently executing, not just queued callbacks. */
size_t ps5_tls_dso_pending(void* dso);
/* Return 0 or -1 with errno and a kernel diagnostic. Unknown tags are tracked.
 * begin_unload closes registration only when pending == 0; reopen also requires
 * pending == 0. The loader must separately quiesce calls and TLS first access:
 * compiler emutls controls contain no DSO identity.
 * Tags (including NULL) and their registry records have process lifetime.
 */
int ps5_tls_begin_unload(void* dso);
int ps5_tls_reopen_dso(void* dso);

/* Also usable for explicit main-thread cleanup. Destructors run before storage
 * is freed, including any destructors registered by callbacks. Do not call
 * pthread_exit/longjmp or throw out of a destructor. Reentrant cleanup is a no-op.
 * Deferred pthread cancellation is supported; asynchronous cancellation is not.
 */
void ps5_tls_thread_exit(void);

/* Link exactly one copy in the process-lifetime owner (title or shared core),
 * never in its clients or alongside SDK emutls/a separate destructor registry.
 * This API alone does not make arbitrary modules unloadable.
 * __emutls_register_common (GCC common TLS) is deliberately not implemented.
 */

#ifdef __cplusplus
}
#endif
#endif
