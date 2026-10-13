/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "RendererPS5.h"

#include "VideoBufferPS5.h"
#include "VideoTimingPS5.h"
#include "cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodec.h"
#include "cores/VideoPlayer/VideoRenderers/RenderFactory.h"
#include "utils/GLUtils.h"
#include "utils/log.h"

#include <EGL/egl.h>

#include <algorithm>
#include <chrono>
#include <string>

using namespace KODI::PLATFORM::PS5;

namespace KODI::PLATFORM::PS5
{
VideoFrameStats& ps5_video_frame_stats()
{
  static VideoFrameStats s;
  return s;
}
} // namespace KODI::PLATFORM::PS5

CBaseRenderer* CRendererPS5::Create(CVideoBuffer* buffer)
{
  if (dynamic_cast<CVideoBufferPS5*>(buffer))
    return new CRendererPS5();
  return nullptr;
}

void CRendererPS5::Register()
{
  VIDEOPLAYER::CRendererFactory::RegisterRenderer("ps5", CRendererPS5::Create);
}

CRendererPS5::~CRendererPS5()
{
  for (int i = 0; i < NUM_BUFFERS; ++i)
    DeleteTexture(i);
  DeleteFrames();
  DeleteGpuTimer();
}

void CRendererPS5::RenderUpdate(
    int index, int index2, bool clear, unsigned int flags, unsigned int alpha)
{
  if (!m_debug)
  {
    CLinuxRendererGL::RenderUpdate(index, index2, clear, flags, alpha);
    return;
  }

  InitGpuTimer();
  GpuTimer& t = m_gpuTimer;
  const int slot = t.next;
  const bool newFrame = index != t.lastIndex;
  const bool timing = t.available && !t.pending[slot];
  if (timing)
    t.beginQuery(GL_TIME_ELAPSED, t.queries[slot]);
  CLinuxRendererGL::RenderUpdate(index, index2, clear, flags, alpha);
  if (timing)
  {
    t.endQuery(GL_TIME_ELAPSED);
    t.pending[slot] = true;
    t.next = (slot + 1) % GpuTimer::kQueries;
  }
  ++t.draws;
  if (newFrame)
  {
    ++t.newFrames;
    const double frameDurationMs =
        m_fps > 0.0f ? 1000.0 / static_cast<double>(m_fps) : 0.0;
    TraceVideoTimingPS5("render", -1.0, frameDurationMs, -1.0,
                        "buffer=" + std::to_string(index));
  }
  t.lastIndex = index;
  CollectGpuTimes();
}

void CRendererPS5::InitGpuTimer()
{
  GpuTimer& t = m_gpuTimer;
  if (t.initialised)
    return;
  t.initialised = true;
  t.genQueries = reinterpret_cast<GpuTimer::GenQueries>(eglGetProcAddress("glGenQueries"));
  t.deleteQueries = reinterpret_cast<GpuTimer::DeleteQueries>(eglGetProcAddress("glDeleteQueries"));
  t.beginQuery = reinterpret_cast<GpuTimer::BeginQuery>(eglGetProcAddress("glBeginQuery"));
  t.endQuery = reinterpret_cast<GpuTimer::EndQuery>(eglGetProcAddress("glEndQuery"));
  t.getQueryiv = reinterpret_cast<GpuTimer::GetQueryiv>(eglGetProcAddress("glGetQueryiv"));
  t.getQueryObjectiv =
      reinterpret_cast<GpuTimer::GetQueryObjectiv>(eglGetProcAddress("glGetQueryObjectiv"));
  t.getQueryObjectui64v =
      reinterpret_cast<GpuTimer::GetQueryObjectui64v>(eglGetProcAddress("glGetQueryObjectui64v"));
  if (!t.genQueries || !t.deleteQueries || !t.beginQuery || !t.endQuery || !t.getQueryiv ||
      !t.getQueryObjectiv || !t.getQueryObjectui64v)
  {
    CLog::Log(LOGWARNING, "CRendererPS5 (kodi-video-debug): no timer query functions: video draw GPU "
              "time not measured");
    return;
  }
  GLint bits = 0;
  t.getQueryiv(GL_TIME_ELAPSED, GL_QUERY_COUNTER_BITS, &bits);
  if (glGetError() != GL_NO_ERROR || bits <= 0)
  {
    CLog::Log(LOGWARNING, "CRendererPS5 (kodi-video-debug): the driver has no GPU timer ({} counter "
              "bits): video draw GPU time not measured", bits);
    return;
  }
  t.genQueries(GpuTimer::kQueries, t.queries);
  t.available = glGetError() == GL_NO_ERROR;
  CLog::Log(LOGINFO, "CRendererPS5 (kodi-video-debug): timing the video draw on the GPU ({} counter "
            "bits){}", bits, t.available ? "" : " - query creation failed");
}

