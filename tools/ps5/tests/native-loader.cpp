/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "cores/DllLoader/DllLoaderContainer.h"
#include "filesystem/SpecialProtocol.h"
#include "platform/ps5/elf/PS5AddonLoader.h"

#include <cassert>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <sys/stat.h>

namespace
{
int loads;
int lookups;
int unloads;
int loadResult = 42;
int startResult;
int lookupResult;
int unloadResult;
int stopResult;
bool nullSymbol;
bool denyChmod;
int entry;
std::string loadedPath;
}

extern "C" int __real_chmod(const char*, mode_t);
extern "C" int __wrap_chmod(const char* path, mode_t mode)
{
  if (denyChmod)
  {
    errno = EACCES;
    return -1;
  }
  return __real_chmod(path, mode);
}

extern "C" int sceKernelLoadStartModule(
    const char* path, size_t size, const void* args, unsigned int flags, const void* options, int* result)
{
  assert(size == 0 && !args && flags == 0 && !options);
  ++loads;
  loadedPath = path;
  struct stat status{};
  assert(stat(path, &status) == 0);
  if ((status.st_mode & 0111) != 0111)
    return static_cast<int>(0x8002000dU);
  *result = startResult;
  return loadResult;
}

extern "C" int sceKernelDlsym(int handle, const char* name, void** result)
{
  assert(handle == 42 && std::string(name) == "ADDON_Create");
  ++lookups;
  *result = nullSymbol ? nullptr : &entry;
  return lookupResult;
}

extern "C" int sceKernelStopUnloadModule(
    int handle, size_t size, const void* args, unsigned int flags, const void* options, int* result)
{
  assert(handle == 42 && size == 0 && !args && flags == 0 && !options);
  ++unloads;
  *result = stopResult;
  return unloadResult;
}

