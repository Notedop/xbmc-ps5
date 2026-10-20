/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int once_error, key_error, set_error, malloc_error;
static unsigned once_calls, key_calls, get_calls, set_calls, allocations, releases, logs;
static int key_live;
static char message[160];

static int test_once(pthread_once_t* once, void (*init)(void))
{
  ++once_calls;
  return once_error ? once_error : pthread_once(once, init);
}

static int test_key_create(pthread_key_t* key, void (*dtor)(void*))
{
  ++key_calls;
  if (key_error)
    return key_error;
  int result = pthread_key_create(key, dtor);
  key_live = result == 0;
  return result;
}

static void* test_getspecific(pthread_key_t key)
{
  assert(key_live);
  ++get_calls;
  return pthread_getspecific(key);
}

static int test_setspecific(pthread_key_t key, const void* value)
{
  assert(key_live);
  ++set_calls;
  return set_error ? set_error : pthread_setspecific(key, value);
}

static void* test_malloc(size_t size)
{
  if (malloc_error)
    return NULL;
  void* node = malloc(size);
  assert(node);
  ++allocations;
  return node;
}

static void test_free(void* node)
{
  assert(node);
  ++releases;
  free(node);
}

#define pthread_once test_once
#define pthread_key_create test_key_create
#define pthread_getspecific test_getspecific
#define pthread_setspecific test_setspecific
#define malloc test_malloc
#define free test_free
/* Do not replace the host C++ runtime's own registration hook. */
#define __cxa_thread_atexit_impl register_dtor
#include "../../../shims/native-app/weak_shims.c"
#undef pthread_once
#undef pthread_key_create
#undef pthread_getspecific
#undef pthread_setspecific
#undef malloc
#undef free
#undef __cxa_thread_atexit_impl

int sceKernelDebugOutText(int channel, const char* text)
{
  assert(channel == 0);
  assert(strlen(text) < sizeof(message));
  strcpy(message, text);
  ++logs;
  return -1; /* Logging failure must not change registration's failure result. */
}

static int order[8];
static unsigned destroyed;
static int rearm;
static int values[] = {1, 2, 3};

static void destroy(void* obj)
{
  assert(destroyed < sizeof(order) / sizeof(*order));
  order[destroyed++] = *(int*)obj;
  if (rearm && obj == &values[1])
  {
    /* pthread clears the key before invoking its destructor. */
    assert(pthread_getspecific(g_tls_dtor_key) == NULL);
    assert(register_dtor(destroy, &values[2], NULL) == 0);
  }
}

static void reset(void)
{
  assert(allocations == releases);
  if (key_live)
  {
    assert(pthread_getspecific(g_tls_dtor_key) == NULL);
    assert(pthread_key_delete(g_tls_dtor_key) == 0);
  }
  /* Reset only between isolated test scenarios, never in the implementation. */
  pthread_once_t initial = PTHREAD_ONCE_INIT;
  g_tls_dtor_once = initial;
  g_tls_dtor_key_result = -1;
  g_tls_dtor_reported = 0;
  once_error = key_error = set_error = malloc_error = key_live = rearm = 0;
  once_calls = key_calls = get_calls = set_calls = allocations = releases = logs = 0;
  destroyed = 0;
  message[0] = '\0';
}

static void drain(void)
{
  void* head = pthread_getspecific(g_tls_dtor_key);
  assert(pthread_setspecific(g_tls_dtor_key, NULL) == 0);
  ps5_run_tls_dtors(head);
}

static void* worker(void* unused)
{
  (void)unused;
  assert(register_dtor(destroy, &values[0], NULL) == 0);
  assert(register_dtor(destroy, &values[1], NULL) == 0);
  assert(destroyed == 0);
  return NULL;
}

/* Stands in for emutls: an earlier key whose destructor frees TLS storage. */
static pthread_key_t storage_key;
static int storage_freed;

static void free_storage(void* value)
{
  (void)value;
  storage_freed = 1;
}

static void destroy_live(void* obj)
{
  assert(!storage_freed);
  destroy(obj);
}

static void* trampolined_worker(void* unused)
{
  (void)unused;
  assert(pthread_setspecific(storage_key, &storage_key) == 0);
  assert(register_dtor(destroy_live, &values[0], NULL) == 0);
  assert(register_dtor(destroy_live, &values[1], NULL) == 0);
  ps5_tls_dtors_thread_exit(); /* what thread_stack.c's trampoline does */
  assert(destroyed == 3 && pthread_getspecific(g_tls_dtor_key) == NULL);
  return NULL;
}

