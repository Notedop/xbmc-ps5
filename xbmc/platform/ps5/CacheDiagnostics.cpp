/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "CacheDiagnostics.h"

#include "ServiceBroker.h"
#include "utils/log.h"

#include <string_view>

namespace KODI::PLATFORM::PS5::CACHE_DIAGNOSTICS
{
namespace
{
CTimings& GetTimings()
{
  static CTimings timings;
  return timings;
}

constexpr std::array<std::string_view, static_cast<size_t>(Stage::COUNT)> STAGE_NAMES{
    "encode",
    "write-open",
    "write-close",
    "db-read-wait",
    "db-read",
    "db-write-wait",
    "db-write",
    "db-transaction",
    "db-commit",
    "cache-job",
    "completion-handoff",
    "dispatch-to-start",
    "ui-frame-interval",
    "present"};
} // namespace

bool IsEnabled()
{
  return CServiceBroker::GetLogging().IsLogLevelLogged(LOGDEBUG);
}

void Record(Stage stage, Clock::duration elapsed)
{
  if (IsEnabled())
    GetTimings().Record(stage, elapsed);
}

void Report(Clock::time_point now, bool enabled)
{
  const auto snapshot = GetTimings().TakeSnapshot(now, enabled);
  if (!snapshot)
    return;

  // Only the GUI thread enqueues reports, after releasing the collector lock.
  for (size_t i = 0; i < snapshot->timings.size(); ++i)
  {
    const auto& timing = snapshot->timings[i];
    if (timing.count == 0)
      continue;
    CLog::Log(LOGDEBUG,
              "[kodi-ps5] Cache timing: stage={} window={:.1f}s count={} avg={:.2f}ms max={:.2f}ms "
              "slow(>={:.0f}ms)={}",
              STAGE_NAMES[i], snapshot->seconds, timing.count, timing.totalMs / timing.count,
              timing.maxMs, SlowThresholdMs(static_cast<Stage>(i)), timing.slow);
  }
}
} // namespace KODI::PLATFORM::PS5::CACHE_DIAGNOSTICS
