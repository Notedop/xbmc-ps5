/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "GPUInfoPS5.h"

extern "C"
{
  int sceKernelGetSocSensorTemperature(int sensorId, int* outCelsius);
}

std::unique_ptr<CGPUInfo> CGPUInfo::GetGPUInfo()
{
  return std::make_unique<CGPUInfoPS5>();
}

bool CGPUInfoPS5::GetGPUPlatformTemperature(CTemperature& temperature) const
{
  int celsius = 0;
  if (sceKernelGetSocSensorTemperature(0, &celsius) != 0)
    return false;

  temperature = CTemperature::CreateFromCelsius(celsius);
  return true;
}