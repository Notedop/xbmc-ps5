/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef PS5_SHARED_HEAP_H
#define PS5_SHARED_HEAP_H

#include <stddef.h>

#define PS5_SHARED_HEAP_API __attribute__((visibility("default")))

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Compile shared_heap.c ONCE into the process-lifetime core, never into clients.
 * Link both that core and every title/add-on/graphics/C++ client with:
 * --wrap=malloc --wrap=calloc --wrap=realloc --wrap=free
 * --wrap=posix_memalign --wrap=malloc_usable_size --wrap=aligned_alloc
 * --wrap=memalign --wrap=reallocf
 *
 * Export these __wrap_* names from the core; do not export plain malloc/free.
 * In the core link, --wrap resolves __real_* to native libc imports, not these
 * exports. Native libc's own internal allocations remain native-owned.
 * reallocarray is not a supported native libc export.
 *
 * Call initialization from core module_start before running constructors.
 * Returns 0, or -1 with errno and kernel diagnostics; failure is sticky after
 * the once initializer runs. PS5_SHARED_HEAP_MIB defaults to 2048 at compile time.
 * The core and successful backing mapping must outlive all clients/destructors.
 */
PS5_SHARED_HEAP_API int ps5_shared_heap_initialize(void);

/*
 * New allocations never fall back to native malloc. Zero-size allocations
 * request one byte; realloc(p, 0) frees p and returns NULL, including native p.
 * Allocation failures set errno=ENOMEM; invalid alignment sets EINVAL.
 * Initialization errors (including pthread_once's error) propagate to callers.
 * posix_memalign leaves errno and *address unchanged on failure, returning the
 * POSIX error. Foreign realloc/free/usable_size go to actual native libc.
 * Range ownership is valid only for live allocator-returned pointers; invalid,
 * interior and already-freed pointers are not supported.
 */
PS5_SHARED_HEAP_API void* __wrap_malloc(size_t size);
PS5_SHARED_HEAP_API void* __wrap_calloc(size_t count, size_t size);
PS5_SHARED_HEAP_API void* __wrap_realloc(void* address, size_t size);
PS5_SHARED_HEAP_API void __wrap_free(void* address);
PS5_SHARED_HEAP_API int __wrap_posix_memalign(void** address, size_t alignment, size_t size);
PS5_SHARED_HEAP_API size_t __wrap_malloc_usable_size(const void* address);
PS5_SHARED_HEAP_API void* __wrap_aligned_alloc(size_t alignment, size_t size);
PS5_SHARED_HEAP_API void* __wrap_memalign(size_t alignment, size_t size);
PS5_SHARED_HEAP_API void* __wrap_reallocf(void* address, size_t size);

#ifdef __cplusplus
}
#endif
#endif
