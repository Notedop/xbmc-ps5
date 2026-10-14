#include "platform/ps5/elf/ElfLoader.h"

#include <cassert>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#if defined(LOADER_TEST_NO_DIRECTORY_OPENAT)
std::atomic<int> directory_open_attempts{0};
extern "C" int __wrap_openat(int, const char*, int, ...)
{
  ++directory_open_attempts;
  errno = ENOSYS;
  return -1;
}
#endif

namespace
{
std::mutex events_mutex;
std::vector<int> events;
const char* constructor_path = nullptr;

extern "C" void loader_event(int value)
{
  std::lock_guard<std::mutex> lock(events_mutex);
  events.push_back(value);
}

void* resolve(const char* name, void* user)
{
  if (std::strcmp(name, "loader_event") == 0)
    return reinterpret_cast<void*>(&loader_event);
  if (std::strcmp(name, "parent_constructor_path") == 0)
    return &constructor_path;
  if (std::strcmp(name, "owner_probe") == 0)
    return ps5elf::symbol(*static_cast<ps5elf::Image**>(user), name);
  return dlsym(RTLD_DEFAULT, name);
}

struct Parent
{
  ps5elf::Image* image = nullptr;
  void* (*open)(const char*, int) = nullptr;
  void* (*lookup)(void*, const char*) = nullptr;
  int (*close)(void*) = nullptr;
  char* (*error)() = nullptr;

  Parent(const std::string& path, bool scoped = true)
  {
    std::ifstream file(path, std::ios::binary);
    const std::vector<char> bytes{std::istreambuf_iterator<char>{file}, {}};
    char message[256]{};
    image = ps5elf::load(bytes.data(), bytes.size(), resolve, &image, message, sizeof(message));
    assert(image);
    if (scoped)
    {
      const char* names[] = {"state-a.so", "state-b.so", "tls.so", "needed.so",
                            "unresolved.so", "invalid.so", "throwing.so"};
      assert(ps5elf::register_auxiliaries(image, path.c_str(), names, 7, message, sizeof(message)));
      assert(!ps5elf::register_auxiliaries(image, path.c_str(), names, 7, message, sizeof(message)));
    }
    ps5elf::run_init(image);
    open = reinterpret_cast<decltype(open)>(ps5elf::symbol(image, "helper_open"));
    lookup = reinterpret_cast<decltype(lookup)>(ps5elf::symbol(image, "helper_symbol"));
    close = reinterpret_cast<decltype(close)>(ps5elf::symbol(image, "helper_close"));
    error = reinterpret_cast<decltype(error)>(ps5elf::symbol(image, "helper_error"));
    assert(open && lookup && close && error);
  }

  void unload()
  {
    ps5elf::unload(image);
    image = nullptr;
  }
  ~Parent() { unload(); }
  void expect_error(const char* fragment)
  {
    const char* text = error();
    assert(text && std::strstr(text, fragment));
    assert(!error()); // consumptive, not a permanently latched failure
  }
};

template<typename Function>
Function function(Parent& parent, void* handle, const char* name)
{
  auto result = reinterpret_cast<Function>(parent.lookup(handle, name));
  assert(result);
  return result;
}
} // namespace

