#!/usr/bin/env python3
"""Exercise the production PS5 idle-thread sampler with deterministic snapshots."""
# SPDX-License-Identifier: GPL-2.0-or-later

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

STUBS = {
    "sys/sysctl.h": """
#pragma once
#include <cstddef>
extern "C" int sysctlbyname(const char*, void*, size_t*, const void*, size_t);
""",
    "threads/SystemClock.h": """
#pragma once
#include <chrono>
#include <cstdint>
extern int64_t testMicroseconds;
namespace XbmcThreads
{
template<typename T = std::chrono::milliseconds>
class EndTime
{
public:
  void Set(T duration)
  {
    m_end = testMicroseconds + std::chrono::duration_cast<std::chrono::microseconds>(duration).count();
  }
  bool IsTimePast() const { return testMicroseconds >= m_end; }
private:
  int64_t m_end{0};
};
}
""",
    "utils/Temperature.h": """
#pragma once
class CTemperature
{
public:
  static CTemperature CreateFromCelsius(double) { return {}; }
};
""",
    "utils/log.h": """
#pragma once
#include <sstream>
#include <string>
enum { LOGWARNING, LOGINFO };
inline int warnings = 0;
inline int infos = 0;
inline std::string lastWarning;
class CLog
{
public:
  template<typename... Args>
  static void Log(int level, const char* format, Args&&... args)
  {
    if (level == LOGWARNING)
    {
      ++warnings;
      std::ostringstream text;
      text << format;
      ((text << " " << args), ...);
      lastWarning = text.str();
    }
    else
      ++infos;
  }
};
""",
    "utils/StringUtils.h": """
#pragma once
#include <cstdio>
#include <sstream>
#include <string>
#include <string_view>
class StringUtils
{
public:
  template<typename... Args> requires(sizeof...(Args) > 2)
  static std::string Format(std::string_view format, const Args&... args)
  {
    std::ostringstream text;
    text << format;
    ((text << " " << args), ...);
    return text.str();
  }
  static std::string Format(std::string_view format, int id, double value)
  {
    char text[64];
    std::snprintf(text, sizeof(text), format.find("1.1f") != format.npos ?
                  "#%d: %1.1f%%" : "#%d: %3.0f%%", id, value);
    return text;
  }
  static std::string Format(std::string_view, double value)
  {
    char text[64];
    std::snprintf(text, sizeof(text), "%3.0f%%", value);
    return text;
  }
};
""",
}

