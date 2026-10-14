#include "adaptive-worker-fixture.h"

#include <chrono>
#include <cstdlib>
#include <future>
#include <iostream>

using namespace std::chrono_literals;

static void Require(bool condition, const char* message)
{
  if (!condition)
  {
    std::cerr << "FAIL: " << message << '\n';
    std::exit(1);
  }
}

static void WaitPhase(AdaptiveStream& stream, int phase)
{
  const auto deadline = std::chrono::steady_clock::now() + 2s;
  while (stream.phase != phase && std::chrono::steady_clock::now() < deadline)
    std::this_thread::yield();
  Require(stream.phase == phase, "worker did not reach expected wait");
}

static void Dispose(AdaptiveStream& stream)
{
  stream.Dispose();
}

int main()
{
  // The publisher must not update the predicate while its waiter holds the mutex.
  {
    AdaptiveStream::THREADDATA data;
    std::unique_lock lock(data.mutexWorker);
    std::promise<void> entered;
    auto request = std::async(std::launch::async, [&] {
      entered.set_value();
      data.RequestExit();
    });
    entered.get_future().wait();
    Require(request.wait_for(20ms) == std::future_status::timeout,
            "exit publication did not share the state-wait mutex");
    Require(!data.IsThreadExit(), "exit was published outside the wait mutex");
    lock.unlock();
    Require(request.wait_for(2s) == std::future_status::ready, "exit publisher stayed blocked");
    request.get();
    Require(data.IsThreadExit(), "exit request was not published");
  }

  for (int iteration = 0; iteration < 500; ++iteration)
  {
    // Shutdown while waiting on STOPPED state.
    AdaptiveStream state;
    state.thread_data_ = new AdaptiveStream::THREADDATA;
    state.thread_data_->Initialize(&state);
    state.thread_data_->StopDownloads();
    state.start = true;
    WaitPhase(state, 1);
    Dispose(state);
    Require(state.finished, "state-wait worker failed to exit");

    // Shutdown while waiting for a segment: exit must precede the queue wake.
    AdaptiveStream segment;
    segment.thread_data_ = new AdaptiveStream::THREADDATA;
    segment.thread_data_->Initialize(&segment);
    segment.start = true;
    WaitPhase(segment, 2);
    Dispose(segment);
    Require(segment.finished, "segment-wait worker failed to exit");
    Require(segment.bufferAccesses == 0, "worker touched buffers during disposal");
  }

  // Exit between the fast predicate check and acquisition of the download mutex.
  AdaptiveStream race;
  race.pauseBeforeDownload = true;
  race.m_segBuffers.Reset();
  race.thread_data_ = new AdaptiveStream::THREADDATA;
  race.thread_data_->Initialize(&race);
  race.start = true;
  WaitPhase(race, 3);
  race.thread_data_->RequestExit();
  race.m_segBuffers.Reset();
  race.resumeDownload = true;
  delete race.thread_data_;
  race.thread_data_ = nullptr;
  Require(race.finished, "download-acquisition race failed to exit");
  Require(race.bufferAccesses == 0, "worker accessed reset buffers after exit publication");
  std::cout << "PASS: synchronized exit, both worker waits, 1000 joins and buffer-reset race\n";
}
