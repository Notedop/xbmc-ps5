/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "HdrOutputPS5.h"

#include "cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodec.h"
#include "rendering/gl/GuiCompositeShaderGL.h"
#include "utils/log.h"

#include "platform/ps5/VideoOutInfo.h"

#include <atomic>

#include <EGL/egl.h>

extern "C"
{
#include <libavutil/pixfmt.h>
}

using namespace KODI::PLATFORM::PS5;

namespace
{
CHdrOutputPS5* g_hdrOutput = nullptr; // for DefaultFramebuffer()

// Pack a pixel of the RGB10_A2 target into the 2:10:10:10 word the HDR
// scanout reads (R in bits 9-0, G 19-10, B 29-20, A 31-30), written into the
// B8G8R8A8 framebuffer as its four bytes: memory order B, G, R, A = word bytes
// 0, 1, 2, 3.
const char* const kPackVertex = R"(#version 150
in vec2 pos;
out vec2 uv;
void main() { uv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); })";

// diagnostic = 1 is used only by the one-time target probe.
const char* const kPackFragment = R"(#version 150
uniform sampler2D tex;
uniform int diagnostic;
in vec2 uv;
out vec4 color;
void main() {
  vec3 c = clamp(texture(tex, uv).rgb, 0.0, 1.0);
  if (diagnostic != 0 && uv.x < 1.0 / 3.0)
    c = vec3(0.58);
  else if (diagnostic != 0 && uv.x < 2.0 / 3.0)
    c = uv.y < 1.0 / 3.0 ? vec3(0.58, 0.0, 0.0)
                         : (uv.y < 2.0 / 3.0 ? vec3(0.0, 0.58, 0.0) : vec3(0.0, 0.0, 0.58));
  uvec3 q = uvec3(round(c * 1023.0));
  uint w = q.r | (q.g << 10u) | (q.b << 20u) | (3u << 30u);
  color = vec4(float((w >> 16u) & 255u), float((w >> 8u) & 255u), float(w & 255u),
               float(w >> 24u)) / 255.0;
})";

GLuint Compile(GLenum type, const char* source)
{
  const GLuint shader = glCreateShader(type);
  glShaderSource(shader, 1, &source, nullptr);
  glCompileShader(shader);
  GLint ok = GL_FALSE;
  glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
  if (!ok)
  {
    char log[1024] = {};
    glGetShaderInfoLog(shader, sizeof(log), nullptr, log);
    CLog::Log(LOGERROR, "CHdrOutputPS5: pack shader: {}", log);
    glDeleteShader(shader);
    return 0;
  }
  return shader;
}
} // namespace

unsigned int KODI::PLATFORM::PS5::DefaultFramebuffer()
{
  return g_hdrOutput ? g_hdrOutput->DefaultFramebuffer() : 0;
}

bool KODI::PLATFORM::PS5::HdrOutputConvertsHlg()
{
  return g_hdrOutput && g_hdrOutput->ConvertsHlg();
}

CHdrOutputPS5::~CHdrOutputPS5()
{
  SwitchScanout(false);
  DestroyTarget();
  if (m_packProgram)
    glDeleteProgram(m_packProgram);
  if (m_packVbo)
    glDeleteBuffers(1, &m_packVbo);
  if (m_packVao)
    glDeleteVertexArrays(1, &m_packVao);
  m_guiFbo.Cleanup();
  if (g_hdrOutput == this)
    g_hdrOutput = nullptr;
}

void CHdrOutputPS5::SwitchScanout(bool hdr)
{
  if (hdr == m_hdrScanout)
    return;
  int32_t results[4];
  const int rc = SetScanoutFormat(hdr ? kScanoutFormatHdr : kScanoutFormatSdr, results);
  CLog::Log(rc == 0 ? LOGINFO : LOGWARNING, "CHdrOutputPS5: scanout to {}: {} ({})",
            hdr ? "HDR (BT.2020 PQ, 10-bit)" : "SDR", rc == 0 ? "in effect" : "refused",
            DescribeScanoutResults(results));
  if (rc == 0)
    m_hdrScanout = hdr;
}