PROBE = r"""
#include "platform/ps5/CPUInfoPS5.h"
#include "utils/log.h"

#include <algorithm>
#include <cassert>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <string>
#include <time.h>
#include <vector>

struct ThreadTime
{
  uint32_t pid, tid;
  int64_t userSeconds, userMicroseconds, systemSeconds, systemMicroseconds;
};
static_assert(sizeof(ThreadTime) == 40);
static std::vector<ThreadTime> threads;
static std::vector<std::string> names;
int64_t testMicroseconds = 1000000000;
static int hardwareCount, queries, reads, nameReads, lastCapacity;
static int queryResult, readResult, nameResult;
static int queryCount, readCount;
static bool failClock;
static int64_t wallOffset;
constexpr int AUTO_COUNT = std::numeric_limits<int>::min();

extern "C" int sysctlbyname(const char* name, void* out, size_t* len, const void*, size_t)
{
  // Any attempt to reintroduce kern.cp_times fails this regression.
  assert(std::strcmp(name, "hw.ncpu") == 0 && *len == sizeof(int));
  *static_cast<int*>(out) = hardwareCount;
  return hardwareCount > 0 ? 0 : -1;
}

extern "C" int sceKernelGetCpuUsage(void* out, int32_t* count)
{
  if (!out)
  {
    ++queries;
    *count = queryCount == AUTO_COUNT ? static_cast<int>(threads.size()) : queryCount;
    return queryResult;
  }
  ++reads;
  const int capacity = *count;
  lastCapacity = capacity;
  assert(capacity >= 3072 && capacity <= 65536);
  *count = readCount == AUTO_COUNT ? static_cast<int>(threads.size()) : readCount;
  if (readResult != 0)
    return readResult;
  const size_t copied = std::min(static_cast<size_t>(capacity), threads.size());
  std::memcpy(out, threads.data(), copied * sizeof(ThreadTime));
  return threads.size() > static_cast<size_t>(capacity) ? -12 : 0;
}

extern "C" int sceKernelGetThreadName(uint32_t tid, char* out)
{
  ++nameReads;
  if (nameResult != 0)
    return nameResult;
  assert(tid > 0 && tid <= names.size());
  std::snprintf(out, 64, "%s", names[tid - 1].c_str());
  return 0;
}

extern "C" int __wrap_clock_gettime(clockid_t id, timespec* out)
{
  assert(id == CLOCK_MONOTONIC);
  if (failClock)
  {
    errno = EIO;
    return -1;
  }
  const int64_t wall = testMicroseconds + wallOffset;
  out->tv_sec = wall / 1000000;
  out->tv_nsec = (wall % 1000000) * 1000;
  return 0;
}

extern "C" long sceKernelGetCpuFrequency() { return 3500000000L; }
extern "C" int sceKernelGetCpuTemperature(int* out) { *out = 50; return 0; }

static void Add(const std::string& name, uint64_t idle = 1000000)
{
  names.push_back(name);
  threads.push_back({0, static_cast<uint32_t>(names.size()), 0, 0,
                     static_cast<int64_t>(idle / 1000000),
                     static_cast<int64_t>(idle % 1000000)});
}

static void Idle(size_t index, uint64_t delta)
{
  auto& t = threads.at(index);
  const uint64_t value = static_cast<uint64_t>(t.systemSeconds) * 1000000 +
                         static_cast<uint64_t>(t.systemMicroseconds) + delta;
  t.systemSeconds = static_cast<int64_t>(value / 1000000);
  t.systemMicroseconds = static_cast<int64_t>(value % 1000000);
}

static void Advance(int64_t delta = 500000) { testMicroseconds += delta; }

static void Reset()
{
  threads.clear();
  names.clear();
  Add("SceIdleCpu0");
  Add("SceIdleCpu1");
  Add("SceWorker");
  hardwareCount = 2;
  queries = reads = nameReads = warnings = infos = lastCapacity = 0;
  lastWarning.clear();
  queryResult = readResult = nameResult = 0;
  queryCount = readCount = AUTO_COUNT;
  failClock = false;
  wallOffset = 0;
  testMicroseconds = 1000000000;
}

static void ExpectCore(CCPUInfoPS5& cpu, int id, double expected)
{
  assert(cpu.HasCoreId(id));
  assert(std::abs(cpu.GetCoreInfo(id).m_usagePercent - expected) < 0.001);
}

static void TestSampling()
{
  Reset();
  CCPUInfoPS5 cpu;
  assert(queries == 0 && reads == 0 && warnings == 0);
  assert(!cpu.SupportsCPUUsage() && cpu.GetCPUCount() == 2);
  assert(cpu.GetCoresUsageString().empty()); // first sample is only a baseline
  assert(reads == 1 && queries == 1);
  Advance(250000);
  cpu.GetCoresUsageString();
  assert(reads == 1);
  Advance(250000);
  Idle(0, 125000);
  Idle(1, 375000);
  const auto text = cpu.GetCoresUsageString();
  assert(cpu.SupportsCPUUsage() && cpu.GetUsedPercentage() == 50);
  assert(text == "#0:  75% #1:  25%");
  ExpectCore(cpu, 0, 75.0);
  ExpectCore(cpu, 1, 25.0);
  assert(reads == 2 && nameReads == 3); // cache IDs, do not rename every worker

  Advance();
  Idle(0, 500000);
  Idle(1, 500000);
  assert(cpu.GetUsedPercentage() == 0);
  ExpectCore(cpu, 0, 0.0);
  Advance();
  assert(cpu.GetUsedPercentage() == 100);
  ExpectCore(cpu, 1, 100.0);

  Advance();
  Idle(0, 750000); // small accounting skew is clamped, not negative utilization
  cpu.GetUsedPercentage();
  ExpectCore(cpu, 0, 0.0);
  Advance();
  threads[0].systemSeconds = threads[0].systemMicroseconds = 0;
  cpu.GetUsedPercentage();
  assert(!cpu.HasCoreId(0) && warnings == 1);
  Advance();
  Idle(0, 250000);
  cpu.GetUsedPercentage();
  ExpectCore(cpu, 0, 50.0);

  Advance(4000000); // suspension or an inactive overlay: discard stale baselines
  assert(cpu.GetCoresUsageString().empty() && !cpu.SupportsCPUUsage());
  Advance();
  Idle(0, 500000);
  Idle(1, 500000);
  assert(!cpu.GetCoresUsageString().empty() && cpu.GetUsedPercentage() == 0);
}

static void TestFailures()
{
  Reset();
  queryResult = -78;
  CCPUInfoPS5 cpu;
  assert(cpu.GetCoresUsageString().empty() && warnings == 1 && reads == 0);
  Advance();
  assert(cpu.GetCoresUsageString().empty() && warnings == 1 && queries == 2);
  queryResult = 0;
  Advance();
  assert(cpu.GetCoresUsageString().empty());
  Advance();
  assert(!cpu.GetCoresUsageString().empty() && cpu.SupportsCPUUsage());

  readResult = -13;
  Advance();
  assert(cpu.GetCoresUsageString().empty() && !cpu.SupportsCPUUsage() && warnings == 2);
  Advance();
  assert(cpu.GetCoresUsageString().empty() && warnings == 2);
  readResult = 0;
  Advance();
  assert(cpu.GetCoresUsageString().empty());
  Advance();
  Idle(0, 250000);
  Idle(1, 250000);
  assert(!cpu.GetCoresUsageString().empty() && cpu.GetUsedPercentage() == 50);
  failClock = true;
  Advance();
  assert(cpu.GetCoresUsageString().empty() && warnings == 3);
}

static void TestTimeFields()
{
  Reset();
  threads[0].userSeconds = 10;
  threads[0].userMicroseconds = 900000;
  CCPUInfoPS5 cpu;
  cpu.GetUsedPercentage();
  Advance();
  threads[0].userSeconds = 11;
  threads[0].userMicroseconds = 25000; // user time advanced by 125000 microseconds
  Idle(0, 125000);
  Idle(1, 250000);
  assert(cpu.GetUsedPercentage() == 50); // both user and system time contribute
  ExpectCore(cpu, 0, 50.0);

  Advance();
  wallOffset -= 500000; // force an identical monotonic timestamp on a fresh read
  assert(cpu.GetCoresUsageString().empty() && !cpu.SupportsCPUUsage());
  Advance();
  Idle(0, 250000);
  Idle(1, 250000);
  assert(!cpu.GetCoresUsageString().empty() && cpu.GetUsedPercentage() == 50);
  Advance();
  wallOffset -= 1000000; // a backward timestamp also discards the baseline
  assert(cpu.GetCoresUsageString().empty() && warnings == 0);
}

static void TestDiscovery()
{
  Reset();
  hardwareCount = 16;
  threads.clear();
  names.clear();
  for (int id = 0; id < 16; ++id)
    Add(id == 13 ? "SceIdleCpuRv" : "SceIdleCpu" + std::to_string(id), 1ULL << 40);
  CCPUInfoPS5 all;
  all.GetUsedPercentage();
  Advance();
  for (size_t id = 0; id < 16; ++id)
    Idle(id, 250000);
  std::reverse(threads.begin(), threads.end()); // records are not a CPU-indexed array
  assert(all.GetUsedPercentage() == 50);
  for (int id = 0; id < 16; ++id)
    ExpectCore(all, id, 50.0);
  assert(all.GetCoresUsageString().find("#13:  50%") != std::string::npos);
  assert(nameReads == 16);

  Reset();
  names[1] = "SceIdleCpu1garbage"; // require an exact name, not sscanf's prefix match
  CCPUInfoPS5 partial;
  partial.GetUsedPercentage();
  Advance();
  Idle(0, 125000);
  assert(partial.GetUsedPercentage() == 75 && partial.SupportsCPUUsage());
  assert(partial.GetCPUCount() == 2 && !partial.HasCoreId(1));
  assert(partial.GetCoresUsageString() == "#0:  75%");

  Reset();
  nameResult = -13;
  CCPUInfoPS5 hidden;
  assert(hidden.GetCoresUsageString().empty() && warnings == 1);
  nameResult = 0;
  Advance(5000000);
  assert(hidden.GetCoresUsageString().empty()); // rediscovery must establish a baseline
  Advance();
  assert(!hidden.GetCoresUsageString().empty() && hidden.SupportsCPUUsage());

  // Replace a missing idle thread; it must not inherit the previous thread's runtime.
  names[0] = "SceWorker";
  Add("SceIdleCpu0", 0);
  threads.erase(threads.begin());
  Advance();
  hidden.GetUsedPercentage();
  assert(!hidden.HasCoreId(0));
  Advance(5000000);
  assert(hidden.GetCoresUsageString().empty());
  Advance();
  Idle(threads.size() - 1, 250000);
  hidden.GetUsedPercentage();
  ExpectCore(hidden, 0, 50.0);
}

static void TestReservedCPU()
{
  for (const bool reservedFirst : {false, true})
  {
    Reset();
    hardwareCount = 16;
    threads.clear();
    names.clear();
    if (reservedFirst)
      Add("SceIdleCpuRv");
    for (int id = 0; id < 16; ++id)
      Add("SceIdleCpu" + std::to_string(id));
    if (!reservedFirst)
      Add("SceIdleCpuRv");

    CCPUInfoPS5 cpu;
    assert(cpu.GetCoresUsageString().empty() && warnings == 0);
    Advance();
    for (size_t index = 0; index < threads.size(); ++index)
      Idle(index, names[index] == "SceIdleCpuRv" ? 125000 : 250000);
    assert(!cpu.GetCoresUsageString().empty() && cpu.SupportsCPUUsage());
    ExpectCore(cpu, 13, 75.0);
    assert(cpu.GetUsedPercentage() == 51 && warnings == 0);
    const int lookups = nameReads;
    std::reverse(threads.begin(), threads.end());
    Advance();
    cpu.GetUsedPercentage();
    ExpectCore(cpu, 13, 100.0);
    assert(nameReads == lookups && warnings == 0);
  }

  Reset();
  hardwareCount = 16;
  names[1] = "SceIdleCpu13";
  CCPUInfoPS5 noReservedThread;
  noReservedThread.GetUsedPercentage();
  Advance();
  noReservedThread.GetUsedPercentage();
  assert(noReservedThread.SupportsCPUUsage() && !noReservedThread.HasCoreId(13));
  assert(warnings == 0);
}

static void TestCapacityAndValidation()
{
  Reset();
  CCPUInfoPS5 growing;
  growing.GetUsedPercentage();
  for (int i = 0; i < 4000; ++i)
    Add("SceWorker");
  Advance();
  assert(growing.GetCoresUsageString().empty() && warnings == 1);
  Advance();
  assert(growing.GetCoresUsageString().empty() && queries == 2 && lastCapacity >= 4003);
  Advance();
  assert(!growing.GetCoresUsageString().empty());

  for (const int count : {0, -1, 65537})
  {
    Reset();
    queryCount = count;
    CCPUInfoPS5 invalid;
    assert(invalid.GetCoresUsageString().empty() && warnings == 1 && reads == 0);
  }
  for (const int count : {0, -1, 65537})
  {
    Reset();
    readCount = count;
    CCPUInfoPS5 invalid;
    assert(invalid.GetCoresUsageString().empty() && warnings == 1 && nameReads == 0);
  }
  for (const int64_t usec : {-1, 1000000})
  {
    Reset();
    threads[0].systemMicroseconds = usec;
    CCPUInfoPS5 invalid;
    assert(invalid.GetCoresUsageString().empty() && warnings == 1);
    assert(lastWarning.find("invalid idle-thread time or ID") != std::string::npos);
    assert(lastWarning.find("SceIdleCpu0") != std::string::npos);
    assert(lastWarning.find("PID") != std::string::npos);
    assert(lastWarning.find("user") != std::string::npos);
    assert(lastWarning.find("system") != std::string::npos);
    Advance();
    invalid.GetUsedPercentage();
    assert(warnings == 1);
  }
  Reset();
  threads[0].userSeconds = threads[0].systemSeconds = std::numeric_limits<int64_t>::max();
  CCPUInfoPS5 overflow;
  assert(overflow.GetCoresUsageString().empty() && warnings == 1);
  assert(lastWarning.find("idle-thread time overflow") != std::string::npos);

  Reset();
  hardwareCount = -1;
  CCPUInfoPS5 fallback;
  assert(fallback.GetCPUCount() == 16);

  Reset();
  queryCount = 65536;
  CCPUInfoPS5 maximum;
  maximum.GetUsedPercentage();
  assert(lastCapacity == 65536);

  Reset();
  threads[0].userSeconds = -1;
  CCPUInfoPS5 negative;
  assert(negative.GetCoresUsageString().empty() && warnings == 1);

  Reset();
  Add("SceIdleCpu0");
  CCPUInfoPS5 duplicate;
  assert(duplicate.GetCoresUsageString().empty() && warnings == 1);
  assert(lastWarning.find("duplicate idle-thread mapping") != std::string::npos);
  assert(lastWarning.find("existing TID") != std::string::npos);

  Reset();
  names[0] = "SceIdleCpu";
  names[1] = "SceIdleCpu999999999999999999";
  Add("SceIdleCpu-1");
  Add("SceIdleCpu16");
  CCPUInfoPS5 badNames;
  assert(badNames.GetCoresUsageString().empty() && warnings == 1);
}

int main()
{
  TestSampling();
  TestFailures();
  TestTimeFields();
  TestDiscovery();
  TestReservedCPU();
  TestCapacityAndValidation();
  puts("PASS: idle-thread units, 16 CPUs, reserved lane, partial visibility, retries, "
       "rediscovery, clock gaps, counter resets, bounds and GUI usage strings");
}
"""


class TestCPUInfo(unittest.TestCase):
    def test_idle_thread_snapshots(self):
        with tempfile.TemporaryDirectory(prefix="ps5-cpu-info-") as directory:
            work = Path(directory)
            for name, text in STUBS.items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            probe = work / "probe.cpp"
            probe.write_text(PROBE)
            binary = work / "probe"
            subprocess.run(
                [os.environ.get("CXX", "clang++-18"), "-std=c++20",
                 "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
                 "-I", str(work), "-I", str(ROOT / "xbmc"), str(probe),
                 str(ROOT / "xbmc/platform/ps5/CPUInfoPS5.cpp"),
                 str(ROOT / "xbmc/utils/CPUInfo.cpp"),
                 "-Wl,--wrap=clock_gettime", "-o", str(binary)],
                check=True,
            )
            subprocess.run([binary], check=True)


if __name__ == "__main__":
    unittest.main()
