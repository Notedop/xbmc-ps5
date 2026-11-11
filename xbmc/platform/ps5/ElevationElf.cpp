/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ElevationMethods.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>

#include <fcntl.h>
#include <unistd.h>

extern "C" int sceKernelDebugOutText(int channel, const char* text);

namespace
{
using namespace KODI::PLATFORM::PS5;

void Klog(const char* text)
{
  sceKernelDebugOutText(0, text);
}

void Klogf(const char* format, ...) __attribute__((format(printf, 1, 2)));
void Klogf(const char* format, ...)
{
  char buffer[512];
  std::va_list args;
  va_start(args, format);
  std::vsnprintf(buffer, sizeof(buffer), format, args);
  va_end(args);
  Klog(buffer);
}

// Wire ABI of the Lapy owned-root helper
// (https://github.com/mpereiraesaa/PS5-Lapy-JB-Daemon,
// source/lapy_elevation_protocol.h, LGPL-2.1-or-later; reimplemented here to
// keep Kodi's GPL-2 caller independent of the upstream header).
constexpr uint32_t kLapyMagic = 0x31564c45; // "ELV1"
constexpr uint16_t kLapyVersion = 1;
enum LapyKind : uint32_t
{
  kRequest = 1,
  kPrepare = 2,
  kPrepared = 3,
  kResponse = 4,
};
enum LapyStatus : uint32_t
{
  kStatusOk = 0,
  kStatusInvalidRequest = 1,
  kStatusUnsupportedVersion = 2,
  kStatusUnsupportedCapability = 3,
  kStatusTargetMismatch = 4,
  kStatusUnavailable = 5,
  kStatusPrepareFailed = 6,
  kStatusApplyFailed = 7,
  kStatusRollbackFailed = 8,
  kStatusTransportError = 9,
  kStatusProtocolError = 10,
};
constexpr uint32_t kCapabilityFilesystem = 1;

struct LapyMessage
{
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t kind;
  uint32_t capability;
  uint32_t pid;
  uint32_t status;
};
static_assert(sizeof(LapyMessage) == 24);

// Bounded waits, in microseconds, for connection and every transfer phase;
// same option set and value the helper exchange is built and tested with.
constexpr int kIoTimeoutUs = 5'000'000;

constexpr const char* kHelperElf = "/app0/share/kodi/system/elevation/lapy.elf";

const char* LapyStatusName(uint32_t status)
{
  switch (status)
  {
    case kStatusOk: return "ok";
    case kStatusInvalidRequest: return "invalid-request";
    case kStatusUnsupportedVersion: return "unsupported-version";
    case kStatusUnsupportedCapability: return "unsupported-capability";
    case kStatusTargetMismatch: return "target-mismatch";
    case kStatusUnavailable: return "unavailable";
    case kStatusPrepareFailed: return "prepare-failed";
    case kStatusApplyFailed: return "apply-failed";
    case kStatusRollbackFailed: return "rollback-failed";
    case kStatusTransportError: return "transport-error";
    case kStatusProtocolError: return "protocol-error";
    default: return "unknown-status";
  }
}

bool SendAll(int sock, const void* data, size_t length)
{
  const uint8_t* bytes = static_cast<const uint8_t*>(data);
  while (length > 0)
  {
    const int sent = sceNetSend(sock, bytes, length, 0);
    if (sent <= 0)
      return false;
    bytes += sent;
    length -= static_cast<size_t>(sent);
  }
  return true;
}

bool RecvAll(int sock, void* data, size_t length)
{
  uint8_t* bytes = static_cast<uint8_t*>(data);
  while (length > 0)
  {
    const int got = sceNetRecv(sock, bytes, length, 0);
    if (got <= 0)
      return false;
    bytes += got;
    length -= static_cast<size_t>(got);
  }
  return true;
}

// True when the helper was authorized (prepared sent) and completed the
// exchange; replyStatus then carries its status, with a transport error
// standing in for completion that was never established.
bool RunExchange(int sock, uint32_t pid, uint32_t& replyStatus)
{
  LapyMessage request{};
  request.magic = kLapyMagic;
  request.version = kLapyVersion;
  request.size = sizeof(request);
  request.kind = kRequest;
  request.capability = kCapabilityFilesystem;
  request.pid = pid;
  if (!SendAll(sock, &request, sizeof(request)))
  {
    Klog("[kodi-ps5] elevation-elf: request send failed\n");
    return false;
  }
  LapyMessage prepare{};
  if (!RecvAll(sock, &prepare, sizeof(prepare)))
  {
    Klog("[kodi-ps5] elevation-elf: no prepare reply after full request; "
         "helper never authorized to apply\n");
    return false;
  }
  const bool validPrepare = prepare.magic == kLapyMagic && prepare.version == kLapyVersion &&
                            prepare.size == sizeof(prepare) && prepare.kind == kPrepare &&
                            prepare.capability == kCapabilityFilesystem && prepare.pid == pid;
  if (!validPrepare)
  {
    if (prepare.magic == kLapyMagic && prepare.kind == kResponse)
      Klogf("[kodi-ps5] elevation-elf: helper refused request (%s)\n",
            LapyStatusName(prepare.status));
    else
      Klog("[kodi-ps5] elevation-elf: unexpected frame before authorize; "
           "helper never authorized to apply\n");
    return false;
  }
  // Cooperative credential step in Kodi's own startup process: the helper
  // applies only after this confirms we can act on our own credentials.
  if (seteuid(geteuid()) != 0)
  {
    Klogf("[kodi-ps5] elevation-elf: seteuid failed (%s); helper never authorized\n",
          std::strerror(errno));
    return false;
  }
  request.kind = kPrepared;
  if (!SendAll(sock, &request, sizeof(request)))
  {
    Klog("[kodi-ps5] elevation-elf: authorize send failed; helper never authorized\n");
    return false;
  }
  // From here the helper is authorized and may act on Kodi.
  LapyMessage response{};
  if (!RecvAll(sock, &response, sizeof(response)))
  {
    Klog("[kodi-ps5] elevation-elf: no response after authorize; completion uncertain\n");
    replyStatus = kStatusTransportError;
    return true;
  }
  const bool validResponse = response.magic == kLapyMagic && response.version == kLapyVersion &&
                             response.size == sizeof(response) && response.kind == kResponse &&
                             response.capability == kCapabilityFilesystem && response.pid == pid;
  if (!validResponse)
  {
    Klog("[kodi-ps5] elevation-elf: unexpected frame after authorize; "
         "completion uncertain\n");
    replyStatus = kStatusTransportError;
    return true;
  }
  replyStatus = response.status;
  return true;
}
} // namespace

