#!/usr/bin/env python3
"""Focused playback pause, PS5 recovery and GPU lifetime regressions."""

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

    def test_vp9_recovery_waits_for_real_keyframes(self):
        source = (VIDEO / "DVDVideoCodecPS5.cpp").read_text()
        start = source.index("bool CDVDVideoCodecPS5::DecodeOne(")
        end = source.index("  std::vector<uint8_t> cleaned;", start)
        gate = source[start:end]
        harness = r"""
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <vector>
constexpr int LOGINFO = 1;
struct CLog {
  template<class... Args> static void Log(int, const char*, Args&&...) {}
};
struct CDVDVideoCodecPS5 {
  bool m_software = false, m_recoveryPending = false;
  bool m_waitKeyframe = true, m_vp9 = true;
  int skippedPts = 0, nativeDecodes = 0, nalParses = 0;
  void NextPts() { ++skippedPts; }
  void CleanAccessUnit(const uint8_t* data, size_t, std::vector<uint8_t>&,
                       bool& irap, bool& hasSlice) {
    ++nalParses;
    irap = data[0] == 0x65;
    hasSlice = true;
  }
  bool DecodeOne(const uint8_t*, size_t);
};
"""
        harness += method("DVDVideoCodecPS5.cpp", "bool IsVp9KeyFrame(")
        harness += "\n" + gate + "  ++nativeDecodes;\n  return true;\n}\n"
        harness += r"""
int main() {
  uint8_t data[] = {0x82, 0x49, 0x83, 0x42};
  for (int header = 0; header < 256; ++header) {
    data[0] = static_cast<uint8_t>(header);
    bool expected = (header >= 0x80 && header <= 0x83) ||
                    (header >= 0x90 && header <= 0x93);
    assert(IsVp9KeyFrame(data, sizeof(data)) == expected);
  }
  data[0] = 0x92;
  for (size_t size = 0; size < 4; ++size)
    assert(!IsVp9KeyFrame(data, size));
  assert(!IsVp9KeyFrame(nullptr, 4));
  for (int index = 1; index < 4; ++index) {
    data[index] ^= 1;
    assert(!IsVp9KeyFrame(data, sizeof(data)));
    data[index] ^= 1;
  }
  CDVDVideoCodecPS5 codec;
  for (uint8_t header : {0x96, 0x9a, 0x65}) {
    data[0] = header;
    assert(codec.DecodeOne(data, sizeof(data)));
    assert(codec.m_waitKeyframe && codec.nativeDecodes == 0);
  }
  assert(codec.skippedPts == 3 && codec.nalParses == 0);
  data[0] = 0x92;
  assert(codec.DecodeOne(data, sizeof(data)));
  assert(!codec.m_waitKeyframe && codec.nativeDecodes == 1);
  data[0] = 0x96;
  assert(codec.DecodeOne(data, sizeof(data)));
  assert(codec.nativeDecodes == 2 && codec.nalParses == 0);
  codec.m_vp9 = false;
  codec.m_waitKeyframe = true;
  data[0] = 0x41;
  codec.DecodeOne(data, sizeof(data));
  assert(codec.m_waitKeyframe && codec.nativeDecodes == 2);
  data[0] = 0x65;
  codec.DecodeOne(data, sizeof(data));
  assert(!codec.m_waitKeyframe && codec.nativeDecodes == 3 && codec.nalParses == 2);
}
"""
        self.run_harness(harness)

    def test_vp9_seek_rearms_keyframe_wait(self):
        harness = r"""
#include <cassert>
#include <deque>
#include <memory>
struct Software {
  int resets = 0;
  void Reset() { ++resets; }
};
struct CDVDVideoCodecPS5 {
  std::unique_ptr<Software> m_software;
  std::deque<int> m_softwareQueue, m_replay;
  int hardwareResets = 0, m_replayBytes = 0;
  int m_failuresBeforeFirst = 0, m_cleanNoSlice = 0, m_cleanWaitIrap = 0;
  int m_midStreamResets = 6;
  bool m_replayOverflow = false, m_seenIrap = false;
  bool m_waitKeyframe = false, m_vp9 = true;
  void ResetDecoderState() { ++hardwareResets; }
  void Reset();
};
"""
        harness += method("DVDVideoCodecPS5.cpp", "void CDVDVideoCodecPS5::Reset()")
        harness += r"""
int main() {
  CDVDVideoCodecPS5 codec;
  codec.Reset();
  assert(codec.m_waitKeyframe && codec.hardwareResets == 1);
  assert(codec.m_midStreamResets == 0);
  codec.m_waitKeyframe = false;
  codec.Reset();
  assert(codec.m_waitKeyframe && codec.hardwareResets == 2);
  codec.m_vp9 = false;
  codec.Reset();
  assert(!codec.m_waitKeyframe && codec.hardwareResets == 3);
  codec.m_software = std::make_unique<Software>();
  codec.m_softwareQueue.push_back(1);
  codec.Reset();
  assert(codec.m_software->resets == 1 && codec.m_softwareQueue.empty());
  assert(codec.hardwareResets == 3);
}
"""
        self.run_harness(harness)

    def test_pause_blocks_pending_output_and_packets_but_preserves_startup_and_steps(self):
        source = (ROOT / "xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp").read_text()
        start = source.index("void CVideoPlayerVideo::Process()")
        control_start = source.index("    auto timeout =", start)
        control_end = source.index("    std::shared_ptr<CDVDMsg> pMsg;", control_start)
        retry_start = source.index("      if (m_outputSate == OUTPUT_AGAIN", control_end)
        retry_end = source.index("      // if we only wanted priority messages", retry_start)
        controls = source[control_start:control_end]
        retries = source[retry_start:retry_end]
        harness = r"""
#include <cassert>
#include <chrono>
using namespace std::chrono_literals;
constexpr int DVD_PLAYSPEED_PAUSE = 0;
constexpr int OUTPUT_AGAIN = 1, OUTPUT_NORMAL = 2;
struct IDVDStreamPlayer {
  enum { SYNC_STARTING, SYNC_WAITSYNC, SYNC_INSYNC };
};
struct ProcessInfo {
  bool advance = false;
  bool IsFrameAdvance() { return advance; }
};
struct RenderManager {
  bool paused = false;
  void SetPaused(bool value) { paused = value; }
};
struct Picture { bool videoBuffer = true; };
struct Player {
  int m_speed = 0, m_syncState = IDVDStreamPlayer::SYNC_INSYNC;
  int m_outputSate = OUTPUT_AGAIN;
  bool m_paused = false, m_stalled = false;
  ProcessInfo m_processInfo;
  RenderManager m_renderManager;
  Picture m_picture;
  int pictures = 0, decoderCalls = 0, packets = 0;
  int OutputPicture(Picture*) { ++pictures; return OUTPUT_NORMAL; }
  bool ProcessDecoderOutput(double&, double&) { ++decoderCalls; return false; }
  void Poll() {
    double frametime = 16683.33, pts = 0;
    bool onlyPrioMsgs = false;
    for (int tick = 0; tick < 20; ++tick) {
"""
        harness += controls + r"""
      assert(timeout.count() >= 0);
      if (iPriority == 0) ++packets;
"""
        harness += retries + r"""
    }
  }
};
int main() {
  Player paused;
  paused.Poll();
  assert(paused.m_renderManager.paused && paused.pictures == 0);
  assert(paused.decoderCalls == 0 && paused.packets == 0);
  Player starting;
  starting.m_syncState = IDVDStreamPlayer::SYNC_STARTING;
  starting.Poll();
  assert(!starting.m_renderManager.paused && starting.pictures == 1);
  assert(starting.decoderCalls > 0 && starting.packets > 0);
  Player waiting;
  waiting.m_syncState = IDVDStreamPlayer::SYNC_WAITSYNC;
  waiting.Poll();
  assert(!waiting.m_renderManager.paused && waiting.packets == 0);
  Player step;
  step.m_processInfo.advance = true;
  step.Poll();
  assert(!step.m_renderManager.paused && step.pictures == 1 && step.decoderCalls > 0);
  Player playing;
  playing.m_speed = 1000;
  playing.Poll();
  assert(!playing.m_renderManager.paused && playing.pictures == 1 && playing.packets > 0);
  Player internalPause;
  internalPause.m_speed = 1000;
  internalPause.m_paused = true;
  internalPause.Poll();
  assert(internalPause.pictures == 0 && internalPause.decoderCalls == 0);
  assert(internalPause.packets == 0);
  paused.m_speed = 1000;
  paused.Poll();
  assert(!paused.m_renderManager.paused && paused.pictures == 1 && paused.packets > 0);
}
"""
        self.run_harness(harness)

    def test_paused_renderer_holds_displayed_frame_despite_late_queued_frames(self):
        source = (ROOT / "xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp").read_text()
        start = source.index("void CRenderManager::PrepareNextRender()")
        end = source.index("  const double frameOnScreen =", start)
        harness = r"""
#include <cassert>
#include <deque>
constexpr int LOGERROR = 1, PRESENT_IDLE = 0;
struct CLog {
  template<class... Args> static void Log(int, const char*, Args&&...) {}
};
struct Event { void notifyAll() {} };
struct CRenderManager {
  bool m_paused = true, m_forceNext = false, m_showVideo = true;
  int m_presentstep = 1, displayed = 1;
  std::deque<int> m_queued{2, 3, 4};
  Event m_presentevent;
  void PrepareNextRender();
};
"""
        harness += source[start:end] + r"""
  displayed = m_queued.front();
  m_queued.pop_front();
}
int main() {
  CRenderManager renderer;
  for (int tick = 0; tick < 60; ++tick) renderer.PrepareNextRender();
  assert(renderer.displayed == 1 && renderer.m_queued.size() == 3);
  renderer.m_forceNext = true;
  renderer.PrepareNextRender();
  assert(renderer.displayed == 2 && renderer.m_queued.size() == 2);
  renderer.m_forceNext = false;
  renderer.PrepareNextRender();
  assert(renderer.displayed == 2 && renderer.m_queued.size() == 2);
  renderer.m_paused = false;
  renderer.PrepareNextRender();
  assert(renderer.displayed == 3 && renderer.m_queued.size() == 1);
}
"""
        self.run_harness(harness)

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
