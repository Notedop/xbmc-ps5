/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "shared_tls.h"

#include <errno.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

extern void sceKernelDebugOutText(int channel, const char* text);

struct dso_record
{
  void* tag;
  size_t pending;
  int closed;
  struct dso_record* next;
};

struct destructor_entry
{
  void (*callback)(void*);
  void* object;
  struct dso_record* dso;
  struct destructor_entry* next;
};

struct tls_slot
{
  void* allocation;
  void* address;
};

struct thread_context
{
  struct tls_slot* slots;
  size_t count;
  struct destructor_entry* destructors;
  int cleaning;
};

static pthread_mutex_t registry_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_once_t key_once = PTHREAD_ONCE_INIT;
static pthread_key_t context_key;
static int key_error;
static uintptr_t last_index;
/* Tags remain tracked for the runtime lifetime, including while closed. */
static struct dso_record* dsos;

static int report_error(int error, const char* message)
{
  sceKernelDebugOutText(0, message);
  errno = error;
  return -1;
}

static void fatal(int error, const char* message)
{
  report_error(error, message);
  abort();
}

static void lock_registry(void)
{
  int error = pthread_mutex_lock(&registry_lock);
  if (error)
    fatal(error, "shared_tls: registry lock failed\n");
}

static void unlock_registry(void)
{
  int error = pthread_mutex_unlock(&registry_lock);
  if (error)
    fatal(error, "shared_tls: registry unlock failed\n");
}

static void destroy_context(void* opaque);

static void create_key(void)
{
  key_error = pthread_key_create(&context_key, destroy_context);
}

static int ensure_key(void)
{
  int error = pthread_once(&key_once, create_key);
  if (!error)
    error = key_error;
  if (error)
    return report_error(error, "shared_tls: context key initialization failed\n");
  return 0;
}

static struct thread_context* get_context(void)
{
  if (ensure_key() != 0)
    return NULL;
  struct thread_context* context = pthread_getspecific(context_key);
  if (context)
    return context;
  context = calloc(1, sizeof(*context));
  if (!context)
  {
    report_error(ENOMEM, "shared_tls: context allocation failed\n");
    return NULL;
  }
  int error = pthread_setspecific(context_key, context);
  if (error)
  {
    free(context);
    report_error(error, "shared_tls: context key assignment failed\n");
    return NULL;
  }
  return context;
}

static struct dso_record* find_dso(void* tag)
{
  for (struct dso_record* record = dsos; record; record = record->next)
    if (record->tag == tag)
      return record;
  return NULL;
}

static struct dso_record* get_dso(void* tag)
{
  struct dso_record* record = find_dso(tag);
  if (!record)
  {
    record = calloc(1, sizeof(*record));
    if (record)
    {
      record->tag = tag;
      record->next = dsos;
      dsos = record;
    }
  }
  return record;
}

void* __emutls_get_address(struct ps5_emutls_control* control)
{
  if (!control || !control->size || !control->align ||
      (control->align & (control->align - 1)) ||
      control->size > SIZE_MAX - (control->align - 1))
    fatal(EINVAL, "shared_tls: invalid emutls control size/alignment\n");

  lock_registry();
  uintptr_t index = control->loc.index;
  if (!index)
  {
    if (last_index >= SIZE_MAX / sizeof(struct tls_slot))
      fatal(EOVERFLOW, "shared_tls: emutls index exhausted\n");
    control->loc.index = index = ++last_index;
  }
  else if (index > last_index)
    fatal(EINVAL, "shared_tls: invalid emutls control index\n");
  unlock_registry();

  struct thread_context* context = get_context();
  if (!context)
    fatal(errno, "shared_tls: emutls context unavailable\n");
  if (index > context->count)
  {
    struct tls_slot* slots = calloc(index, sizeof(*slots));
    if (!slots)
      fatal(ENOMEM, "shared_tls: emutls slot allocation failed\n");
    if (context->count)
      memcpy(slots, context->slots, context->count * sizeof(*slots));
    free(context->slots);
    context->slots = slots;
    context->count = index;
  }
  struct tls_slot* slot = &context->slots[index - 1];
  if (!slot->address)
  {
    size_t bytes = control->size + control->align - 1;
    void* allocation = malloc(bytes);
    if (!allocation)
      fatal(ENOMEM, "shared_tls: emutls storage allocation failed\n");
    uintptr_t base = (uintptr_t)allocation;
    if (base > UINTPTR_MAX - (control->align - 1))
      fatal(EOVERFLOW, "shared_tls: emutls address overflow\n");
    void* address = (void*)((base + control->align - 1) & ~(control->align - 1));
    if (control->template_value)
      memcpy(address, control->template_value, control->size);
    else
      memset(address, 0, control->size);
    slot->allocation = allocation;
    slot->address = address;
  }
  return slot->address;
}

