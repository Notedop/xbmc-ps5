/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "CPUInfoPS5.h"

#include <sys/sysctl.h>
#include <sys/types.h>

extern "C"
{
  long sceKernelGetCpuFrequency(void);
  int sceKernelGetCpuTemperature(int* outCelsius);
  int sceKernelGetCpuCoreClock(int* outMHzPerCore);
  int sceKernelGetSocSensorTemperature(int sensorId, int* outCelsius);
  int sceKernelGetCpuUsage(void* out, int32_t* count);
}

std::shared_ptr<CCPUInfo> CCPUInfo::GetCPUInfo()
{
  return std::make_shared<CCPUInfoPS5>();
}

float CCPUInfoPS5::GetCPUFrequency()
{
  const long hz = sceKernelGetCpuFrequency();
  return hz > 0 ? static_cast<float>(hz / 1000000.0) : 0.0f;
}

bool CCPUInfoPS5::GetTemperature(CTemperature& temperature)
{
  int celsius = 0;
  if (sceKernelGetCpuTemperature(&celsius) != 0)
    return false;

  temperature = CTemperature::CreateFromCelsius(celsius);
  return true;
}

CCPUInfoPS5::CCPUInfoPS5()
{
  // Oberon SoC: 8 Zen 2 cores (one is normally reserved for the system).
  // The loader decides what this process may be pinned to; ask the kernel and
  // fall back to the physical count if the sysctl is not permitted.
  int ncpu = 0;
  size_t len = sizeof(ncpu);
  if (sysctlbyname("hw.ncpu", &ncpu, &len, nullptr, 0) != 0 || ncpu <= 0)
    ncpu = 8;

  m_cpuCount = ncpu;
  m_cpuModel = "AMD Zen 2 (PlayStation 5)";

  for (int core = 0; core < m_cpuCount; core++)
  {
    CoreInfo coreInfo;
    coreInfo.m_id = core;
    m_cores.emplace_back(coreInfo);
  }
}
