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
