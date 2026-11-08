/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#pragma once

#include <cstdint>

extern "C"
{
int sceMbusConvertToLocalDeviceId2(uint64_t deviceId, uint32_t deviceClass, uint64_t* localId);
}
