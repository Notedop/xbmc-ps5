/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ConfigPS5.h"

#include <fcntl.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <utility>

#include <nlohmann/json.hpp>

namespace
{
using namespace KODI::PLATFORM::PS5;

constexpr std::array<std::pair<const char*, ElevationMethodId>, 3> kMethodIds{{
    { "elf_9021", ElevationMethodId::elf9021 },
    { "jailbreak_marker", ElevationMethodId::jailbreakMarker },
    { "etahen_ipc", ElevationMethodId::etahenIpc },
}};

bool ParseElevation(const nlohmann::json& parsed, Config& config, std::string& error)
{
  const auto elevation = parsed.find("elevation");
  if (elevation == parsed.end())
    return true;
  if (!elevation->is_object())
  {
    error = "\"elevation\" must be an object";
    return false;
  }
  const auto methods = elevation->find("methods");
  if (methods == elevation->end())
    return true;
  if (!methods->is_array())
  {
    error = "\"elevation.methods\" must be an array";
    return false;
  }

  std::vector<ElevationMethodId> configured;
  for (size_t index = 0; index < methods->size(); ++index)
  {
    const auto& entry = (*methods)[index];
    const std::string where = "elevation.methods[" + std::to_string(index) + "]";
    if (!entry.is_object())
    {
      error = where + ": expected an object";
      return false;
    }
    const auto id = entry.find("id");
    if (id == entry.end() || !id->is_string())
    {
      error = where + ": missing string \"id\"";
      return false;
    }
    ElevationMethodId method{};
    bool known = false;
    for (const auto& [name, candidate] : kMethodIds)
      if (id->get<std::string>() == name)
      {
        method = candidate;
        known = true;
        break;
      }
    if (!known)
    {
      error = where + ": unknown method id \"" + id->get<std::string>() + "\"";
      return false;
    }
    if (std::find(configured.begin(), configured.end(), method) != configured.end())
    {
      error = where + ": duplicate method id \"" + id->get<std::string>() + "\"";
      return false;
    }
    bool enabled = true;
    const auto flag = entry.find("enabled");
    if (flag != entry.end())
    {
      if (!flag->is_boolean())
      {
        error = where + ": \"enabled\" must be a boolean";
        return false;
      }
      enabled = flag->get<bool>();
    }
    if (enabled)
      configured.push_back(method);
  }
  config.elevationMethods = std::move(configured);
  return true;
}

bool ParseDebug(const nlohmann::json& parsed, DebugConfig& config, std::string& error)
{
  const auto debug = parsed.find("debug");
  if (debug == parsed.end())
    return true;
  if (!debug->is_object())
  {
    error = "\"debug\" must be an object";
    return false;
  }
  for (const auto& [name, target] :
       {std::pair{"enabled", &config.enabled}, {"video", &config.video}, {"addons", &config.addons}})
  {
    const auto value = debug->find(name);
    if (value == debug->end())
      continue;
    if (!value->is_boolean())
    {
      error = std::string("debug.") + name + ": must be a boolean";
      return false;
    }
    *target = value->get<bool>();
  }
  return true;
}
} // namespace

namespace KODI::PLATFORM::PS5
{
const char* ElevationMethodName(ElevationMethodId id)
{
  for (const auto& [name, method] : kMethodIds)
    if (method == id)
      return name;
  return "unknown";
}

std::vector<ElevationMethodId> DefaultElevationOrder()
{
  std::vector<ElevationMethodId> order;
  for (const auto& [name, method] : kMethodIds)
    order.push_back(method);
  return order;
}

bool ParseConfig(const std::string& json, Config& config, std::string& error)
{
  error.clear();
  const nlohmann::json parsed = nlohmann::json::parse(json, nullptr, false);
  if (parsed.is_discarded())
  {
    error = "malformed JSON";
    return false;
  }
  if (!parsed.is_object())
  {
    error = "expected a JSON object";
    return false;
  }
  Config candidate;
  if (!ParseElevation(parsed, candidate, error) || !ParseDebug(parsed, candidate.debug, error))
    return false;
  config = std::move(candidate);
  return true;
}

ConfigStatus LoadConfig(const std::string& path,
                        const std::string& defaultPath,
                        Config& config,
                        std::string& error)
{
  error.clear();
  std::string selectedPath = path;
  int fd = open(selectedPath.c_str(), O_RDONLY | O_CLOEXEC);
  if (fd < 0 && errno == ENOENT)
  {
    selectedPath = defaultPath;
    fd = open(selectedPath.c_str(), O_RDONLY | O_CLOEXEC);
  }
  if (fd < 0)
  {
    if (errno == ENOENT)
    {
      config = Config{};
      return ConfigStatus::defaults;
    }
    error = selectedPath + ": " + std::strerror(errno);
    return ConfigStatus::error;
  }
  std::string body;
  char buffer[4096];
  ssize_t count = 0;
  bool tooLarge = false;
  while ((count = read(fd, buffer, sizeof(buffer))) > 0)
  {
    body.append(buffer, static_cast<size_t>(count));
    if (body.size() > 64u * 1024u)
    {
      tooLarge = true;
      break;
    }
  }
  const int readError = errno;
  close(fd);
  if (tooLarge)
  {
    error = selectedPath + ": exceeds 64 KiB";
    return ConfigStatus::error;
  }
  if (count < 0)
  {
    error = selectedPath + ": read failed: " + std::strerror(readError);
    return ConfigStatus::error;
  }
  if (!ParseConfig(body, config, error))
  {
    error = selectedPath + ": " + error;
    return ConfigStatus::error;
  }
  return ConfigStatus::loaded;
}

bool ApplyDebugConfig(const DebugConfig& config, std::string& error)
{
  error.clear();
  for (const auto& [name, enabled] :
       {std::pair{"KODI_PS5_DEBUG", config.enabled}, {"KODI_PS5_VIDEO_DEBUG", config.video},
        {"KODI_PS5_ADDON_DEBUG", config.addons}})
  {
    if ((enabled ? setenv(name, "1", 1) : unsetenv(name)) != 0)
    {
      error = std::string(name) + ": " + std::strerror(errno);
      return false;
    }
  }
  return true;
}
} // namespace KODI::PLATFORM::PS5
