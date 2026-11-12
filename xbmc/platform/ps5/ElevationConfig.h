/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string>
#include <vector>

namespace KODI::PLATFORM::PS5
{
enum class ElevationMethodId
{
  elf9021,
  jailbreakMarker,
  etahenIpc,
};

enum class ElevationConfigStatus
{
  // No configuration file, or no "elevation" section: the default order is
  // loaded.
  defaults,
  // The order came from the configuration file; entries with
  // "enabled": false are left out, as are methods missing from the list.
  configured,
  // Malformed configuration; error carries the reason.
  error,
};

const char* ElevationMethodName(ElevationMethodId id);

// Default order: elf_9021, jailbreak_marker, etahen_ipc.
std::vector<ElevationMethodId> DefaultElevationOrder();

// Parse the configuration body. Unknown ids, duplicates, wrong types or
// malformed JSON produce ElevationConfigStatus::error so startup can stop
// instead of silently skipping a method the user meant to enable.
ElevationConfigStatus ParseElevationConfig(const std::string& json,
                                           std::vector<ElevationMethodId>& order,
                                           std::string& error);

// Read path, or defaultPath only if path is missing. If both are missing, use
// the built-in defaults. An existing invalid or unreadable file is an error.
ElevationConfigStatus LoadElevationConfig(const std::string& path,
                                          const std::string& defaultPath,
                                          std::vector<ElevationMethodId>& order,
                                          std::string& error);
} // namespace KODI::PLATFORM::PS5
