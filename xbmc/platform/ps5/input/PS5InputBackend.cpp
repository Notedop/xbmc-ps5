/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PS5InputBackend.h"

#include "platform/ps5/sce/SceDeviceService.h"
#include "platform/ps5/sce/SceMbus.h"
#include "platform/ps5/sce/SceUserService.h"
#include "utils/log.h"

#include <algorithm>
#include <cmath>
#include <string>

using namespace KODI::PLATFORM::PS5;
using namespace std::chrono_literals;

namespace
{
constexpr auto RETRY_INTERVAL = 1s;
constexpr int REPORT_BATCH_SIZE = 64;
constexpr auto LOTUS_LOOKBACK = 100ms;

float StickAxis(uint8_t value)
{
  // Both native center values must be exactly neutral, including without a button map.
  if (value == 127 || value == 128)
    return 0.0f;
  return value < 127 ? (static_cast<float>(value) - 127.0f) / 127.0f
                     : (static_cast<float>(value) - 128.0f) / 127.0f;
}
} // namespace

PS5InputState PS5InputState::Translate(const PadData& data, int portType, uint32_t lotusButtons)
{
  PS5InputState state;
  if (portType == PAD_PORT_TYPE_REMOTE_CONTROL)
  {
    state.buttons = data.buttons & ~PS5_REMOTE_SYSTEM_BUTTONS;
    state.buttons |= lotusButtons & PS5_REMOTE_LOTUS_BUTTONS;
    // Payload keys reuse the existing driver indices so saved button maps remain valid.
    if (data.deviceUniqueDataLen == 1)
    {
      switch (data.deviceUniqueData[0])
      {
        case 0x0d:
          state.buttons |= PAD_BUTTON_CROSS;
          break;
        case 0x0f:
          state.buttons |= PAD_BUTTON_CIRCLE;
          break;
      }
    }
    return state;
  }
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

bool CPS5InputBackend::InitializeDeviceService()
{
  if (m_deviceServiceInitialized)
    return true;
  if (m_lotusImportsUnavailable)
    return false;
  // Native title imports can remain null when their export library does not match.
  auto* volatile initialize = &sceDeviceServiceInitialize;
  auto* volatile query = &sceDeviceServiceQueryDeviceInfo_;
  auto* volatile convert = &sceMbusConvertToLocalDeviceId2;
  auto* volatile buttons = &scePadLastLotusButtonData;
  if (!initialize || !query || !convert || !buttons)
  {
    CLog::Log(LOGERROR, "PS5 input: media remote Lotus imports are not bound; "
                       "playback buttons unavailable");
    m_lotusImportsUnavailable = true;
    return false;
  }
  const int result = initialize(DEVICE_SERVICE_VERSION, nullptr);
  if (result != 0 && result != DEVICE_SERVICE_ERROR_ALREADY_INITIALIZED)
  {
    CLog::Log(LOGERROR, "PS5 input: device service initialization failed: {:#x}",
              static_cast<uint32_t>(result));
    return false;
  }
  m_deviceServiceInitialized = true;
  return true;
}

void CPS5InputBackend::ResolveLotus(Pad& pad)
{
  std::array<DeviceServiceRecord, 16> records{};
  int count = -1;
  int extra = -1;
  int result = sceDeviceServiceQueryDeviceInfo_(DEVICE_SERVICE_TYPE_MEDIA_REMOTE, nullptr, 0,
                                                records.data(), static_cast<int>(records.size()),
                                                &count, &extra, DEVICE_SERVICE_RECORD_SIZE);
  if (result < 0 || count <= 0 || count > static_cast<int>(records.size()))
  {
    if (!pad.lotusError)
      CLog::Log(LOGWARNING, "PS5 input: media remote device query failed: {:#x} count={}",
                static_cast<uint32_t>(result), count);
    pad.lotusError = result;
    pad.lotusRetryAt = std::chrono::steady_clock::now() + RETRY_INTERVAL;
    return;
  }
  uint64_t localId = 0;
  for (int i = 0; i < count; ++i)
  {
    result = sceMbusConvertToLocalDeviceId2(records[i].deviceId, 0, &localId);
    if (result == 0 && localId <= INT32_MAX)
    {
      pad.lotusId = static_cast<int>(localId);
      pad.lotusError.reset();
      CLog::Log(LOGINFO, "PS5 input: media remote Lotus id {:#x}",
                static_cast<uint32_t>(pad.lotusId));
      return;
    }
  }
  if (!pad.lotusError)
    CLog::Log(LOGWARNING, "PS5 input: media remote Lotus id conversion failed: {:#x}",
              static_cast<uint32_t>(result));
  pad.lotusError = result;
  pad.lotusRetryAt = std::chrono::steady_clock::now() + RETRY_INTERVAL;
}

void CPS5InputBackend::PollLotus(Pad& pad)
{
  if (pad.lotusId < 0)
  {
    pad.lotusButtons = 0;
    return;
  }
  uint32_t mask = 0;
  const int result = scePadLastLotusButtonData(pad.lotusId,
                                               std::chrono::duration_cast<std::chrono::milliseconds>(
                                                   LOTUS_LOOKBACK)
                                                   .count(),
                                               &mask);
  if (result < 0)
  {
    CLog::Log(LOGWARNING, "PS5 input: media remote Lotus query failed: {:#x}",
              static_cast<uint32_t>(result));
    pad.lotusId = -1;
    pad.lotusButtons = 0;
    pad.lotusRetryAt = std::chrono::steady_clock::now() + RETRY_INTERVAL;
    return;
  }
  pad.lotusButtons = mask & PS5_REMOTE_LOTUS_BUTTONS;
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
    if (it->first.second != PAD_PORT_TYPE_REMOTE_CONTROL &&
        std::find(users.begin(), users.end(), it->first.first) == users.end())
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
    ScanPad(user, PAD_PORT_TYPE_STANDARD);
  }
  // The remote port is system-owned, regardless of its Bluetooth user binding.
  ScanPad(USER_ID_SYSTEM, PAD_PORT_TYPE_REMOTE_CONTROL);
  const auto remote = m_pads.find({USER_ID_SYSTEM, PAD_PORT_TYPE_REMOTE_CONTROL});
  if (remote != m_pads.end() && remote->second.handle > 0 && remote->second.connected &&
      remote->second.lotusId < 0 && std::chrono::steady_clock::now() >= remote->second.lotusRetryAt &&
      InitializeDeviceService())
    ResolveLotus(remote->second);
  return true;
}

