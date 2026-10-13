/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PS5InterfaceForCLog.h"

#include <condition_variable>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <utility>

#include <spdlog/details/null_mutex.h>
#include <spdlog/sinks/base_sink.h>
#include <spdlog/sinks/dist_sink.h>

extern "C" int sceKernelDebugOutText(int channel, const char* text);

namespace
{
// The distribution sink serialises formatting; the queue also has a consumer thread.
class CKlogSink : public spdlog::sinks::base_sink<spdlog::details::null_mutex>
{
public:
  CKlogSink() : m_thread([this] { Process(); }) {}

  ~CKlogSink() override
  {
    {
      std::lock_guard<std::mutex> lock(m_queueMutex);
      m_stopping = true;
    }
    m_ready.notify_one();
    m_thread.join();
  }

protected:
  void sink_it_(const spdlog::details::log_msg& msg) override
  {
    spdlog::memory_buf_t formatted;
    formatter_->format(msg, formatted);
    std::string line("[kodi] ");
    line.append(formatted.data(), formatted.size());
    {
      std::lock_guard<std::mutex> lock(m_queueMutex);
      // Reject whole messages. Once overflow starts, preserve its position by
      // rejecting new arrivals until the worker emits the loss report.
      if (m_dropped != 0 || m_queue.size() >= MAX_QUEUE_MESSAGES ||
          line.size() > MAX_QUEUE_BYTES - m_queuedBytes)
      {
        ++m_dropped;
      }
      else
      {
        m_queue.emplace_back(std::move(line));
        m_queuedBytes += m_queue.back().size();
      }
    }
    m_ready.notify_one();
  }

  void flush_() override
  {
    // CLog flushes on every debug-or-higher message. Kernel output has no
    // userspace buffer to flush; waiting here would make logging synchronous.
    // Destruction drains the queue and joins the worker instead.
    m_ready.notify_one();
  }

private:
  static constexpr size_t MAX_QUEUE_MESSAGES = 1024;
  static constexpr size_t MAX_QUEUE_BYTES = 1024 * 1024;

  static void Write(const std::string& line)
  {
    // keep each call well below the kernel's debug-print buffer
    for (size_t pos = 0; pos < line.size(); pos += 900)
    {
      std::string part = line.substr(pos, 900);
      if (part.back() != '\n')
        part.push_back('\n');
      sceKernelDebugOutText(0, part.c_str());
    }
  }

  void Process()
  {
    std::unique_lock<std::mutex> lock(m_queueMutex);
    for (;;)
    {
      m_ready.wait(lock, [this] { return m_stopping || !m_queue.empty() || m_dropped != 0; });
      std::string line;
      if (!m_queue.empty())
      {
        line = std::move(m_queue.front());
        m_queue.pop_front();
        m_queuedBytes -= line.size();
      }
      else if (m_dropped != 0)
      {
        line = "[kodi] Kernel log queue overflow: dropped " + std::to_string(m_dropped) +
               " log messages\n";
        m_dropped = 0;
      }
      else if (m_stopping)
      {
        return;
      }
      lock.unlock();
      Write(line);
      lock.lock();
    }
  }

  std::mutex m_queueMutex;
  std::condition_variable m_ready;
  std::deque<std::string> m_queue;
  size_t m_queuedBytes{0};
  size_t m_dropped{0};
  bool m_stopping{false};
  std::thread m_thread;
};
} // namespace

std::unique_ptr<IPlatformLog> IPlatformLog::CreatePlatformLog()
{
  return std::make_unique<CPS5InterfaceForCLog>();
}

void CPS5InterfaceForCLog::AddSinks(
    std::shared_ptr<spdlog::sinks::dist_sink<std::mutex>> distributionSink) const
{
  distributionSink->add_sink(std::make_shared<CKlogSink>());
}
