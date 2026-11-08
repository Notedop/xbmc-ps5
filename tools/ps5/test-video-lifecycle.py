#!/usr/bin/env python3
"""Compile production PS5 fallback and GPU-retirement methods against lifetime mocks."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIDEO = ROOT / "xbmc/platform/ps5/video"


def method(file, signature):
    source = (VIDEO / file).read_text()
    start = source.index(signature)
    return source[start:source.index("\n}", start) + 2]


class VideoLifecycleTests(unittest.TestCase):
    def run_harness(self, source):
        compiler = shutil.which("clang++-18") or shutil.which("c++")
        self.assertIsNotNone(compiler, "A host C++20 compiler is required")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            fixture = path / "video-lifecycle.cpp"
            fixture.write_text(source)
            output = path / "video-lifecycle"
            subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-g", str(fixture),
                            "-o", str(output)], check=True)
            subprocess.run([str(output)], check=True, timeout=30)

    def test_fallback_retains_displayed_frames_and_handles_pending_transition(self):
        harness = r"""
#include <cassert>
#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <utility>
#include <vector>
constexpr int CODEC_FORCE_SOFTWARE = 1;
constexpr int LOGERROR = 1, LOGWARNING = 2;
struct CLog {
  template<class... Args> static void Log(int, const char*, Args&&...) {}
};
struct CDVDStreamInfo { int codecOptions = 0; };
struct CDVDCodecOptions {};
struct DemuxPacket { uint8_t* pData; int iSize; };
struct CDVDVideoCodecFFmpeg {
  static inline bool openSuccess = true;
  static inline int packets = 0;
  explicit CDVDVideoCodecFFmpeg(int&) {}
  bool Open(CDVDStreamInfo& hints, CDVDCodecOptions&) {
    assert(hints.codecOptions & CODEC_FORCE_SOFTWARE);
    return openSuccess;
  }
  const char* GetName() { return "software"; }
  bool AddData(const DemuxPacket&) { ++packets; return true; }
};
struct Decoder {
  static inline int closes = 0;
  ~Decoder() { Close(); }
  void Close() { ++closes; }
  bool HasFreeFrame() { return true; }
};
struct CDVDVideoCodecPS5 {
  struct ReplayPacket {
    std::vector<uint8_t> data;
    double pts = 0, dts = 0;
    bool recoveryPoint = false;
  };
  int m_processInfo = 0;
  CDVDStreamInfo m_hints;
  CDVDCodecOptions m_options;
  std::string m_streamName = "VP9";
  std::shared_ptr<Decoder> m_decoder = std::make_shared<Decoder>();
  std::unique_ptr<CDVDVideoCodecFFmpeg> m_software;
  std::deque<std::shared_ptr<Decoder>> m_decoded;
  std::deque<ReplayPacket> m_replay, m_softwareQueue;
  std::deque<int> m_pendingAus, m_pts;
  int m_picturesOut = 0, m_failuresBeforeFirst = 0;
  unsigned m_replayBytes = 0;
  bool m_fatal = false, m_recoveryPending = false, switchInRetry = false;
  void ClearQueue() { m_decoded.clear(); }
  bool RetryPending() { return switchInRetry ? SwitchToSoftware() : true; }
  bool RunRecovery() { return SwitchToSoftware(); }
  void BufferForReplay(const DemuxPacket&) {}
  bool FeedHardware(const DemuxPacket&) { return true; }
  bool SwitchToSoftware();
  bool AddData(const DemuxPacket&);
};
"""
        # AddData also reconstructs demux packets when replaying the initial stream.
        harness = harness.replace(
            "struct DemuxPacket { uint8_t* pData; int iSize; };",
            "struct DemuxPacket { uint8_t* pData; int iSize; "
            "double pts = 0, dts = 0; bool recoveryPoint = false; };")
        harness += method("DVDVideoCodecPS5.cpp", "bool CDVDVideoCodecPS5::SwitchToSoftware()")
        harness += "\n" + method("DVDVideoCodecPS5.cpp", "bool CDVDVideoCodecPS5::AddData(")
        harness += r"""
