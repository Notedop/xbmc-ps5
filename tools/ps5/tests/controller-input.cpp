/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "platform/ps5/input/PS5InputBackend.h"
#include "platform/ps5/sce/SceUserService.h"
#include "utils/log.h"

#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <deque>
#include <iostream>
#include <limits>
#include <map>
#include <thread>
#include <vector>

using namespace KODI::PLATFORM::PS5;
using namespace std::chrono_literals;

namespace
{
PadData Neutral(bool connected = true)
{
  PadData data{};
  data.connected = connected;
  data.leftStick = {127, 128};
  data.rightStick = {128, 127};
  return data;
}

struct FakePad
{
  PadData state{Neutral()};
  std::deque<PadData> reports;
  std::vector<PadVibration> vibration;
  int opens{0};
  int closes{0};
  int modes{0};
  int openError{0};
  int readError{0};
  int stateError{0};
  int vibrationError{0};
  int modeError{0};
  bool borrowed{false};
};

std::array<int32_t, 4> users{-1, -1, -1, -1};
std::map<int, FakePad> pads;
int scanError = 0;
int initError = 0;

void Reset(std::array<int32_t, 4> loggedIn)
{
  users = loggedIn;
  pads.clear();
  scanError = initError = 0;
  inputErrors = 0;
}

void TestTranslation()
{
  auto data = Neutral();
  auto state = PS5InputState::Translate(data);
  assert(state.IsNeutral());
  for (float axis : state.axes)
    assert(axis == 0);
  data.leftStick = {0, 255};
  data.rightStick = {255, 0};
  data.analogButtons = {0, 255};
  data.buttons = PAD_BUTTON_CROSS | PAD_BUTTON_TOUCH_PAD | 0x80000000u;
  state = PS5InputState::Translate(data);
  assert(!state.IsNeutral());
  assert((state.axes == std::array<float, 6>{-1, 1, 1, -1, 0, 1}));
  assert(state.buttons == (PAD_BUTTON_CROSS | PAD_BUTTON_TOUCH_PAD));
  assert(PS5_BUTTON_COUNT == 16);
}

void TestLifecycle()
{
  Reset({10, 20, 30, 40});
  CPS5InputBackend backend;
  assert(backend.Scan());
  assert(backend.GetDevices().empty());
  const auto frames = backend.Poll(false);
  assert(frames.size() == 4);
  const auto original = backend.GetDevices();
  assert(original.size() == 4);
  users = {40, 20, 10, 30};
  assert(backend.Scan());
  auto reordered = backend.GetDevices();
  for (size_t i = 0; i < original.size(); ++i)
  {
    assert(original[i].userId == reordered[i].userId);
    assert(original[i].port == reordered[i].port);
    assert(pads[original[i].userId].opens == 1);
    assert(pads[original[i].userId].closes == 0);
  }
  scanError = -1;
  assert(!backend.Scan());
  assert(backend.GetDevices().size() == 4);
  assert(inputErrors == 1);
  assert(!backend.Scan());
  assert(inputErrors == 1);
  scanError = 0;
  users = {5, 20, 30, 40};
  assert(backend.Scan());
  backend.Poll(false);
  const auto replaced = backend.GetDevices();
  assert(replaced.size() == 4);
  assert(replaced[0].userId == 5 && replaced[0].port == original[0].port);
  assert(pads[10].closes == 1);
  backend.Close();
  backend.Close();
  assert(backend.GetDevices().empty());
  for (int user : users)
    assert(pads[user].closes == 1);
}

void TestReportsAndIme()
{
  Reset({10, -1, -1, -1});
  CPS5InputBackend backend;
  assert(backend.Scan());
  auto pressed = Neutral();
  pressed.buttons = PAD_BUTTON_CROSS;
  pads[10].state = pressed;
  assert(backend.Poll(false)[0].state.IsNeutral());
  pads[10].state = Neutral();
  backend.Poll(false);
  auto released = Neutral();
  pads[10].reports = {pressed, released, pressed};
  auto frames = backend.Poll(false);
  assert(frames.size() == 3);
  assert(frames[0].state.buttons == PAD_BUTTON_CROSS);
  assert(frames[1].state.buttons == 0);
  assert(frames[2].state.buttons == PAD_BUTTON_CROSS);
  frames = backend.Poll(true);
  assert(frames[0].state.IsNeutral() && frames[0].imeActivity);
  frames = backend.Poll(false);
  assert(frames[0].state.buttons == 0);
  pads[10].state = Neutral();
  pads[10].state.rightStick.x = 255;
  assert(backend.Poll(false)[0].state.IsNeutral());
  pads[10].state = Neutral();
  pads[10].state.analogButtons.r2 = 255;
  assert(backend.Poll(false)[0].state.IsNeutral());
  pads[10].state = Neutral();
  assert(backend.Poll(false)[0].state.IsNeutral());
  pads[10].state = pressed;
  assert(backend.Poll(false)[0].state.buttons == PAD_BUTTON_CROSS);
  pads[10].state = Neutral(false);
  assert(!backend.Poll(false)[0].connected);
  assert(backend.GetDevices().empty());
  pads[10].state = pressed;
  assert(backend.Poll(false)[0].state.IsNeutral());
  pads[10].state = released;
  backend.Poll(false);
  for (int i = 0; i < 70; ++i)
    pads[10].reports.push_back(i % 2 == 0 ? pressed : released);
  frames = backend.Poll(false);
  assert(frames.size() == 64 && pads[10].reports.size() == 6);
  for (size_t i = 0; i < frames.size(); ++i)
    assert(frames[i].state.buttons == (i % 2 == 0 ? PAD_BUTTON_CROSS : 0));
  assert(backend.Poll(false).size() == 6);
}

void TestRetryAndOwnership()
{
  Reset({10, -1, -1, -1});
  pads[10].openError = -1;
  CPS5InputBackend backend;
  assert(backend.Scan());
  assert(pads[10].opens == 1);
  assert(backend.Scan());
  assert(pads[10].opens == 1);
  pads[10].openError = 0;
  std::this_thread::sleep_for(1100ms);
  assert(backend.Scan());
  assert(pads[10].opens == 2);
  backend.Poll(false);
  pads[10].readError = -1;
  auto frames = backend.Poll(false);
  assert(!frames[0].connected && frames[0].state.IsNeutral());
  assert(pads[10].closes == 1 && backend.GetDevices().empty());
  pads[10].readError = 0;
  pads[10].borrowed = true;
  std::this_thread::sleep_for(1100ms);
  assert(backend.Scan());
  backend.Poll(false);
  assert(!backend.GetDevices()[0].supportsRumble);
  assert(!backend.SetMotorState(10, 0, 1));
  backend.Close();
  assert(pads[10].closes == 1 && pads[10].modes == 1);
}

void TestRumble()
{
  Reset({10, -1, -1, -1});
  CPS5InputBackend backend;
  assert(backend.Scan());
  backend.Poll(false);
  assert(backend.GetDevices()[0].supportsRumble);
  assert(backend.SetMotorState(10, 0, 1));
  assert(backend.SetMotorState(10, 1, 0.5f));
  assert(pads[10].vibration.back().largeMotor == 255);
  assert(pads[10].vibration.back().smallMotor == 128);
  const auto writes = pads[10].vibration.size();
  assert(!backend.SetMotorState(10, 2, 1));
  assert(!backend.SetMotorState(10, 0, -1));
  assert(!backend.SetMotorState(10, 0, 2));
  assert(!backend.SetMotorState(10, 0, std::numeric_limits<float>::quiet_NaN()));
  assert(!backend.SetMotorState(10, 0, std::numeric_limits<float>::infinity()));
  assert(pads[10].vibration.size() == writes);
  std::thread poller([&backend]
                    {
                      for (int i = 0; i < 100; ++i)
                        backend.Poll(false);
                    });
  std::thread output([&backend]
                    {
                      for (int i = 0; i < 100; ++i)
                        assert(backend.SetMotorState(10, 0, 1));
                    });
  poller.join();
  output.join();
  pads[10].vibrationError = -1;
  backend.Poll(true);
  pads[10].vibrationError = 0;
  backend.Poll(true);
  assert(pads[10].vibration.back().largeMotor == 0);
  assert(pads[10].vibration.back().smallMotor == 0);
  assert(!backend.SetMotorState(10, 0, 1));
  backend.Poll(false);
  assert(backend.SetMotorState(10, 1, 1));
  backend.Close();
  assert(pads[10].vibration.back().smallMotor == 0);
}

void TestFailures()
{
  Reset({10, -1, -1, -1});
  initError = -1;
  CPS5InputBackend backend;
  assert(!backend.Scan());
  assert(inputErrors != 0);
  initError = 0;
  pads[10].modeError = -1;
  assert(backend.Scan());
  backend.Poll(false);
  assert(backend.GetDevices().size() == 1);
  assert(!backend.GetDevices()[0].supportsRumble);
  pads[10].stateError = -1;
  assert(!backend.Poll(false)[0].connected);
  assert(pads[10].closes == 1);
}

void TestInvalidReportCount()
{
  Reset({10, -1, -1, -1});
  CPS5InputBackend backend;
  assert(backend.Scan());
  backend.Poll(false);
  pads[10].readError = 65;
  const auto frames = backend.Poll(false);
  assert(frames.size() == 1 && !frames[0].connected);
  assert(frames[0].state.IsNeutral() && pads[10].closes == 1);
}
} // namespace