namespace
{
// kodi-video-debug: the driver's own account of each GL error, with the stage of
// the frame it happened in
const char* g_glStage = "outside a frame";
std::atomic<unsigned> g_glMessages{0};

void GLAPIENTRY OnGlDebugMessage(GLenum source,
                                 GLenum type,
                                 GLuint id,
                                 GLenum severity,
                                 GLsizei length,
                                 const GLchar* message,
                                 const void* userParam)
{
  if (type != GL_DEBUG_TYPE_ERROR && severity != GL_DEBUG_SEVERITY_HIGH &&
      severity != GL_DEBUG_SEVERITY_MEDIUM)
    return;
  if (g_glMessages++ >= 80)
    return;
  CLog::Log(LOGWARNING, "CHdrOutputPS5 (kodi-video-debug): GL [{}] type {:#x} id {} severity {:#x}: {}",
            g_glStage, type, id, severity, message ? message : "");
}
} // namespace

void CHdrOutputPS5::SetStage(const char* stage)
{
  g_glStage = stage;
}

void CHdrOutputPS5::InstallDebugCallback()
{
  if (!m_debug || m_debugCallbackInstalled)
    return;
  m_debugCallbackInstalled = true;
  using DebugMessageCallback = void(GLAPIENTRY*)(GLDEBUGPROC, const void*);
  using DebugMessageControl =
      void(GLAPIENTRY*)(GLenum, GLenum, GLenum, GLsizei, const GLuint*, GLboolean);
  const auto callback =
      reinterpret_cast<DebugMessageCallback>(eglGetProcAddress("glDebugMessageCallback"));
  const auto control =
      reinterpret_cast<DebugMessageControl>(eglGetProcAddress("glDebugMessageControl"));
  if (!callback)
  {
    CLog::Log(LOGWARNING, "CHdrOutputPS5 (kodi-video-debug): glDebugMessageCallback not available");
    return;
  }
  while (glGetError() != GL_NO_ERROR)
  {
  }
  glEnable(GL_DEBUG_OUTPUT);
  glEnable(GL_DEBUG_OUTPUT_SYNCHRONOUS);
  callback(&OnGlDebugMessage, nullptr);
  if (control)
    control(GL_DONT_CARE, GL_DONT_CARE, GL_DONT_CARE, 0, nullptr, GL_TRUE);
  CLog::Log(LOGINFO, "CHdrOutputPS5 (kodi-video-debug): GL debug output on (GL error {:#x})",
            glGetError());
}

bool CHdrOutputPS5::SetHDR(const VideoPicture* picture)
{
  g_hdrOutput = this;
  InstallDebugCallback();
  // The platform's HDR format is BT.2020 PQ: PQ video passes through
  // unchanged, HLG is converted to PQ in Kodi's YUV shader (patch 0014)
  const bool pq = picture && picture->color_transfer == AVCOL_TRC_SMPTE2084;
  const bool hlg = picture && picture->color_transfer == AVCOL_TRC_ARIB_STD_B67;
  const bool wantHdr = (pq || hlg) && IsDisplayHdr();
  m_hlg = wantHdr && hlg;
  if (wantHdr && !CreatePackProgram())
    return false;
  SwitchScanout(wantHdr);
  m_active = wantHdr && m_hdrScanout;
  if (!m_active)
    DestroyTarget();
  CLog::Log(LOGINFO, "CHdrOutputPS5: HDR output {}{}", m_active ? "active" : "off",
            m_active && m_hlg ? " (HLG converted to PQ)" : "");
  return m_active;
}

