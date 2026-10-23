/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "utils/GpuInfo.h"

class CGPUInfoPS5 : public CGPUInfo
{
public:
  CGPUInfoPS5() = default;
  ~CGPUInfoPS5() = default;

protected:
  bool SupportsPlatformTemperature() const override { return true; }
  bool SupportsCustomTemperatureCommand() const override { return false; }
  bool GetGPUPlatformTemperature(CTemperature& temperature) const override;
  bool GetGPUTemperatureFromCommand(CTemperature&, const std::string&) const override
  {
    return false;
  }
};
