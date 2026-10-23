/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stddef.h>
#include <stdint.h>

#ifdef __PROSPERO__
// The SIE frontend does not implement ELF visibility attributes.
asm(".hidden __ps5_init_array_start\n"
    ".hidden __ps5_init_array_end\n"
    ".hidden __ps5_fini_array_start\n"
    ".hidden __ps5_fini_array_end\n"
    ".hidden __ps5_eh_frame_start\n"
    ".hidden __dso_handle\n"
    ".hidden __ps5_native_lifecycle_v1\n"
    ".hidden __ps5_finalize_globals\n"
    ".hidden module_start\n"
    ".hidden module_stop\n");
#endif

using Initializer = void (*)(void);

extern "C"
{
__attribute__((visibility("hidden"))) extern Initializer __ps5_init_array_start[];
__attribute__((visibility("hidden"))) extern Initializer __ps5_init_array_end[];
__attribute__((visibility("hidden"))) extern Initializer __ps5_fini_array_start[];
__attribute__((visibility("hidden"))) extern Initializer __ps5_fini_array_end[];
__attribute__((visibility("hidden"))) extern const unsigned char __ps5_eh_frame_start[];

void __cxa_finalize(void*);
void __unw_add_dynamic_eh_frame_section(uintptr_t);
void __unw_remove_dynamic_eh_frame_section(uintptr_t);

#ifdef PS5_SHARED_RUNTIME_CORE
int ps5_shared_heap_initialize(void);
#endif

__attribute__((visibility("hidden"))) void* __dso_handle = &__dso_handle;
__attribute__((visibility("hidden"), used))
extern const unsigned char __ps5_native_lifecycle_v1[17] = "PS5-NATIVE-CRT\0\1";

__attribute__((visibility("hidden"))) void __ps5_finalize_globals()
{
  __cxa_finalize(__dso_handle);
}
}

namespace
{
enum class State
{
  Stopped,
  Starting,
  Started,
  Stopping,
  Failed
};

State state = State::Stopped;

// Priority zero is reserved for CRT cleanup, after user fini-array entries.
__attribute__((section(".fini_array.00000"), used))
Initializer const finalizeGlobals = __ps5_finalize_globals;
}

extern "C" __attribute__((visibility("hidden")))
int module_start(size_t, const void*) noexcept
{
  if (state != State::Stopped)
    return -1;
  state = State::Starting;
#ifdef PS5_SHARED_RUNTIME_CORE
  if (ps5_shared_heap_initialize() != 0)
  {
    state = State::Stopped;
    return -1;
  }
#endif
  const auto frames = reinterpret_cast<uintptr_t>(__ps5_eh_frame_start);
  __unw_add_dynamic_eh_frame_section(frames);
  try
  {
    for (auto entry = __ps5_init_array_start; entry != __ps5_init_array_end; ++entry)
      (*entry)();
    state = State::Started;
    return 0;
  }
  catch (...)
  {
    // No C++ exception may escape into the native loader.
    __cxa_finalize(__dso_handle);
  }
  __unw_remove_dynamic_eh_frame_section(frames);
  state = State::Stopped;
  return -1;
}

extern "C" __attribute__((visibility("hidden")))
int module_stop(size_t, const void*) noexcept
{
  if (state == State::Stopped)
    return 0;
  if (state != State::Started)
    return -1;
  state = State::Stopping;
  try
  {
    for (auto entry = __ps5_fini_array_end; entry != __ps5_fini_array_start;)
      (*--entry)();
  }
  catch (...)
  {
    state = State::Failed;
    return -1;
  }
  __unw_remove_dynamic_eh_frame_section(reinterpret_cast<uintptr_t>(__ps5_eh_frame_start));
  state = State::Stopped;
  return 0;
}
