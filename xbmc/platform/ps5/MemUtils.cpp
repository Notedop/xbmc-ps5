/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "utils/MemUtils.h"
#include "utils/log.h"

#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <stdexcept>

extern "C"
{
int sceKernelConfiguredFlexibleMemorySize(size_t* size);
int sceKernelAvailableFlexibleMemorySize(size_t* size);
}

namespace KODI
{
namespace MEMORY
{

void* AlignedMalloc(size_t s, size_t alignTo)
{
  void* p = nullptr;
  int res = posix_memalign(&p, alignTo, s);
  if (res == EINVAL)
    throw std::runtime_error("Failed to align memory, alignment is not a multiple of 2");
  else if (res == ENOMEM)
    throw std::runtime_error("Failed to align memory, insufficient memory available");
  return p;
}

void AlignedFree(void* p)
{
  if (!p)
    return;
  free(p);
}

void GetMemoryStatus(MemoryStatus* buffer)
{
  if (!buffer)
    return;

  *buffer = {};
  size_t total = 0;
  size_t available = 0;
  const int totalResult = sceKernelConfiguredFlexibleMemorySize(&total);
  const int availableResult = sceKernelAvailableFlexibleMemorySize(&available);
  if (totalResult != 0 || availableResult != 0 || total == 0 || available > total)
  {
    static std::atomic_flag logged = ATOMIC_FLAG_INIT;
    if (!logged.test_and_set())
      CLog::Log(LOGERROR,
                "PS5 flexible-memory query failed: total status {:#x}, available status {:#x}, "
                "total {}, available {}",
                totalResult, availableResult, total, available);
    return;
  }

  // Flexible-memory budgets exclude the separate direct/graphics memory pool.
  buffer->totalPhys = total;
  buffer->availPhys = available;
  buffer->memoryLoad = static_cast<unsigned int>((total - available) * 100 / total);
}

} // namespace MEMORY
} // namespace KODI
