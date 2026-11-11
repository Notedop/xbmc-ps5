/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ElevationMethods.h"

#include <array>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstring>

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

// etaHEN legacy command service wire layout, preserved exactly from the
// official README's non-whitelist app-jailbreak example (JAILBREAK_CMD = 5,
// magic 0xdeadbeef, ret pre-set to -1337).
constexpr int kEtaHenPort = 9028;
constexpr uint32_t kHjMagic = 0xdeadbeef;
constexpr int kJailbreakCmd = 5;
constexpr int kLegacyRetUnset = -1337;

struct HijackerCommand
{
  uint32_t magic;
  int32_t cmd;
  int32_t pid;
  int32_t ret;
  char msg1[0x500];
  char msg2[0x500];
};
static_assert(sizeof(HijackerCommand) == 2576);

// Bound for both connect and the response wait, in microseconds.
constexpr int kIoTimeoutUs = 5'000'000;

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
} // namespace

namespace KODI::PLATFORM::PS5
{
MethodOutcome TryEtaHenIpc()
{
  EnsureSceNet();

  const int sock = sceNetSocket("kodi-ipc", kSceNetAfInet, 1 /* SOCK_STREAM */, 6 /* TCP */);
  if (sock < 0)
  {
    Klogf("[kodi-ps5] elevation-ipc: socket failed (%d)\n", sock);
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
  endpoint.port = static_cast<uint16_t>((kEtaHenPort << 8) | (kEtaHenPort >> 8));
  endpoint.address = kLoopbackV4;
  const int connected = configured ? sceNetConnect(sock, &endpoint, sizeof(endpoint)) : -1;
  if (connected < 0)
  {
    Klog("[kodi-ps5] elevation-ipc: connect to 127.0.0.1:9028 failed "
         "(legacy CMD server off?)\n");
    sceNetSocketClose(sock);
    return MethodOutcome::failedSafe;
  }

  MethodOutcome outcome = MethodOutcome::failedSafe;
  HijackerCommand command{};
  command.magic = kHjMagic;
  command.cmd = kJailbreakCmd;
  command.pid = getpid();
  command.ret = kLegacyRetUnset;
  if (!SendAll(sock, &command, sizeof(command)))
  {
    // Partial send: the daemon never received a complete command.
    Klog("[kodi-ps5] elevation-ipc: command send incomplete\n");
    sceNetSocketClose(sock);
    return outcome;
  }
  Klog("[kodi-ps5] elevation-ipc: command submitted\n");

  HijackerCommand reply{};
  if (!RecvAll(sock, &reply, sizeof(reply)))
  {
    // The command was fully submitted; the daemon may still be acting.
    Klog("[kodi-ps5] elevation-ipc: no complete reply after submission; "
         "completion uncertain\n");
    sceNetSocketClose(sock);
    return MethodOutcome::unsafe;
  }
  if (reply.ret != 0 && reply.ret != kLegacyRetUnset)
  {
    Klogf("[kodi-ps5] elevation-ipc: etaHEN rejected the request (ret=%d)\n", reply.ret);
    outcome = MethodOutcome::failedSafe;
  }
  else
  {
    // ret 0 is success; -1337 was never rewritten by the daemon but the
    // example treats it as success too (legacy quirk). Privileges are only
    // trusted after the runner's IsSandboxOpen() check.
    Klogf("[kodi-ps5] elevation-ipc: etaHEN completed (ret=%d)\n", reply.ret);
    outcome = MethodOutcome::providerOk;
  }
  sceNetSocketClose(sock);
  return outcome;
}
} // namespace KODI::PLATFORM::PS5
