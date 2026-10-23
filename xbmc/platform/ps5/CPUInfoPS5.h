/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "utils/CPUInfo.h"

class CCPUInfoPS5 : public CCPUInfo
{
public:

  CCPUInfoPS5();
  ~CCPUInfoPS5() = default;

  bool SupportsCPUUsage() const override {return true;}
  int GetUsedPercentage() override {return 0;}
  float GetCPUFrequency() override;
  bool GetTemperature(CTemperature& temperature) override;
};
