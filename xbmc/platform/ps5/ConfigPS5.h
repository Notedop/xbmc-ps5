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

struct DebugConfig
{
  bool enabled{false};
  bool video{false};
  bool addons{false};
};

const char* ElevationMethodName(ElevationMethodId id);
std::vector<ElevationMethodId> DefaultElevationOrder();

struct Config
{
  std::vector<ElevationMethodId> elevationMethods{DefaultElevationOrder()};
  DebugConfig debug;
};

enum class ConfigStatus
{
  // Neither configuration file exists; built-in defaults are loaded.
  defaults,
  loaded,
  error,
};

// Missing settings use built-in defaults. Invalid settings leave config unchanged.
bool ParseConfig(const std::string& json, Config& config, std::string& error);

// Read path, or defaultPath only if path is missing. If both are missing, use
// the built-in defaults. An existing invalid or unreadable file is an error.
ConfigStatus LoadConfig(const std::string& path,
                        const std::string& defaultPath,
                        Config& config,
                        std::string& error);

// Bridge to existing Kodi and external add-on diagnostics without reparsing JSON.
bool ApplyDebugConfig(const DebugConfig& config, std::string& error);
} // namespace KODI::PLATFORM::PS5
