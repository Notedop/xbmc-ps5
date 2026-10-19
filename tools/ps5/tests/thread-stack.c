/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>

/* No actual threads are launched. Explicit attributes must pass through intact. */
typedef struct
{
  size_t size, guard;
  void* address;
  int detach, inherit, policy, scope;
  struct sched_param scheduling;
  unsigned long affinity;
} test_attr;

static const char* fail_api;
static int failure_rc, create_rc, memory_rc, destroy_rc;
static int init_calls, set_calls, destroy_calls, create_calls, log_calls;
static size_t default_size;
static test_attr attempted;
static const test_attr* attempted_attr;
static char messages[4096];

static int fault(const char* api)
{
  return fail_api && strcmp(fail_api, api) == 0 ? failure_rc : 0;
}

static int stub_init(test_attr* attr)
{
  ++init_calls;
  if (fault("init"))
    return failure_rc;
  memset(attr, 0, sizeof(*attr));
  attr->size = default_size;
  errno = EIO;
  return 0;
}

static int stub_destroy(test_attr* attr)
{
  (void)attr;
  ++destroy_calls;
  errno = EIO;
  return destroy_rc;
}

static int stub_getstacksize(const test_attr* attr, size_t* size)
{
  errno = EIO;
  if (fault("getstacksize"))
    return failure_rc;
  *size = attr->size;
  return 0;
}

static int stub_setstacksize(test_attr* attr, size_t size)
{
  ++set_calls;
  errno = EIO;
  if (fault("setstacksize"))
    return failure_rc;
  attr->size = size;
  return 0;
}

#define pthread_attr_t test_attr
#define pthread_attr_init stub_init
#define pthread_attr_destroy stub_destroy
#define pthread_attr_getstacksize stub_getstacksize
#define pthread_attr_setstacksize stub_setstacksize
#include "../../../shims/native-app/thread_stack.c"

int __real_pthread_create(pthread_t* thread, const test_attr* attr,
                          void* (*start)(void*), void* arg)
{
  (void)thread;
  (void)start;
  (void)arg;
  ++create_calls;
  errno = EIO;
  attempted_attr = attr;
  attempted = *attr;
  return create_rc;
}

int sceKernelAvailableFlexibleMemorySize(size_t* size)
{
  errno = ENOSPC;
  *size = 123456;
  return memory_rc;
}

int sceKernelDebugOutText(int channel, const char* text)
{
  assert(channel == 0);
  ++log_calls;
  assert(strlen(messages) + strlen(text) < sizeof(messages));
  strcat(messages, text);
  errno = EIO;
  return -1;
}

static void reset(void)
{
  fail_api = NULL;
  failure_rc = ENOMEM;
  create_rc = memory_rc = destroy_rc = 0;
  init_calls = set_calls = destroy_calls = create_calls = log_calls = 0;
  default_size = 1024u * 1024u;
  memset(&attempted, 0, sizeof(attempted));
  attempted_attr = NULL;
  messages[0] = '\0';
  errno = EDOM;
}

static int run(const test_attr* attr)
{
  test_attr snapshot;
  if (attr)
    snapshot = *attr;
  pthread_t thread;
  int result = __wrap_pthread_create(&thread, attr, NULL, NULL);
  assert(errno == EDOM);
  assert(create_calls <= 1);
  if (attr)
    assert(memcmp(&snapshot, attr, sizeof(snapshot)) == 0);
  return result;
}

static void expect_log(const char* stage, int rc, size_t size)
{
  char expected[180];
  snprintf(expected, sizeof(expected),
           "[kodi-ps5] DIAGNOSTIC thread-create: stage=%s pthread_rc=%d "
           "attempted_stack_bytes=%zu", stage, rc, size);
  assert(strstr(messages, expected));
  assert(strstr(messages, "flexible_memory_bytes=123456"));
}

