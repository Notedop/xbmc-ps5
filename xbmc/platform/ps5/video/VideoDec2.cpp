/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "VideoDec2.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include <sys/mman.h>

#include "utils/log.h"

// ---------------------------------------------------------------------------
// libSceVideodec2 / libkernel / libSceSysmodule interface (clean-room)
// ---------------------------------------------------------------------------
extern "C"
{
struct sceVideodec2DecoderConfig
{
  uint64_t size;
  uint32_t resourceType, codecType, profile, maxLevel;
  int32_t maxWidth, maxHeight, maxDpbFrames;
  uint32_t pipelineDepth;
  uint64_t computeQueue, cpuAffinity;
  int32_t cpuPriority;
  uint32_t optimizeProgressive, checkMemoryType, reserved;
};

struct sceVideodec2DecoderMemory
{
  uint64_t size, cpuSize;
  void* cpu;
  uint64_t gpuSize;
  void* gpu;
  uint64_t cpuGpuSize;
  void* cpuGpu;
  uint64_t maxFrameSize;
  uint32_t frameAlignment, reserved;
};

struct sceVideodec2ComputeConfig
{
  uint64_t size;
  uint16_t pipeId, queueId;
  uint8_t checkMemoryType, reserved0;
  uint16_t reserved1;
};

struct sceVideodec2ComputeMemory
{
  uint64_t size, cpuGpuSize;
  void* cpuGpu;
};

struct sceVideodec2Input
{
  uint64_t size;
  void* au;
  uint64_t auSize, pts, dts, attached;
};

struct sceVideodec2Frame
{
  uint64_t size;
  void* buffer;
  uint64_t bufferSize;
  uint32_t accepted, reserved;
};

struct sceVideodec2Output
{
  uint64_t size;
  uint8_t valid, error, pictureCount, discarded;
  uint32_t codec, width, pitch, height, reserved;
  void* buffer;
  uint64_t bufferSize;
  uint32_t frameFormat, pitchBytes;
};

int32_t sceVideodec2QueryComputeMemoryInfo(sceVideodec2ComputeMemory* memory);
int32_t sceVideodec2AllocateComputeQueue(const sceVideodec2ComputeConfig* config,
                                         const sceVideodec2ComputeMemory* memory, void** queue);
int32_t sceVideodec2ReleaseComputeQueue(void* queue);
int32_t sceVideodec2QueryDecoderMemoryInfo(const sceVideodec2DecoderConfig* config,
                                           sceVideodec2DecoderMemory* memory);
int32_t sceVideodec2CreateDecoder(const sceVideodec2DecoderConfig* config,
                                  const sceVideodec2DecoderMemory* memory, void** decoder);
int32_t sceVideodec2DeleteDecoder(void* decoder);
int32_t sceVideodec2Decode(void* decoder, sceVideodec2Input* input, sceVideodec2Frame* frame,
                           sceVideodec2Output* output);
int32_t sceVideodec2Flush(void* decoder, sceVideodec2Frame* frame, sceVideodec2Output* output);
int32_t sceVideodec2Reset(void* decoder);

int32_t sceSysmoduleLoadModule(uint32_t id);

int64_t sceKernelGetDirectMemorySize(void);
int sceKernelAvailableFlexibleMemorySize(size_t* size);
int sceKernelAllocateDirectMemory(int64_t searchStart, int64_t searchEnd, size_t length,
                                  size_t alignment, int memoryType, int64_t* start);
int sceKernelMapDirectMemory(void** address, size_t length, int protection, int flags,
                             int64_t start, size_t alignment);
int sceKernelReleaseDirectMemory(int64_t start, size_t length);
int sceKernelMapNamedFlexibleMemory(void** address, size_t length, int protection, int flags,
                                    const char* name);
int sceKernelDebugOutText(int channel, const char* text);
}