extern "C"
{
int sceUserServiceInitialize(const void*) { return USER_SERVICE_ERROR_ALREADY_INITIALIZED; }
int scePadInit() { return initError; }
int sceUserServiceGetLoginUserIdList(int32_t* output)
{
  if (scanError)
    return scanError;
  std::copy(users.begin(), users.end(), output);
  return 0;
}
int scePadOpen(int user, int type, int index, const void*)
{
  assert(type == PAD_PORT_TYPE_STANDARD && index == 0);
  auto& pad = pads[user];
  ++pad.opens;
  if (pad.openError)
    return pad.openError;
  return pad.borrowed ? static_cast<int>(PAD_ERROR_ALREADY_OPENED) : user + 100;
}
int scePadGetHandle(int user, int type, int index)
{
  assert(type == PAD_PORT_TYPE_STANDARD && index == 0);
  return user + 100;
}
int scePadClose(int handle)
{
  ++pads[handle - 100].closes;
  return 0;
}
int scePadRead(int handle, void* output, int capacity)
{
  assert(capacity == 64);
  auto& pad = pads[handle - 100];
  if (pad.readError)
    return pad.readError;
  const int count = std::min(capacity, static_cast<int>(pad.reports.size()));
  for (int i = 0; i < count; ++i)
  {
    pad.state = pad.reports.front();
    pad.reports.pop_front();
    static_cast<PadData*>(output)[i] = pad.state;
  }
  return count;
}
int scePadReadState(int handle, void* output)
{
  auto& pad = pads[handle - 100];
  *static_cast<PadData*>(output) = pad.state;
  return pad.stateError;
}
int scePadSetVibrationMode(int handle, int mode)
{
  assert(mode == 2);
  auto& pad = pads[handle - 100];
  ++pad.modes;
  return pad.modeError;
}
int scePadSetVibration(int handle, const void* vibration)
{
  auto& pad = pads[handle - 100];
  if (pad.vibrationError)
    return pad.vibrationError;
  pad.vibration.push_back(*static_cast<const PadVibration*>(vibration));
  return 0;
}
}

int main()
{
  TestTranslation();
  TestLifecycle();
  TestReportsAndIme();
  TestRetryAndOwnership();
  TestRumble();
  TestFailures();
  TestInvalidReportCount();
  std::cout << "PASS: translation, four-user lifecycle, ordered reports, IME neutrality, "
               "disconnects, retry, borrowed ownership, native errors and independent motors\n";
}
