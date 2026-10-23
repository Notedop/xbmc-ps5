/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <kodi/c-api/addon-instance/screensaver.h>
#include <kodi/versions.h>

#if defined(PS5_SPRX_START_TRACE_DIAGNOSTIC)
#include "native_start_trace.h"
#endif

extern inline const char* GetTypeVersion(int type);
extern inline const char* GetTypeMinVersion(int type);

static AddonGlobalInterface* interface;
static KODI_ADDON_INSTANCE_STRUCT* active_instance;
static bool started;
static bool rendered;

static void log_message(const char* message)
{
  interface->toKodi->addon_log_msg(interface->toKodi->kodiBase, ADDON_LOG_INFO, message);
}

static bool start(const KODI_ADDON_SCREENSAVER_HDL handle)
{
  if (handle != active_instance)
    return false;
  rendered = false;
  log_message("SPRX sample: screensaver start");
  return true;
}

static void stop(const KODI_ADDON_SCREENSAVER_HDL handle)
{
  if (handle == active_instance)
    log_message("SPRX sample: screensaver stop");
}

static void render(const KODI_ADDON_SCREENSAVER_HDL handle)
{
  if (handle == active_instance && !rendered)
  {
    rendered = true;
    log_message("SPRX sample: first render callback");
  }
}

static ADDON_STATUS create_instance(const KODI_ADDON_HDL handle,
                                    KODI_ADDON_INSTANCE_STRUCT* instance)
{
  (void)handle;
  if (instance->info->type != ADDON_INSTANCE_SCREENSAVER || active_instance)
  {
    log_message("SPRX sample: unsupported instance type or concurrent instance");
    return ADDON_STATUS_NOT_IMPLEMENTED;
  }
  active_instance = instance;
  instance->hdl = instance;
  instance->screensaver->toAddon->start = start;
  instance->screensaver->toAddon->stop = stop;
  instance->screensaver->toAddon->render = render;
  log_message("SPRX sample: instance create");
  return ADDON_STATUS_OK;
}

static void destroy_instance(const KODI_ADDON_HDL handle, KODI_ADDON_INSTANCE_STRUCT* instance)
{
  (void)handle;
  if (instance == active_instance)
  {
    log_message("SPRX sample: instance destroy");
    instance->hdl = NULL;
    active_instance = NULL;
  }
}

static void destroy(const KODI_ADDON_HDL handle)
{
  (void)handle;
  log_message("SPRX sample: addon destroy");
  interface = NULL;
}

ATTR_DLL_EXPORT ADDON_STATUS ADDON_Create(void* callback)
{
  interface = callback;
  if (!started)
  {
    log_message("SPRX sample: ERROR native module_start was not called");
    interface = NULL;
    return ADDON_STATUS_PERMANENT_FAILURE;
  }
  interface->addonBase = interface;
  interface->toAddon->destroy = destroy;
  interface->toAddon->create_instance = create_instance;
  interface->toAddon->destroy_instance = destroy_instance;
  log_message("SPRX sample: addon create after native module_start");
  return ADDON_STATUS_OK;
}

ATTR_DLL_EXPORT const char* ADDON_GetTypeVersion(int type)
{
  return GetTypeVersion(type);
}

ATTR_DLL_EXPORT const char* ADDON_GetTypeMinVersion(int type)
{
  return GetTypeMinVersion(type);
}

int module_start(size_t size, const void* args)
{
#if defined(PS5_SPRX_START_TRACE_DIAGNOSTIC)
  if (size != sizeof(PS5SprxStartTrace) || !args)
    return -1;
  const PS5SprxStartTrace* trace = args;
  if (trace->version != 1 || !trace->emit)
    return -1;
  trace->emit(PS5_SPRX_TRACE_ENTER);
#else
  (void)size;
  (void)args;
#endif
  started = true;
#if defined(PS5_SPRX_START_TRACE_DIAGNOSTIC)
  trace->emit(PS5_SPRX_TRACE_LEAVE);
#endif
  return 0;
}

int module_stop(size_t size, const void* args)
{
  (void)size;
  (void)args;
  return active_instance || interface ? -1 : 0;
}
