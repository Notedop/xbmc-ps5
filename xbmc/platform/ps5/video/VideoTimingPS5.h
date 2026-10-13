/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <string_view>

namespace KODI::PLATFORM::PS5
{
void TraceVideoTimingPS5(std::string_view stage,
                         double ptsMs,
                         double frameDurationMs,
                         double workMs,
                         std::string_view detail);
} // namespace KODI::PLATFORM::PS5