void CPS5InputBackend::ScanPad(int userId, int portType)
{
  const auto key = std::make_pair(userId, portType);
  auto [it, added] = m_pads.try_emplace(key);
  Pad& pad = it->second;
  if (added)
  {
    if (portType == PAD_PORT_TYPE_REMOTE_CONTROL)
      pad.port = USER_SERVICE_MAX_USERS;
    else
    {
      for (unsigned int port = 0; port < USER_SERVICE_MAX_USERS; ++port)
      {
        const bool used = std::any_of(m_pads.begin(), m_pads.end(),
                                      [key, port](const auto& entry)
                                      { return entry.first != key && entry.second.port == port; });
        if (!used)
        {
          pad.port = port;
          break;
        }
      }
    }
  }
  if (pad.handle <= 0 && std::chrono::steady_clock::now() >= pad.retryAt)
    OpenPad(userId, portType, pad);
}

void CPS5InputBackend::OpenPad(int userId, int portType, Pad& pad)
{
  pad.retryAt = std::chrono::steady_clock::now() + RETRY_INTERVAL;
  int handle = scePadOpen(userId, portType, 0, nullptr);
  const bool owned = static_cast<uint32_t>(handle) != PAD_ERROR_ALREADY_OPENED;
  if (!owned)
    handle = scePadGetHandle(userId, portType, 0);
  if (handle <= 0)
  {
    if (pad.openError != handle)
      CLog::Log(LOGWARNING, "PS5 input: opening pad port {} for user {:#x} failed: {:#x}",
                portType, static_cast<uint32_t>(userId), static_cast<uint32_t>(handle));
    pad.openError = handle;
    return;
  }
  pad.openError.reset();
  pad.handle = handle;
  pad.owned = owned;
  pad.waitForNeutral = true;
  // Borrowed handles are input-only: don't mutate another owner's vibration mode.
  if (owned && portType == PAD_PORT_TYPE_STANDARD)
  {
    const int result = scePadSetVibrationMode(handle, 2);
    pad.supportsRumble = result == 0;
    if (result != 0)
      CLog::Log(LOGWARNING, "PS5 input: vibration mode unavailable for user {:#x}: {:#x}",
                static_cast<uint32_t>(userId), static_cast<uint32_t>(result));
  }
  CLog::Log(LOGINFO, "PS5 input: {} {} for user {:#x}",
            owned ? "opened" : "borrowing",
            portType == PAD_PORT_TYPE_REMOTE_CONTROL ? "media remote port" : "standard pad",
            static_cast<uint32_t>(userId));
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
  for (const auto& [key, pad] : m_pads)
    if (pad.connected)
      devices.push_back({key.first, pad.port, pad.supportsRumble, key.second});
  return devices;
}

