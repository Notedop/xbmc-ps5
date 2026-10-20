/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>

namespace KODI::PLATFORM::PS5::CACHE_DIAGNOSTICS
{
using Clock = std::chrono::steady_clock;

enum class Stage
{
  ENCODE,
  WRITE,
  CLOSE,
  DATABASE_READ_WAIT,
  DATABASE_READ,
  DATABASE_WRITE_WAIT,
  DATABASE_WRITE,
  TRANSACTION,
  COMMIT,
  JOB,
  COMPLETION,
  DISPATCH,
  FRAME_INTERVAL,
  PRESENT,
  COUNT
};

struct Timing
{
  uint64_t count{0};
  double totalMs{0.0};
  double maxMs{0.0};
  uint64_t slow{0};
};

struct Snapshot
{
  double seconds{0.0};
  std::array<Timing, static_cast<size_t>(Stage::COUNT)> timings{};
};

constexpr double SlowThresholdMs(Stage stage)
{
  return stage == Stage::FRAME_INTERVAL ? 50.0 : 100.0;
}

class CTimings
{
public:
  void Record(Stage stage, Clock::duration elapsed)
  {
    const double ms = std::chrono::duration<double, std::milli>(elapsed).count();
    std::lock_guard lock(m_mutex);
    auto& timing = m_timings[static_cast<size_t>(stage)];
    ++timing.count;
    timing.totalMs += ms;
    timing.maxMs = std::max(timing.maxMs, ms);
    if (ms >= SlowThresholdMs(stage))
      ++timing.slow;
  }

  std::optional<Snapshot> TakeSnapshot(Clock::time_point now, bool enabled)
  {
    std::lock_guard lock(m_mutex);
    if (!enabled || !m_window)
    {
      m_timings = {};
      m_window = enabled ? std::optional{now} : std::nullopt;
      return std::nullopt;
    }
    const double seconds = std::chrono::duration<double>(now - *m_window).count();
    if (seconds < 5.0)
      return std::nullopt;

    Snapshot snapshot{seconds, m_timings};
    m_timings = {};
    m_window = now;
    return snapshot;
  }

private:
  std::mutex m_mutex;
  std::optional<Clock::time_point> m_window;
  std::array<Timing, static_cast<size_t>(Stage::COUNT)> m_timings{};
};

bool IsEnabled();
void Record(Stage stage, Clock::duration elapsed);
void Report(Clock::time_point now, bool enabled);

class CScopedTiming
{
public:
  explicit CScopedTiming(Stage stage) : m_stage(stage)
  {
    if (IsEnabled())
      m_start = Clock::now();
  }

  ~CScopedTiming() { Stop(); }

  CScopedTiming(const CScopedTiming&) = delete;
  CScopedTiming& operator=(const CScopedTiming&) = delete;

  void Stop()
  {
    if (m_start)
    {
      Record(m_stage, Clock::now() - *m_start);
      m_start.reset();
    }
  }

private:
  Stage m_stage;
  std::optional<Clock::time_point> m_start;
};
} // namespace KODI::PLATFORM::PS5::CACHE_DIAGNOSTICS
