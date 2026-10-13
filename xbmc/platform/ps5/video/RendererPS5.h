/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

/*
 * Zero-copy renderer for the PS5 hardware decoder (kodi-zerocopy): the GL
 * textures lie over the decoder's frames (driver additions in
 * patches/ps5-opengl), so nothing is copied or uploaded per frame. Modelled
 * on Kodi's VAAPI GL renderer: NV12 shader, textures per frame, fences so a
 * frame returns to the decoder only after the GPU is done with it.
 */

#include "cores/VideoPlayer/VideoRenderers/LinuxRendererGL.h"

#include <cstdint>
#include <cstdlib>
#include <map>
#include <tuple>

class CRendererPS5 : public CLinuxRendererGL
{
public:
  CRendererPS5() = default;
  ~CRendererPS5() override;

  static CBaseRenderer* Create(CVideoBuffer* buffer);
  static void Register();

  bool Configure(const VideoPicture& picture, float fps, unsigned int orientation) override;
  void ReleaseBuffer(int idx) override;
  bool NeedBuffer(int idx) override;
  void RenderUpdate(
      int index, int index2, bool clear, unsigned int flags, unsigned int alpha) override;

protected:
  bool LoadShadersHook() override { return false; }
  bool RenderHook(int idx) override { return false; }
  void AfterRenderHook(int idx) override;
  bool UploadTexture(int index) override;
  void DeleteTexture(int index) override;
  bool CreateTexture(int index) override;
  EShaderFormat GetShaderFormat() override { return SHADER_NV12; }

private:
  struct FrameTextures
  {
    GLuint luma = 0;
    GLuint chroma = 0;
    void* lumaImage = nullptr;
    void* chromaImage = nullptr;
  };
  // a frame's memory never moves: its textures are made once and reused
  using FrameKey = std::tuple<const void*, unsigned, unsigned, unsigned>; // data, pitch, rows, bits
  std::map<FrameKey, FrameTextures> m_frames;
  GLsync m_fences[NUM_BUFFERS] = {};
  using ImageTargetTexture2D = void (*)(GLenum target, void* image);
  ImageTargetTexture2D m_imageTargetTexture2D = nullptr;
  bool m_failureLogged = false;
  bool m_layoutLogged = false;
  const bool m_debug = std::getenv("KODI_PS5_VIDEO_DEBUG") != nullptr; // per-buffer texture log

  // kodi-video-debug: GPU time of the video draw, from timer queries read back a
  // few frames later (never waited for)
  struct GpuTimer
  {
    using GenQueries = void (*)(GLsizei, GLuint*);
    using DeleteQueries = void (*)(GLsizei, const GLuint*);
    using BeginQuery = void (*)(GLenum, GLuint);
    using EndQuery = void (*)(GLenum);
    using GetQueryiv = void (*)(GLenum, GLenum, GLint*);
    using GetQueryObjectiv = void (*)(GLuint, GLenum, GLint*);
    using GetQueryObjectui64v = void (*)(GLuint, GLenum, GLuint64*);
    GenQueries genQueries = nullptr;
    DeleteQueries deleteQueries = nullptr;
    BeginQuery beginQuery = nullptr;
    EndQuery endQuery = nullptr;
    GetQueryiv getQueryiv = nullptr;
    GetQueryObjectiv getQueryObjectiv = nullptr;
    GetQueryObjectui64v getQueryObjectui64v = nullptr;
    static constexpr int kQueries = 8;
    GLuint queries[kQueries] = {};
    bool pending[kQueries] = {};
    int next = 0;
    bool initialised = false;
    bool available = false;
    unsigned draws = 0, timed = 0;
    unsigned newFrames = 0; // draws of a different picture than the draw before
    int lastIndex = -1;
    double totalMs = 0.0, maxMs = 0.0;
    int64_t windowStart = 0;
  } m_gpuTimer;

  void DeleteFrames();
  void InitGpuTimer();
  void CollectGpuTimes();
  void DeleteGpuTimer();
};
