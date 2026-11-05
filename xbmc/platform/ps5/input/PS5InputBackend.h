/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "platform/ps5/sce/ScePad.h"

#include <array>
#include <chrono>
#include <cstdint>
#include <map>
#include <mutex>
#include <vector>

namespace KODI::PLATFORM::PS5
{
// Driver indices are part of the persisted button-map contract.
inline constexpr std::array<uint32_t, 16> PS5_BUTTONS{
    PAD_BUTTON_CROSS, PAD_BUTTON_CIRCLE, PAD_BUTTON_SQUARE, PAD_BUTTON_TRIANGLE,
    PAD_BUTTON_OPTIONS, PAD_BUTTON_TOUCH_PAD, PAD_BUTTON_UP, PAD_BUTTON_RIGHT,
    PAD_BUTTON_DOWN, PAD_BUTTON_LEFT, PAD_BUTTON_L3, PAD_BUTTON_R3,
    PAD_BUTTON_L1, PAD_BUTTON_R1, PAD_BUTTON_L2, PAD_BUTTON_R2};
inline constexpr unsigned int PS5_R2_BUTTON = 15;
inline constexpr unsigned int PS5_BUTTON_COUNT = PS5_BUTTONS.size();
inline constexpr unsigned int PS5_AXIS_COUNT = 6;
inline constexpr const char* PS5_CONTROLLER_ID = "game.controller.dualsense";

struct PS5InputState
{
  uint32_t buttons{0};
  std::array<float, PS5_AXIS_COUNT> axes{};

  static PS5InputState Translate(const PadData& data);
  bool IsNeutral() const;
};

struct PS5InputFrame
{
  int userId{-1};
  PS5InputState state;
  bool connected{false};
  bool imeActivity{false};
};

struct PS5InputDevice
{
  int userId{-1};
  unsigned int port{0};
  bool supportsRumble{false};
};

class CPS5InputBackend
{
public:
  ~CPS5InputBackend();

  bool Scan();
  std::vector<PS5InputDevice> GetDevices() const;
  std::vector<PS5InputFrame> Poll(bool imeActive);
  bool SetMotorState(int userId, unsigned int motorIndex, float magnitude);
  void Close();

private:
  struct Pad
  {
    int handle{-1};
    bool owned{false};
    bool connected{false};
    bool supportsRumble{false};
    bool waitForNeutral{true};
    unsigned int port{0};
    PadData data{};
    PadVibration vibration{};
    std::chrono::steady_clock::time_point retryAt{};
    std::chrono::steady_clock::time_point activityAt{};
  };

  bool Initialize();
  void OpenPad(int userId, Pad& pad);
  void ClosePad(Pad& pad);
  void StopRumble(Pad& pad);
  PS5InputFrame MakeFrame(int userId, Pad& pad, bool imeActive);

  mutable std::mutex m_mutex;
  std::map<int, Pad> m_pads;
  bool m_initialized{false};
  bool m_scanError{false};
};
} // namespace KODI::PLATFORM::PS5
