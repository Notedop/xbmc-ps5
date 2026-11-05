/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PeripheralBusPS5.h"

#include "PS5ImeDialog.h"
#include "games/controllers/ControllerIDs.h"
#include "input/joysticks/interfaces/IButtonMap.h"
#include "peripherals/devices/PeripheralJoystick.h"
#include "utils/StringUtils.h"
#include "utils/log.h"

#include <array>
#include <chrono>
#include <mutex>

using namespace PERIPHERALS;
using namespace KODI;
using namespace KODI::JOYSTICK;
using namespace KODI::PLATFORM::PS5;
using namespace std::chrono_literals;

CPeripheralBusPS5::CPeripheralBusPS5(CPeripherals& manager)
  : CPeripheralBus("PeripBusPS5", manager, PERIPHERAL_BUS_PS5)
{
  m_iRescanTime = 1s;
}

CPeripheralBusPS5::~CPeripheralBusPS5()
{
  Clear();
}

std::string CPeripheralBusPS5::Location(int userId)
{
  // This is a logical port identity, not a controller serial number.
  return StringUtils::Format("ps5/user/{:08x}/port/0/index/0", static_cast<uint32_t>(userId));
}

bool CPeripheralBusPS5::ScanForDevices()
{
  std::unique_lock lock(m_dispatchMutex);
  return CPeripheralBus::ScanForDevices();
}

bool CPeripheralBusPS5::PerformDeviceScan(PeripheralScanResults& results)
{
  // A transient native failure must not stop the bus thread or remove existing devices.
  m_backend.Scan();
  for (const auto& device : m_backend.GetDevices())
  {
    PeripheralScanResult result(PERIPHERAL_BUS_PS5);
    result.m_type = result.m_mappedType = PERIPHERAL_JOYSTICK;
    result.m_strLocation = Location(device.userId);
    result.m_strDeviceName = "PS5 standard controller";
    result.m_iSequence = device.port;
    results.m_results.push_back(std::move(result));
  }
  return true;
}

bool CPeripheralBusPS5::InitializeProperties(CPeripheral& peripheral)
{
  if (peripheral.Type() != PERIPHERAL_JOYSTICK)
    return false;
  for (const auto& device : m_backend.GetDevices())
  {
    if (Location(device.userId) == peripheral.Location())
    {
      auto& joystick = static_cast<CPeripheralJoystick&>(peripheral);
      joystick.SetProvider("ps5");
      joystick.SetRequestedPort(device.port);
      joystick.SetButtonCount(PS5_BUTTON_COUNT);
      joystick.SetAxisCount(PS5_AXIS_COUNT);
      joystick.SetMotorCount(device.supportsRumble ? 2 : 0);
      return true;
    }
  }
  CLog::Log(LOGERROR, "PS5 input: missing controller at {}", peripheral.Location());
  return false;
}

bool CPeripheralBusPS5::InitializeButtonMap(const CPeripheral& peripheral,
                                           IButtonMap& buttonMap) const
{
  if (peripheral.Type() != PERIPHERAL_JOYSTICK)
    return false;
  if (buttonMap.ControllerID() != GAME::DEFAULT_CONTROLLER_ID &&
      buttonMap.ControllerID() != PS5_CONTROLLER_ID)
    return false;

  constexpr std::array<const char*, 14> features{
      "a", "b", "x", "y", "start", "back", "up", "right", "down", "left",
      "leftthumb", "rightthumb", "leftbumper", "rightbumper"};
  for (unsigned int i = 0; i < features.size(); ++i)
    buttonMap.AddScalar(features[i], {PRIMITIVE_TYPE::BUTTON, i});
  buttonMap.AddScalar("lefttrigger", {4, 0, SEMIAXIS_DIRECTION::POSITIVE, 1});
  buttonMap.AddScalar("righttrigger", {5, 0, SEMIAXIS_DIRECTION::POSITIVE, 1});

  for (unsigned int stick = 0; stick < 2; ++stick)
  {
    const char* feature = stick == 0 ? "leftstick" : "rightstick";
    buttonMap.AddAnalogStick(feature, ANALOG_STICK_DIRECTION::LEFT,
                            {stick * 2, 0, SEMIAXIS_DIRECTION::NEGATIVE, 1});
    buttonMap.AddAnalogStick(feature, ANALOG_STICK_DIRECTION::RIGHT,
                            {stick * 2, 0, SEMIAXIS_DIRECTION::POSITIVE, 1});
    buttonMap.AddAnalogStick(feature, ANALOG_STICK_DIRECTION::UP,
                            {stick * 2 + 1, 0, SEMIAXIS_DIRECTION::NEGATIVE, 1});
    buttonMap.AddAnalogStick(feature, ANALOG_STICK_DIRECTION::DOWN,
                            {stick * 2 + 1, 0, SEMIAXIS_DIRECTION::POSITIVE, 1});
  }
  const auto& joystick = static_cast<const CPeripheralJoystick&>(peripheral);
  if (joystick.MotorCount() == 2)
  {
    buttonMap.AddScalar("leftmotor", {PRIMITIVE_TYPE::MOTOR, 0});
    buttonMap.AddScalar("rightmotor", {PRIMITIVE_TYPE::MOTOR, 1});
  }
  buttonMap.SetIgnoredPrimitives(
      {{PRIMITIVE_TYPE::BUTTON, 14}, {PRIMITIVE_TYPE::BUTTON, PS5_R2_BUTTON}});
  if (!buttonMap.SetAppearance(PS5_CONTROLLER_ID))
  {
    CLog::Log(LOGERROR, "PS5 input: unable to set DualSense appearance for {}", buttonMap.Location());
    return false;
  }
  buttonMap.SaveButtonMap();
  return true;
}

