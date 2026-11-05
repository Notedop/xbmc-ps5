/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PS5InputBackend.h"

#include "platform/ps5/sce/SceUserService.h"
#include "utils/log.h"

#include <algorithm>
#include <cmath>

using namespace KODI::PLATFORM::PS5;
using namespace std::chrono_literals;

namespace
{
constexpr auto RETRY_INTERVAL = 1s;
constexpr int REPORT_BATCH_SIZE = 64;

float StickAxis(uint8_t value)
{
  // Both native center values must be exactly neutral, including without a button map.
  if (value == 127 || value == 128)
    return 0.0f;
  return value < 127 ? (static_cast<float>(value) - 127.0f) / 127.0f
                     : (static_cast<float>(value) - 128.0f) / 127.0f;
}
} // namespace

PS5InputState PS5InputState::Translate(const PadData& data)
{
  PS5InputState state;
  for (uint32_t bit : PS5_BUTTONS)
    state.buttons |= data.buttons & bit;
  state.axes = {StickAxis(data.leftStick.x), StickAxis(data.leftStick.y),
                StickAxis(data.rightStick.x), StickAxis(data.rightStick.y),
                data.analogButtons.l2 / 255.0f, data.analogButtons.r2 / 255.0f};
  return state;
}

bool PS5InputState::IsNeutral() const
{
  return buttons == 0 && std::all_of(axes.begin(), axes.end(),
                                    [](float axis) { return std::abs(axis) <= 0.2f; });
}

CPS5InputBackend::~CPS5InputBackend()
{
  Close();
}

bool CPS5InputBackend::Initialize()
{
  if (m_initialized)
    return true;
  int result = sceUserServiceInitialize(nullptr);
  if (result != 0 && result != USER_SERVICE_ERROR_ALREADY_INITIALIZED)
  {
    CLog::Log(LOGERROR, "PS5 input: user service initialization failed: {:#x}",
              static_cast<uint32_t>(result));
    return false;
  }
  result = scePadInit();
  if (result != 0)
  {
    CLog::Log(LOGERROR, "PS5 input: pad initialization failed: {:#x}",
              static_cast<uint32_t>(result));
    return false;
  }
  m_initialized = true;
  return true;
}

bool CPS5InputBackend::Scan()
{
  std::unique_lock lock(m_mutex);
  if (!Initialize())
    return false;

  std::array<int32_t, USER_SERVICE_MAX_USERS> users;
  users.fill(-1);
  const int result = sceUserServiceGetLoginUserIdList(users.data());
  if (result != 0)
  {
    if (!m_scanError)
      CLog::Log(LOGERROR, "PS5 input: login user scan failed: {:#x}",
                static_cast<uint32_t>(result));
    m_scanError = true;
    return false;
  }
  m_scanError = false;

  for (auto it = m_pads.begin(); it != m_pads.end();)
  {
    if (std::find(users.begin(), users.end(), it->first) == users.end())
    {
      ClosePad(it->second);
      it = m_pads.erase(it);
    }
    else
      ++it;
  }
  for (int user : users)
  {
    if (user == -1)
      continue;
    auto [it, added] = m_pads.try_emplace(user);
    Pad& pad = it->second;
    if (added)
    {
      for (unsigned int port = 0; port < USER_SERVICE_MAX_USERS; ++port)
      {
        const bool used = std::any_of(m_pads.begin(), m_pads.end(),
                                      [user, port](const auto& entry)
                                      { return entry.first != user && entry.second.port == port; });
        if (!used)
        {
          pad.port = port;
          break;
        }
      }
    }
    if (pad.handle <= 0 && std::chrono::steady_clock::now() >= pad.retryAt)
      OpenPad(user, pad);
  }
  return true;
}

void CPS5InputBackend::OpenPad(int userId, Pad& pad)
{
  pad.retryAt = std::chrono::steady_clock::now() + RETRY_INTERVAL;
  int handle = scePadOpen(userId, PAD_PORT_TYPE_STANDARD, 0, nullptr);
  const bool owned = static_cast<uint32_t>(handle) != PAD_ERROR_ALREADY_OPENED;
  if (!owned)
    handle = scePadGetHandle(userId, PAD_PORT_TYPE_STANDARD, 0);
  if (handle <= 0)
  {
    CLog::Log(LOGWARNING, "PS5 input: opening standard pad for user {:#x} failed: {:#x}",
              static_cast<uint32_t>(userId), static_cast<uint32_t>(handle));
    return;
  }
  pad.handle = handle;
  pad.owned = owned;
  pad.waitForNeutral = true;
  // Borrowed handles are input-only: don't mutate another owner's vibration mode.
  if (owned)
  {
    const int result = scePadSetVibrationMode(handle, 2);
    pad.supportsRumble = result == 0;
    if (result != 0)
      CLog::Log(LOGWARNING, "PS5 input: vibration mode unavailable for user {:#x}: {:#x}",
                static_cast<uint32_t>(userId), static_cast<uint32_t>(result));
  }
  CLog::Log(LOGINFO, "PS5 input: {} standard pad for user {:#x}",
            owned ? "opened" : "borrowing", static_cast<uint32_t>(userId));
}

