extern "C" void loader_event(int);
extern "C" int __cxa_atexit(void (*)(void*), void*, void*);
extern "C" void __cxa_finalize(void*);
extern "C"
{
__attribute__((visibility("hidden"))) void* __dso_handle = &__dso_handle;
}

struct Global
{
  int id;
  ~Global()
  {
    try
    {
      throw id;
    }
    catch (int value)
    {
      loader_event(value);
    }
  }
};
Global first{1};
Global second{2};

__attribute__((destructor(201))) static void fini_first() { loader_event(3); }
__attribute__((destructor(202))) static void fini_second() { loader_event(4); }

static void registered(void*) { loader_event(5); }
extern "C" int register_late()
{
  return __cxa_atexit(registered, nullptr, &__dso_handle);
}
extern "C" void finalize_now() { __cxa_finalize(&__dso_handle); }

#if defined(LOADER_THROWING_CTOR)
struct ConstructorException
{
  ~ConstructorException() { loader_event(6); }
};
struct Throwing
{
  Throwing() { throw ConstructorException{}; }
};
Throwing failing;
#endif