int main(int argc, char** argv)
{
  if (argc == 2)
  {
    CPS5AddonLoader module(argv[1]);
    assert(module.Load());
    return 0;
  }

  std::string temporary =
      (std::filesystem::temp_directory_path() / "ps5-native-loader-XXXXXX").string();
  assert(mkdtemp(temporary.data()));
  CSpecialProtocol::root = temporary;
  const auto path = [&](const char* name) { return temporary + "/" + name; };
  for (const char* name : {"addon.sprx", "fail.sprx", "start-fail.sprx", "shared.sprx",
                           "retained.sprx", "start-retained.sprx", "stop-refused.sprx",
                           "denied.sprx", "readonly.sprx", "restricted.sprx"})
  {
    std::ofstream(path(name), std::ios::binary).write("\x54\x14\xf5\xee", 4);
    assert(chmod(path(name).c_str(), 0644) == 0);
  }
  {
    CPS5AddonLoader invalid("/app0/addon.so");
    assert(!invalid.Load() && loads == 0);
  }
  {
    CPS5AddonLoader invalid("addon.sprx");
    assert(!invalid.Load() && loads == 0);
  }
  {
    CPS5AddonLoader module("special://xbmc/addon.sprx");
    assert(module.Load() && loadedPath == path("addon.sprx"));
    struct stat status{};
    assert(stat(loadedPath.c_str(), &status) == 0 && (status.st_mode & 0777) == 0755);
    assert(module.Load() && loads == 1);
    void* symbol = nullptr;
    assert(module.ResolveExport("ADDON_Create", &symbol) && symbol == &entry);
    lookupResult = -5;
    assert(!module.ResolveExport("ADDON_Create", &symbol) && !symbol);
    lookupResult = 0;
    nullSymbol = true;
    assert(!module.ResolveExport("ADDON_Create", &symbol) && !symbol);
    nullSymbol = false;
    module.Unload();
    module.Unload();
    assert(unloads == 1 && !module.GetHModule());
    assert(module.Load() && loads == 2);
  }
  assert(unloads == 2);

  loadResult = -8;
  assert(!DllLoaderContainer::LoadModule(path("fail.sprx").c_str()));
  assert(!DllLoaderContainer::GetModule(path("fail.sprx").c_str()));
  loadResult = 42;
  startResult = -1;
  assert(!DllLoaderContainer::LoadModule(path("start-fail.sprx").c_str()));
  assert(!DllLoaderContainer::GetModule(path("start-fail.sprx").c_str()));
  startResult = 0;

  LibraryLoader* first = DllLoaderContainer::LoadModule(path("shared.sprx").c_str());
  const int beforeShared = loads;
  LibraryLoader* second = DllLoaderContainer::LoadModule(path("shared.sprx").c_str());
  assert(first && second == first && loads == beforeShared);
  const int beforeRelease = unloads;
  DllLoaderContainer::ReleaseModule(first);
  assert(unloads == beforeRelease);
  DllLoaderContainer::ReleaseModule(second);
  assert(!second && unloads == beforeRelease + 1);
  assert(!DllLoaderContainer::GetModule(path("shared.sprx").c_str()));
  first = DllLoaderContainer::LoadModule(path("shared.sprx").c_str());
  assert(first && loads == beforeShared + 1);
  DllLoaderContainer::ReleaseModule(first);

  first = DllLoaderContainer::LoadModule(path("retained.sprx").c_str());
  assert(first);
  unloadResult = -16;
  const int beforeFailure = unloads;
  DllLoaderContainer::ReleaseModule(first);
  assert(!first && unloads == beforeFailure + 1);
  first = DllLoaderContainer::GetModule(path("retained.sprx").c_str());
  assert(first && first->GetHModule());
  void* symbol = &entry;
  assert(!first->ResolveExport("ADDON_Create", &symbol) && !symbol);
  first->Unload();
  assert(unloads == beforeFailure + 1);
  delete first; // Mock module has no real mapping; production retains this object until exit.
  assert(!DllLoaderContainer::GetModule(path("retained.sprx").c_str()));

  startResult = -1;
  assert(!DllLoaderContainer::LoadModule(path("start-retained.sprx").c_str()));
  first = DllLoaderContainer::GetModule(path("start-retained.sprx").c_str());
  assert(first && first->GetHModule());
  delete first;
  startResult = 0;
  unloadResult = 0;
  stopResult = -1;
  first = DllLoaderContainer::LoadModule(path("stop-refused.sprx").c_str());
  assert(first);
  DllLoaderContainer::ReleaseModule(first);
  assert(!first);
  first = DllLoaderContainer::GetModule(path("stop-refused.sprx").c_str());
  assert(first && first->GetHModule());
  assert(!first->ResolveExport("ADDON_Create", &symbol));
  delete first;
  stopResult = 0;

  const int beforePermissions = loads;
  {
    CPS5AddonLoader missing(path("missing.sprx"));
    assert(!missing.Load() && loads == beforePermissions);
    std::filesystem::create_directory(path("directory.sprx"));
    CPS5AddonLoader directory(path("directory.sprx"));
    assert(!directory.Load() && loads == beforePermissions);
    CPS5AddonLoader denied(path("denied.sprx"));
    denyChmod = true;
    assert(!denied.Load() && loads == beforePermissions);
    denyChmod = false;
    assert(denied.Load() && loads == beforePermissions + 1);
  }
  assert(chmod(path("readonly.sprx").c_str(), 0555) == 0);
  {
    denyChmod = true;
    CPS5AddonLoader readonly(path("readonly.sprx"));
    assert(readonly.Load());
    denyChmod = false;
    struct stat status{};
    assert(stat(path("readonly.sprx").c_str(), &status) == 0 &&
           (status.st_mode & 0777) == 0555);
  }
  assert(chmod(path("restricted.sprx").c_str(), 0640) == 0);
  {
    CPS5AddonLoader restricted(path("restricted.sprx"));
    assert(restricted.Load());
    struct stat status{};
    assert(stat(path("restricted.sprx").c_str(), &status) == 0 &&
           (status.st_mode & 0777) == 0751);
  }
  std::filesystem::remove_all(temporary);
  std::puts("PASS: native API errors, translated paths, shared ownership, unload/reload, quarantine");
  std::puts("PASS: executable mode repair, missing/non-regular files, chmod errors and readonly modules");
}