void CRendererPS5::CollectGpuTimes()
{
  GpuTimer& t = m_gpuTimer;
  if (t.available)
  {
    for (int i = 0; i < GpuTimer::kQueries; ++i)
    {
      if (!t.pending[i])
        continue;
      GLint ready = 0;
      t.getQueryObjectiv(t.queries[i], GL_QUERY_RESULT_AVAILABLE, &ready);
      if (!ready)
        continue;
      GLuint64 ns = 0;
      t.getQueryObjectui64v(t.queries[i], GL_QUERY_RESULT, &ns);
      t.pending[i] = false;
      const double ms = static_cast<double>(ns) / 1e6;
      t.totalMs += ms;
      t.maxMs = std::max(t.maxMs, ms);
      ++t.timed;
    }
  }

  const int64_t now = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::steady_clock::now().time_since_epoch())
                          .count();
  if (t.windowStart == 0)
    t.windowStart = now;
  if (now - t.windowStart < 5000)
    return;
  const double seconds = static_cast<double>(now - t.windowStart) / 1000.0;
  if (t.timed > 0)
    CLog::Log(LOGINFO, "CRendererPS5 (kodi-video-debug): video draw on the GPU {:.2f} ms average / "
              "{:.2f} ms longest over {} of {} draws in {:.1f} s ({}x{} source)",
              t.totalMs / t.timed, t.maxMs, t.timed, t.draws, seconds, m_sourceWidth,
              m_sourceHeight);
  CLog::Log(LOGINFO, "CRendererPS5 (kodi-video-debug): {:.1f} new pictures/s shown for {:.3f} fps "
            "video ({} draws in {:.1f} s)",
            t.newFrames / seconds, m_fps, t.draws, seconds);
  t.draws = t.timed = t.newFrames = 0;
  t.totalMs = t.maxMs = 0.0;
  t.windowStart = now;
}

void CRendererPS5::DeleteGpuTimer()
{
  GpuTimer& t = m_gpuTimer;
  if (t.available && t.deleteQueries)
    t.deleteQueries(GpuTimer::kQueries, t.queries);
  t = GpuTimer{};
}

void CRendererPS5::DeleteFrames()
{
  for (auto& [key, frame] : m_frames)
  {
    if (frame.luma)
      glDeleteTextures(1, &frame.luma);
    if (frame.chroma)
      glDeleteTextures(1, &frame.chroma);
    DestroyMemoryImage(frame.lumaImage); // the textures keep their own reference
    DestroyMemoryImage(frame.chromaImage);
  }
  m_frames.clear();
}

bool CRendererPS5::Configure(const VideoPicture& picture, float fps, unsigned int orientation)
{
  m_imageTargetTexture2D =
      reinterpret_cast<ImageTargetTexture2D>(eglGetProcAddress("glEGLImageTargetTexture2DOES"));
  for (auto& fence : m_fences)
    fence = {};
  m_layoutLogged = false;
  if (m_debug)
    CLog::Log(LOGINFO, "CRendererPS5 (kodi-video-debug): Configure {}x{} @ {:.3f} fps, {} cached frames dropped",
              picture.iWidth, picture.iHeight, fps, m_frames.size());
  DeleteFrames(); // a new stream: frames may sit elsewhere now
  const bool ok = CLinuxRendererGL::Configure(picture, fps, orientation);
  // EGL images bind to 2D textures (the copying path uses rectangle textures)
  m_textureTarget = GL_TEXTURE_2D;
  CLog::Log(LOGINFO, "CRendererPS5: zero-copy video {}",
            m_imageTargetTexture2D ? "enabled" : "unavailable (no glEGLImageTargetTexture2DOES)");
  return ok;
}

