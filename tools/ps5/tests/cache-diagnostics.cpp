/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "platform/ps5/CacheDiagnostics.h"

#include <cassert>
#include <iostream>
#include <thread>
#include <vector>

using namespace KODI::PLATFORM::PS5::CACHE_DIAGNOSTICS;
using namespace std::chrono_literals;

namespace
{
bool enabled{false};
unsigned int scopedSamples{0};
Stage lastStage{Stage::COUNT};

const Timing& GetTiming(const Snapshot& snapshot, Stage stage)
{
  return snapshot.timings[static_cast<size_t>(stage)];
}

void TestWindowsAndThresholds()
{
  CTimings timings;
  const Clock::time_point start{};
  assert(!timings.TakeSnapshot(start, true));
  timings.Record(Stage::ENCODE, 10ms);
  timings.Record(Stage::ENCODE, 100ms);
  timings.Record(Stage::ENCODE, 190ms);
  timings.Record(Stage::FRAME_INTERVAL, 49ms);
  timings.Record(Stage::FRAME_INTERVAL, 50ms);
  assert(!timings.TakeSnapshot(start + 4999ms, true));
  const auto snapshot = timings.TakeSnapshot(start + 5s, true);
  assert(snapshot && snapshot->seconds == 5.0);
  const auto& encode = GetTiming(*snapshot, Stage::ENCODE);
  assert(encode.count == 3 && encode.totalMs == 300.0 && encode.maxMs == 190.0);
  assert(encode.slow == 2);
  const auto& frames = GetTiming(*snapshot, Stage::FRAME_INTERVAL);
  assert(frames.count == 2 && frames.slow == 1);
  assert(GetTiming(*snapshot, Stage::COMMIT).count == 0);
  timings.Record(Stage::COMMIT, 250ms);
  const auto next = timings.TakeSnapshot(start + 12s, true);
  assert(next && next->seconds == 7.0);
  assert(GetTiming(*next, Stage::ENCODE).count == 0);
  assert(GetTiming(*next, Stage::COMMIT).count == 1);
}

void TestDebugToggle()
{
  CTimings timings;
  const Clock::time_point start{};
  assert(!timings.TakeSnapshot(start, false));
  assert(!timings.TakeSnapshot(start + 1s, true));
  timings.Record(Stage::WRITE, 1s);
  assert(!timings.TakeSnapshot(start + 2s, false));
  assert(!timings.TakeSnapshot(start + 20s, true));
  const auto snapshot = timings.TakeSnapshot(start + 25s, true);
  assert(snapshot && GetTiming(*snapshot, Stage::WRITE).count == 0);
}

void TestConcurrentWriters()
{
  CTimings timings;
  const Clock::time_point start{};
  assert(!timings.TakeSnapshot(start, true));
  std::vector<std::thread> writers;
  for (int i = 0; i < 4; ++i)
    writers.emplace_back(
        [&timings]()
        {
          for (int j = 0; j < 1000; ++j)
            timings.Record(Stage::DATABASE_READ_WAIT, 100ms);
        });
  for (auto& writer : writers)
    writer.join();
  const auto snapshot = timings.TakeSnapshot(start + 5s, true);
  assert(snapshot);
  const auto& timing = GetTiming(*snapshot, Stage::DATABASE_READ_WAIT);
  assert(timing.count == 4000 && timing.slow == 4000);
  assert(timing.totalMs == 400000.0 && timing.maxMs == 100.0);
}

void TestScopedTiming()
{
  {
    CScopedTiming disabled{Stage::ENCODE};
  }
  assert(scopedSamples == 0);
  enabled = true;
  {
    CScopedTiming timing{Stage::COMMIT};
    timing.Stop();
    timing.Stop();
  }
  assert(scopedSamples == 1 && lastStage == Stage::COMMIT);
  {
    CScopedTiming timing{Stage::WRITE};
  }
  assert(scopedSamples == 2 && lastStage == Stage::WRITE);
}
} // namespace

namespace KODI::PLATFORM::PS5::CACHE_DIAGNOSTICS
{
bool IsEnabled()
{
  return enabled;
}

void Record(Stage stage, Clock::duration elapsed)
{
  assert(elapsed >= Clock::duration::zero());
  ++scopedSamples;
  lastStage = stage;
}
} // namespace KODI::PLATFORM::PS5::CACHE_DIAGNOSTICS

int main()
{
  TestWindowsAndThresholds();
  TestDebugToggle();
  TestConcurrentWriters();
  TestScopedTiming();
  std::cout << "PASS: cache timing aggregation, thresholds, reset, debug gating and concurrency\n";
}
