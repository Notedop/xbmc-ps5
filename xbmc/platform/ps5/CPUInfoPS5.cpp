/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "CPUInfoPS5.h"

#include "utils/StringUtils.h"
#include "utils/log.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <limits>
#include <string_view>

#include <sys/sysctl.h>
#include <sys/types.h>
#include <time.h>

extern "C"
{
  long sceKernelGetCpuFrequency(void);
  int sceKernelGetCpuTemperature(int* outCelsius);
  int sceKernelGetCpuUsage(void* out, int32_t* count);
  int sceKernelGetThreadName(uint32_t threadId, char* out);
}

namespace
{
constexpr int MAX_LOGICAL_CPUS = 16;
constexpr int32_t MAX_THREAD_RECORDS = 65536;
constexpr int32_t MIN_THREAD_RECORDS = 3072;
constexpr int32_t THREAD_HEADROOM = 256;
constexpr std::chrono::seconds IDLE_RESCAN_INTERVAL{5};
constexpr double MAX_SAMPLE_INTERVAL = 3.0;
} // namespace

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
  m_lastUsedPercentage = 0;

  // hw.ncpu counts logical CPUs, not this process's affinity mask.
  int ncpu = 0;
  size_t len = sizeof(ncpu);
  if (sysctlbyname("hw.ncpu", &ncpu, &len, nullptr, 0) != 0 || len != sizeof(ncpu) || ncpu <= 0 ||
      ncpu > MAX_LOGICAL_CPUS)
    ncpu = MAX_LOGICAL_CPUS;

  m_cpuCount = ncpu;
  m_cpuModel = "AMD Zen 2 (PlayStation 5)";

  static_assert(sizeof(ThreadTime) == 0x28);
  m_idleSamples.resize(m_cpuCount);

  for (int core = 0; core < m_cpuCount; core++)
  {
    CoreInfo coreInfo;
    coreInfo.m_id = core;
    m_cores.emplace_back(coreInfo);
  }
}

int CCPUInfoPS5::GetUsedPercentage()
{
  if (!m_nextUsedReadTime.IsTimePast())
    return m_lastUsedPercentage;

  m_nextUsedReadTime.Set(MINIMUM_TIME_BETWEEN_READS);
  ReadCPUUsage();
  return m_lastUsedPercentage;
}

void CCPUInfoPS5::UsageReadFailed(const char* operation, int result, int count)
{
  if (!m_usageReadFailed)
    CLog::Log(LOGWARNING,
              "CCPUInfoPS5: CPU usage unavailable: {} (result {:#x}, count {}); will retry", operation,
              static_cast<uint32_t>(result), count);
  m_usageReadFailed = true;
  m_supportsCPUUsage = false;
  m_haveUsageSample = false;
  m_cores.clear();
}