bool CRendererPS5::CreateTexture(int index)
{
  CPictureBuffer& buf = m_buffers[index];
  YuvImage& im = buf.image;
  CYuvPlane(&planes)[YuvImage::MAX_PLANES] = buf.fields[0];

  DeleteTexture(index);
  im = {};
  for (auto& plane : planes)
    plane = {};
  im.height = m_sourceHeight;
  im.width = m_sourceWidth;
  im.cshift_x = 1;
  im.cshift_y = 1;
  planes[0].id = 1; // textures are attached per frame in UploadTexture
  // as the base renderer does: a picture still held in this buffer must be
  // attached again before it is rendered (after a reconfigure, e.g. the
  // refresh-rate switch for VRR, the ids above are placeholders)
  buf.loaded = false;
  if (m_debug)
    CLog::Log(LOGINFO, "CRendererPS5 (kodi-video-debug): CreateTexture {} ({}x{})", index, im.width,
              im.height);
  return true;
}

void CRendererPS5::DeleteTexture(int index)
{
  ReleaseBuffer(index);
  CPictureBuffer& buf = m_buffers[index];
  buf.fields[FIELD_FULL][0].id = 0;
  buf.fields[FIELD_FULL][1].id = 0;
  buf.fields[FIELD_FULL][2].id = 0;
}

