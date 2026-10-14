/*
 * Stage-2 binary add-on for the PS5 in-process ELF loader.
 * Uses this checkout's dev-kit ABI, with no direct Kodi or graphics imports.
 */
#include <kodi/addon-instance/Screensaver.h>

#include <pthread.h>
#include <system_error>
#include <thread>

#ifndef PS5_TEST_BUILD_ID
#error Build this probe with tools/ps5/build-test-addon.sh
#endif

namespace
{
volatile int g_ctorRan = 0;
volatile int g_deletions = 0;

struct ConstructorProbe
{
  ConstructorProbe() { g_ctorRan = 0xC0FFEE; }
} g_constructorProbe;

struct Base
{
  virtual int Tag() const = 0;
  virtual ~Base() { ++g_deletions; }
};

struct Derived : Base
{
  int Tag() const override { return 0x5151; }
};

// Keep allocation, virtual dispatch and throw/catch real even in optimized builds.
__attribute__((noinline)) Base* Allocate() { return new Derived; }
__attribute__((noinline)) int Dispatch(const Base* object) { return object->Tag(); }
__attribute__((noinline)) void Release(Base* object) { delete object; }
__attribute__((noinline)) void Throw() { throw 0x5151; }

bool Check(const char* name, bool passed)
{
  kodi::Log(passed ? ADDON_LOG_INFO : ADDON_LOG_ERROR, "[test.binary.ps5] %s: %s",
            name, passed ? "PASS" : "FAIL");
  return passed;
}

struct ThreadProbe
{
  pthread_key_t key;
  int error = 0;
  bool values = false;
  bool destroyed = false;
};

void DestroyThreadValue(void* value)
{
  static_cast<ThreadProbe*>(value)->destroyed = true;
}

bool CheckThread()
{
  ThreadProbe probe{};
  int error = pthread_key_create(&probe.key, DestroyThreadValue);
  if (error != 0)
  {
    kodi::Log(ADDON_LOG_ERROR, "[test.binary.ps5] pthread_key_create error=%d", error);
    return false;
  }
  bool joined = false;
  try
  {
    std::thread worker([&probe]() {
      probe.error = pthread_setspecific(probe.key, &probe);
      if (probe.error != 0)
        return;
      probe.values = pthread_getspecific(probe.key) == &probe;
      probe.error = pthread_setspecific(probe.key, nullptr);
      if (probe.error != 0)
        return;
      probe.values = probe.values && pthread_getspecific(probe.key) == nullptr;
      probe.error = pthread_setspecific(probe.key, &probe);
    });
    worker.join();
    joined = true;
  }
  catch (const std::system_error& exception)
  {
    kodi::Log(ADDON_LOG_ERROR, "[test.binary.ps5] std::thread error: %s", exception.what());
  }
  error = pthread_key_delete(probe.key);
  if (probe.error != 0 || error != 0)
    kodi::Log(ADDON_LOG_ERROR, "[test.binary.ps5] TLS set error=%d delete error=%d",
              probe.error, error);
  return joined && probe.error == 0 && error == 0 && probe.values && probe.destroyed;
}

class Screensaver : public kodi::addon::CInstanceScreensaver
{
public:
  explicit Screensaver(const kodi::addon::IInstanceInfo& instance) : CInstanceScreensaver(instance)
  {
    kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] instance created; properties=%dx%d",
              Width(), Height());
  }

  ~Screensaver() override
  {
    kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] instance destroyed");
  }

  bool Start() override
  {
    kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] Start enter; build=%s", PS5_TEST_BUILD_ID);
    m_rendered = false;
    m_passed = Check("static constructor", g_ctorRan == 0xC0FFEE);
    m_passed = Check("Kodi properties callback", Width() > 0 && Height() > 0) && m_passed;

    const int before = g_deletions;
    Base* object = Allocate();
    const int tag = Dispatch(object);
    Release(object);
    m_passed = Check("heap and virtual dispatch", tag == 0x5151 && g_deletions == before + 1) &&
               m_passed;

    kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] throw/catch enter");
    bool caught = false;
    try
    {
      Throw();
    }
    catch (int value)
    {
      caught = value == 0x5151;
    }
    m_passed = Check("C++ throw/catch", caught) && m_passed;
    kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] thread/TSD enter");
    m_passed = Check("std::thread/TSD values and exit destructor", CheckThread()) && m_passed;
    kodi::Log(m_passed ? ADDON_LOG_INFO : ADDON_LOG_ERROR,
              "[test.binary.ps5] Start exit; success=%d", m_passed);
    return m_passed;
  }

  void Render() override
  {
    if (!m_rendered)
    {
      m_rendered = true;
      kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] first Render");
      if (m_passed)
        kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] STAGE 2 RUNTIME PASS; build=%s",
                  PS5_TEST_BUILD_ID);
    }
  }

  void Stop() override
  {
    kodi::Log(m_passed && m_rendered ? ADDON_LOG_INFO : ADDON_LOG_ERROR,
              "[test.binary.ps5] Stop; started=%d rendered=%d", m_passed, m_rendered);
    m_passed = false;
  }

private:
  bool m_passed = false;
  bool m_rendered = false;
};

class Addon : public kodi::addon::CAddonBase
{
public:
  ADDON_STATUS Create() override
  {
    kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] Create; build=%s", PS5_TEST_BUILD_ID);
    return Check("static constructor at Create", g_ctorRan == 0xC0FFEE)
               ? ADDON_STATUS_OK
               : ADDON_STATUS_PERMANENT_FAILURE;
  }

  ADDON_STATUS CreateInstance(const kodi::addon::IInstanceInfo& instance,
                              KODI_ADDON_INSTANCE_HDL& handle) override
  {
    if (!instance.IsType(ADDON_INSTANCE_SCREENSAVER))
    {
      kodi::Log(ADDON_LOG_ERROR, "[test.binary.ps5] FAIL: unexpected instance type");
      return ADDON_STATUS_UNKNOWN;
    }
    handle = new Screensaver(instance);
    return ADDON_STATUS_OK;
  }

  ~Addon() override { kodi::Log(ADDON_LOG_INFO, "[test.binary.ps5] add-on destroyed"); }
};
} // namespace

ADDONCREATOR(Addon)
