/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "AsyncLogSink.h"

#include <chrono>
#include <exception>
#include <string>
#include <utility>

#include <spdlog/pattern_formatter.h>

extern "C" int sceKernelDebugOutText(int channel, const char* text);

namespace
{
constexpr auto FLUSH_INTERVAL = std::chrono::seconds(1);

void ReportFailure(const std::exception& error)
{
  // Never recurse through CLog when its file sink has failed.
  const std::string message =
      std::string("[kodi] PS5 file logging failed: ") + error.what() + "\n";
  sceKernelDebugOutText(0, message.c_str());
}
} // namespace

CPS5AsyncLogSink::CPS5AsyncLogSink(std::shared_ptr<spdlog::sinks::sink> backend)
  : m_backend(std::move(backend)), m_formatter(std::make_shared<spdlog::pattern_formatter>())
{
  m_thread = std::thread([this] { Process(); });
}

CPS5AsyncLogSink::~CPS5AsyncLogSink()
{
  {
    std::lock_guard lock(m_mutex);
    m_stopping = true;
  }
  m_ready.notify_one();
  m_thread.join();
}

void CPS5AsyncLogSink::log(const spdlog::details::log_msg& message)
{
  {
    std::lock_guard lock(m_mutex);
    // Keep the loss report after all accepted messages and before new arrivals.
    if (m_dropped != 0 || m_queue.size() >= MAX_QUEUE_MESSAGES ||
        message.payload.size() > MAX_QUEUE_BYTES - m_queuedBytes ||
        message.logger_name.size() > MAX_QUEUE_BYTES - m_queuedBytes - message.payload.size())
    {
      ++m_dropped;
    }
    else
    {
      m_queue.push_back({spdlog::details::log_msg_buffer(message), m_formatter});
      m_queuedBytes += message.payload.size() + message.logger_name.size();
    }
  }
  m_ready.notify_one();
}

void CPS5AsyncLogSink::flush()
{
  // spdlog requests a flush on every debug-or-higher message. The worker
  // coalesces these into periodic flushes; destruction drains and flushes.
  m_ready.notify_one();
}

void CPS5AsyncLogSink::set_pattern(const std::string& pattern)
{
  set_formatter(std::make_unique<spdlog::pattern_formatter>(pattern));
}

void CPS5AsyncLogSink::set_formatter(std::unique_ptr<spdlog::formatter> formatter)
{
  std::lock_guard lock(m_mutex);
  m_formatter = std::move(formatter);
}

void CPS5AsyncLogSink::FlushBackend()
{
  try
  {
    m_backend->flush();
  }
  catch (const std::exception& error)
  {
    ReportFailure(error);
  }
}

void CPS5AsyncLogSink::Process()
{
  using Clock = std::chrono::steady_clock;
  auto nextFlush = Clock::now() + FLUSH_INTERVAL;
  bool dirty{false};
  std::shared_ptr<spdlog::formatter> activeFormatter;
  std::unique_lock lock(m_mutex);
  for (;;)
  {
    if (dirty && Clock::now() >= nextFlush)
    {
      lock.unlock();
      FlushBackend();
      nextFlush = Clock::now() + FLUSH_INTERVAL;
      dirty = false;
      lock.lock();
    }

    Message next;
    std::string overflow;
    if (!m_queue.empty())
    {
      next = std::move(m_queue.front());
      m_queue.pop_front();
      m_queuedBytes -= next.message.payload.size() + next.message.logger_name.size();
    }
    else if (m_dropped != 0)
    {
      overflow = "PS5 file log queue overflow: dropped " + std::to_string(m_dropped) +
                 " log messages";
      const spdlog::details::log_msg message("general", spdlog::level::warn, overflow);
      next = {spdlog::details::log_msg_buffer(message), m_formatter};
      m_dropped = 0;
    }
    else if (m_stopping)
    {
      lock.unlock();
      FlushBackend();
      return;
    }
    else
    {
      const auto ready = [this] { return m_stopping || !m_queue.empty() || m_dropped != 0; };
      if (dirty)
        m_ready.wait_until(lock, nextFlush, ready);
      else
        m_ready.wait(lock, ready);
      continue;
    }

    lock.unlock();
    if (!overflow.empty())
    {
      const std::string message = "[kodi] " + overflow + "\n";
      sceKernelDebugOutText(0, message.c_str());
    }
    try
    {
      if (activeFormatter != next.formatter)
      {
        m_backend->set_formatter(next.formatter->clone());
        activeFormatter = next.formatter;
      }
      m_backend->log(next.message);
      dirty = true;
    }
    catch (const std::exception& error)
    {
      ReportFailure(error);
    }
    lock.lock();
  }
}
