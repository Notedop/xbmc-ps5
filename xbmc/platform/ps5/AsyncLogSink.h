/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <condition_variable>
#include <cstddef>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include <spdlog/details/log_msg_buffer.h>
#include <spdlog/sinks/sink.h>

class CPS5AsyncLogSink final : public spdlog::sinks::sink
{
public:
  static constexpr size_t MAX_QUEUE_MESSAGES = 1024;
  static constexpr size_t MAX_QUEUE_BYTES = 1024 * 1024;

  explicit CPS5AsyncLogSink(std::shared_ptr<spdlog::sinks::sink> backend);
  ~CPS5AsyncLogSink() override;

  void log(const spdlog::details::log_msg& message) override;
  void flush() override;
  void set_pattern(const std::string& pattern) override;
  void set_formatter(std::unique_ptr<spdlog::formatter> formatter) override;

private:
  struct Message
  {
    spdlog::details::log_msg_buffer message;
    std::shared_ptr<spdlog::formatter> formatter;
  };

  void Process();
  void FlushBackend();

  const std::shared_ptr<spdlog::sinks::sink> m_backend;
  std::mutex m_mutex;
  std::condition_variable m_ready;
  std::deque<Message> m_queue;
  std::shared_ptr<spdlog::formatter> m_formatter;
  size_t m_queuedBytes{0};
  size_t m_dropped{0};
  bool m_stopping{false};
  std::thread m_thread;
};
