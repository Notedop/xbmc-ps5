/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string>
#include <sys/stat.h>

namespace KODI::PLATFORM::PS5
{
// Capture /app0 before sandbox escape; resolve its mounted path afterward.
class AppDirectory
{
public:
  AppDirectory() noexcept;
  ~AppDirectory();

  AppDirectory(const AppDirectory&) = delete;
  AppDirectory& operator=(const AppDirectory&) = delete;

  bool IsValid() const { return m_fd >= 0; }
  bool Resolve(std::string& title) const;

private:
  int m_fd = -1;
  struct stat m_identity{};
};
} // namespace KODI::PLATFORM::PS5