std::string CPeripheralBusPS5::GetAppearance(const CPeripheral& peripheral) const
{
  return peripheral.Type() == PERIPHERAL_JOYSTICK ? PS5_CONTROLLER_ID : "";
}

void CPeripheralBusPS5::Dispatch(CPeripheralJoystick& joystick, const PS5InputState& state)
{
  auto& previous = m_states[joystick.Location()];
  for (unsigned int i = 0; i < PS5_BUTTON_COUNT; ++i)
  {
    const uint32_t bit = PS5_BUTTONS[i];
    if ((previous.buttons ^ state.buttons) & bit)
      joystick.OnButtonMotion(i, (state.buttons & bit) != 0);
  }
  for (unsigned int i = 0; i < PS5_AXIS_COUNT; ++i)
    joystick.OnAxisMotion(i, state.axes[i]);
  previous = state;
  joystick.OnInputFrame();
}

void CPeripheralBusPS5::ProcessEvents()
{
  std::unique_lock lock(m_dispatchMutex);
  bool rescan = false;
  for (const auto& frame : m_backend.Poll(CPS5ImeDialog::IsActive()))
  {
    if (frame.imeActivity)
      CPS5ImeDialog::NotifyInputActivity();
    auto device = GetPeripheral(Location(frame.userId));
    if (frame.connected != static_cast<bool>(device))
      rescan = true;
    if (device && device->Type() == PERIPHERAL_JOYSTICK)
      Dispatch(static_cast<CPeripheralJoystick&>(*device), frame.state);
  }
  if (rescan)
    TriggerDeviceScan();
}

void CPeripheralBusPS5::UnregisterRemovedDevices(const PeripheralScanResults& results)
{
  for (auto it = m_states.begin(); it != m_states.end();)
  {
    PeripheralScanResult result;
    if (!results.GetDeviceOnLocation(it->first, &result))
    {
      auto device = GetPeripheral(it->first);
      if (device && device->Type() == PERIPHERAL_JOYSTICK)
        Dispatch(static_cast<CPeripheralJoystick&>(*device), {});
      it = m_states.erase(it);
    }
    else
      ++it;
  }
  CPeripheralBus::UnregisterRemovedDevices(results);
}

bool CPeripheralBusPS5::SetMotorState(const std::string& location,
                                     unsigned int motorIndex,
                                     float magnitude)
{
  if (CPS5ImeDialog::IsActive())
    return false;
  for (const auto& device : m_backend.GetDevices())
    if (Location(device.userId) == location)
      return m_backend.SetMotorState(device.userId, motorIndex, magnitude);
  return false;
}

void CPeripheralBusPS5::Clear()
{
  StopThread(false);
  m_triggerEvent.Set();
  StopThread(true);
  std::unique_lock lock(m_dispatchMutex);
  for (const auto& [location, state] : m_states)
  {
    auto device = GetPeripheral(location);
    if (device && device->Type() == PERIPHERAL_JOYSTICK)
      Dispatch(static_cast<CPeripheralJoystick&>(*device), {});
  }
  CPeripheralBus::Clear();
  m_states.clear();
  m_backend.Close();
}
