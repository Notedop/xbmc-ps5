/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include <cstddef>
#include <cstdint>

namespace KODI::PLATFORM::PS5
{
// Result of one elevation method, as seen by the sequence runner.
enum class MethodOutcome
{
  // The provider reported success; the runner still verifies with
  // IsSandboxOpen() before trusting it.
  providerOk,
  // The request was never fully submitted, or the provider explicitly
  // refused or failed it: safe to try the next method.
  failedSafe,
  // The request was fully submitted but completion was not established:
  // the provider may still act on Kodi, so no further request may be sent.
  unsafe,
};

// Raw BSD socket()/connect() are denied in the title sandbox; sceNet* is the
// allowed transport (same path Kodi's curl and the Python socket shim use).
extern "C" {
int sceNetSocket(const char* name, int family, int type, int protocol);
int sceNetSocketClose(int s);
int sceNetConnect(int s, const void* addr, int addrlen);
int sceNetSend(int s, const void* buf, std::size_t len, int flags);
int sceNetRecv(int s, void* buf, std::size_t len, int flags);
int sceNetSetsockopt(int s, int level, int optname, const void* optval, int optlen);
int sceNetInit(void);
int sceNetPoolCreate(const char* name, int size, int flags);
}

struct SceNetSockaddrIn
{
  uint8_t len;
  uint8_t family;
  uint16_t port; // network order
  uint32_t address; // network order
  uint16_t virtual_port;
  uint8_t zero[6];
};

constexpr int kSceNetAfInet = 2;
constexpr int kSceNetSolSocket = 0xffff;
// sceNet socket-level timeouts, values in microseconds; the option set the
// Lapy helper exchange is built and tested with.
constexpr int kSceNetTimeoutOptions[] = { 0x1105, 0x1106, 0x1109 };
constexpr uint32_t kLoopbackV4 = 0x0100007f; // 127.0.0.1, network order

// One-time sceNet library and buffer-pool init; idempotent (sceNetInit
// reports "already initialized" once any other user - the resolver or Python
// socket shim - has initialized it).
void EnsureSceNet();

// Send the bundled one-shot helper ELF to an ELF loader on 127.0.0.1:<port>
// and run the cooperative elevation exchange on the calling thread.
MethodOutcome TryElfLoader(int port);

// Request elevation from etaHEN's legacy command server (127.0.0.1:9028).
MethodOutcome TryEtaHenIpc();
} // namespace KODI::PLATFORM::PS5
