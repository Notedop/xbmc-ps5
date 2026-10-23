/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once

#include <stddef.h>
#include <stdint.h>

inline constexpr uint32_t KODI_NATIVE_STARTUP_ABI = 1;

struct KodiNativeApplication
{
  void (*run)(int, char**, char**);
  int (*finalize)();
};

struct KodiNativeStartup
{
  uint32_t version;
  uint32_t size;
  KodiNativeApplication* application;
};

static_assert(sizeof(KodiNativeApplication) == 16);
static_assert(sizeof(KodiNativeStartup) == 16);
static_assert(offsetof(KodiNativeStartup, application) == 8);
