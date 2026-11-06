/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "utils/CPUInfo.h"

#include <cstdint>
#include <vector>

class CCPUInfoPS5 : public CCPUInfo
{
public:
  CCPUInfoPS5();
  ~CCPUInfoPS5() = default;

  bool SupportsCPUUsage() const override { return m_supportsCPUUsage; }
  int GetUsedPercentage() override;
  float GetCPUFrequency() override;
  bool GetTemperature(CTemperature& temperature) override;

private:
  struct ThreadTime
  {
    uint32_t processId;
    uint32_t threadId;
    int64_t userSeconds;
    int64_t userMicroseconds;
    int64_t systemSeconds;
    int64_t systemMicroseconds;
  };

  struct IdleSample
  {
    uint32_t threadId{0};
    uint64_t microseconds{0};
  };

  bool ReadCPUUsage();
  void UsageReadFailed(const char* operation, int result, int count);

  std::vector<ThreadTime> m_threadTimes;
  std::vector<IdleSample> m_idleSamples;
  XbmcThreads::EndTime<> m_nextIdleScanTime;
  double m_previousSampleTime{0.0};
  bool m_supportsCPUUsage{false};
  bool m_haveUsageSample{false};
  bool m_usageReadFailed{false};
};
