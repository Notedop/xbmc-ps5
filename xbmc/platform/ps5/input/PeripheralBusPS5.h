/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "PS5InputBackend.h"
#include "peripherals/bus/PeripheralBus.h"

#include <map>
#include <mutex>
#include <string>

namespace PERIPHERALS
{
class CPeripheralJoystick;

class CPeripheralBusPS5 : public CPeripheralBus
{
public:
  explicit CPeripheralBusPS5(CPeripherals& manager);
  ~CPeripheralBusPS5() override;

  bool SupportsFeature(PeripheralFeature feature) const override
  {
    return feature == FEATURE_JOYSTICK || feature == FEATURE_RUMBLE;
  }

  bool InitializeProperties(CPeripheral& peripheral) override;
  bool InitializeButtonMap(const CPeripheral& peripheral,
                           KODI::JOYSTICK::IButtonMap& buttonMap) const override;
  std::string GetAppearance(const CPeripheral& peripheral) const override;
  void ProcessEvents() override;
  void Clear() override;
  bool SetMotorState(const std::string& location, unsigned int motorIndex, float magnitude) override;

protected:
  bool ScanForDevices() override;
  bool PerformDeviceScan(PeripheralScanResults& results) override;
  void UnregisterRemovedDevices(const PeripheralScanResults& results) override;

private:
  static std::string Location(int userId, int portType);
  void Dispatch(CPeripheralJoystick& joystick, const KODI::PLATFORM::PS5::PS5InputState& state);

  KODI::PLATFORM::PS5::CPS5InputBackend m_backend;
  std::map<std::string, KODI::PLATFORM::PS5::PS5InputState> m_states;
  std::mutex m_dispatchMutex;
};
} // namespace PERIPHERALS
