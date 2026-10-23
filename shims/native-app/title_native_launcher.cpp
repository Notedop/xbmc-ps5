/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <cstddef>
#include "title_native_startup.h"

extern "C"
{
int sceKernelLoadStartModule(const char*, size_t, const void*, unsigned int, const void*, int*);
void sceKernelDebugOutText(int, const char*);
int snprintf(char*, size_t, const char*, ...);
int atexit(void (*)());
[[noreturn]] void _exit(int);
}

namespace
{
int (*finalizeApplication)();

template<typename... Args>
void trace(const char* format, Args... args) noexcept
{
  char message[256];
  const int length = snprintf(message, sizeof(message), format, args...);
  if (length < 0 || static_cast<size_t>(length) >= sizeof(message))
  {
    sceKernelDebugOutText(0, "[kodi-launcher] diagnostic formatting failed\n");
    _exit(1);
  }
  sceKernelDebugOutText(0, message);
}

int load(const char* path, size_t size = 0, const void* arguments = nullptr) noexcept
{
  trace("[kodi-launcher] load-start %s\n", path);
  int result = -1;
  const int handle = sceKernelLoadStartModule(path, size, arguments, 0, nullptr, &result);
  trace("[kodi-launcher] %s handle=0x%x initializer=0x%x\n", path,
        static_cast<unsigned int>(handle), static_cast<unsigned int>(result));
  if (handle < 0 || result != 0)
    _exit(1);
  return handle;
}

void finalize() noexcept
{
  const int result = finalizeApplication();
  if (result != 0)
  {
    trace("[kodi-launcher] application finalization failed result=0x%x\n",
          static_cast<unsigned int>(result));
    _exit(1);
  }
}
}

int main(int argc, char** argv, char** envp)
{
  load("/app0/sce_module/libkodi-runtime.sprx");
  load("/app0/sce_module/libkodi-gl.sprx");
  KodiNativeApplication application{};
  const KodiNativeStartup startup{
      KODI_NATIVE_STARTUP_ABI, sizeof(KodiNativeStartup), &application};
  load("/app0/sce_module/libkodi-app.sprx", sizeof(startup), &startup);
  if (application.run == nullptr || application.finalize == nullptr)
  {
    sceKernelDebugOutText(0, "[kodi-launcher] application startup interface missing\n");
    _exit(1);
  }
  finalizeApplication = application.finalize;
  if (atexit(finalize) != 0)
  {
    sceKernelDebugOutText(0, "[kodi-launcher] cannot register application finalizer\n");
    _exit(1);
  }
  sceKernelDebugOutText(0, "[kodi-launcher] entering native Kodi application\n");
  application.run(argc, argv, envp);
  sceKernelDebugOutText(0, "[kodi-launcher] application entry unexpectedly returned\n");
  _exit(1);
}