bool CHdrOutputPS5::CreatePackProgram()
{
  if (m_packProgram)
    return true;
  const GLuint vs = Compile(GL_VERTEX_SHADER, kPackVertex);
  const GLuint fs = Compile(GL_FRAGMENT_SHADER, kPackFragment);
  if (!vs || !fs)
  {
    if (vs)
      glDeleteShader(vs);
    if (fs)
      glDeleteShader(fs);
    return false;
  }
  m_packProgram = glCreateProgram();
  glAttachShader(m_packProgram, vs);
  glAttachShader(m_packProgram, fs);
  glBindAttribLocation(m_packProgram, 0, "pos");
  glLinkProgram(m_packProgram);
  glDeleteShader(vs);
  glDeleteShader(fs);
  GLint ok = GL_FALSE;
  glGetProgramiv(m_packProgram, GL_LINK_STATUS, &ok);
  if (!ok)
  {
    CLog::Log(LOGERROR, "CHdrOutputPS5: pack program did not link");
    glDeleteProgram(m_packProgram);
    m_packProgram = 0;
    return false;
  }
  m_packTexLoc = glGetUniformLocation(m_packProgram, "tex");
  m_packDiagnosticLoc = glGetUniformLocation(m_packProgram, "diagnostic");
  const GLfloat quad[] = {-1.0f, -1.0f, 1.0f, -1.0f, -1.0f, 1.0f, 1.0f, 1.0f};
  // Kodi binds its one vertex array at start-up and relies on it staying
  // bound (core profile: no draw works without one) - restore it
  GLint previousVao = 0;
  GLint previousArrayBuffer = 0;
  glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &previousVao);
  glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &previousArrayBuffer);
  glGenVertexArrays(1, &m_packVao);
  glGenBuffers(1, &m_packVbo);
  glBindVertexArray(m_packVao);
  glBindBuffer(GL_ARRAY_BUFFER, m_packVbo);
  glBufferData(GL_ARRAY_BUFFER, sizeof(quad), quad, GL_STATIC_DRAW);
  glEnableVertexAttribArray(0);
  glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, nullptr);
  glBindVertexArray(static_cast<GLuint>(previousVao));
  glBindBuffer(GL_ARRAY_BUFFER, static_cast<GLuint>(previousArrayBuffer));
  return true;
}

namespace
{
struct TargetFormat
{
  GLint internalFormat;
  GLenum type;
  const char* name;
};

// In order of preference. A format the driver calls complete may still not
// be rendered to, so each is tested by drawing into it (ProbeTarget).
constexpr TargetFormat kTargetFormats[] = {
    {GL_RGB10_A2, GL_UNSIGNED_INT_2_10_10_10_REV, "RGB10_A2"},
    {GL_RGBA16F, GL_HALF_FLOAT, "RGBA16F"},
    {GL_RGBA16, GL_UNSIGNED_SHORT, "RGBA16"},
    {GL_RGBA8, GL_UNSIGNED_BYTE, "RGBA8 (8-bit: may band)"},
};

bool Near(const float* got, const float* want, float tolerance)
{
  for (int i = 0; i < 3; ++i)
    if (got[i] < want[i] - tolerance || got[i] > want[i] + tolerance)
      return false;
  return true;
}
} // namespace

bool CHdrOutputPS5::CreateTarget(int width, int height)
{
  if (m_fbo && m_width == width && m_height == height)
    return true;
  DestroyTarget();
  for (const TargetFormat& format : kTargetFormats)
  {
    glGenTextures(1, &m_texture);
    glBindTexture(GL_TEXTURE_2D, m_texture);
    glTexImage2D(GL_TEXTURE_2D, 0, format.internalFormat, width, height, 0, GL_RGBA, format.type,
                 nullptr);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glBindTexture(GL_TEXTURE_2D, 0);
    glGenRenderbuffers(1, &m_depth);
    glBindRenderbuffer(GL_RENDERBUFFER, m_depth);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, width, height);
    glBindRenderbuffer(GL_RENDERBUFFER, 0);
    glGenFramebuffers(1, &m_fbo);
    glBindFramebuffer(GL_FRAMEBUFFER, m_fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, m_texture, 0);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, m_depth);
    const GLenum status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    if (status != GL_FRAMEBUFFER_COMPLETE)
      CLog::Log(LOGWARNING, "CHdrOutputPS5: {} target {}x{} incomplete ({:#x})", format.name, width,
                height, status);
    else if (ProbeTarget(width, height, format.name))
    {
      m_width = width;
      m_height = height;
      CLog::Log(LOGINFO, "CHdrOutputPS5: {} intermediate target {}x{}", format.name, width, height);
      return true;
    }
    DestroyTarget();
  }
  CLog::Log(LOGERROR, "CHdrOutputPS5: no renderable intermediate target format");
  return false;
}

