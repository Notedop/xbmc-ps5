#include "platform/ps5/elf/ElfLoader.h"

#include <atomic>
#include <cassert>
#include <cerrno>
#include <cstring>
#include <dlfcn.h>
#include <fstream>
#include <iterator>
#include <pthread.h>
#include <thread>
#include <vector>

extern "C" int __wrap_pthread_setspecific(pthread_key_t, const void*);
static std::atomic<unsigned> unregistered_callers{0};
static std::atomic<unsigned> destructors{0};
static int forced_error = 0;

extern "C" __attribute__((noinline)) int __real_pthread_setspecific(pthread_key_t key,
                                                                  const void* value)
{
  Dl_info caller{};
  Dl_info wrapper{};
  assert(dladdr(reinterpret_cast<void*>(&__wrap_pthread_setspecific), &wrapper));
  if (!dladdr(__builtin_return_address(0), &caller) || caller.dli_fbase != wrapper.dli_fbase)
  {
    ++unregistered_callers;
    return EFAULT;
  }
  if (forced_error)
    return forced_error;
  return pthread_setspecific(key, value);
}

static void destroy_value(void* value)
{
  assert(value);
  ++destructors;
}

static void* resolve(const char* name, void* wrapped)
{
  if (std::strcmp(name, "pthread_setspecific") == 0)
    return wrapped ? reinterpret_cast<void*>(&__wrap_pthread_setspecific)
                   : reinterpret_cast<void*>(&__real_pthread_setspecific);
  return dlsym(RTLD_DEFAULT, name);
}

int main(int argc, char** argv)
{
  assert(argc == 2);
  std::ifstream file(argv[1], std::ios::binary);
  const std::vector<char> bytes{std::istreambuf_iterator<char>{file}, {}};
  assert(!bytes.empty());
  for (int iteration = 0; iteration != 3; ++iteration)
  {
    for (bool wrapped : {false, true})
    {
      char error[256]{};
      auto* image = ps5elf::load(bytes.data(), bytes.size(), resolve,
                                wrapped ? &forced_error : nullptr, error, sizeof(error));
      assert(image);
      ps5elf::run_init(image);
      auto set_value = reinterpret_cast<int (*)(pthread_key_t, const void*)>(
          ps5elf::symbol(image, "set_value"));
      assert(set_value);
      pthread_key_t key;
      assert(pthread_key_create(&key, destroy_value) == 0);
      int value = 42;
      forced_error = 0;
      std::thread worker([&]() {
        assert(pthread_getspecific(key) == nullptr);
        assert(set_value(key, &value) == (wrapped ? 0 : EFAULT));
        assert(pthread_getspecific(key) == (wrapped ? &value : nullptr));
        if (wrapped)
        {
          assert(set_value(key, nullptr) == 0);
          assert(pthread_getspecific(key) == nullptr);
          assert(set_value(key, &value) == 0);
        }
      });
      worker.join();
      assert(pthread_getspecific(key) == nullptr);
      if (wrapped)
      {
        forced_error = ERANGE;
        std::thread failing_worker([&]() {
          assert(set_value(key, &value) == ERANGE);
          assert(pthread_getspecific(key) == nullptr);
        });
        failing_worker.join();
        forced_error = 0;
      }
      assert(pthread_key_delete(key) == 0);
      ps5elf::unload(image);
    }
  }
  assert(unregistered_callers == 3);
  assert(destructors == 3);
}
