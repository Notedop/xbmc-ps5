#include <dlfcn.h>

extern "C" void loader_event(int);
extern "C" const char* parent_constructor_path;

namespace
{
void* finalizer_handle = nullptr;
struct Parent
{
  Parent()
  {
    if (parent_constructor_path)
    {
      void* handle = dlopen(parent_constructor_path, RTLD_NOW);
      if (handle)
      {
        loader_event(91);
        dlclose(handle);
      }
    }
  }
  ~Parent()
  {
    loader_event(90);
    if (finalizer_handle)
    {
      auto current = reinterpret_cast<int (*)()>(dlsym(finalizer_handle, "current"));
      if (current && current() == 0 && dlclose(finalizer_handle) == 0)
        loader_event(92);
    }
  }
} parent;
}

// Compile optimized: these become tail calls. Import routing must retain
// module identity even when there is no return address in the mapped image.
extern "C" void* helper_open(const char* path, int flags) { return dlopen(path, flags); }
extern "C" void* helper_symbol(void* handle, const char* name) { return dlsym(handle, name); }
extern "C" int helper_close(void* handle) { return dlclose(handle); }
extern "C" char* helper_error() { return dlerror(); }
extern "C" int owner_probe() { return 7; }
extern "C" void retain_for_finalizer(void* handle) { finalizer_handle = handle; }
