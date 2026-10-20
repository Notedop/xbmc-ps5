/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "platform/ps5/AsyncLogSink.h"

#include <cassert>
#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <spdlog/logger.h>
#include <spdlog/pattern_formatter.h>
#include <spdlog/sinks/basic_file_sink.h>
#include <spdlog/sinks/dup_filter_sink.h>

using namespace std::chrono_literals;

namespace
{
std::mutex diagnosticsMutex;
std::vector<std::string> diagnostics;

class CProbeSink final : public spdlog::sinks::sink
{
public:
  struct Entry
  {
    std::string payload;
    std::string formatted;
    spdlog::log_clock::time_point time;
    size_t thread;
    spdlog::level::level_enum level;
    std::string logger;
  };

  void log(const spdlog::details::log_msg& message) override
  {
    std::unique_lock lock(m_mutex);
    m_entered = true;
    m_ready.notify_all();
    m_ready.wait(lock, [this] { return !m_blocked; });
    if (m_failWrites)
      throw std::runtime_error("test write failure");
    spdlog::memory_buf_t formatted;
    m_formatter->format(message, formatted);
    m_entries.push_back({std::string(message.payload.data(), message.payload.size()),
                         std::string(formatted.data(), formatted.size()), message.time,
                         message.thread_id, message.level,
                         std::string(message.logger_name.data(), message.logger_name.size())});
    m_ready.notify_all();
  }

  void flush() override
  {
    std::unique_lock lock(m_mutex);
    ++m_flushes;
    m_ready.notify_all();
    m_ready.wait(lock, [this] { return !m_flushBlocked; });
    if (m_failFlushes)
      throw std::runtime_error("test flush failure");
  }

  void set_pattern(const std::string& pattern) override
  {
    set_formatter(std::make_unique<spdlog::pattern_formatter>(pattern));
  }

  void set_formatter(std::unique_ptr<spdlog::formatter> formatter) override
  {
    std::lock_guard lock(m_mutex);
    m_formatter = std::move(formatter);
  }

  void Block()
  {
    std::lock_guard lock(m_mutex);
    m_blocked = true;
  }

  void Release()
  {
    std::lock_guard lock(m_mutex);
    m_blocked = false;
    m_flushBlocked = false;
    m_ready.notify_all();
  }

  void WaitForWrite()
  {
    std::unique_lock lock(m_mutex);
    assert(m_ready.wait_for(lock, 3s, [this] { return m_entered; }));
  }

  void BlockFlush()
  {
    std::lock_guard lock(m_mutex);
    m_flushBlocked = true;
  }

  void WaitForFlush()
  {
    std::unique_lock lock(m_mutex);
    assert(m_ready.wait_for(lock, 3s, [this] { return m_flushes != 0; }));
  }

  void WaitForEntries(size_t count)
  {
    std::unique_lock lock(m_mutex);
    assert(m_ready.wait_for(lock, 3s, [this, count] { return m_entries.size() >= count; }));
  }

  std::vector<Entry> Entries()
  {
    std::lock_guard lock(m_mutex);
    return m_entries;
  }

  size_t Flushes()
  {
    std::lock_guard lock(m_mutex);
    return m_flushes;
  }

  void FailWrites(bool fail)
  {
    std::lock_guard lock(m_mutex);
    m_failWrites = fail;
  }

