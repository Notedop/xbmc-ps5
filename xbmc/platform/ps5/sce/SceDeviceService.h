/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#pragma once

#include <cstddef>
#include <cstdint>

namespace KODI::PLATFORM::PS5
{
struct DeviceServiceRecord
{
  uint64_t deviceId;
  uint8_t reserved[0x68];
};
static_assert(sizeof(DeviceServiceRecord) == 0x70);

// DeviceService queries are exported by libSceMbus but belong to the
// libSceDeviceService library. Type 0x9001 maps to remote subtype 26.
inline constexpr uint32_t DEVICE_SERVICE_VERSION = 3;
inline constexpr uint32_t DEVICE_SERVICE_TYPE_MEDIA_REMOTE = 0x9001;
inline constexpr uint32_t DEVICE_SERVICE_RECORD_SIZE = 0x70;
inline constexpr int DEVICE_SERVICE_ERROR_ALREADY_INITIALIZED = 0x809b000b;
}

extern "C"
{
  int sceDeviceServiceInitialize(uint32_t version, const void* param);
  int sceDeviceServiceQueryDeviceInfo_(uint32_t type, const void* filter, uint32_t flags,
                                       void* records, int capacity, int* count, int* extra,
                                       uint32_t recordSize);
}