PS5InputFrame CPS5InputBackend::MakeFrame(int userId, int portType, Pad& pad, bool imeActive)
{
  PS5InputFrame frame;
  frame.userId = userId;
  frame.portType = portType;
  frame.connected = pad.connected;
  if (!pad.connected)
  {
    pad.waitForNeutral = true;
    pad.hasLoggedRemoteReport = false;
    pad.lotusButtons = 0;
    if (portType == PAD_PORT_TYPE_REMOTE_CONTROL)
    {
      if (pad.lotusId >= 0)
        CLog::Log(LOGINFO, "PS5 input: media remote disconnected; invalidating Lotus id {:#x}",
                  static_cast<uint32_t>(pad.lotusId));
      pad.lotusId = -1;
      pad.lotusError.reset();
      pad.lotusRetryAt = {};
    }
    StopRumble(pad);
    return frame;
  }
  const auto state = PS5InputState::Translate(pad.data, portType, pad.lotusButtons);
  if (portType == PAD_PORT_TYPE_REMOTE_CONTROL)
  {
    std::array<uint8_t, 12> payload{};
    const size_t payloadSize = std::min<size_t>(pad.data.deviceUniqueDataLen, payload.size());
    std::copy_n(pad.data.deviceUniqueData, payloadSize, payload.begin());
    if (!pad.hasLoggedRemoteReport || pad.loggedButtons != pad.data.buttons ||
        pad.loggedPayloadLength != pad.data.deviceUniqueDataLen || pad.loggedPayload != payload)
    {
      std::string hexPayload;
      constexpr char HEX_DIGITS[] = "0123456789abcdef";
      for (size_t i = 0; i < payloadSize; ++i)
      {
        hexPayload += HEX_DIGITS[payload[i] >> 4];
        hexPayload += HEX_DIGITS[payload[i] & 0xf];
      }
      if (pad.data.deviceUniqueDataLen > payload.size())
        CLog::Log(LOGWARNING, "PS5 input: invalid media remote payload length {}; captured {} bytes",
                  pad.data.deviceUniqueDataLen, payloadSize);
      CLog::Log(LOGDEBUG,
                "PS5 input: media remote buttons={:#010x} payload_length={} payload={}",
                pad.data.buttons, pad.data.deviceUniqueDataLen,
                hexPayload.empty() ? "-" : hexPayload);
      pad.loggedButtons = pad.data.buttons;
      pad.loggedPayloadLength = pad.data.deviceUniqueDataLen;
      pad.loggedPayload = payload;
      pad.hasLoggedRemoteReport = true;
    }
  }
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
  for (auto& [key, pad] : m_pads)
  {
    const auto [user, portType] = key;
    if (pad.handle <= 0)
      continue;
    if (portType == PAD_PORT_TYPE_REMOTE_CONTROL && pad.connected)
      PollLotus(pad);
    std::array<PadData, REPORT_BATCH_SIZE> reports{};
    const int count = scePadRead(pad.handle, reports.data(), REPORT_BATCH_SIZE);
    if (count < 0 || count > REPORT_BATCH_SIZE)
    {
      CLog::Log(LOGWARNING, "PS5 input: reading pad port {} for user {:#x} failed: {:#x}",
                portType, static_cast<uint32_t>(user), static_cast<uint32_t>(count));
      ClosePad(pad);
      frames.push_back(MakeFrame(user, portType, pad, imeActive));
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
      frames.push_back(MakeFrame(user, portType, pad, imeActive));
    }
    for (int i = 0; i < count; ++i)
    {
      pad.data = reports[i];
      pad.connected = pad.data.connected != 0;
      frames.push_back(MakeFrame(user, portType, pad, imeActive));
    }
  }
  return frames;
}

bool CPS5InputBackend::SetMotorState(int userId, unsigned int motorIndex, float magnitude)
{
  std::unique_lock lock(m_mutex);
  auto it = m_pads.find({userId, PAD_PORT_TYPE_STANDARD});
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
  for (auto& [key, pad] : m_pads)
    ClosePad(pad);
  m_pads.clear();
}