int __cxa_thread_atexit_impl(void (*destructor)(void*), void* object, void* dso)
{
  if (!destructor)
    return report_error(EINVAL, "shared_tls: null TLS destructor\n");
  struct thread_context* context = get_context();
  if (!context)
    return -1;
  struct destructor_entry* entry = malloc(sizeof(*entry));
  if (!entry)
    return report_error(ENOMEM, "shared_tls: destructor allocation failed\n");
  lock_registry();
  struct dso_record* record = get_dso(dso);
  int error = !record ? ENOMEM : record->closed ? EBUSY :
              record->pending == SIZE_MAX ? EOVERFLOW : 0;
  if (error)
  {
    unlock_registry();
    free(entry);
    return report_error(error, "shared_tls: TLS destructor registration refused\n");
  }
  ++record->pending;
  entry->callback = destructor;
  entry->object = object;
  entry->dso = record;
  entry->next = context->destructors;
  context->destructors = entry;
  unlock_registry();
  return 0;
}

size_t ps5_tls_dso_pending(void* dso)
{
  lock_registry();
  struct dso_record* record = find_dso(dso);
  size_t pending = record ? record->pending : 0;
  unlock_registry();
  return pending;
}

static int set_dso_closed(void* dso, int closed)
{
  lock_registry();
  struct dso_record* record = get_dso(dso);
  int error = !record ? ENOMEM : record->pending ? EBUSY : 0;
  if (!error)
    record->closed = closed;
  unlock_registry();
  if (error)
    return report_error(error, "shared_tls: DSO lifecycle transition refused\n");
  return 0;
}

int ps5_tls_begin_unload(void* dso)
{
  return set_dso_closed(dso, 1);
}

int ps5_tls_reopen_dso(void* dso)
{
  return set_dso_closed(dso, 0);
}

static void destroy_context(void* opaque)
{
  struct thread_context* context = opaque;
  if (!context || context->cleaning)
    return;
  int previous_cancel;
  int error = pthread_setcancelstate(PTHREAD_CANCEL_DISABLE, &previous_cancel);
  if (error)
    fatal(error, "shared_tls: cannot disable cancellation for cleanup\n");
  context->cleaning = 1;
  /* POSIX clears the key before entering its destructor. Restore it so both
   * compiler accessors and recursively registered destructors share this owner.
   */
  error = pthread_setspecific(context_key, context);
  if (error)
    fatal(error, "shared_tls: cannot preserve context during cleanup\n");
  while (context->destructors)
  {
    struct destructor_entry* entry = context->destructors;
    context->destructors = entry->next;
    entry->callback(entry->object);
    lock_registry();
    --entry->dso->pending;
    unlock_registry();
    free(entry);
  }
  for (size_t i = 0; i < context->count; ++i)
    free(context->slots[i].allocation);
  free(context->slots);
  error = pthread_setspecific(context_key, NULL);
  if (error)
    fatal(error, "shared_tls: cannot clear context after cleanup\n");
  free(context);
  error = pthread_setcancelstate(previous_cancel, NULL);
  if (error)
    fatal(error, "shared_tls: cannot restore cancellation state\n");
}

void ps5_tls_thread_exit(void)
{
  if (ensure_key() != 0)
    fatal(errno, "shared_tls: explicit cleanup key unavailable\n");
  destroy_context(pthread_getspecific(context_key));
}