int main(void)
{
  const size_t sizes[] = {64u * 1024u, PS5_DEFAULT_THREAD_STACK, 8u * 1024u * 1024u};
  for (size_t i = 0; i < 3; ++i)
  {
    reset();
    default_size = sizes[i];
    assert(run(NULL) == 0);
    assert(init_calls == 1 && destroy_calls == 1 && create_calls == 1);
    assert(set_calls == (i == 0 ? 1 : 0) && log_calls == 0);
    assert(attempted.size == (i == 0 ? PS5_DEFAULT_THREAD_STACK : sizes[i]));
  }

  test_attr attr = {.size = sizes[0], .guard = 16384, .detach = 17,
                   .inherit = 23, .policy = 42, .scope = 37,
                   .scheduling = {.sched_priority = 29}, .affinity = 0x55};
  for (size_t i = 0; i < 3; ++i)
  {
    reset();
    attr.size = sizes[i];
    assert(run(&attr) == 0 && create_calls == 1);
    assert(memcmp(&attr, &attempted, sizeof(attr)) == 0);
    assert(init_calls == 0 && destroy_calls == 0);
    assert(attempted_attr == &attr && set_calls == 0);
  }

  /* Supplied backing memory is never enlarged or replaced. */
  attr.address = &attr;
  for (size_t i = 0; i < 3; ++i)
  {
    reset();
    attr.size = sizes[i];
    assert(run(&attr) == 0 && create_calls == 1 && log_calls == 0);
    assert(attempted_attr == &attr);
    assert(memcmp(&attr, &attempted, sizeof(attr)) == 0);
    assert(init_calls == 0 && set_calls == 0 && destroy_calls == 0);
  }
  attr.size = sizes[0];
  reset();
  create_rc = EAGAIN;
  assert(run(&attr) == EAGAIN && create_calls == 1 && destroy_calls == 0);
  assert(attempted_attr == &attr && init_calls == 0 && set_calls == 0);
  expect_log("pthread_create", EAGAIN, sizes[0]);
  attr.address = NULL;

  const char* default_apis[] = {"init", "getstacksize", "setstacksize"};
  const char* stages[] = {"attr-init", "attr-getstacksize", "attr-setstacksize"};
  for (size_t i = 0; i < 3; ++i)
  {
    reset();
    fail_api = default_apis[i];
    assert(run(NULL) == ENOMEM && create_calls == 0 && log_calls == 1);
    assert(destroy_calls == (i == 0 ? 0 : 1));
    expect_log(stages[i], ENOMEM, i == 2 ? PS5_DEFAULT_THREAD_STACK : 0);
  }

  const int failures[] = {EAGAIN, EINVAL, EPERM, ENOMEM};
  for (size_t i = 0; i < sizeof(failures) / sizeof(failures[0]); ++i)
  {
    reset();
    create_rc = failures[i];
    assert(run(NULL) == failures[i] && create_calls == 1 && destroy_calls == 1);
    expect_log("pthread_create", failures[i], PS5_DEFAULT_THREAD_STACK);
    assert(strstr(messages, "flexible_memory_rc=0"));
    assert(strstr(messages, "flexible_memory_valid=1"));
  }

  reset();
  create_rc = EAGAIN;
  memory_rc = -123;
  assert(run(&attr) == EAGAIN && create_calls == 1 && destroy_calls == 0);
  expect_log("pthread_create", EAGAIN, attr.size);
  assert(strstr(messages, "flexible_memory_rc=-123"));
  assert(strstr(messages, "flexible_memory_valid=0"));

  reset();
  destroy_rc = EINVAL;
  assert(run(NULL) == 0 && create_calls == 1);
  expect_log("attr-destroy", EINVAL, PS5_DEFAULT_THREAD_STACK);

  reset();
  create_rc = EAGAIN;
  destroy_rc = EINVAL;
  assert(run(NULL) == EAGAIN && log_calls == 2 && create_calls == 1);
  expect_log("pthread_create", EAGAIN, PS5_DEFAULT_THREAD_STACK);
  expect_log("attr-destroy", EINVAL, PS5_DEFAULT_THREAD_STACK);

  reset();
  fail_api = "getstacksize";
  assert(run(&attr) == 0 && create_calls == 1 && log_calls == 0);
  reset();
  fail_api = "getstacksize";
  create_rc = EAGAIN;
  assert(run(&attr) == EAGAIN && create_calls == 1 && destroy_calls == 0);
  expect_log("pthread_create", EAGAIN, 0);
  assert(strstr(messages, "stack_attr_rc=12"));
  assert(strstr(messages, "explicit_attr=1"));
  assert(strstr(messages, "creator_thread="));
  assert(strstr(messages, "entry="));
  puts("PASS: thread-stack host regressions");
  return 0;
}
