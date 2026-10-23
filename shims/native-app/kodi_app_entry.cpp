/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stddef.h>
#include <stdint.h>
#include "title_native_startup.h"

extern int application_main(int, char**, char**) asm("main");
extern "C" [[noreturn]] void __wrap_exit(int);
extern "C" int ps5_kodi_crt_finalize(size_t, const void*) noexcept;
extern "C" int ps5_kodi_crt_initialize(size_t, const void*) noexcept;
extern "C" void sceKernelDebugOutText(int, const char*);

extern "C"
{
__attribute__((visibility("hidden"), used))
extern const unsigned char __ps5_native_process_lifetime_v1[17] = "PS5-NATIVE-LIFE\1";
}

#ifdef __PROSPERO__
asm(".hidden main\n.hidden module_start\n.hidden module_stop\n"
    ".hidden ps5_kodi_crt_initialize\n.hidden ps5_kodi_crt_finalize\n"
    ".hidden __ps5_native_process_lifetime_v1\n");
#endif

#ifdef PS5_PRIVATE_LEGACY_LIFECYCLE
extern "C"
{
extern void (*__ps5_legacy_init_start[])();
extern void (*__ps5_legacy_init_end[])();
extern void (*__ps5_legacy_fini_start[])();
extern void (*__ps5_legacy_fini_end[])();
}

#ifdef __PROSPERO__
asm(".hidden __ps5_legacy_init_start\n.hidden __ps5_legacy_init_end\n"
    ".hidden __ps5_legacy_fini_start\n.hidden __ps5_legacy_fini_end\n");
#endif

namespace
{
using Initializer = void (*)();

bool isSentinel(Initializer entry)
{
  const auto value = reinterpret_cast<uintptr_t>(entry);
  return value == 0 || value == UINTPTR_MAX;
}

void initializeLegacy()
{
  for (auto entry = __ps5_legacy_init_end; entry != __ps5_legacy_init_start;)
  {
    auto callback = *--entry;
    if (!isSentinel(callback))
      callback();
  }
}

void finalizeLegacy()
{
  for (auto entry = __ps5_legacy_fini_start; entry != __ps5_legacy_fini_end; ++entry)
    if (!isSentinel(*entry))
      (*entry)();
}

// Legacy _init/_fini work surrounds ordinary array callbacks. The builder
// validates every slot and permits sentinels only at the two ABI boundaries.
__attribute__((section(".init_array.00000"), used))
Initializer const legacyInitializer = initializeLegacy;
__attribute__((section(".fini_array.00001"), used))
Initializer const legacyFinalizer = finalizeLegacy;
}
#endif

extern "C" [[noreturn]] void kodi_native_run(int argc, char** argv, char** envp)
{
  __wrap_exit(application_main(argc, argv, envp));
}

extern "C" int kodi_native_finalize() noexcept
{
  return ps5_kodi_crt_finalize(0, nullptr);
}

extern "C" __attribute__((visibility("hidden")))
int module_start(size_t size, const void* arguments) noexcept
{
  if (size != sizeof(KodiNativeStartup) || arguments == nullptr)
  {
    sceKernelDebugOutText(0, "[kodi-app] invalid startup argument size or address\n");
    return -1;
  }
  const auto* startup = static_cast<const KodiNativeStartup*>(arguments);
  if (startup->version != KODI_NATIVE_STARTUP_ABI || startup->size != sizeof(*startup) ||
      startup->application == nullptr)
  {
    sceKernelDebugOutText(0, "[kodi-app] incompatible startup interface\n");
    return -1;
  }
  *startup->application = {};
  const int result = ps5_kodi_crt_initialize(0, nullptr);
  if (result != 0)
  {
    sceKernelDebugOutText(0, "[kodi-app] native CRT initialization failed\n");
    return result;
  }
  *startup->application = {kodi_native_run, kodi_native_finalize};
  sceKernelDebugOutText(0, "[kodi-app] startup interface ready\n");
  return 0;
}

extern "C" __attribute__((visibility("hidden")))
int module_stop(size_t, const void*) noexcept
{
  return -1;
}