int main() {
  {
    CDVDVideoCodecPS5 codec;
    auto displayedFrame = codec.m_decoder;
    codec.m_decoded.push_back(codec.m_decoder);
    codec.m_picturesOut = 50;
    codec.m_pendingAus.push_back(1);
    assert(codec.SwitchToSoftware());
    assert(!codec.m_decoder && codec.m_decoded.empty() && codec.m_pendingAus.empty());
    assert(Decoder::closes == 0);
    displayedFrame.reset();
    assert(Decoder::closes == 1);
  }
  {
    CDVDVideoCodecPS5 codec;
    assert(codec.SwitchToSoftware());
    assert(Decoder::closes == 2);
  }
  {
    CDVDVideoCodecPS5 codec;
    codec.m_replay.push_back({{1}});
    codec.m_replayBytes = 1;
    assert(codec.SwitchToSoftware());
    assert(codec.m_replay.empty() && codec.m_replayBytes == 0);
    assert(codec.m_softwareQueue.size() == 1);
    uint8_t byte = 1;
    DemuxPacket packet{&byte, 1};
    assert(codec.AddData(packet));
    assert(CDVDVideoCodecFFmpeg::packets == 2);
  }
  {
    CDVDVideoCodecPS5 codec;
    auto displayedFrame = codec.m_decoder;
    codec.m_picturesOut = 50;
    codec.switchInRetry = true;
    uint8_t byte = 1;
    DemuxPacket packet{&byte, 1};
    assert(codec.AddData(packet));
    assert(codec.m_software && !codec.m_decoder);
    assert(CDVDVideoCodecFFmpeg::packets == 3);
  }
  {
    CDVDVideoCodecPS5 codec;
    auto decoder = codec.m_decoder;
    codec.m_decoded.push_back(decoder);
    CDVDVideoCodecFFmpeg::openSuccess = false;
    assert(!codec.SwitchToSoftware());
    assert(codec.m_decoder == decoder && !codec.m_software);
    assert(codec.m_decoded.size() == 1);
  }
}
"""
        self.run_harness(harness)

    def test_forced_release_and_reconfiguration_retire_gpu_reads(self):
        harness = r"""
#include <cassert>
#include <string>
#include <vector>
using GLsync = int;
using GLint = int;
using GLsizei = int;
constexpr int GL_SYNC_STATUS = 1, GL_SIGNALED = 2, GL_TEXTURE_2D = 3;
constexpr int LOGINFO = 1;
std::string events;
bool signaled[4] = {true, false, true, false};
bool glIsSync(GLsync sync) { return sync != 0; }
void glGetSynciv(GLsync sync, int, int, GLsizei*, GLint* state) {
  *state = signaled[sync] ? GL_SIGNALED : 0;
}
void glFinish() {
  events += 'F';
  for (auto& value : signaled) value = true;
}
void glDeleteSync(GLsync sync) {
  assert(sync && signaled[sync]);
  events += 'D';
}
void* eglGetProcAddress(const char*) { return nullptr; }
struct CLog {
  template<class... Args> static void Log(int, const char*, Args&&...) {}
};
struct VideoPicture { int iWidth = 3840, iHeight = 1620; };
struct CLinuxRendererGL {
  void ReleaseBuffer(int) { events += 'R'; }
  bool Configure(const VideoPicture&, float, unsigned) { events += 'C'; return true; }
};
struct CRendererPS5 : CLinuxRendererGL {
  GLsync m_fences[3] = {1, 2, 0};
  using ImageTargetTexture2D = void (*)(int, void*);
  ImageTargetTexture2D m_imageTargetTexture2D = nullptr;
  bool m_layoutLogged = false, m_debug = false;
  int m_textureTarget = 0;
  std::vector<int> m_frames{1};
  void DeleteFrames() {
    assert(signaled[1] && signaled[2]);
    assert(m_fences[0] == 0 && m_fences[1] == 0);
    events += 'T';
    m_frames.clear();
  }
  bool NeedBuffer(int);
  void ReleaseBuffer(int);
  bool Configure(const VideoPicture&, float, unsigned);
};
"""
        for signature in ("bool CRendererPS5::NeedBuffer(",
                          "void CRendererPS5::ReleaseBuffer(",
                          "bool CRendererPS5::Configure("):
            harness += "\n" + method("RendererPS5.cpp", signature)
        harness += r"""
int main() {
  CRendererPS5 renderer;
  assert(renderer.NeedBuffer(0));
  assert(events.empty());
  renderer.ReleaseBuffer(0);
  assert(events == "FDR" && renderer.m_fences[0] == 0);
  events.clear();
  renderer.ReleaseBuffer(1);
  assert(events == "DR" && renderer.m_fences[1] == 0);
  events.clear();
  renderer.ReleaseBuffer(2);
  assert(events == "R");
  events.clear();
  renderer.m_fences[0] = 1;
  renderer.m_fences[1] = 2;
  signaled[1] = false;
  assert(renderer.Configure(VideoPicture{}, 24.0f, 0));
  assert(events == "FDDTC" && renderer.m_frames.empty());
}
"""
        self.run_harness(harness)


if __name__ == "__main__":
    unittest.main()
