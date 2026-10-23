/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include <kodi/c-api/addon-instance/screensaver.h>
#include <kodi/versions.h>

#include <assert.h>
#include <stdio.h>
#include <string.h>

ADDON_STATUS ADDON_Create(void*);
const char* ADDON_GetTypeVersion(int);
const char* ADDON_GetTypeMinVersion(int);
int module_start(size_t, const void*);
int module_stop(size_t, const void*);

static unsigned int messages;
static void addon_log(const KODI_ADDON_BACKEND_HDL handle, const int level, const char* message)
{
  (void)handle;
  assert(level == ADDON_LOG_INFO);
  puts(message);
  ++messages;
}

int main(void)
{
  for (int cycle = 0; cycle < 2; ++cycle)
  {
    AddonToKodiFuncTable_Addon to_kodi = {.addon_log_msg = addon_log};
    KodiToAddonFuncTable_Addon to_addon = {0};
    AddonGlobalInterface interface = {.toKodi = &to_kodi, .toAddon = &to_addon};
    KodiToAddonFuncTable_Screensaver screensaver_funcs = {0};
    AddonInstance_Screensaver screensaver = {.toAddon = &screensaver_funcs};
    KODI_ADDON_INSTANCE_INFO info = {.type = ADDON_INSTANCE_SCREENSAVER};
    KODI_ADDON_INSTANCE_STRUCT instance = {.info = &info, .screensaver = &screensaver};
    assert(module_start(0, NULL) == 0);
    assert(strcmp(ADDON_GetTypeVersion(ADDON_GLOBAL_MAIN), ADDON_GLOBAL_VERSION_MAIN) == 0);
    assert(strcmp(ADDON_GetTypeVersion(ADDON_INSTANCE_SCREENSAVER),
                  ADDON_INSTANCE_VERSION_SCREENSAVER) == 0);
    assert(strcmp(ADDON_GetTypeMinVersion(ADDON_INSTANCE_SCREENSAVER),
                  ADDON_INSTANCE_VERSION_SCREENSAVER_MIN) == 0);
    assert(ADDON_Create(&interface) == ADDON_STATUS_OK);
    assert(module_stop(0, NULL) != 0);
    assert(to_addon.create_instance(interface.addonBase, &instance) == ADDON_STATUS_OK);
    assert(screensaver_funcs.start(instance.hdl));
    unsigned int before = messages;
    screensaver_funcs.render(instance.hdl);
    screensaver_funcs.render(instance.hdl);
    assert(messages == before + 1);
    screensaver_funcs.stop(instance.hdl);
    to_addon.destroy_instance(interface.addonBase, &instance);
    to_addon.destroy(interface.addonBase);
    assert(module_stop(0, NULL) == 0);
  }
  puts("PASS: real Kodi dev-kit C ABI screensaver lifecycle, twice");
}
