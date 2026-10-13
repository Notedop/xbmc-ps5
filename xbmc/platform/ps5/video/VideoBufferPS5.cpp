/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "VideoBufferPS5.h"
#include "VideoTimingPS5.h"

#include "utils/log.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <map>
#include <mutex>
#include <string>

extern "C"
{
// patches/ps5-opengl/kodi-additions.py (zero-copy part). Weak: a GL driver
// built without them leaves these null, and Kodi keeps copying frames.
void* ps5_opengl_memory_image_create(void* data, unsigned width, unsigned height,
                                     unsigned stride, unsigned components,
                                     unsigned bytes_per_component) __attribute__((weak));
void ps5_opengl_memory_image_destroy(void* image) __attribute__((weak));
}

namespace KODI::PLATFORM::PS5
{
namespace
{
struct TimingState
{
  std::chrono::steady_clock::time_point last;
  std::chrono::steady_clock::time_point intervalStart;
  double lastPtsMs = -1.0;
  uint64_t sequence = 0;
  uint64_t samples = 0;
  uint64_t gaps = 0;
  uint64_t slowWork = 0;
  uint64_t ptsJumps = 0;
  double maxGapMs = 0.0;
  double maxWorkMs = -1.0;
  double maxPtsDeltaMs = 0.0;
};
} // namespace

void TraceVideoTimingPS5(std::string_view stage,
                         double ptsMs,
                         double frameDurationMs,
                         double workMs,
                         std::string_view detail)
{
  static const bool enabled = std::getenv("KODI_PS5_VIDEO_DEBUG") != nullptr;
  if (!enabled)
    return;

  static std::mutex mutex;
  static std::map<std::string, TimingState> states;
  const auto now = std::chrono::steady_clock::now();
  const double nowMs =
      std::chrono::duration<double, std::milli>(now.time_since_epoch()).count();

  TimingState summary;
  double intervalMs = 0.0;
  {
    std::lock_guard<std::mutex> lock(mutex);
    TimingState& state = states[std::string(stage)];
    if (state.sequence == 0)
      state.intervalStart = now;
    const double gapMs = state.sequence == 0
                             ? 0.0
                             : std::chrono::duration<double, std::milli>(now - state.last).count();
    const double ptsDeltaMs =
        ptsMs >= 0.0 && state.lastPtsMs >= 0.0 ? ptsMs - state.lastPtsMs : 0.0;
    const double gapLimitMs =
        frameDurationMs > 0.0 ? std::max(frameDurationMs * 1.5, frameDurationMs + 10.0) : 0.0;
    ++state.samples;
    if (gapLimitMs > 0.0 && gapMs > gapLimitMs)
      ++state.gaps;
    if (workMs >= 0.0 && frameDurationMs > 0.0 && workMs > frameDurationMs)
      ++state.slowWork;
    if (std::abs(ptsDeltaMs) > 500.0)
      ++state.ptsJumps;
    state.maxGapMs = std::max(state.maxGapMs, gapMs);
    state.maxWorkMs = std::max(state.maxWorkMs, workMs);
    if (std::abs(ptsDeltaMs) > std::abs(state.maxPtsDeltaMs))
      state.maxPtsDeltaMs = ptsDeltaMs;
    state.last = now;
    state.lastPtsMs = ptsMs;
    ++state.sequence;

    intervalMs = std::chrono::duration<double, std::milli>(now - state.intervalStart).count();
    if (intervalMs < 5000.0)
      return;
    summary = state;
    state.intervalStart = now;
    state.samples = state.gaps = state.slowWork = state.ptsJumps = 0;
    state.maxGapMs = 0.0;
    state.maxWorkMs = -1.0;
    state.maxPtsDeltaMs = 0.0;
  }

  // Aggregate across frames and log outside the lock to avoid stalling other video stages.
  CLog::Log(LOGINFO,
            "PS5 video timing (kodi-video-debug): t={:.3f}s stage={} seq={} samples={} in={:.2f}s "
            "rate={:.1f}/s gaps={} maxGap={:.2f}ms expected={:.2f}ms slowWork={} "
            "maxWork={:.2f}ms ptsJumps={} maxPtsDelta={:.2f}ms pts={:.3f}ms latest: {}",
            nowMs / 1000.0, stage, summary.sequence, summary.samples, intervalMs / 1000.0,
            summary.samples * 1000.0 / intervalMs, summary.gaps, summary.maxGapMs,
            frameDurationMs, summary.slowWork, summary.maxWorkMs, summary.ptsJumps,
            summary.maxPtsDeltaMs, ptsMs, detail);
}

bool IsZeroCopyAvailable()
{
  return ps5_opengl_memory_image_create != nullptr && ps5_opengl_memory_image_destroy != nullptr;
}

void* CreateMemoryImage(const void* data, unsigned width, unsigned height, unsigned stride,
                        unsigned components, unsigned bytesPerComponent)
{
  if (!IsZeroCopyAvailable())
    return nullptr;
  return ps5_opengl_memory_image_create(const_cast<void*>(data), width, height, stride,
                                        components, bytesPerComponent);
}

void DestroyMemoryImage(void* image)
{
  if (image && IsZeroCopyAvailable())
    ps5_opengl_memory_image_destroy(image);
}

void CVideoBufferPS5::Set(const std::shared_ptr<CVideoDec2>& decoder,
                          const VideoDec2Picture& picture, AVPixelFormat format)
{
  m_decoder = decoder;
  m_frameIndex = picture.frameIndex;
  m_data = picture.data;
  m_pitch = picture.pitch;
  m_codedHeight = picture.height;
  m_chromaOffsetHeight = picture.chromaOffsetHeight;
  m_chromaHeight = picture.chromaHeight;
  m_bitDepth = picture.bitDepth;
  m_pixFormat = format;
  m_diagnosticPicture = picture;
  m_diagnosticPts = -1.0;
  m_contentRendered = false;
}

void CVideoBufferPS5::TraceContent(const char* stage, double pts)
{
  if (m_decoder && m_diagnosticPicture.diagnosticSequence != 0)
  {
    m_diagnosticPts = pts;
    m_decoder->TraceContent(stage, m_diagnosticPicture, pts);
  }
}

void CVideoBufferPS5::TraceRenderContent()
{
  if (!m_contentRendered)
  {
    m_contentRendered = true;
    TraceContent("render", m_diagnosticPts);
  }
}

void CVideoBufferPS5::ReturnFrame()
{
  TraceContent("release", m_diagnosticPts);
  if (m_decoder && m_frameIndex >= 0)
    m_decoder->ReleaseFrame(m_frameIndex);
  m_decoder.reset(); // the last buffer out keeps the decoder's memory alive until here
  m_frameIndex = -1;
  m_data = nullptr;
  m_diagnosticPicture = {};
}

void CVideoBufferPS5::GetPlanes(uint8_t* (&planes)[YuvImage::MAX_PLANES])
{
  // a CPU reader (screenshot, a copying renderer): the decoder wrote the
  // frame behind the CPU's back, and the frame is not flushed per picture
  // in zero-copy mode
  if (m_data)
    CVideoDec2::InvalidateForRead(
        m_data, static_cast<size_t>(m_pitch) * (m_chromaOffsetHeight + m_chromaHeight));
  planes[0] = const_cast<uint8_t*>(m_data);
  planes[1] = m_data ? const_cast<uint8_t*>(Chroma()) : nullptr;
  planes[2] = nullptr;
}

void CVideoBufferPS5::GetStrides(int (&strides)[YuvImage::MAX_PLANES])
{
  strides[0] = static_cast<int>(m_pitch);
  strides[1] = static_cast<int>(m_pitch);
  strides[2] = 0;
}

CVideoBufferPoolPS5::~CVideoBufferPoolPS5()
{
  for (CVideoBufferPS5* buffer : m_all)
    delete buffer;
}

CVideoBuffer* CVideoBufferPoolPS5::Get()
{
  std::unique_lock<std::mutex> lock(m_mutex);
  CVideoBufferPS5* buffer;
  if (!m_free.empty())
  {
    buffer = m_all[m_free.back()];
    m_free.pop_back();
  }
  else
  {
    buffer = new CVideoBufferPS5(static_cast<int>(m_all.size()));
    m_all.push_back(buffer);
  }
  lock.unlock();
  buffer->Acquire(GetPtr());
  return buffer;
}

void CVideoBufferPoolPS5::Return(int id)
{
  std::lock_guard<std::mutex> lock(m_mutex);
  if (id < 0 || id >= static_cast<int>(m_all.size()))
    return;
  m_all[id]->ReturnFrame();
  m_free.push_back(id);
}

} // namespace KODI::PLATFORM::PS5