bool CRendererPS5::UploadTexture(int index)
{
  CPictureBuffer& buf = m_buffers[index];
  auto* frame = dynamic_cast<CVideoBufferPS5*>(buf.videoBuffer);
  if (!frame || !frame->Luma() || !m_imageTargetTexture2D)
  {
    if (m_debug)
      CLog::Log(LOGWARNING, "CRendererPS5 (kodi-video-debug): buffer {} has {}", index,
                !frame ? "no zero-copy picture" : (!frame->Luma() ? "an empty picture" : "no EGL image function"));
    return false;
  }

  const unsigned bytes = frame->BitDepth() > 8 ? 2 : 1;
  frame->TraceRenderContent();
  const unsigned pitch = frame->Pitch();
  const unsigned rows = frame->CodedHeight();
  // texture widths from the pitch: the driver's linear rows are exactly
  // width x bytes rounded up to 256; only the visible part is sampled
  const unsigned lumaWidth = pitch / bytes;
  const unsigned chromaWidth = pitch / (2 * bytes);
  const unsigned chromaRows = frame->ChromaHeight();

  const FrameKey key{frame->Luma(), pitch, rows, bytes};
  auto it = m_frames.find(key);
  if (it == m_frames.end())
  {
    FrameTextures textures;
    textures.lumaImage = CreateMemoryImage(frame->Luma(), lumaWidth, rows, pitch, 1, bytes);
    textures.chromaImage =
        CreateMemoryImage(frame->Chroma(), chromaWidth, chromaRows, pitch, 2, bytes);
    if (!textures.lumaImage || !textures.chromaImage)
    {
      DestroyMemoryImage(textures.lumaImage);
      DestroyMemoryImage(textures.chromaImage);
      if (!m_failureLogged)
        CLog::Log(LOGERROR,
                  "CRendererPS5: the GL driver refused a texture over the decoder's frame "
                  "({} bytes per row, {} rows, {}-bit)",
                  pitch, rows, bytes * 8);
      m_failureLogged = true;
      return false;
    }
    glGenTextures(1, &textures.luma);
    glGenTextures(1, &textures.chroma);
    const std::pair<GLuint, void*> bind[] = {{textures.luma, textures.lumaImage},
                                             {textures.chroma, textures.chromaImage}};
    for (const auto& [texture, image] : bind)
    {
      glBindTexture(GL_TEXTURE_2D, texture);
      m_imageTargetTexture2D(GL_TEXTURE_2D, image);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    }
    glBindTexture(GL_TEXTURE_2D, 0);
    VerifyGLState();
    it = m_frames.emplace(key, textures).first;
    if (m_debug)
      CLog::Log(LOGINFO,
                "CRendererPS5 (kodi-video-debug): buffer {}: textures {} (luma) / {} (chroma) over frame "
                "{} ({}x{} texels, {}-bit)",
                index, textures.luma, textures.chroma, static_cast<const void*>(frame->Luma()),
                lumaWidth, rows, bytes * 8);
    if (m_frames.size() == 1)
      CLog::Log(LOGINFO,
                "CRendererPS5: first frame shown without copying ({} bytes per row, {} rows, "
                "{}-bit textures, {}-bit values)",
                pitch, rows, bytes * 8, bytes == 2 ? buf.m_srcBits : 8);
  }

  CYuvPlane(&planes)[3] = buf.fields[0];
  planes[0].texwidth = lumaWidth;
  planes[0].texheight = rows;
  planes[1].texwidth = chromaWidth;
  planes[1].texheight = chromaRows;
  planes[2].texwidth = chromaWidth;
  planes[2].texheight = chromaRows;
  for (auto& plane : planes)
  {
    plane.pixpertex_x = 1;
    plane.pixpertex_y = 1;
  }
  planes[0].id = it->second.luma;
  planes[1].id = it->second.chroma;
  planes[2].id = it->second.chroma;
  // Kodi's convention: the depth of the values inside the texture, not the
  // texture's width - 10 for samples in the lower 10 bits of 16-bit words
  // (the shader then scales by 65535/1023), 16 for upper-aligned ones. The
  // codec reports that as the picture's colorBits (buf.m_srcBits).
  buf.m_srcTextureBits = bytes == 2 ? buf.m_srcBits : 8;

  CalculateTextureSourceRects(index, 3);
  if (m_debug && !m_layoutLogged)
  {
    const CRect& yRect = planes[0].rect;
    const CRect& cRect = planes[1].rect;
    CLog::Log(LOGINFO,
              "CRendererPS5 (kodi-video-debug): first plane layout luma={}x{} chroma={}x{} "
              "offset={} bytes pitch={} source={}x{} Y rect {:.6f},{:.6f}..{:.6f},{:.6f} "
              "UV rect {:.6f},{:.6f}..{:.6f},{:.6f}",
              planes[0].texwidth, planes[0].texheight, planes[1].texwidth, planes[1].texheight,
              static_cast<size_t>(frame->Chroma() - frame->Luma()), pitch, m_sourceWidth,
              m_sourceHeight, yRect.x1, yRect.y1, yRect.x2, yRect.y2, cRect.x1, cRect.y1,
              cRect.x2, cRect.y2);
    m_layoutLogged = true;
  }
  return true;
}

void CRendererPS5::AfterRenderHook(int idx)
{
  // a frame has been drawn for this buffer: counts as presented
  ps5_video_frame_stats().presented.fetch_add(1, std::memory_order_relaxed);

  if (glIsSync(m_fences[idx]))
    glDeleteSync(m_fences[idx]);
  m_fences[idx] = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
}

bool CRendererPS5::NeedBuffer(int idx)
{
  // keep the frame from the decoder until the GPU has finished sampling it
  if (glIsSync(m_fences[idx]))
  {
    GLint state = 0;
    GLsizei length = 0;
    glGetSynciv(m_fences[idx], GL_SYNC_STATUS, 1, &length, &state);
    if (state != GL_SIGNALED)
      return true;
    glDeleteSync(m_fences[idx]);
    m_fences[idx] = {};
  }
  return false;
}

void CRendererPS5::ReleaseBuffer(int idx)
{
  if (glIsSync(m_fences[idx]))
  {
    glDeleteSync(m_fences[idx]);
    m_fences[idx] = {};
  }
  CLinuxRendererGL::ReleaseBuffer(idx);
}