int main(void)
{
  reset();
  once_error = EINVAL;
  for (int i = 0; i < 3; ++i)
    assert(register_dtor(destroy, values, NULL) == -1);
  assert(once_calls == 3 && key_calls == 0 && allocations == 0);
  assert(get_calls == 0 && set_calls == 0);
  assert(logs == 1 && strstr(message, "pthread_once failed"));

  reset();
  key_error = EAGAIN;
  for (int i = 0; i < 3; ++i)
    assert(register_dtor(destroy, values, NULL) == -1);
  assert(once_calls == 3 && key_calls == 1 && allocations == 0);
  assert(get_calls == 0 && set_calls == 0);
  assert(logs == 1 && strstr(message, "pthread_key_create failed"));
  /* Successful once completion must not retry a failed key creation. */
  key_error = 0;
  assert(register_dtor(destroy, values, NULL) == -1);
  assert(key_calls == 1 && logs == 1);

  reset();
  malloc_error = 1;
  for (int i = 0; i < 3; ++i)
    assert(register_dtor(destroy, values, NULL) == -1);
  assert(key_calls == 1 && allocations == 0 && releases == 0);
  assert(get_calls == 0 && set_calls == 0);
  assert(logs == 1 && strstr(message, "malloc failed"));
  malloc_error = 0;
  assert(register_dtor(destroy, values, NULL) == 0);
  drain();
  assert(destroyed == 1 && order[0] == 1 && allocations == releases);

  reset();
  set_error = ENOMEM;
  for (int i = 0; i < 3; ++i)
    assert(register_dtor(destroy, values, NULL) == -1);
  assert(allocations == 3 && releases == 3 && destroyed == 0);
  assert(pthread_getspecific(g_tls_dtor_key) == NULL);
  assert(logs == 1 && strstr(message, "pthread_setspecific failed"));
  set_error = 0;
  assert(register_dtor(destroy, values, NULL) == 0);
  void* previous = pthread_getspecific(g_tls_dtor_key);
  set_error = EINVAL;
  assert(register_dtor(destroy, &values[1], NULL) == -1);
  assert(pthread_getspecific(g_tls_dtor_key) == previous);
  assert(allocations == releases + 1 && destroyed == 0 && logs == 1);
  set_error = 0;
  assert(register_dtor(destroy, &values[1], NULL) == 0);
  drain();
  assert(destroyed == 2 && order[0] == 2 && order[1] == 1);
  assert(allocations == releases && key_calls == 1);

  reset();
  rearm = 1;
  pthread_t thread;
  assert(pthread_create(&thread, NULL, worker, NULL) == 0);
  assert(pthread_join(thread, NULL) == 0);
  assert(destroyed == 3 && order[0] == 2 && order[1] == 1 && order[2] == 3);
  assert(allocations == 3 && releases == 3 && key_calls == 1 && logs == 0);

  /* Thread-exit drain runs before older keys' destructors free storage,
   * re-runs for destructors that register more, and is a no-op when empty. */
  reset();
  rearm = 1;
  assert(pthread_key_create(&storage_key, free_storage) == 0);
  assert(pthread_create(&thread, NULL, trampolined_worker, NULL) == 0);
  assert(pthread_join(thread, NULL) == 0);
  assert(storage_freed && order[0] == 2 && order[1] == 1 && order[2] == 3);
  assert(allocations == 3 && releases == 3 && key_calls == 1 && logs == 0);
  assert(pthread_key_delete(storage_key) == 0);
  ps5_tls_dtors_thread_exit();
  assert(destroyed == 3 && allocations == releases);

  reset();
  ps5_tls_dtors_thread_exit(); /* first use creates the key, runs nothing */
  assert(key_calls == 1 && destroyed == 0 && allocations == 0);
  reset();
  key_error = EAGAIN;
  ps5_tls_dtors_thread_exit();
  assert(key_calls == 1 && get_calls == 0 && set_calls == 0 && logs == 0);
  reset();
  puts("PASS: registration failure injection, real pthread destructor rearming and "
       "thread-exit drain ordering");
  return 0;
}
