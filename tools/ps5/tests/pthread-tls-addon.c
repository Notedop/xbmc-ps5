#include <pthread.h>

__attribute__((noinline, disable_tail_calls))
int set_value(pthread_key_t key, const void* value)
{
  return pthread_setspecific(key, value);
}
