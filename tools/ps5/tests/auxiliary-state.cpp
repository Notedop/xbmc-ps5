#include <dlfcn.h>

#ifndef HELPER_ID
#define HELPER_ID 1
#endif

extern "C" void loader_event(int);
extern "C" int owner_probe();

extern "C"
{
int helper_state = 0;
int* helper_state_pointer = &helper_state;
}

namespace
{
struct Lifetime
{
  Lifetime() { loader_event(HELPER_ID); }
  ~Lifetime()
  {
    // The helper needs its own registered unwind info, and the parent's
    // callback must remain mapped during owned-child teardown.
    try
    {
      throw HELPER_ID;
    }
    catch (int value)
    {
      if (owner_probe() == 7)
        loader_event(value + 10);
    }
  }
} lifetime;
}

extern "C" int increment() { return ++helper_state; }
extern "C" int current() { return *helper_state_pointer; }
extern "C" void* nested_open(const char* path) { return dlopen(path, RTLD_NOW); }
extern "C" int nested_close(void* handle) { return dlclose(handle); }
extern "C" __attribute__((visibility("hidden"))) int hidden_symbol() { return -1; }

#ifdef HELPER_TLS
thread_local int tls_state;
extern "C" int tls_increment() { return ++tls_state; }
#endif
#ifdef HELPER_UNRESOLVED
extern "C" int missing_strong_import();
extern "C" int unresolved() { return missing_strong_import(); }
#endif