bool CHdrOutputPS5::ProbeTarget(int width, int height, const char* name)
{
  // Clear to a known colour, then draw the pack shader's grey test strip
  // (left third, diagnostic on) and read both back.
  GLint viewport[4] = {};
  GLint program = 0;
  GLint vao = 0;
  GLint texture = 0;
  GLint activeTexture = 0;
  GLfloat clearColor[4] = {};
  glGetIntegerv(GL_VIEWPORT, viewport);
  glGetIntegerv(GL_CURRENT_PROGRAM, &program);
  glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &vao);
  glGetIntegerv(GL_ACTIVE_TEXTURE, &activeTexture);
  glActiveTexture(GL_TEXTURE0);
  glGetIntegerv(GL_TEXTURE_BINDING_2D, &texture);
  glGetFloatv(GL_COLOR_CLEAR_VALUE, clearColor);
  const GLboolean blend = glIsEnabled(GL_BLEND);
  const GLboolean scissor = glIsEnabled(GL_SCISSOR_TEST);
  const GLboolean depthTest = glIsEnabled(GL_DEPTH_TEST);
  while (glGetError() != GL_NO_ERROR)
  {
  }

  glBindFramebuffer(GL_FRAMEBUFFER, m_fbo);
  glDisable(GL_BLEND);
  glDisable(GL_SCISSOR_TEST);
  glDisable(GL_DEPTH_TEST);
  glViewport(0, 0, width, height);
  glClearColor(0.25f, 0.5f, 0.75f, 1.0f);
  glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
  float cleared[4] = {};
  glReadPixels(width / 2, height / 2, 1, 1, GL_RGBA, GL_FLOAT, cleared);
  const GLenum clearError = glGetError();

  glUseProgram(m_packProgram);
  glBindTexture(GL_TEXTURE_2D, 0);
  glUniform1i(m_packTexLoc, 0);
  glUniform1i(m_packDiagnosticLoc, 1);
  glBindVertexArray(m_packVao);
  glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
  float drawn[4] = {};
  glReadPixels(width / 6, height / 2, 1, 1, GL_RGBA, GL_FLOAT, drawn);
  const GLenum drawError = glGetError();

  glBindFramebuffer(GL_FRAMEBUFFER, 0);
  glBindVertexArray(static_cast<GLuint>(vao));
  glUseProgram(static_cast<GLuint>(program));
  glBindTexture(GL_TEXTURE_2D, static_cast<GLuint>(texture));
  glActiveTexture(static_cast<GLenum>(activeTexture));
  glViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
  glClearColor(clearColor[0], clearColor[1], clearColor[2], clearColor[3]);
  if (blend)
    glEnable(GL_BLEND);
  if (scissor)
    glEnable(GL_SCISSOR_TEST);
  if (depthTest)
    glEnable(GL_DEPTH_TEST);

  // the grey strip's packed bytes 0x19, 0x46, 0x51 (see kPackFragment)
  const float wantCleared[3] = {0.25f, 0.5f, 0.75f};
  const float wantDrawn[3] = {0x19 / 255.0f, 0x46 / 255.0f, 0x51 / 255.0f};
  const bool clearOk = clearError == GL_NO_ERROR && Near(cleared, wantCleared, 0.01f);
  const bool drawOk = drawError == GL_NO_ERROR && Near(drawn, wantDrawn, 2.0f / 255.0f);
  if (!clearOk || !drawOk || m_debug)
    CLog::Log(clearOk && drawOk ? LOGINFO : LOGWARNING,
              "CHdrOutputPS5: {} target test: clear {} ({:.3f} {:.3f} {:.3f}, GL error {:#x}), draw "
              "{} ({:.3f} {:.3f} {:.3f}, GL error {:#x})",
              name, clearOk ? "ok" : "FAILED", cleared[0], cleared[1], cleared[2], clearError,
              drawOk ? "ok" : "FAILED", drawn[0], drawn[1], drawn[2], drawError);
  return clearOk && drawOk;
}