namespace KODI::PLATFORM::PS5
{
void EnsureSceNet()
{
  static const bool ready = []
  {
    sceNetInit(); // "already initialized" is fine
    sceNetPoolCreate("kodi-elev", 0x10000, 0); // a heap for socket buffers
    return true;
  }();
  (void)ready;
}

MethodOutcome TryElfLoader(int port)
{
  EnsureSceNet();

  const int sock = sceNetSocket("kodi-elevation", kSceNetAfInet, 1 /* SOCK_STREAM */, 6 /* TCP */);
  if (sock < 0)
  {
    Klogf("[kodi-ps5] elevation-elf: socket failed (%d)\n", sock);
    return MethodOutcome::failedSafe;
  }
  bool configured = true;
  for (const int option : kSceNetTimeoutOptions)
    configured = configured &&
                 sceNetSetsockopt(sock, kSceNetSolSocket, option, &kIoTimeoutUs,
                                  sizeof(kIoTimeoutUs)) >= 0;

  SceNetSockaddrIn endpoint{};
  endpoint.len = sizeof(endpoint);
  endpoint.family = kSceNetAfInet;
  endpoint.port = static_cast<uint16_t>((port << 8) | (port >> 8));
  endpoint.address = kLoopbackV4;
  const int connected = configured ? sceNetConnect(sock, &endpoint, sizeof(endpoint)) : -1;
  if (connected < 0)
  {
    Klogf("[kodi-ps5] elevation-elf: connect to 127.0.0.1:%d failed (loader absent?)\n", port);
    sceNetSocketClose(sock);
    return MethodOutcome::failedSafe;
  }

  const int elf = open(kHelperElf, O_RDONLY | O_CLOEXEC);
  if (elf < 0)
  {
    Klogf("[kodi-ps5] elevation-elf: cannot open %s (%s)\n", kHelperElf, std::strerror(errno));
    sceNetSocketClose(sock);
    return MethodOutcome::failedSafe;
  }

  MethodOutcome outcome = MethodOutcome::failedSafe;
  bool streamed = true;
  std::array<uint8_t, 4096> buffer{};
  for (;;)
  {
    const ssize_t count = read(elf, buffer.data(), buffer.size());
    if (count == 0)
      break;
    if (count < 0 || !SendAll(sock, buffer.data(), static_cast<size_t>(count)))
    {
      streamed = false;
      break;
    }
  }
  close(elf);
  if (!streamed)
  {
    // An incomplete upload cannot run: no request was ever submitted.
    Klogf("[kodi-ps5] elevation-elf: helper upload to port %d incomplete\n", port);
    sceNetSocketClose(sock);
    return outcome;
  }
  Klogf("[kodi-ps5] elevation-elf: helper uploaded to port %d\n", port);

  uint32_t status = kStatusTransportError;
  if (RunExchange(sock, static_cast<uint32_t>(getpid()), status))
  {
    // Authorize was submitted: the helper may have acted; only an explicit
    // completion frame may classify the attempt as safe-to-continue.
    if (status == kStatusOk)
      outcome = MethodOutcome::providerOk;
    else if (status == kStatusTransportError)
      outcome = MethodOutcome::unsafe;
    else
      outcome = MethodOutcome::failedSafe;
    Klogf("[kodi-ps5] elevation-elf: helper finished (%s)\n", LapyStatusName(status));
  }
  else
  {
    // The helper was never authorized to apply: no request is active.
    Klogf("[kodi-ps5] elevation-elf: exchange aborted before authorize on port %d\n", port);
  }
  sceNetSocketClose(sock);
  return outcome;
}
} // namespace KODI::PLATFORM::PS5
