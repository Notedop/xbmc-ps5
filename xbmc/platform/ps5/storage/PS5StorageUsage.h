/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#pragma once

#include <cstdint>
#include <optional>

struct PS5InternalStorageUsage
{
  std::optional<uint64_t> total;
  std::optional<uint64_t> free;
  std::optional<uint64_t> games;
  std::optional<uint64_t> media;
  std::optional<uint64_t> savedData;
  std::optional<uint64_t> other;
};

PS5InternalStorageUsage GetPS5InternalStorageUsage();

// Call before sandbox escape; retain the system module for the title lifetime.
bool PreloadPS5UsbStorage();

// Call after sandbox escape so privileged module loading uses the updated process context.
bool PreloadPS5InternalStorageModules();

// Success means the system accepted the request; the provider must observe removal separately.
bool EjectPS5Disc(const char* device);
bool UnmountPS5Storage(const char* device);