void CHdrOutputPS5::CheckErrors(const char* stage)
{
  if (!m_debug || m_errorReports >= 40)
    return;
  for (GLenum error = glGetError(); error != GL_NO_ERROR; error = glGetError())
  {
    if (m_errorReports++ < 40)
      CLog::Log(LOGWARNING, "CHdrOutputPS5 (kodi-video-debug): GL error {:#x} before end of {}", error,
                stage);
  }
}

void CHdrOutputPS5::DestroyTarget()
{
  if (m_fbo)
    glDeleteFramebuffers(1, &m_fbo);
  if (m_depth)
    glDeleteRenderbuffers(1, &m_depth);
  if (m_texture)
    glDeleteTextures(1, &m_texture);
  m_fbo = m_depth = m_texture = 0;
  m_width = m_height = 0;
}

void CHdrOutputPS5::BindTarget(int width, int height)
{
  SetStage(m_active ? "frame start (HDR)" : "SDR frame");
  if (!m_active)
    return;
  if (!CreateTarget(width, height))
  {
    // no target: fall back to SDR output rather than show nothing
    SwitchScanout(false);
    m_active = false;
    return;
  }
  glBindFramebuffer(GL_FRAMEBUFFER, m_fbo);
  CheckErrors("BindTarget");
  SetStage("GUI render");
}

void CHdrOutputPS5::Pack(int width, int height)
{
  if (!m_active || !m_fbo || !m_packProgram)
    return;
  CheckErrors("frame (before Pack)");
  SetStage("Pack");
  // Kodi's core-profile rendering keeps its own vertex array bound: restore it
  GLint previousVao = 0;
  glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &previousVao);
  glBindFramebuffer(GL_FRAMEBUFFER, 0);
  glDisable(GL_BLEND);
  glDisable(GL_DEPTH_TEST);
  glDisable(GL_SCISSOR_TEST);
  glDisable(GL_DITHER); // the bytes must land exactly as computed
  glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
  glViewport(0, 0, width, height);
  glUseProgram(m_packProgram);
  glActiveTexture(GL_TEXTURE0);
  glBindTexture(GL_TEXTURE_2D, m_texture);
  glUniform1i(m_packTexLoc, 0);
  glUniform1i(m_packDiagnosticLoc, 0);
  glBindVertexArray(m_packVao);
  glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
  glBindVertexArray(static_cast<GLuint>(previousVao));
  glBindTexture(GL_TEXTURE_2D, 0);
  glUseProgram(0);
  glEnable(GL_DITHER);
  SetStage("between frames");
}

// ---- GUI compositing, as CWinSystemGbmGLContext (Kodi's Linux GL) does it ----

bool CHdrOutputPS5::SetGuiCompositing(int colorTransfer, bool limitedColor)
{
  m_guiCompositing = colorTransfer != 0 && m_active;
  if (m_guiCompositing)
  {
    if (!m_compositeShader)
    {
      std::string defines;
      if (limitedColor)
        defines += "#define KODI_LIMITED_RANGE 1\n";
      m_compositeShader = std::make_unique<CGuiCompositeShaderGL>(defines);
      if (!m_compositeShader->CompileAndLink())
      {
        CLog::Log(LOGERROR, "CHdrOutputPS5: failed to compile the GUI composite shader");
        m_compositeShader.reset();
        m_guiCompositing = false;
      }
    }
  }
  else
  {
    m_guiFbo.Cleanup();
    m_guiFboWidth = 0;
    m_guiFboHeight = 0;
    m_compositeShader.reset();
  }
  return m_guiCompositing;
}