namespace
{
constexpr uint32_t kSysmoduleVideodec2 = 207;
constexpr uint32_t kCodecH264 = 1;
constexpr uint32_t kCodecHEVC = 0x000ee049;
// VP9: the value the prosper project maps to VP9 (console-proven VP9 decode:
// BlackBearReloaded's ps5-hardware-video-decoding-research, EVO Player)
constexpr uint32_t kCodecVP9 = 2382845;
constexpr int kMemoryType = 12;       // direct memory type used on hardware
constexpr int kProtGpu = 0x32;        // CPU write + GPU read/write
constexpr int kProtCpuGpu = 0x33;     // CPU read/write + GPU read/write
constexpr int kProtCpu = 0x03;
constexpr size_t kInputSlot = 8 * 1024 * 1024;

size_t Align16k(size_t v)
{
  return (v + 0x3fff) & ~static_cast<size_t>(0x3fff);
}

void Log(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
void Log(const char* fmt, ...)
{
  char buf[256];
  va_list ap;
  va_start(ap, fmt);
  std::vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  sceKernelDebugOutText(0, buf);
}

std::string Hex(const char* what, int32_t rc)
{
  char buf[96];
  std::snprintf(buf, sizeof(buf), "%s failed (0x%08x)", what, static_cast<uint32_t>(rc));
  return buf;
}

} // namespace

namespace KODI::PLATFORM::PS5
{

// The decoder writes frames behind the CPU's back: drop any cached copy of
// the lines before reading them.
void CVideoDec2::InvalidateForRead(const void* data, size_t size)
{
  if (!data || size == 0)
    return;
  const auto first = reinterpret_cast<uintptr_t>(data) & ~uintptr_t{63};
  const auto end = reinterpret_cast<uintptr_t>(data) + size;
  for (uintptr_t p = first; p < end; p += 64)
    __builtin_ia32_clflush(reinterpret_cast<const void*>(p));
  __builtin_ia32_mfence();
}

uint64_t CVideoDec2::Fingerprint(const VideoDec2Picture& picture)
{
  // Sparse cache-line reads, not a whole-frame flush or GPU readback.
  uint64_t hash = 14695981039346656037ULL;
  const unsigned bytes = picture.bitDepth > 8 ? 2 : 1;
  for (unsigned plane = 0; plane < 2; ++plane)
  {
    const unsigned rows = plane == 0 ? picture.height : picture.chromaHeight;
    const uint8_t* base = picture.data +
                         (plane == 0 ? 0 : static_cast<size_t>(picture.pitch) *
                                               picture.chromaOffsetHeight);
    for (unsigned y = 0; y < 4; ++y)
      for (unsigned x = 0; x < 4; ++x)
      {
        const unsigned column = (picture.width - 1) * (2 * x + 1) / 8;
        const unsigned row = (rows - 1) * (2 * y + 1) / 8;
        const uint8_t* sample = base + static_cast<size_t>(row) * picture.pitch + column * bytes;
        InvalidateForRead(sample, bytes);
        for (unsigned b = 0; b < bytes; ++b)
          hash = (hash ^ sample[b]) * 1099511628211ULL;
      }
  }
  return hash;
}

void CVideoDec2::CaptureContent(VideoDec2Picture& picture)
{
  if (!m_contentTracing || picture.frameIndex < 0 || picture.width == 0 ||
      picture.height == 0 || picture.chromaHeight == 0)
    return;
  picture.diagnosticFingerprint = Fingerprint(picture);
  uint64_t previousSequence;
  uint64_t previousFingerprint;
  uint64_t matchingSequence = 0;
  {
    std::lock_guard<std::mutex> lock(m_contentMutex);
    const int index = picture.frameIndex;
    picture.diagnosticSequence = ++m_contentSequence;
    m_nonzeroOutputStatus += picture.outputStatus != 0;
    picture.diagnosticGeneration = m_frameGenerations[index];
    previousSequence = m_frameSequences[index];
    previousFingerprint = m_frameFingerprints[index];
    if (previousSequence != 0 && previousFingerprint == picture.diagnosticFingerprint)
      ++m_sameSlotContents;
    for (unsigned i = 0; i < 128; ++i)
      if (m_recentSequences[i] != 0 &&
          m_recentFingerprints[i] == picture.diagnosticFingerprint)
        matchingSequence = std::max(matchingSequence, m_recentSequences[i]);
    m_repeatedContents += matchingSequence != 0;
    picture.diagnosticMatchingSequence = matchingSequence;
    const unsigned slot = picture.diagnosticSequence % 128;
    m_recentFingerprints[slot] = picture.diagnosticFingerprint;
    m_recentSequences[slot] = picture.diagnosticSequence;
    m_frameSequences[index] = picture.diagnosticSequence;
    m_frameFingerprints[index] = picture.diagnosticFingerprint;
  }
  if (picture.diagnosticSequence <= 120)
    CLog::Log(LOGINFO,
              "PS5 content: decoder={} stage=return seq={} frame={} generation={} hash={:016x} "
              "previousSlotSeq={} previousSlotHash={:016x} matchingRecentSeq={} immediate={} "
              "outputStatus={:02x} pictureCount={}",
              m_instance, picture.diagnosticSequence, picture.frameIndex,
              picture.diagnosticGeneration, picture.diagnosticFingerprint, previousSequence,
              previousFingerprint, matchingSequence, picture.immediate, picture.outputStatus,
              picture.pictureCount);
}

void CVideoDec2::TraceContent(const char* stage, const VideoDec2Picture& picture, double pts)
{
  if (picture.diagnosticSequence == 0)
    return;
  const uint64_t hash = Fingerprint(picture);
  const bool changed = hash != picture.diagnosticFingerprint;
  bool reused;
  bool report;
  bool repeatedShownReport = false;
  {
    std::lock_guard<std::mutex> lock(m_contentMutex);
    reused = m_frameGenerations[picture.frameIndex] != picture.diagnosticGeneration;
    ++m_contentChecks;
    m_contentChanges += changed;
    m_generationChanges += reused;
    report = (changed || reused) && m_contentWarnings < 16;
    if (report)
      ++m_contentWarnings;
    if (picture.diagnosticMatchingSequence != 0)
    {
      if (std::strcmp(stage, "shown") == 0)
      {
        ++m_repeatedShown;
        repeatedShownReport = m_repeatReports < 16;
        if (repeatedShownReport)
          ++m_repeatReports;
      }
      else if (std::strcmp(stage, "hidden") == 0)
        ++m_repeatedHidden;
    }
  }
  if (picture.diagnosticSequence <= 120 || report || repeatedShownReport)
    CLog::Log(LOGINFO,
              "PS5 content: decoder={} stage={} seq={} frame={} generation={} pts={} "
              "hash={:016x} returnHash={:016x} changed={} reused={} matchingRecentSeq={} "
              "outputStatus={:02x} pictureCount={}",
              m_instance, stage, picture.diagnosticSequence, picture.frameIndex,
              picture.diagnosticGeneration, pts, hash, picture.diagnosticFingerprint, changed,
              reused, picture.diagnosticMatchingSequence, picture.outputStatus, picture.pictureCount);
}

CVideoDec2::~CVideoDec2()
{
  Close();
}

bool CVideoDec2::AllocateDirect(size_t size, int protection, DirectMemory& out, std::string& error)
{
  int64_t start = -1;
  int rc = sceKernelAllocateDirectMemory(0, sceKernelGetDirectMemorySize(), size, 0x4000,
                                         kMemoryType, &start);
  if (rc != 0)
  {
    error = Hex("sceKernelAllocateDirectMemory", rc);
    return false;
  }
  void* address = nullptr;
  rc = sceKernelMapDirectMemory(&address, size, protection, 0, start, 0x4000);
  if (rc != 0 || !address)
  {
    sceKernelReleaseDirectMemory(start, size);
    error = Hex("sceKernelMapDirectMemory", rc);
    return false;
  }
  out.address = address;
  out.size = size;
  out.start = start;
  return true;
}

void CVideoDec2::FreeDirect(DirectMemory& mem)
{
  if (mem.address)
    munmap(mem.address, mem.size);
  if (mem.start >= 0)
    sceKernelReleaseDirectMemory(mem.start, mem.size);
  mem = DirectMemory{};
}

int32_t CVideoDec2::LoadModule()
{
  // main() preloads before sandbox escape. Later callers must not reload:
  // the loader can reject even an already loaded module after escape.
  static const int32_t result = []
  {
    const int32_t status = sceSysmoduleLoadModule(kSysmoduleVideodec2);
    Log("[kodi-ps5] video decoder module preload: 0x%08x\n", static_cast<uint32_t>(status));
    return status;
  }();
  return result;
}

bool CVideoDec2::Open(VideoDec2Codec codec, int width, int height, std::string& error,
                     bool interlaced, int streamLevel)
{
  Close();
  const bool uhd = width > 1920 || height > 1088;
  const bool vp9 = codec == VideoDec2Codec::VP9 || codec == VideoDec2Codec::VP9Profile2;
  m_contentTracing = vp9 && std::getenv("KODI_PS5_VIDEO_DEBUG") != nullptr;
  {
    std::lock_guard<std::mutex> lock(m_contentMutex);
    m_contentSequence = 0;
    m_contentChecks = m_contentChanges = m_generationChanges = m_sameSlotContents =
        m_contentWarnings = m_repeatedContents = m_repeatedShown = m_repeatedHidden =
            m_repeatReports = m_nonzeroOutputStatus = 0;
    for (unsigned i = 0; i < kFrameBuffers; ++i)
      m_frameGenerations[i] = m_frameFingerprints[i] = m_frameSequences[i] = 0;
    for (unsigned i = 0; i < 128; ++i)
      m_recentFingerprints[i] = m_recentSequences[i] = 0;
  }
  const bool h264 = codec == VideoDec2Codec::H264 || codec == VideoDec2Codec::H264High10;
  m_codecType = h264 ? kCodecH264 : (vp9 ? kCodecVP9 : kCodecHEVC);
  m_tenBit = codec == VideoDec2Codec::HEVCMain10 || codec == VideoDec2Codec::VP9Profile2 ||
             codec == VideoDec2Codec::H264High10;
  m_formatLogged = false;

  int32_t rc = LoadModule();
  if (rc < 0)
  {
    error = Hex("loading the video decoder module", rc);
    return false;
  }
  m_moduleLoaded = true;

  // compute queue the decoder runs its GPU work on
  sceVideodec2ComputeMemory compute{};
  compute.size = sizeof(compute);
  rc = sceVideodec2QueryComputeMemoryInfo(&compute);
  if (rc != 0)
  {
    error = Hex("sceVideodec2QueryComputeMemoryInfo", rc);
    return false;
  }
  if (!AllocateDirect(Align16k(compute.cpuGpuSize), kProtCpuGpu, m_computeMemory, error))
    return false;
  compute.cpuGpu = m_computeMemory.address;
  compute.cpuGpuSize = m_computeMemory.size;
  sceVideodec2ComputeConfig computeConfig{};
  computeConfig.size = sizeof(computeConfig);
  rc = sceVideodec2AllocateComputeQueue(&computeConfig, &compute, &m_computeQueue);
  if (rc != 0)
  {
    m_computeQueue = nullptr;
    error = Hex("sceVideodec2AllocateComputeQueue", rc);
    return false;
  }

  // decoder: sized for 1080p or 2160p streams, with a full-size DPB for files
  sceVideodec2DecoderConfig config{};
  config.size = sizeof(config);
  config.resourceType = 1;
  config.codecType = m_codecType;
  if (h264)
  {
    config.profile = m_tenBit ? 110 : 100; // profile_idc: High 10 / High // High (covers Baseline/Main)
    config.maxLevel = uhd ? 52 : 51;
  }
  else if (vp9)
  {
    config.profile = m_tenBit ? 2 : 0; // VP9 profile
    config.maxLevel = uhd ? 51 : 41;   // first candidate; see the query below
  }
  else
  {
    config.profile = m_tenBit ? 2 : 1; // HEVC profile_idc: Main 10 / Main
    config.maxLevel = uhd ? 153 : 123;
  }
  config.maxWidth = uhd ? 3840 : 1920;
  // VP9 surfaces are exactly the frame size (the research's proven modes)
  config.maxHeight = vp9 ? (uhd ? 2160 : 1080) : (uhd ? 2176 : 1088);
  config.maxDpbFrames = 16;
  // Pipeline depth 1: the decode call returns the picture of the access unit
  // it was given, complete. Deeper pipelines (2 and 4, traced on hardware)
  // produce black pictures for about a second after every reset - the
  // decoder decodes without error but against nothing, whatever keyframe or
  // parameter sets it is given - and 10-bit decoders refuse them outright
  // (0x811d0111). Real time at 4K60 is therefore the decoder's own job.
  // 8K VP9 (configured at the stream's size) was tried as well: at depth 1
  // the decoder needs 35-75 ms a picture (more than a 24 fps frame period),
  // with or without Kodi drawing the video and with more CPUs; at depth 2
  // stream errors (0x811d0303), black flashes and distortion. Above 4K
  // streams are left to FFmpeg (see CDVDVideoCodecPS5::Open).
  config.pipelineDepth = 1;
  config.computeQueue = reinterpret_cast<uint64_t>(m_computeQueue);
  config.cpuAffinity = 0x3f;
  config.cpuPriority = 700;
  config.optimizeProgressive = interlaced ? 0 : 1; // interlaced: probe (kodi-hw-interlaced)

  // The default maximum level (HEVC 4.1 / H.264 5.1 at 1080p) covers most
  // files, but encodes tagged higher (e.g. 1080p HEVC at level 5.0/5.1) were
  // configured too low and every access unit was refused (0x811d0303), with
  // no picture ever produced. Raise the level to the stream's own when it is
  // above the default; if the decoder refuses that configuration, fall back
  // to exactly the default one below, so streams that work are unaffected.
  const uint32_t defaultLevel = config.maxLevel;
  if (!vp9 && streamLevel > 0 && static_cast<uint32_t>(streamLevel) > defaultLevel)
    config.maxLevel = std::min<uint32_t>(static_cast<uint32_t>(streamLevel), h264 ? 52u : 156u);

  sceVideodec2DecoderMemory memory{};
  memory.size = sizeof(memory);
  rc = sceVideodec2QueryDecoderMemoryInfo(&config, &memory);
  if (rc != 0 && !vp9 && config.maxLevel != defaultLevel)
  {
    Log("[kodi-ps5] videodec2: level %u refused (0x%x), using the default level %u\n",
        config.maxLevel, static_cast<unsigned>(rc), defaultLevel);
    config.maxLevel = defaultLevel;
    memory = {};
    memory.size = sizeof(memory);
    rc = sceVideodec2QueryDecoderMemoryInfo(&config, &memory);
  }
  if (rc != 0 && vp9)
  {
    // VP9's level numbering and reference-frame budget are not documented:
    // take the first combination the decoder accepts, and say which
    const uint32_t levels[] = {uhd ? 51u : 41u, uhd ? 153u : 123u, 0u};
    const int32_t dpbs[] = {16, 8, 4};
    for (const int32_t dpb : dpbs)
    {
      for (const uint32_t level : levels)
      {
        config.maxLevel = level;
        config.maxDpbFrames = dpb;
        memory = {};
        memory.size = sizeof(memory);
        rc = sceVideodec2QueryDecoderMemoryInfo(&config, &memory);
        if (rc == 0)
          break;
      }
      if (rc == 0)
        break;
    }
  }
  if (rc != 0)
  {
    error = Hex("sceVideodec2QueryDecoderMemoryInfo", rc);
    return false;
  }
  if (vp9)
    Log("[kodi-ps5] videodec2: VP9 profile %u accepted with level %u, %d reference frames\n",
        config.profile, config.maxLevel, config.maxDpbFrames);
  Log("[kodi-ps5] videodec2: pipeline depth %d, level %u (stream level %d)\n", config.pipelineDepth,
      config.maxLevel, streamLevel);
  Log("[kodi-ps5] videodec2: %s %dx%d needs cpu=%llx gpu=%llx shared=%llx frame=%llx\n",
      h264 ? (m_tenBit ? "H.264 High 10" : "H.264")
           : (vp9 ? (m_tenBit ? "VP9 Profile 2" : "VP9") : (m_tenBit ? "HEVC Main10" : "HEVC")),
      config.maxWidth, config.maxHeight,
      static_cast<unsigned long long>(memory.cpuSize),
      static_cast<unsigned long long>(memory.gpuSize),
      static_cast<unsigned long long>(memory.cpuGpuSize),
      static_cast<unsigned long long>(memory.maxFrameSize));

  m_cpuWorkspaceSize = Align16k(memory.cpuSize);
  if (m_cpuWorkspaceSize)
  {
    rc = sceKernelMapNamedFlexibleMemory(&m_cpuWorkspace, m_cpuWorkspaceSize, kProtCpu, 0,
                                         "KodiVdecCpu");
    if (rc != 0)
    {
      m_cpuWorkspace = nullptr;
      /* A title has only ~448 MiB of flexible memory, and every thread stack
         (8 MiB minimum, see thread_stack.c) comes out of it, so with enough
         threads alive it runs out (0x8002000c = ENOMEM) and 4K HEVC fell back
         to software decoding. The workspace is plain CPU memory: take it from
         direct memory instead, as the heap does. The flexible path above is
         still tried first, so consoles where it works are unaffected. */
      size_t flexFree = 0;
      sceKernelAvailableFlexibleMemorySize(&flexFree);
      std::string directError;
      if (!AllocateDirect(m_cpuWorkspaceSize, kProtCpu, m_cpuWorkspaceDirect, directError))
      {
        error = Hex("mapping the decoder's CPU workspace", rc) + " (flexible free " +
                std::to_string(flexFree >> 20) + " MiB); direct fallback: " + directError;
        return false;
      }
      Log("[kodi-ps5] videodec2: CPU workspace %llx from direct memory (flexible map 0x%x, %zu MiB "
          "flexible free)\n",
          static_cast<unsigned long long>(m_cpuWorkspaceSize), static_cast<unsigned>(rc),
          flexFree >> 20);
      m_cpuWorkspace = m_cpuWorkspaceDirect.address;
    }
  }
  memory.cpu = m_cpuWorkspace;
  memory.cpuSize = m_cpuWorkspaceSize;

  if (!AllocateDirect(Align16k(memory.gpuSize), kProtGpu, m_gpuMemory, error))
    return false;
  memory.gpu = m_gpuMemory.address;
  memory.gpuSize = m_gpuMemory.size;
  if (memory.cpuGpuSize)
  {
    if (!AllocateDirect(Align16k(memory.cpuGpuSize), kProtCpuGpu, m_cpuGpuMemory, error))
      return false;
    memory.cpuGpu = m_cpuGpuMemory.address;
    memory.cpuGpuSize = m_cpuGpuMemory.size;
  }

  m_inputSize = kInputSlot;
  const int ioProtection = vp9 ? kProtCpuGpu : kProtGpu;
  if (vp9)
    CLog::Log(LOGINFO,
              "PS5 VP9: input/output direct memory protection={:#x} (CPU read/write, GPU read/write)",
              ioProtection);
  if (!AllocateDirect(m_inputSize, ioProtection, m_inputMemory, error))
    return false;
  m_frameSize = Align16k(memory.maxFrameSize);
  if (!AllocateDirect(m_frameSize * kFrameBuffers, ioProtection, m_frameMemory, error))
    return false;

  rc = sceVideodec2CreateDecoder(&config, &memory, &m_decoder);
  if (rc != 0)
  {
    m_decoder = nullptr;
    error = Hex("sceVideodec2CreateDecoder", rc);
    return false;
  }
  rc = sceVideodec2Reset(m_decoder);
  if (rc != 0)
  {
    error = Hex("sceVideodec2Reset", rc);
    return false;
  }
  Log("[kodi-ps5] videodec2: decoder ready (%u frame buffers of %zu KiB)\n", kFrameBuffers,
      m_frameSize / 1024);
  m_streamHeight = static_cast<uint32_t>(height);
  return true;
}

uint64_t CVideoDec2::NextInstance()
{
  static std::atomic<uint64_t> next{1};
  return next.fetch_add(1, std::memory_order_relaxed);
}

void CVideoDec2::Close()
{
  // Traced straight to the klog, step by step: if a call ever blocks here, the
  // last line names it even when nothing else gets out.
  const bool traced = m_decoder || m_computeQueue;
  if (traced && m_contentTracing)
  {
    std::lock_guard<std::mutex> lock(m_contentMutex);
    CLog::Log(LOGINFO,
              "PS5 content summary: decoder={} outputs={} checks={} changed={} reused={} "
              "sameSlotContents={} repeatedContents={} repeatedShown={} repeatedHidden={} "
              "nonzeroOutputStatus={} (identical samples can also mean static content)",
              m_instance, m_contentSequence, m_contentChecks, m_contentChanges,
              m_generationChanges, m_sameSlotContents, m_repeatedContents, m_repeatedShown,
              m_repeatedHidden, m_nonzeroOutputStatus);
  }
  const auto start = std::chrono::steady_clock::now();
  auto elapsed = [&start]
  {
    return static_cast<long long>(std::chrono::duration_cast<std::chrono::milliseconds>(
                                      std::chrono::steady_clock::now() - start)
                                      .count());
  };
  if (traced)
    Log("[ps5vdec] decoder %llu: closing\n", static_cast<unsigned long long>(m_instance));
  if (m_decoder)
  {
    const int32_t rc = sceVideodec2DeleteDecoder(m_decoder);
    Log("[ps5vdec] decoder %llu: deleted (%#x, %lld ms)\n",
        static_cast<unsigned long long>(m_instance), static_cast<unsigned>(rc), elapsed());
  }
  m_decoder = nullptr;
  if (m_computeQueue)
  {
    const int32_t rc = sceVideodec2ReleaseComputeQueue(m_computeQueue);
    Log("[ps5vdec] decoder %llu: compute queue released (%#x, %lld ms)\n",
        static_cast<unsigned long long>(m_instance), static_cast<unsigned>(rc), elapsed());
  }
  m_computeQueue = nullptr;
  FreeDirect(m_frameMemory);
  FreeDirect(m_inputMemory);
  FreeDirect(m_cpuGpuMemory);
  FreeDirect(m_gpuMemory);
  FreeDirect(m_computeMemory);
  if (m_cpuWorkspaceDirect.address)
    FreeDirect(m_cpuWorkspaceDirect); // fallback path: unmap + release direct memory
  else if (m_cpuWorkspace)
    munmap(m_cpuWorkspace, m_cpuWorkspaceSize);
  m_cpuWorkspace = nullptr;
  m_cpuWorkspaceSize = 0;
  m_nextFrame = 0;
  m_streamHeight = 0;
  if (traced)
    Log("[ps5vdec] decoder %llu: closed, memory released (%lld ms)\n",
        static_cast<unsigned long long>(m_instance), elapsed());
  std::lock_guard<std::mutex> lock(m_frameMutex);
  for (auto& state : m_frameState)
    state = FrameState::Free;
  for (auto& references : m_frameReferences)
    references = 0;
  m_sharedFrameLogged = false;
}

uint8_t* CVideoDec2::NextFrameBuffer(int& index)
{
  uint8_t* base = static_cast<uint8_t*>(m_frameMemory.address);
  if (!m_pooled)
  {
    // ring: every frame is overwritten kFrameBuffers decodes later
    index = static_cast<int>(m_nextFrame);
    m_nextFrame = (m_nextFrame + 1) % kFrameBuffers;
    return base + static_cast<size_t>(index) * m_frameSize;
  }
  std::lock_guard<std::mutex> lock(m_frameMutex);
  for (unsigned n = 0; n < kFrameBuffers; ++n)
  {
    const unsigned i = (m_nextFrame + n) % kFrameBuffers;
    if (m_frameState[i] == FrameState::Free)
    {
      m_nextFrame = (i + 1) % kFrameBuffers;
      index = static_cast<int>(i);
      return base + static_cast<size_t>(i) * m_frameSize;
    }
  }
  index = -1;
  return nullptr;
}

int CVideoDec2::FrameIndexOf(const void* buffer) const
{
  const auto* base = static_cast<const uint8_t*>(m_frameMemory.address);
  const auto* buf = static_cast<const uint8_t*>(buffer);
  if (!base || buf < base || buf >= base + m_frameSize * kFrameBuffers)
    return -1;
  return static_cast<int>(static_cast<size_t>(buf - base) / m_frameSize);
}

bool CVideoDec2::HasFreeFrame() const
{
  if (!m_pooled)
    return true;
  std::lock_guard<std::mutex> lock(m_frameMutex);
  for (const auto& state : m_frameState)
    if (state == FrameState::Free)
      return true;
  return false;
}

void CVideoDec2::ReleaseFrame(int index)
{
  if (index < 0 || index >= static_cast<int>(kFrameBuffers))
    return;
  std::lock_guard<std::mutex> lock(m_frameMutex);
  if (m_frameState[index] == FrameState::Kodi && m_frameReferences[index] > 0)
  {
    if (--m_frameReferences[index] == 0)
      m_frameState[index] = FrameState::Free;
  }
}

// kodi-video-debug, first picture: sample interleaved pairs across the width around
// both the reported and stream luma heights to identify the actual plane edge.
void CVideoDec2::LogPlaneBoundary(const uint8_t* buf,
                                 uint32_t rowBytes,
                                 uint32_t reportedHeight,
                                 uint32_t streamHeight) const
{
  const unsigned bytes = m_tenBit ? 2 : 1;
  constexpr unsigned kHorizontalSamples = 5;
  const uint32_t boundary = std::min(reportedHeight, streamHeight);
  const uint32_t first = boundary > 8 ? boundary - 8 : 0;
  const uint32_t last = std::max(reportedHeight, streamHeight) + 12;
  for (uint32_t row = first; row <= last; row += 2)
  {
    Log("[kodi-ps5] videodec2 (kodi-video-debug): row %u%s%s:",
        row, row == reportedHeight ? " reported-end" : "",
        row == streamHeight ? " stream-end" : "");
    for (unsigned sample = 0; sample < kHorizontalSamples; ++sample)
    {
      size_t at = static_cast<size_t>(row) * rowBytes +
                  rowBytes * (2 * sample + 1) / (2 * kHorizontalSamples);
      at -= at % (2 * bytes);
      if (at + 2 * bytes > m_frameSize)
        break;
      const uint8_t* p = buf + at;
      InvalidateForRead(p, 2 * bytes);
      const unsigned firstValue =
          bytes == 2 ? reinterpret_cast<const uint16_t*>(p)[0] : p[0];
      const unsigned secondValue =
          bytes == 2 ? reinterpret_cast<const uint16_t*>(p)[1] : p[1];
      Log(" %u:%u,%u", sample, firstValue, secondValue);
    }
    Log("\n");
  }
}

bool CVideoDec2::ToPicture(const void* out, VideoDec2Picture* picture)
{
  const auto* output = static_cast<const sceVideodec2Output*>(out);
  if (!output->valid || output->error || !output->buffer || output->pitch == 0)
    return false;
  const auto* base = static_cast<const uint8_t*>(m_frameMemory.address);
  const auto* buf = static_cast<const uint8_t*>(output->buffer);
  if (buf < base || buf >= base + m_frameSize * kFrameBuffers)
    return false; // not one of ours
  // Bytes per row: for 8-bit NV12 the pitch; for 10-bit (16 bits per
  // sample) the decoder's byte pitch, or twice the pitch if it gives none.
  const uint32_t rowBytes =
      m_tenBit ? (output->pitchBytes ? output->pitchBytes : output->pitch * 2) : output->pitch;
  if (!m_formatLogged)
  {
    m_formatLogged = true;
    Log("[kodi-ps5] videodec2: first picture %ux%u, frameFormat %u, pitch %u, pitchBytes %u, "
        "pictureCount %u, reserved %u, bufferSize %llu (%llu rows at 4:2:0), stream height %u "
        "-> %u bytes per row (%s)\n",
        output->width, output->height, output->frameFormat, output->pitch, output->pitchBytes,
        output->pictureCount, output->reserved, static_cast<unsigned long long>(output->bufferSize),
        static_cast<unsigned long long>(rowBytes ? output->bufferSize * 2 / 3 / rowBytes : 0),
        m_streamHeight, rowBytes, m_tenBit ? "10-bit" : "8-bit NV12");
    if (std::getenv("KODI_PS5_VIDEO_DEBUG"))
      LogPlaneBoundary(buf, rowBytes, output->height, m_streamHeight);
  }
  picture->frameIndex = FrameIndexOf(buf);
  if (m_pooled && picture->frameIndex >= 0)
  {
    std::lock_guard<std::mutex> lock(m_frameMutex);
    // VP9 can show the same reference frame again while an earlier picture still holds it.
    const unsigned references = ++m_frameReferences[picture->frameIndex];
    if (references > 1 && !m_sharedFrameLogged)
    {
      m_sharedFrameLogged = true;
      Log("[kodi-ps5] videodec2: frame %d returned while still held (%u picture references); "
          "reuse deferred until all pictures release it\n", picture->frameIndex, references);
    }
    m_frameState[picture->frameIndex] = FrameState::Kodi;
  }
  picture->data = buf;
  picture->width = output->width;
  picture->height = output->height;
  // The decoder pads luma before chroma; the stream height only limits visible chroma rows.
  picture->chromaOffsetHeight = output->height;
  const uint32_t visibleHeight =
      m_streamHeight > 0 ? std::min(m_streamHeight, output->height) : output->height;
  picture->chromaHeight = (visibleHeight + 1) / 2;
  picture->pitch = rowBytes;
  picture->bitDepth = m_tenBit ? 10 : 8;
  picture->outputStatus = output->discarded;
  picture->pictureCount = output->pictureCount;
  if (m_cpuReadsFrames)
    InvalidateForRead(buf, static_cast<size_t>(rowBytes) * output->height * 3 / 2);
  CaptureContent(*picture);
  return true;
}

bool CVideoDec2::Decode(const uint8_t* au, size_t size, bool& gotPicture,
                        VideoDec2Picture* picture, std::string& error)
{
  gotPicture = false;
  if (!m_decoder)
  {
    error = "decoder not open";
    return false;
  }
  if (size == 0 || size > m_inputSize)
  {
    error = "access unit too large";
    return false;
  }
  m_stalled = false;
  int frameIndex = -1;
  uint8_t* frameBuffer = NextFrameBuffer(frameIndex);
  if (!frameBuffer)
  {
    m_stalled = true; // pooled: every frame is held; Kodi releases some first
    // If Kodi holds none of them, the decoder kept every frame it was offered
    // without returning pictures: worth knowing, once.
    static bool warned = false;
    if (!warned)
    {
      std::lock_guard<std::mutex> lock(m_frameMutex);
      unsigned kodi = 0;
      for (const auto& state : m_frameState)
        kodi += state == FrameState::Kodi;
      if (kodi == 0)
      {
        warned = true;
        Log("[kodi-ps5] videodec2: all %u frames are with the decoder and none with Kodi: "
            "the decoder is not returning pictures\n", kFrameBuffers);
      }
    }
    return true;
  }
  std::memcpy(m_inputMemory.address, au, size);

  sceVideodec2Input input{};
  input.size = sizeof(input);
  input.au = m_inputMemory.address;
  input.auSize = size;
  input.pts = 0;
  input.dts = UINT64_MAX;
  sceVideodec2Frame frame{};
  frame.size = sizeof(frame);
  frame.buffer = frameBuffer;
  frame.bufferSize = m_frameSize;
  sceVideodec2Output output{};
  output.size = sizeof(output);

  const int32_t rc = sceVideodec2Decode(m_decoder, &input, &frame, &output);
  picture->immediate = output.buffer == frameBuffer;
  picture->offeredIndex = frameIndex;
  picture->offeredAccepted = frame.accepted != 0;
  picture->pictureCount = output.pictureCount;
  if (m_contentTracing && frame.accepted)
  {
    std::lock_guard<std::mutex> lock(m_contentMutex);
    ++m_frameGenerations[frameIndex];
  }
  if (m_pooled && frame.accepted)
  {
    std::lock_guard<std::mutex> lock(m_frameMutex);
    m_frameState[frameIndex] = FrameState::Decoder;
  }
  if (rc != 0 || output.error)
  {
    error = rc != 0 ? Hex("sceVideodec2Decode", rc) : "decoder reported a stream error";
    return false;
  }
  gotPicture = ToPicture(&output, picture);
  return true;
}

bool CVideoDec2::Flush(VideoDec2Picture* picture)
{
  if (!m_decoder)
    return false;
  int frameIndex = -1;
  uint8_t* frameBuffer = NextFrameBuffer(frameIndex);
  if (!frameBuffer)
    return false;
  sceVideodec2Frame frame{};
  frame.size = sizeof(frame);
  frame.buffer = frameBuffer;
  frame.bufferSize = m_frameSize;
  sceVideodec2Output output{};
  output.size = sizeof(output);
  if (sceVideodec2Flush(m_decoder, &frame, &output) != 0)
    return false;
  picture->immediate = output.buffer == frameBuffer;
  picture->offeredIndex = frameIndex;
  picture->offeredAccepted = frame.accepted != 0;
  picture->pictureCount = output.pictureCount;
  if (m_contentTracing && frame.accepted)
  {
    std::lock_guard<std::mutex> lock(m_contentMutex);
    ++m_frameGenerations[frameIndex];
  }
  if (m_pooled && frame.accepted)
  {
    std::lock_guard<std::mutex> lock(m_frameMutex);
    m_frameState[frameIndex] = FrameState::Decoder;
  }
  return ToPicture(&output, picture);
}

void CVideoDec2::Reset()
{
  if (m_decoder)
    sceVideodec2Reset(m_decoder);
  // the decoder lets go of every frame it held; Kodi's pictures stay Kodi's
  std::lock_guard<std::mutex> lock(m_frameMutex);
  for (auto& state : m_frameState)
    if (state == FrameState::Decoder)
      state = FrameState::Free;
  m_stalled = false;
}

} // namespace KODI::PLATFORM::PS5