  void FailFlushes()
  {
    std::lock_guard lock(m_mutex);
    m_failFlushes = true;
  }

private:
  std::mutex m_mutex;
  std::condition_variable m_ready;
  std::unique_ptr<spdlog::formatter> m_formatter;
  std::vector<Entry> m_entries;
  size_t m_flushes{0};
  bool m_entered{false};
  bool m_blocked{false};
  bool m_flushBlocked{false};
  bool m_failWrites{false};
  bool m_failFlushes{false};
};

void Log(CPS5AsyncLogSink& sink, const std::string& text)
{
  sink.log(spdlog::details::log_msg("test", spdlog::level::debug, text));
}

void TestBlockedBackend()
{
  const auto backend = std::make_shared<CProbeSink>();
  backend->Block();
  const auto sink = std::make_shared<CPS5AsyncLogSink>(backend);
  Log(*sink, "first");
  backend->WaitForWrite();
  const auto timestamp = spdlog::log_clock::now() - 10s;
  auto producer = std::async(std::launch::async, [&]
                            {
                              std::string name = "temporary logger";
                              std::string text = "temporary payload";
                              spdlog::details::log_msg message(
                                  timestamp, spdlog::source_loc{}, name, spdlog::level::err, text);
                              message.thread_id = 12345;
                              sink->log(message);
                              sink->flush();
                              sink->set_pattern("new: %v");
                              Log(*sink, "third");
                            });
  assert(producer.wait_for(1s) == std::future_status::ready);
  producer.get();
  backend->Release();
}

void TestMetadataAndFormatterOrder()
{
  const auto backend = std::make_shared<CProbeSink>();
  backend->Block();
  const auto timestamp = spdlog::log_clock::now() - 10s;
  {
    CPS5AsyncLogSink sink(backend);
    sink.set_pattern("old: %v");
    Log(sink, "first");
    backend->WaitForWrite();
    {
      std::string name = "temporary logger";
      std::string text = "temporary payload";
      spdlog::details::log_msg message(
          timestamp, spdlog::source_loc{}, name, spdlog::level::err, text);
      message.thread_id = 12345;
      sink.log(message);
    }
    sink.set_pattern("new: %v");
    Log(sink, "third");
    backend->Release();
  }
  const auto entries = backend->Entries();
  assert(entries.size() == 3);
  assert(entries[1].payload == "temporary payload" && entries[1].logger == "temporary logger");
  assert(entries[1].time == timestamp && entries[1].thread == 12345);
  assert(entries[1].level == spdlog::level::err);
  assert(entries[1].formatted == "old: temporary payload\n");
  assert(entries[2].formatted == "new: third\n");
  assert(backend->Flushes() != 0);
}

void TestBlockedFlush()
{
  const auto backend = std::make_shared<CProbeSink>();
  backend->BlockFlush();
  {
    CPS5AsyncLogSink sink(backend);
    Log(sink, "first");
    backend->WaitForFlush();
    auto producer = std::async(std::launch::async, [&]
                              {
                                Log(sink, "second");
                                sink.flush();
                              });
    assert(producer.wait_for(1s) == std::future_status::ready);
    producer.get();
    backend->Release();
  }
  const auto entries = backend->Entries();
  assert(entries.size() == 2 && entries[1].payload == "second");
}

void TestQueueLimits(bool byteLimit)
{
  const auto backend = std::make_shared<CProbeSink>();
  backend->Block();
  {
    CPS5AsyncLogSink sink(backend);
    Log(sink, "in flight");
    backend->WaitForWrite();
    if (byteLimit)
    {
      const std::string exactLimit(CPS5AsyncLogSink::MAX_QUEUE_BYTES - 4, 'x');
      Log(sink, exactLimit); // "test" logger name also occupies four bytes.
    }
    else
    {
      for (size_t i = 0; i < CPS5AsyncLogSink::MAX_QUEUE_MESSAGES; ++i)
        Log(sink, std::to_string(i));
    }
    Log(sink, "overflow");
    Log(sink, "also dropped");
    backend->Release();
  }
  const auto entries = backend->Entries();
  assert(entries.size() == (byteLimit ? 3 : CPS5AsyncLogSink::MAX_QUEUE_MESSAGES + 2));
  assert(entries.front().payload == "in flight");
  assert(entries.back().payload == "PS5 file log queue overflow: dropped 2 log messages");
  if (!byteLimit)
  {
    for (size_t i = 0; i < CPS5AsyncLogSink::MAX_QUEUE_MESSAGES; ++i)
      assert(entries[i + 1].payload == std::to_string(i));
  }
}

void TestOversizedMessage()
{
  const auto backend = std::make_shared<CProbeSink>();
  {
    CPS5AsyncLogSink sink(backend);
    Log(sink, std::string(CPS5AsyncLogSink::MAX_QUEUE_BYTES + 1, 'x'));
    backend->WaitForEntries(1);
    Log(sink, "recovered");
  }
  const auto entries = backend->Entries();
  assert(entries.size() == 2);
  assert(entries[0].payload == "PS5 file log queue overflow: dropped 1 log messages");
  assert(entries[1].payload == "recovered");
}

void TestConcurrentProducers()
{
  const auto backend = std::make_shared<CProbeSink>();
  {
    CPS5AsyncLogSink sink(backend);
    std::vector<std::thread> producers;
    for (int thread = 0; thread < 4; ++thread)
      producers.emplace_back([&, thread]
                             {
                               for (int i = 0; i < 100; ++i)
                                 Log(sink, std::to_string(thread) + ":" + std::to_string(i));
                             });
    for (auto& producer : producers)
      producer.join();
  }
  const auto entries = backend->Entries();
  assert(entries.size() == 400);
  for (int thread = 0; thread < 4; ++thread)
  {
    int expected = 0;
    const std::string prefix = std::to_string(thread) + ":";
    for (const auto& entry : entries)
      if (entry.payload.starts_with(prefix))
        assert(entry.payload == prefix + std::to_string(expected++));
    assert(expected == 100);
  }
}

void TestPeriodicFlushAndDuplicateFiltering()
{
  const auto backend = std::make_shared<CProbeSink>();
  const auto filter = std::make_shared<spdlog::sinks::dup_filter_sink_st>(10s);
  filter->add_sink(backend);
  {
    const auto sink = std::make_shared<CPS5AsyncLogSink>(filter);
    spdlog::logger logger("test", sink);
    logger.set_level(spdlog::level::debug);
    logger.flush_on(spdlog::level::debug);
    logger.debug("duplicate");
    logger.debug("duplicate");
    logger.debug("different");
    backend->WaitForFlush();
  }
  const auto entries = backend->Entries();
  assert(entries.size() == 3);
  assert(entries[0].payload == "duplicate");
  assert(entries[1].payload == "Skipped 1 duplicate messages..");
  assert(entries[2].payload == "different");
  assert(backend->Flushes() == 2); // Periodic flush followed by shutdown flush.
}

void TestBackendFailures()
{
  const auto backend = std::make_shared<CProbeSink>();
  backend->FailWrites(true);
  backend->FailFlushes();
  {
    CPS5AsyncLogSink sink(backend);
    Log(sink, "write fails");
    backend->WaitForWrite();
    backend->FailWrites(false);
    Log(sink, "recovered after write error");
  }
  const auto entries = backend->Entries();
  assert(entries.size() == 1 && entries[0].payload == "recovered after write error");
  std::lock_guard lock(diagnosticsMutex);
  bool writeReported{false};
  bool flushReported{false};
  for (const auto& message : diagnostics)
  {
    writeReported |= message.find("PS5 file logging failed: test write failure") != std::string::npos;
    flushReported |= message.find("PS5 file logging failed: test flush failure") != std::string::npos;
  }
  assert(writeReported && flushReported);
}

void TestFileDrain()
{
  const auto path = std::filesystem::temp_directory_path() /
                    ("ps5-async-logging-" +
                     std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) +
                     ".log");
  {
    const auto file = std::make_shared<spdlog::sinks::basic_file_sink_st>(path.string(), true);
    const auto filter = std::make_shared<spdlog::sinks::dup_filter_sink_st>(10s);
    filter->add_sink(file);
    {
      const auto sink = std::make_shared<CPS5AsyncLogSink>(filter);
      sink->set_pattern("%n: %v");
      spdlog::logger logger("file-test", sink);
      logger.info("first");
      logger.info("second");
    }
    std::ifstream input(path);
    std::string line;
    assert(std::getline(input, line) && line == "file-test: first");
    assert(std::getline(input, line) && line == "file-test: second");
    assert(!std::getline(input, line));
  }
  assert(std::filesystem::remove(path));
}
} // namespace

extern "C" int sceKernelDebugOutText(int, const char* text)
{
  std::lock_guard lock(diagnosticsMutex);
  diagnostics.emplace_back(text);
  return 0;
}

int main()
{
  TestBlockedBackend();
  TestMetadataAndFormatterOrder();
  TestBlockedFlush();
  TestQueueLimits(false);
  TestQueueLimits(true);
  TestOversizedMessage();
  TestConcurrentProducers();
  TestPeriodicFlushAndDuplicateFiltering();
  TestBackendFailures();
  TestFileDrain();
  std::cout << "PS5 asynchronous logging tests passed\n";
}