int main(int argc, char** argv)
{
  assert(argc == 2);
  {
    std::vector<std::string> helpers;
    char message[256]{};
    const auto parse = [&](const std::string& text, const char* primary = "main.so.1") {
      return ps5elf::parse_auxiliary_manifest(text.data(), text.size(), primary, helpers,
                                              message, sizeof(message));
    };
    assert(parse("# generated\nformat 1\nprimary main.so.1\nhelper liba.so\r\nhelper libb.so\n"));
    assert((helpers == std::vector<std::string>{"liba.so", "libb.so"}));
    assert(parse("format 1\nprimary main.so.1") && helpers.empty());
    for (const char* bad : {"", "primary main.so.1\nformat 1\n", "format 2\nprimary main.so.1\n",
                            "format 1\n", "format 1\nprimary other.so\n",
                            "format 1\nprimary main.so.1\nprimary main.so.1\n",
                            "format 1\nprimary main.so.1\nhelper ../liba.so\n",
                            "format 1\nprimary main.so.1\nhelper dir/liba.so\n",
                            "format 1\nprimary main.so.1\nhelper .hidden.so\n",
                            "format 1\nprimary main.so.1\nhelper liba.txt\n",
                            "format 1\nprimary main.so.1\nhelper main.so.1\n",
                            "format 1\nprimary main.so.1\nhelper liba.so\nhelper liba.so\n",
                            "format 1\nprimary main.so.1\nhelper  liba.so\n",
                            "format 1\nprimary main.so.1\nextra liba.so\n"})
    {
      assert(!parse(bad) && helpers.empty() && message[0]);
      message[0] = '\0';
    }
    std::string many = "format 1\nprimary main.so.1\n";
    for (int i = 0; i < 65; ++i)
      many += "helper lib" + std::to_string(i) + ".so\n";
    assert(!parse(many) && std::strstr(message, "too many"));
    const char embedded[] = "format 1\nprimary main.so.1\nhelper li\0b.so\n";
    assert(!parse(std::string(embedded, sizeof(embedded) - 1)));
  }
  const std::string directory = argv[1];
  const std::string primary = directory + "/parent.so";
  const std::string a_path = directory + "/state-a.so";
  const std::string b_path = directory + "/state-b.so";
  constructor_path = a_path.c_str();
  {
    Parent during_init(primary);
    assert((events == std::vector<int>{1, 91, 11}));
  }
  constructor_path = nullptr;
  events.clear();
  {
    Parent during_fini(primary);
    void* b = during_fini.open(b_path.c_str(), RTLD_NOW);
    assert(b);
    auto retain = reinterpret_cast<void (*)(void*)>(
        ps5elf::symbol(during_fini.image, "retain_for_finalizer"));
    assert(retain);
    retain(b);
    events.clear();
    during_fini.unload();
    assert((events == std::vector<int>{90, 12, 92}));
  }
  for (int iteration = 0; iteration < 3; ++iteration)
  {
    events.clear();
    Parent parent(primary);
    Parent other(primary);
    Parent unscoped(primary, false);
    assert(!unscoped.open(a_path.c_str(), RTLD_NOW));
    unscoped.expect_error("no active trusted");
    void* a = parent.open(a_path.c_str(), RTLD_LAZY);
    assert(a && parent.open(a_path.c_str(), RTLD_NOW) == a);
    assert(parent.open((directory + "//./state-a.so").c_str(), RTLD_LAZY) == a);
    assert(parent.close(a) == 0); // canonical aliases share the same reference count
    void* b = parent.open(b_path.c_str(), RTLD_NOW);
    void* other_a = other.open(a_path.c_str(), RTLD_NOW);
    assert(b && other_a && a != b && a != other_a);
    auto increment_a = function<int (*)()>(parent, a, "increment");
    auto increment_b = function<int (*)()>(parent, b, "increment");
    auto increment_other = function<int (*)()>(other, other_a, "increment");
    auto* state_a = static_cast<int*>(parent.lookup(a, "helper_state"));
    auto* state_b = static_cast<int*>(parent.lookup(b, "helper_state"));
    auto* state_other = static_cast<int*>(other.lookup(other_a, "helper_state"));
    auto** pointer_a = static_cast<int**>(parent.lookup(a, "helper_state_pointer"));
    auto** pointer_b = static_cast<int**>(parent.lookup(b, "helper_state_pointer"));
    assert(state_a && state_b && state_other && pointer_a && pointer_b);
    assert(state_a != state_b && state_a != state_other && state_b != state_other);
    // Defined-symbol GLOB_DAT and absolute pointer relocations must bind
    // locally, even though all three images export identical names.
    assert(*pointer_a == state_a && *pointer_b == state_b);
    assert(increment_a() == 1 && increment_a() == 2);
    assert(increment_b() == 1 && increment_other() == 1);
    assert(parent.close(a) == 0); // one reference remains
    assert(increment_a() == 3);
    assert(!other.lookup(a, "increment"));
    other.expect_error("foreign");
    assert(other.close(a) == -1);
    other.expect_error("foreign");
    assert(!parent.lookup(a, "missing"));
    parent.expect_error("symbol not found");
    assert(!parent.lookup(a, "hidden_symbol"));
    parent.expect_error("not publicly exported");
    assert(!parent.lookup(a, nullptr));
    parent.expect_error("symbol not found");
    assert(!parent.open(b_path.c_str(), RTLD_NOW | RTLD_GLOBAL));
    parent.expect_error("RTLD_LOCAL");
    assert(!parent.open(nullptr, RTLD_NOW));
    parent.expect_error("absolute path");
    assert(!parent.open("state-a.so", RTLD_NOW));
    parent.expect_error("absolute path");
    assert(!parent.open((directory + "/outside/state-a.so").c_str(), RTLD_NOW));
    parent.expect_error("allowlist");
    assert(!parent.open((directory + "/escape.so").c_str(), RTLD_NOW));
    parent.expect_error("allowlist");
    assert(!parent.open((directory + "/parent.so").c_str(), RTLD_NOW));
    parent.expect_error("allowlist");
    assert(!parent.open((directory + "/not-found.so").c_str(), RTLD_NOW));
    parent.expect_error("canonicalize");
    for (const auto& failure : std::vector<std::pair<const char*, const char*>>{
             {"tls.so", "PT_TLS"}, {"needed.so", "DT_NEEDED"},
             {"unresolved.so", "missing_strong_import"}, {"invalid.so", "image too small"},
             {"throwing.so", "constructor threw"}})
    {
      assert(!parent.open((directory + "/" + failure.first).c_str(), RTLD_NOW));
      parent.expect_error(failure.second);
    }
    auto nested_open = function<void* (*)(const char*)>(parent, a, "nested_open");
    auto nested_close = function<int (*)(void*)>(parent, a, "nested_close");
    assert(!nested_open(b_path.c_str()));
    parent.expect_error("dependency loading is unsupported");
    assert(nested_close(a) == -1); // never unmap the currently executing helper
    parent.expect_error("currently executing");
    assert(parent.close(a) == 0);
    assert(!parent.lookup(a, "current"));
    parent.expect_error("closed");
    assert(parent.close(a) == -1);
    parent.expect_error("closed");
    void* reloaded = parent.open(a_path.c_str(), RTLD_NOW);
    assert(reloaded && reloaded != a);
    assert(function<int (*)()>(parent, reloaded, "current")() == 0);
    assert(!parent.lookup(a, "current")); // old tokens cannot alias reloads
    parent.expect_error("closed");
    assert(parent.close(reloaded) == 0);

    // Native system handles are never interpreted as manually mapped images.
    void* native = dlopen(nullptr, RTLD_NOW);
    assert(native && parent.lookup(native, "malloc") == dlsym(native, "malloc"));
    assert(!parent.lookup(native, "missing_native_symbol"));
    parent.expect_error("missing_native_symbol");
    assert(parent.close(native) == 0);

    assert(!parent.lookup(b, "main_thread_missing"));
    std::vector<std::thread> workers;
    for (int worker = 0; worker < 6; ++worker)
      workers.emplace_back([&] {
        for (int i = 0; i < 30; ++i)
        {
          void* handle = parent.open(b_path.c_str(), RTLD_NOW);
          assert(handle == b);
          assert(parent.lookup(handle, "current"));
          assert(!parent.lookup(handle, "thread_missing"));
          parent.expect_error("symbol not found");
          assert(parent.close(handle) == 0);
          void* transient = parent.open(a_path.c_str(), RTLD_NOW);
          assert(transient);
          assert(function<int (*)()>(parent, transient, "current")() == 0);
          assert(parent.close(transient) == 0);
        }
      });
    // The worker failures must not leak into the main thread.
    for (auto& worker : workers)
      worker.join();
    parent.expect_error("main_thread_missing");
    assert(!parent.error());
    assert(parent.open(b_path.c_str(), RTLD_NOW) == b); // leave two references
    events.clear();
    parent.unload(); // force-close owned children after parent finalizers
    assert((events == std::vector<int>{90, 12}));
    assert(!other.lookup(b, "current"));
    other.expect_error("foreign"); // stale tokens never fall through to libc
    assert(increment_other() == 2); // no cross-parent sharing
    events.clear();
    other.unload();
    assert((events == std::vector<int>{90, 11}));
  }
#if defined(LOADER_TEST_NO_DIRECTORY_OPENAT)
  assert(directory_open_attempts > 0);
  {
    const std::string pinned = directory + "/pinned";
    const std::string moved = directory + "/pinned-original";
    const std::string replacement = directory + "/replacement";
    Parent parent(pinned + "/parent.so");
    assert(std::rename(pinned.c_str(), moved.c_str()) == 0);
    assert(std::rename(replacement.c_str(), pinned.c_str()) == 0);
    assert(!parent.open((pinned + "/state-a.so").c_str(), RTLD_NOW));
    parent.expect_error("directory changed before open");
    assert(std::rename(pinned.c_str(), replacement.c_str()) == 0);
    assert(std::rename(moved.c_str(), pinned.c_str()) == 0);
    void* handle = parent.open((pinned + "/state-a.so").c_str(), RTLD_NOW);
    assert(handle && parent.close(handle) == 0);
  }
#endif
}
