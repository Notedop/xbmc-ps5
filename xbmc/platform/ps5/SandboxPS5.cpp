/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "SandboxPS5.h"

#include "ElevationConfig.h"
#include "ElevationMethods.h"

#include <algorithm>
#include <cerrno>
#include <cstdarg>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>

extern "C" int sceKernelDebugOutText(int channel, const char* text);

namespace
{
using namespace KODI::PLATFORM::PS5;

constexpr const char* kRequest = "/download0/etahen_jailbreak";
constexpr const char* kEscapedTitleRoot = "/data/homebrew/kodi-psmc";
constexpr const char* kSandboxMnt = "/mnt/sandbox";
constexpr const char* kElevationConfig = "/app0/config.json";
constexpr const char* kDefaultElevationConfig = "/app0/default-config.json";
constexpr int kElfPort9021 = 9021;

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

bool DropRequest()
{
  const int fd = open(kRequest, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd < 0)
  {
    Klog("[kodi-ps5] sandbox-thread: cannot write /download0/etahen_jailbreak\n");
    return false;
  }
  const std::string body = "{\"PID\":\"" + std::to_string(getpid()) + "\"}";
  const ssize_t written = write(fd, body.data(), body.size());
  close(fd);
  return written == static_cast<ssize_t>(body.size());
}

// One request: drop it, wait up to `ticks` x 250 ms for the sandbox to open.
bool Attempt(int ticks, bool& consumed)
{
  consumed = false;
  if (!DropRequest())
    return false;
  for (int i = 0; i < ticks; ++i)
  {
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
    struct stat st;
    if (!consumed && stat(kRequest, &st) != 0)
      consumed = true; // the daemon unlinks the file when it acts
    if (KODI::PLATFORM::PS5::IsSandboxOpen())
      return true;
  }
  return false;
}

// The existing marker contract: {"PID":"<pid>"} to /download0/etahen_jailbreak,
// consumed by a resident jailbreak daemon (OnionHEN, Lapy). A marker that was
// consumed without opening the sandbox gets the existing single retry: a first
// attempt can lose a timing race, but no request is ever active twice.
MethodOutcome TryJailbreakMarker()
{
  bool consumed = false;
  bool opened = Attempt(12, consumed);
  if (!opened && consumed)
    opened = Attempt(12, consumed);
  if (!consumed)
    unlink(kRequest); // no daemon: leave nothing behind for a later one

  if (opened)
  {
    Klog("[kodi-ps5] sandbox-thread: sandbox opened by jailbreak daemon\n");
    return MethodOutcome::providerOk;
  }
  if (consumed)
    Klog("[kodi-ps5] sandbox-thread: daemon consumed request but title-root remap did not appear\n");
  else
    Klog("[kodi-ps5] sandbox-thread: no jailbreak daemon answered\n");
  return MethodOutcome::failedSafe;
}

MethodOutcome AttemptMethod(ElevationMethodId id)
{
  switch (id)
  {
    case ElevationMethodId::elf9021:
      return TryElfLoader(kElfPort9021);
    case ElevationMethodId::jailbreakMarker:
      return TryJailbreakMarker();
    case ElevationMethodId::etahenIpc:
      return TryEtaHenIpc();
  }
  return MethodOutcome::failedSafe;
}
} // namespace

bool KODI::PLATFORM::PS5::IsSandboxOpen()
{
  DIR* sandbox = opendir(kSandboxMnt);
  if (!sandbox)
    return false;
  closedir(sandbox);

  DIR* rootedTitle = opendir(kEscapedTitleRoot);
  if (!rootedTitle && errno == ENOENT)
  {
    if (mkdir(kEscapedTitleRoot, 0777) != 0 && errno != EEXIST)
      return false;
    rootedTitle = opendir(kEscapedTitleRoot);
  }
  if (!rootedTitle)
    return false;

  closedir(rootedTitle);
  return true;
}

ElevationResult KODI::PLATFORM::PS5::RequestSandboxOpen()
{
  if (IsSandboxOpen())
  {
    Klog("[kodi-ps5] elevation: initial privilege check passed, no request needed\n");
    return ElevationResult::opened;
  }
  Klog("[kodi-ps5] elevation: initial privilege check failed\n");

  std::vector<ElevationMethodId> order;
  std::string error;
  const ElevationConfigStatus status =
      LoadElevationConfig(kElevationConfig, kDefaultElevationConfig, order, error);
  if (status == ElevationConfigStatus::error)
  {
    Klogf("[kodi-ps5] elevation: configuration error: %s\n", error.c_str());
    return ElevationResult::configError;
  }
  std::string effectiveOrder;
  for (const auto& id : order)
  {
    if (!effectiveOrder.empty())
      effectiveOrder += ", ";
    effectiveOrder += ElevationMethodName(id);
  }
  Klogf("[kodi-ps5] elevation: effective order: %s (%s)\n",
        effectiveOrder.empty() ? "none" : effectiveOrder.c_str(),
        status == ElevationConfigStatus::defaults ? "defaults" : "configured");
  {
    std::string disabled;
    for (const auto& id : DefaultElevationOrder())
      if (std::find(order.begin(), order.end(), id) == order.end())
      {
        if (!disabled.empty())
          disabled += ", ";
        disabled += ElevationMethodName(id);
      }
    if (!disabled.empty())
      Klogf("[kodi-ps5] elevation: disabled: %s\n", disabled.c_str());
  }

  for (const auto id : order)
  {
    const char* name = ElevationMethodName(id);
    Klogf("[kodi-ps5] elevation: attempting %s\n", name);
    const auto started = std::chrono::steady_clock::now();
    const MethodOutcome outcome = AttemptMethod(id);
    const auto elapsedMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                             std::chrono::steady_clock::now() - started)
                             .count();
    switch (outcome)
    {
      case MethodOutcome::providerOk:
        Klogf("[kodi-ps5] elevation: %s reports success (%lld ms)\n", name,
              static_cast<long long>(elapsedMs));
        break;
      case MethodOutcome::failedSafe:
        Klogf("[kodi-ps5] elevation: %s failed safely (%lld ms)\n", name,
              static_cast<long long>(elapsedMs));
        break;
      case MethodOutcome::unsafe:
        Klogf("[kodi-ps5] elevation: %s ended with uncertain completion (%lld ms); "
              "stopping without further requests\n",
              name, static_cast<long long>(elapsedMs));
        return ElevationResult::unsafe;
    }

    if (IsSandboxOpen())
    {
      Klogf("[kodi-ps5] elevation: sandbox opened by %s\n", name);
      return ElevationResult::opened;
    }
    Klog("[kodi-ps5] elevation: privilege check still failing\n");
  }

  Klog("[kodi-ps5] elevation: all enabled methods exhausted\n");
  return ElevationResult::failed;
}