bool CHdrOutputPS5::BeginGuiComposite(bool guiWillRender, int width, int height, bool depth)
{
  if (!m_guiCompositing)
    return false;
  m_guiWillRender = guiWillRender;
  if (!m_guiFbo.IsValid() || m_guiFboWidth != width || m_guiFboHeight != height)
  {
    m_guiFbo.Cleanup();
    if (!m_guiFbo.Initialize() ||
        !m_guiFbo.CreateAndBindToTexture(GL_TEXTURE_2D, width, height, GL_RGBA) ||
        (depth && !m_guiFbo.AttachDepthBuffer(width, height)))
    {
      CLog::Log(LOGERROR, "CHdrOutputPS5: failed to create the GUI FBO {}x{}", width, height);
      m_guiFbo.Cleanup();
      return false;
    }
    m_guiFboWidth = width;
    m_guiFboHeight = height;
    m_guiFboClean = false;
  }
  if (!guiWillRender)
    return true;
  if (!m_guiFbo.BeginRender())
    return false;
  if (!m_guiFboClean)
  {
    glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    m_guiFboClean = true;
  }
  return true;
}

void CHdrOutputPS5::EndGuiComposite()
{
  if (m_guiWillRender)
    m_guiFbo.EndRender(); // binds the "default" framebuffer: our 10-bit target
  glClearColor(0.0f, 0.0f, 0.0f, 0.0f);
  glClear(GL_COLOR_BUFFER_BIT);
  CheckErrors("GUI render (EndGuiComposite)");
  SetStage("video render");
}

void CHdrOutputPS5::CompositeGui(unsigned guiElementCount)
{
  CheckErrors("video render (before CompositeGui)");
  SetStage("GUI composite");
  if (!m_guiFbo.IsValid() || !m_guiFbo.IsBound() || !m_compositeShader)
    return;
  if (m_guiWillRender)
  {
    m_guiFboClean = guiElementCount == 0;
    if (m_guiFboClean)
      return;
  }
  else if (m_guiFboClean)
    return;

  glActiveTexture(GL_TEXTURE0);
  glBindTexture(GL_TEXTURE_2D, m_guiFbo.Texture());
  glEnable(GL_BLEND);
  glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

  const float w = static_cast<float>(m_guiFboWidth);
  const float h = static_cast<float>(m_guiFboHeight);
  const GLfloat proj[16] = {2.0f / w, 0, 0, 0, 0, -2.0f / h, 0, 0, 0, 0, -1, 0, -1.0f, 1.0f, 0, 1};
  m_compositeShader->SetProjection(proj);
  m_compositeShader->SetSdrPeak(203.0f / 10000.0f);
  m_compositeShader->Enable();
  const GLint posLoc = m_compositeShader->GetPosLoc();
  const GLint texLoc = m_compositeShader->GetTexLoc();
  const GLfloat vert[4][2] = {{0, 0}, {w, 0}, {w, h}, {0, h}};
  const GLfloat tex[4][2] = {{0, 1}, {1, 1}, {1, 0}, {0, 0}};
  const GLubyte idx[4] = {0, 1, 3, 2};
  GLuint vbo[3];
  glGenBuffers(3, vbo);
  glBindBuffer(GL_ARRAY_BUFFER, vbo[0]);
  glBufferData(GL_ARRAY_BUFFER, sizeof(vert), vert, GL_STREAM_DRAW);
  glVertexAttribPointer(posLoc, 2, GL_FLOAT, GL_FALSE, 0, nullptr);
  glEnableVertexAttribArray(posLoc);
  glBindBuffer(GL_ARRAY_BUFFER, vbo[1]);
  glBufferData(GL_ARRAY_BUFFER, sizeof(tex), tex, GL_STREAM_DRAW);
  glVertexAttribPointer(texLoc, 2, GL_FLOAT, GL_FALSE, 0, nullptr);
  glEnableVertexAttribArray(texLoc);
  glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, vbo[2]);
  glBufferData(GL_ELEMENT_ARRAY_BUFFER, sizeof(idx), idx, GL_STREAM_DRAW);
  glDrawElements(GL_TRIANGLE_STRIP, 4, GL_UNSIGNED_BYTE, nullptr);
  glDisableVertexAttribArray(posLoc);
  glDisableVertexAttribArray(texLoc);
  glBindBuffer(GL_ARRAY_BUFFER, 0);
  glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0);
  glDeleteBuffers(3, vbo);
  m_compositeShader->Disable();
}
