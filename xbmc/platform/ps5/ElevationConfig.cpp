/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ElevationConfig.h"

#include <fcntl.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>

#include <nlohmann/json.hpp>

namespace
{
using namespace KODI::PLATFORM::PS5;

constexpr std::array<std::pair<const char*, ElevationMethodId>, 3> kMethodIds{{
    { "elf_9021", ElevationMethodId::elf9021 },
    { "jailbreak_marker", ElevationMethodId::jailbreakMarker },
    { "etahen_ipc", ElevationMethodId::etahenIpc },
}};
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

ElevationConfigStatus ParseElevationConfig(const std::string& json,
                                           std::vector<ElevationMethodId>& order,
                                           std::string& error)
{
  order = DefaultElevationOrder();
  const nlohmann::json parsed = nlohmann::json::parse(json, nullptr, false);
  if (parsed.is_discarded())
  {
    error = "elevation.json: malformed JSON";
    return ElevationConfigStatus::error;
  }
  if (!parsed.is_object())
  {
    error = "elevation.json: expected a JSON object";
    return ElevationConfigStatus::error;
  }
  const auto elevation = parsed.find("elevation");
  if (elevation == parsed.end())
    return ElevationConfigStatus::defaults;
  if (!elevation->is_object())
  {
    error = "elevation.json: \"elevation\" must be an object";
    return ElevationConfigStatus::error;
  }
  const auto methods = elevation->find("methods");
  if (methods == elevation->end())
    return ElevationConfigStatus::defaults;
  if (!methods->is_array())
  {
    error = "elevation.json: \"elevation.methods\" must be an array";
    return ElevationConfigStatus::error;
  }

  std::vector<ElevationMethodId> configured;
  for (size_t index = 0; index < methods->size(); ++index)
  {
    const auto& entry = (*methods)[index];
    const std::string where = "elevation.json: elevation.methods[" + std::to_string(index) + "]";
    if (!entry.is_object())
    {
      error = where + ": expected an object";
      return ElevationConfigStatus::error;
    }
    const auto id = entry.find("id");
    if (id == entry.end() || !id->is_string())
    {
      error = where + ": missing string \"id\"";
      return ElevationConfigStatus::error;
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
      return ElevationConfigStatus::error;
    }
    if (std::find(configured.begin(), configured.end(), method) != configured.end())
    {
      error = where + ": duplicate method id \"" + id->get<std::string>() + "\"";
      return ElevationConfigStatus::error;
    }
    bool enabled = true;
    const auto flag = entry.find("enabled");
    if (flag != entry.end())
    {
      if (!flag->is_boolean())
      {
        error = where + ": \"enabled\" must be a boolean";
        return ElevationConfigStatus::error;
      }
      enabled = flag->get<bool>();
    }
    if (enabled)
      configured.push_back(method);
  }
  order = std::move(configured);
  return ElevationConfigStatus::configured;
}

ElevationConfigStatus LoadElevationConfig(const std::string& path,
                                          std::vector<ElevationMethodId>& order,
                                          std::string& error)
{
  const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC);
  if (fd < 0)
  {
    if (errno == ENOENT || errno == ENOTDIR)
      return ElevationConfigStatus::defaults;
    error = path + ": " + std::strerror(errno);
    return ElevationConfigStatus::error;
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
    error = path + ": exceeds 64 KiB";
    return ElevationConfigStatus::error;
  }
  if (count < 0)
  {
    error = path + ": read failed: " + std::strerror(readError);
    return ElevationConfigStatus::error;
  }
  return ParseElevationConfig(body, order, error);
}
} // namespace KODI::PLATFORM::PS5