bool CCPUInfoPS5::ReadCPUUsage()
{
  int32_t count = 0;
  if (m_threadTimes.empty())
  {
    const int result = sceKernelGetCpuUsage(nullptr, &count);
    if (result != 0 || count <= 0 || count > MAX_THREAD_RECORDS)
    {
      UsageReadFailed("sceKernelGetCpuUsage count query", result, count);
      return false;
    }

    const int32_t capacity =
        std::min(MAX_THREAD_RECORDS, std::max(MIN_THREAD_RECORDS, count + THREAD_HEADROOM));
    m_threadTimes.resize(capacity);
  }

  count = static_cast<int32_t>(m_threadTimes.size());
  const int result = sceKernelGetCpuUsage(m_threadTimes.data(), &count);
  if (result != 0 || count <= 0 || static_cast<size_t>(count) > m_threadTimes.size())
  {
    m_threadTimes.clear();
    UsageReadFailed("sceKernelGetCpuUsage thread read", result, count);
    return false;
  }

  timespec now{};
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
  {
    UsageReadFailed("clock_gettime", errno, count);
    return false;
  }
  const double sampleTime = static_cast<double>(now.tv_sec) + static_cast<double>(now.tv_nsec) / 1e9;
  const double elapsed = sampleTime - m_previousSampleTime;
  if (m_haveUsageSample && (elapsed <= 0.0 || elapsed > MAX_SAMPLE_INTERVAL))
  {
    CLog::Log(LOGINFO, "CCPUInfoPS5: CPU sample gap {:.3f}s; restarting baseline", elapsed);
    m_haveUsageSample = false;
  }

  const bool scanNames =
      std::any_of(m_idleSamples.begin(), m_idleSamples.end(),
                  [](const IdleSample& sample) { return sample.threadId == 0; }) &&
      m_nextIdleScanTime.IsTimePast();
  if (scanNames)
    m_nextIdleScanTime.Set(IDLE_RESCAN_INTERVAL);

  std::vector<IdleSample> current(m_idleSamples.size());
  int nameResult = 0;
  for (int32_t index = 0; index < count; ++index)
  {
    const auto& thread = m_threadTimes[index];
    const auto known =
        std::find_if(m_idleSamples.begin(), m_idleSamples.end(), [&thread](const IdleSample& sample)
                     { return sample.threadId != 0 && sample.threadId == thread.threadId; });
    size_t cpu = m_idleSamples.size();
    if (known != m_idleSamples.end())
      cpu = static_cast<size_t>(known - m_idleSamples.begin());
    else if (scanNames)
    {
      std::array<char, 64> name{};
      const int status = sceKernelGetThreadName(thread.threadId, name.data());
      if (status != 0)
      {
        nameResult = status;
        continue;
      }
      name.back() = '\0';
      std::string_view text{name.data()};
      if (text == "SceIdleCpuRv")
        cpu = 13;
      else if (text.starts_with("SceIdleCpu"))
      {
        text.remove_prefix(std::strlen("SceIdleCpu"));
        unsigned int id = 0;
        const auto parsed = std::from_chars(text.data(), text.data() + text.size(), id);
        // CPU 13 is represented by SceIdleCpuRv, not the numeric idle-thread name.
        if (parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size() && id != 13)
          cpu = id;
      }
    }

    if (cpu >= current.size())
      continue;

    const auto failIdleRecord = [&](const char* reason)
    {
      std::string operation{reason};
      if (!m_usageReadFailed)
      {
        std::array<char, 64> name{};
        const int status = sceKernelGetThreadName(thread.threadId, name.data());
        name.back() = '\0';
        operation = StringUtils::Format(
            "{}: CPU {}, PID {}, TID {}, name '{}' (name result {:#x}), existing TID {}, "
            "user {}s/{}us, system {}s/{}us",
            reason, cpu, thread.processId, thread.threadId, name.data(),
            static_cast<uint32_t>(status), current[cpu].threadId, thread.userSeconds,
            thread.userMicroseconds, thread.systemSeconds, thread.systemMicroseconds);
      }
      UsageReadFailed(operation.c_str(), 0, count);
    };

    if (current[cpu].threadId != 0)
    {
      failIdleRecord("duplicate idle-thread mapping");
      return false;
    }

    if (thread.userSeconds < 0 || thread.systemSeconds < 0 || thread.userMicroseconds < 0 ||
        thread.userMicroseconds >= 1000000 || thread.systemMicroseconds < 0 ||
        thread.systemMicroseconds >= 1000000 || thread.threadId == 0)
    {
      failIdleRecord("invalid idle-thread time or ID");
      return false;
    }
    const auto seconds =
        static_cast<uint64_t>(thread.userSeconds) + static_cast<uint64_t>(thread.systemSeconds);
    const auto microseconds = static_cast<uint64_t>(thread.userMicroseconds) +
                              static_cast<uint64_t>(thread.systemMicroseconds);
    if (seconds > (std::numeric_limits<uint64_t>::max() - microseconds) / 1000000)
    {
      failIdleRecord("idle-thread time overflow");
      return false;
    }
    current[cpu] = {thread.threadId, seconds * 1000000 + microseconds};
  }

  const auto found =
      std::count_if(current.begin(), current.end(),
                    [](const IdleSample& sample) { return sample.threadId != 0; });
  if (found == 0)
  {
    m_idleSamples = std::move(current);
    UsageReadFailed("no CPU idle threads visible (sceKernelGetThreadName)", nameResult, count);
    return false;
  }

  std::vector<CoreInfo> cores;
  double totalUsage = 0.0;
  for (size_t cpu = 0; cpu < current.size(); ++cpu)
  {
    const auto& previous = m_idleSamples[cpu];
    const auto& sample = current[cpu];
    if (!m_haveUsageSample || sample.threadId == 0 || previous.threadId != sample.threadId)
      continue;
    if (sample.microseconds < previous.microseconds)
    {
      CLog::Log(LOGWARNING, "CCPUInfoPS5: CPU {} idle time reset; restarting baseline", cpu);
      continue;
    }
    const double idle = static_cast<double>(sample.microseconds - previous.microseconds) / 1e6;
    CoreInfo core;
    core.m_id = static_cast<int>(cpu);
    core.m_usagePercent = std::clamp(1.0 - idle / elapsed, 0.0, 1.0) * 100.0;
    totalUsage += core.m_usagePercent;
    cores.emplace_back(core);
  }

  if (!cores.empty())
  {
    if (!m_supportsCPUUsage || cores.size() != m_cores.size())
      CLog::Log(LOGINFO,
                "CCPUInfoPS5: CPU usage available for {} of {} logical CPUs ({} thread records)",
                cores.size(), m_cpuCount, count);
    m_lastUsedPercentage = static_cast<int>(totalUsage / static_cast<double>(cores.size()));
  }
  m_supportsCPUUsage = !cores.empty();
  m_cores = std::move(cores);
  m_idleSamples = std::move(current);
  m_previousSampleTime = sampleTime;
  m_haveUsageSample = true;
  m_usageReadFailed = false;
  return true;
}
