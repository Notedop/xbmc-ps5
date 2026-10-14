#include "platform/ps5/elf/ElfLoader.h"

#include <cassert>
#include <cstring>
#include <dlfcn.h>
#include <elf.h>
#include <fstream>
#include <iterator>
#include <thread>
#include <vector>

static std::vector<int> events;
static unsigned host_registrations = 0;
extern "C" void loader_event(int value) { events.push_back(value); }
static int unsafe_atexit(void (*)(void*), void*, void*)
{
  ++host_registrations;
  return -1;
}
static void* resolve(const char* name, void*)
{
  if (std::strcmp(name, "__cxa_atexit") == 0)
    return reinterpret_cast<void*>(&unsafe_atexit);
  if (std::strcmp(name, "loader_event") == 0)
    return reinterpret_cast<void*>(&loader_event);
  return dlsym(RTLD_DEFAULT, name);
}

int main(int argc, char** argv)
{
  assert(argc == 3);
  std::ifstream file(argv[1], std::ios::binary);
  const std::vector<char> bytes{std::istreambuf_iterator<char>{file}, {}};
  assert(!bytes.empty());
  auto load = [&]() {
    char error[256]{};
    auto* image = ps5elf::load(bytes.data(), bytes.size(), resolve, nullptr, error, sizeof(error));
    assert(image && "load failed");
    return image;
  };
  for (int iteration = 0; iteration != 3; ++iteration)
  {
    auto* a = load();
    auto* b = load();
    ps5elf::run_init(a);
    ps5elf::run_init(a); // idempotent
    ps5elf::run_init(b);
    auto finalize_a = reinterpret_cast<void (*)()>(ps5elf::symbol(a, "finalize_now"));
    auto register_b = reinterpret_cast<int (*)()>(ps5elf::symbol(b, "register_late"));
    assert(finalize_a && register_b);
    std::vector<std::thread> workers;
    for (int worker = 0; worker != 4; ++worker)
      workers.emplace_back([&]() {
        for (int registration = 0; registration != 100; ++registration)
          assert(register_b() == 0);
      });
    for (auto& worker : workers)
      worker.join(); // quiesce before unloading
    events.clear();
    finalize_a();
    finalize_a(); // no double destruction
    assert((events == std::vector<int>{2, 1}));
    events.clear();
    ps5elf::unload(a);
    assert((events == std::vector<int>{4, 3}));
    events.clear();
    ps5elf::unload(b); // independent DSO; throw/catch in destructors needs live EH
    std::vector<int> expected{4, 3};
    expected.insert(expected.end(), 400, 5);
    expected.insert(expected.end(), {2, 1});
    assert(events == expected);
    events.clear();
    auto* uninitialized = load();
    ps5elf::unload(uninitialized);
    assert(events.empty());
  }
  assert(host_registrations == 0);
  auto damaged = bytes;
  const auto* header = reinterpret_cast<const Elf64_Ehdr*>(damaged.data());
  const auto* phdr = reinterpret_cast<const Elf64_Phdr*>(damaged.data() + header->e_phoff);
  bool changed = false;
  for (unsigned i = 0; i < header->e_phnum; ++i)
    if (phdr[i].p_type == PT_DYNAMIC)
    {
      auto* dynamic = reinterpret_cast<Elf64_Dyn*>(damaged.data() + phdr[i].p_offset);
      for (; dynamic->d_tag != DT_NULL; ++dynamic)
        if (dynamic->d_tag == DT_FINI_ARRAYSZ)
        {
          dynamic->d_un.d_val = ~uint64_t{7};
          changed = true;
        }
    }
  assert(changed);
  char error[256]{};
  assert(!ps5elf::load(damaged.data(), damaged.size(), resolve, nullptr, error, sizeof(error)));
  assert(std::strstr(error, "invalid finalizer"));
  std::ifstream throwing_file(argv[2], std::ios::binary);
  const std::vector<char> throwing_bytes{std::istreambuf_iterator<char>{throwing_file}, {}};
  auto* throwing_image = ps5elf::load(throwing_bytes.data(), throwing_bytes.size(),
                                    resolve, nullptr, error, sizeof(error));
  assert(throwing_image);
  events.clear();
  bool failed = false;
  try
  {
    ps5elf::run_init(throwing_image);
  }
  catch (...)
  {
    failed = true;
  }
  assert(failed && (events == std::vector<int>{6}));
  // Exception destruction has finished; only now can unload release its code.
  ps5elf::unload(throwing_image);
  assert((events == std::vector<int>{6, 4, 3, 2, 1}));
}
