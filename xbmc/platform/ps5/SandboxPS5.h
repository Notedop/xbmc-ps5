/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

namespace KODI::PLATFORM::PS5
{
enum class ElevationResult
{
  // IsSandboxOpen() is true: startup may continue.
  opened,
  // Every enabled method completed without opening the sandbox.
  failed,
  // The elevation configuration was invalid; no request was sent.
  configError,
  // A fully submitted request ended with uncertain completion: no further
  // request may be sent and startup must stop.
  unsafe,
};

// Request privilege elevation through the configured method sequence
// (elf_9021, elf_9020, jailbreak marker, etaHEN IPC; see
// /app0/share/kodi/system/elevation/elevation.json): send requests
// sequentially, verifying with IsSandboxOpen() after each attempt.
// Runs on the caller's thread; bounded waits, never concurrent requests.
// Call after the system modules Kodi needs later are loaded: once the
// sandbox is open, loading a system module fails.
ElevationResult RequestSandboxOpen();
bool IsSandboxOpen();
} // namespace KODI::PLATFORM::PS5