void CPS5InputBackend::StopRumble(Pad& pad)
{
  if (pad.handle > 0 && pad.owned &&
      (pad.vibration.largeMotor != 0 || pad.vibration.smallMotor != 0))
  {
    const PadVibration stopped{};
    const int result = scePadSetVibration(pad.handle, &stopped);
    if (result != 0)
    {
      CLog::Log(LOGWARNING, "PS5 input: stopping vibration failed: {:#x}",
                static_cast<uint32_t>(result));
      return;
    }
  }
  pad.vibration = {};
}

void CPS5InputBackend::ClosePad(Pad& pad)
{
  StopRumble(pad);
  if (pad.handle > 0 && pad.owned)
  {
    const int result = scePadClose(pad.handle);
    if (result != 0)
      CLog::Log(LOGWARNING, "PS5 input: closing pad failed: {:#x}",
                static_cast<uint32_t>(result));
  }
  const auto retryAt = std::chrono::steady_clock::now() + RETRY_INTERVAL;
  const unsigned int port = pad.port;
  pad = {};
  pad.port = port;
  pad.retryAt = retryAt;
}

std::vector<PS5InputDevice> CPS5InputBackend::GetDevices() const
{
  std::unique_lock lock(m_mutex);
  std::vector<PS5InputDevice> devices;
  for (const auto& [user, pad] : m_pads)
    if (pad.connected)
      devices.push_back({user, pad.port, pad.supportsRumble});
  return devices;
}

PS5InputFrame CPS5InputBackend::MakeFrame(int userId, Pad& pad, bool imeActive)
{
  PS5InputFrame frame;
  frame.userId = userId;
  frame.connected = pad.connected;
  if (!pad.connected)
  {
    pad.waitForNeutral = true;
    StopRumble(pad);
    return frame;
  }
  const auto state = PS5InputState::Translate(pad.data);
  if (imeActive || pad.waitForNeutral)
  {
    pad.waitForNeutral = imeActive || !state.IsNeutral();
    if (imeActive)
    {
      StopRumble(pad);
      const auto now = std::chrono::steady_clock::now();
      if (!state.IsNeutral() && now >= pad.activityAt)
      {
        frame.imeActivity = true;
        pad.activityAt = now + 80ms;
      }
    }
    return frame;
  }
  frame.state = state;
  return frame;
}

std::vector<PS5InputFrame> CPS5InputBackend::Poll(bool imeActive)
{
  std::unique_lock lock(m_mutex);
  std::vector<PS5InputFrame> frames;
  for (auto& [user, pad] : m_pads)
  {
    if (pad.handle <= 0)
      continue;
    std::array<PadData, REPORT_BATCH_SIZE> reports{};
    const int count = scePadRead(pad.handle, reports.data(), REPORT_BATCH_SIZE);
    if (count < 0 || count > REPORT_BATCH_SIZE)
    {
      CLog::Log(LOGWARNING, "PS5 input: reading pad for user {:#x} failed: {:#x}",
                static_cast<uint32_t>(user), static_cast<uint32_t>(count));
      ClosePad(pad);
      frames.push_back(MakeFrame(user, pad, imeActive));
      continue;
    }
    if (count == 0)
    {
      // ReadState supplies initial/cached state, including idle disconnects.
      PadData data{};
      const int result = scePadReadState(pad.handle, &data);
      if (result != 0)
      {
        CLog::Log(LOGWARNING, "PS5 input: reading connection state failed: {:#x}",
                  static_cast<uint32_t>(result));
        ClosePad(pad);
      }
      else
      {
        pad.data = data;
        pad.connected = data.connected != 0;
      }
      frames.push_back(MakeFrame(user, pad, imeActive));
    }
    for (int i = 0; i < count; ++i)
    {
      pad.data = reports[i];
      pad.connected = pad.data.connected != 0;
      frames.push_back(MakeFrame(user, pad, imeActive));
    }
  }
  return frames;
}

bool CPS5InputBackend::SetMotorState(int userId, unsigned int motorIndex, float magnitude)
{
  std::unique_lock lock(m_mutex);
  auto it = m_pads.find(userId);
  if (it == m_pads.end() || !it->second.connected || !it->second.supportsRumble ||
      it->second.waitForNeutral)
    return false;
  if (motorIndex >= 2 || !std::isfinite(magnitude) || magnitude < 0.0f || magnitude > 1.0f)
  {
    CLog::Log(LOGERROR, "PS5 input: invalid motor {} magnitude {}", motorIndex, magnitude);
    return false;
  }
  Pad& pad = it->second;
  PadVibration vibration = pad.vibration;
  const auto intensity = static_cast<uint8_t>(std::lround(magnitude * 255.0f));
  if (motorIndex == 0)
    vibration.largeMotor = intensity;
  else
    vibration.smallMotor = intensity;
  const int result = scePadSetVibration(pad.handle, &vibration);
  if (result != 0)
  {
    CLog::Log(LOGERROR, "PS5 input: vibration failed: {:#x}", static_cast<uint32_t>(result));
    return false;
  }
  pad.vibration = vibration;
  return true;
}

void CPS5InputBackend::Close()
{
  std::unique_lock lock(m_mutex);
  for (auto& [user, pad] : m_pads)
    ClosePad(pad);
  m_pads.clear();
}
