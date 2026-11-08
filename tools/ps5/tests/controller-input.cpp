/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "platform/ps5/input/PS5InputBackend.h"
#include "platform/ps5/sce/SceDeviceService.h"
#include "platform/ps5/sce/SceMbus.h"
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
FakePad remote;
constexpr int REMOTE_HANDLE = 10000;
int scanError = 0;
int initError = 0;

struct LotusFake
{
  int initRet{0};
  int initCalls{0};
  int queryRet{0};
  int queryCalls{0};
  int records{1};
  int reportedCount{-1};
  uint64_t recordId{0x00070302};
  int convertRet{0};
  uint64_t localId{0x00070302};
  int reads{0};
} lotus;
uint32_t lotusButtons = 0;
int lotusReadRet = 0;

void Reset(std::array<int32_t, 4> loggedIn)
{
  users = loggedIn;
  pads.clear();
  remote = {};
  remote.state = Neutral(false);
  scanError = initError = 0;
  inputErrors = 0;
  inputDebugLogs.clear();
  lotus = {};
  lotusButtons = 0;
  lotusReadRet = 0;
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
  data = {};
  data.connected = true;
  data.buttons = PAD_BUTTON_CROSS | 0x80000000u | PS5_REMOTE_SYSTEM_BUTTONS;
  data.analogButtons = {255, 255};
  state = PS5InputState::Translate(data, PAD_PORT_TYPE_REMOTE_CONTROL);
  assert(state.buttons == (PAD_BUTTON_CROSS | 0x80000000u));
  for (float axis : state.axes)
    assert(axis == 0);
  data.buttons = PS5_REMOTE_SYSTEM_BUTTONS;
  assert(PS5InputState::Translate(data, PAD_PORT_TYPE_REMOTE_CONTROL).IsNeutral());

  data = Neutral();
  data.deviceUniqueDataLen = 1;
  for (auto [code, expected] :
       std::array<std::pair<uint8_t, uint32_t>, 3>{{{0x0d, PAD_BUTTON_CROSS},
                                                 {0x0f, PAD_BUTTON_CIRCLE}, {0x00, 0}}})
  {
    data.deviceUniqueData[0] = code;
    state = PS5InputState::Translate(data, PAD_PORT_TYPE_REMOTE_CONTROL);
    assert(state.buttons == expected);
    assert(state.IsNeutral() == (expected == 0));
    assert(PS5InputState::Translate(data).IsNeutral());
  }
  data.buttons = PAD_BUTTON_UP;
  data.deviceUniqueData[0] = 0x0f;
  assert(PS5InputState::Translate(data, PAD_PORT_TYPE_REMOTE_CONTROL).buttons ==
         (PAD_BUTTON_UP | PAD_BUTTON_CIRCLE));
  for (uint8_t length : {0, 2, 12, 255})
  {
    data.deviceUniqueDataLen = length;
    assert(PS5InputState::Translate(data, PAD_PORT_TYPE_REMOTE_CONTROL).buttons == PAD_BUTTON_UP);
  }
  data.deviceUniqueDataLen = 1;
  data.deviceUniqueData[0] = 0x49;
  assert(PS5InputState::Translate(data, PAD_PORT_TYPE_REMOTE_CONTROL).buttons == PAD_BUTTON_UP);
  data.deviceUniqueDataLen = 0;
  data.buttons = PAD_BUTTON_CROSS;
  assert(PS5InputState::Translate(data, PAD_PORT_TYPE_REMOTE_CONTROL,
                                  PS5_REMOTE_LOTUS_PLAYPAUSE | PS5_REMOTE_LOTUS_BUTTONS)
             .buttons == (PAD_BUTTON_CROSS | PS5_REMOTE_LOTUS_BUTTONS));
  assert(PS5InputState::Translate(Neutral(), PAD_PORT_TYPE_REMOTE_CONTROL, PAD_BUTTON_UP).IsNeutral());
}

void TestLifecycle()
{
  Reset({10, 20, 30, 40});
  CPS5InputBackend backend;
  assert(backend.Scan());
  assert(backend.GetDevices().empty());
  const auto frames = backend.Poll(false);
  assert(frames.size() == 5);
  assert(!frames.back().connected && frames.back().portType == PAD_PORT_TYPE_REMOTE_CONTROL);
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
  assert(frames.size() == 4);
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
  assert(frames.size() == 65 && pads[10].reports.size() == 6);
  for (size_t i = 0; i < 64; ++i)
    assert(frames[i].state.buttons == (i % 2 == 0 ? PAD_BUTTON_CROSS : 0));
  assert(backend.Poll(false).size() == 7);
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
  assert(frames.size() == 2 && !frames[0].connected);
  assert(frames[0].state.IsNeutral() && pads[10].closes == 1);
}

void TestRemoteOnly()
{
  Reset({10, -1, -1, -1});
  pads[10].state = Neutral(false);
  remote.state = {};
  remote.state.connected = true;
  CPS5InputBackend backend;
  assert(backend.Scan());
  backend.Poll(false);
  const auto devices = backend.GetDevices();
  assert(devices.size() == 1);
  assert(devices[0].userId == USER_ID_SYSTEM);
  assert(devices[0].portType == PAD_PORT_TYPE_REMOTE_CONTROL);
  assert(devices[0].port == USER_SERVICE_MAX_USERS && !devices[0].supportsRumble);
  assert(remote.modes == 0 && remote.vibration.empty());
  auto pressed = remote.state;
  pressed.deviceUniqueDataLen = 1;
  pressed.deviceUniqueData[0] = 0x0d;
  auto released = remote.state;
  released.deviceUniqueDataLen = 1;
  remote.reports = {pressed, released, pressed};
  auto frames = backend.Poll(false);
  assert(frames.size() == 4);
  assert(frames[1].portType == PAD_PORT_TYPE_REMOTE_CONTROL);
  assert(frames[1].state.buttons == PAD_BUTTON_CROSS);
  assert(frames[2].state.IsNeutral());
  assert(frames[3].state.buttons == PAD_BUTTON_CROSS);
  frames = backend.Poll(true);
  assert(frames.back().imeActivity && frames.back().state.IsNeutral());
  assert(backend.Poll(false).back().state.IsNeutral());
  remote.state = released;
  backend.Poll(false);
  remote.state = pressed;
  assert(backend.Poll(false).back().state.buttons == PAD_BUTTON_CROSS);
  remote.state.connected = false;
  assert(!backend.Poll(false).back().connected);
  assert(backend.GetDevices().empty());
  remote.state = pressed;
  assert(backend.Poll(false).back().state.IsNeutral());
  remote.state = released;
  backend.Poll(false);
  assert(backend.GetDevices().size() == 1);
  users.fill(-1);
  assert(backend.Scan());
  assert(pads[10].closes == 1);
  assert(backend.Poll(false).size() == 1 && backend.GetDevices().size() == 1);
  backend.Close();
  assert(remote.closes == 1 && remote.vibration.empty());
}

void TestRemotePayloadFrames()
{
  Reset({-1, -1, -1, -1});
  auto select = Neutral();
  select.deviceUniqueDataLen = 1;
  select.deviceUniqueData[0] = 0x0d;
  auto back = select;
  back.deviceUniqueData[0] = 0x0f;
  auto released = select;
  released.deviceUniqueData[0] = 0x00;
  remote.state = select;
  CPS5InputBackend backend;
  assert(backend.Scan());
  assert(backend.Poll(false).front().state.IsNeutral());
  remote.state = released;
  assert(backend.Poll(false).front().state.IsNeutral());
  remote.reports = {select, released, back, released};
  const auto frames = backend.Poll(false);
  assert(frames.size() == 4);
  constexpr std::array<uint32_t, 4> expected{PAD_BUTTON_CROSS, 0, PAD_BUTTON_CIRCLE, 0};
  for (size_t i = 0; i < expected.size(); ++i)
  {
    assert(frames[i].connected && frames[i].portType == PAD_PORT_TYPE_REMOTE_CONTROL);
    assert(frames[i].state.buttons == expected[i]);
    for (float axis : frames[i].state.axes)
      assert(axis == 0);
  }
  remote.state = back;
  const auto imeFrames = backend.Poll(true);
  assert(imeFrames.front().imeActivity && imeFrames.front().state.IsNeutral());
  assert(backend.Poll(false).front().state.IsNeutral());
  remote.state = released;
  backend.Poll(false);
  remote.state = back;
  assert(backend.Poll(false).front().state.buttons == PAD_BUTTON_CIRCLE);
  assert(remote.vibration.empty());
}

void TestRemoteLotusKeys()
{
  Reset({-1, -1, -1, -1});
  remote.state = Neutral();
  CPS5InputBackend backend;
  assert(backend.Scan());
  assert(lotus.queryCalls == 0);
  assert(backend.Poll(false).front().state.IsNeutral() && lotus.reads == 0);
  assert(backend.Scan());
  assert(lotus.initCalls == 1 && lotus.queryCalls == 1);
  auto frames = backend.Poll(false);
  assert(frames.front().state.IsNeutral() && lotus.reads == 1);

  lotusButtons = PS5_REMOTE_LOTUS_PLAYPAUSE;
  auto pulse = backend.Poll(false);
  assert(pulse.front().state.buttons == PS5_REMOTE_LOTUS_PLAYPAUSE);
  lotusButtons = 0;
  assert(backend.Poll(false).front().state.IsNeutral());

  for (uint32_t unrelated : {PAD_BUTTON_UP, PAD_BUTTON_L2, PAD_BUTTON_CROSS})
  {
    lotusButtons = unrelated;
    assert(backend.Poll(false).front().state.IsNeutral());
  }

  lotusButtons = PS5_REMOTE_LOTUS_REWIND;
  remote.reports = []
  {
    auto data = Neutral();
    data.buttons = PAD_BUTTON_UP;
    return std::deque<PadData>{data};
  }();
  const auto merged = backend.Poll(false);
  assert(merged.front().state.buttons == (PAD_BUTTON_UP | PS5_REMOTE_LOTUS_REWIND));
  lotusButtons = 0;
  remote.reports.clear();
  remote.state = Neutral();
  assert(backend.Poll(false).front().state.IsNeutral());

  lotusReadRet = 0x803b0003;
  assert(backend.Poll(false).front().state.IsNeutral());
  assert(lotus.queryCalls == 1);
  std::this_thread::sleep_for(1100ms);
  assert(backend.Scan());
  assert(lotus.queryCalls == 2);
  lotusReadRet = 0;
  assert(backend.Poll(false).front().connected);

  backend.Close();
  lotus.queryRet = 0x809b0001;
  assert(backend.Scan());
  assert(backend.Poll(false).front().connected);
  assert(backend.Scan());
  assert(lotus.queryCalls == 3);
  auto retried = backend.Poll(false);
  assert(retried.front().connected && retried.front().state.IsNeutral());
  std::this_thread::sleep_for(1100ms);
  lotus.queryRet = 0;
  assert(backend.Scan());
  assert(lotus.queryCalls == 4);
  const int readsBefore = lotus.reads;
  assert(backend.Poll(false).front().connected);
  assert(lotus.reads == readsBefore + 1);
  backend.Close();
  assert(remote.closes == 2);
}

void TestInvalidLotusCount()
{
  for (int count : {0, 17, std::numeric_limits<int>::max()})
  {
    Reset({10, -1, -1, -1});
    remote.state = Neutral();
    lotus.reportedCount = count;
    CPS5InputBackend backend;
    assert(backend.Scan());
    backend.Poll(false);
    assert(backend.Scan());
    assert(lotus.queryCalls == 1 && lotus.reads == 0);
    assert(inputErrors > 0);
    remote.state.buttons = PAD_BUTTON_UP;
    const auto frames = backend.Poll(false);
    assert(std::any_of(frames.begin(), frames.end(), [](const auto& frame)
                      { return frame.state.buttons == PAD_BUTTON_UP; }));
    assert(lotus.reads == 0);
  }
}

void TestLotusReconnect()
{
  for (bool borrowed : {false, true})
  {
    for (bool batched : {false, true})
    {
      Reset({10, -1, -1, -1});
      remote.borrowed = borrowed;
      remote.state = Neutral();
      CPS5InputBackend backend;
      assert(backend.Scan());
      backend.Poll(false);
      assert(backend.Scan());
      backend.Poll(false);
      lotusButtons = PS5_REMOTE_LOTUS_PLAYPAUSE;
      pads[10].state.buttons = PAD_BUTTON_CROSS;
      auto frames = backend.Poll(false);
      assert(frames.back().state.buttons == PS5_REMOTE_LOTUS_PLAYPAUSE);
      const int queries = lotus.queryCalls;
      if (batched)
        remote.reports = {Neutral(false), Neutral()};
      else
        remote.state = Neutral(false);
      frames = backend.Poll(false);
      assert(frames.front().state.buttons == PAD_BUTTON_CROSS);
      assert(!frames[1].connected && frames[1].state.IsNeutral());
      if (batched)
        assert(frames[2].connected && frames[2].state.IsNeutral());
      assert(remote.closes == 0 && remote.opens == 1);

      lotus.recordId = lotus.localId = 0x000b0302;
      lotusButtons = 0;
      const int reads = lotus.reads;
      backend.Poll(false);
      assert(lotus.reads == reads);
      if (!batched)
      {
        assert(backend.Scan() && lotus.queryCalls == queries);
        remote.state = Neutral();
        assert(backend.Poll(false).back().connected);
        assert(lotus.reads == reads);
      }
      assert(backend.Scan());
      assert(lotus.queryCalls == queries + 1 && lotus.initCalls == 1);
      assert(backend.Poll(false).back().state.IsNeutral());
      lotusButtons = PS5_REMOTE_LOTUS_FASTFORWARD;
      assert(backend.Poll(false).back().state.buttons == PS5_REMOTE_LOTUS_FASTFORWARD);

      // Waking again with the same ID must also rediscover it.
      remote.state = Neutral(false);
      backend.Poll(false);
      lotusButtons = PS5_REMOTE_LOTUS_REWIND;
      remote.state = Neutral();
      remote.state.buttons = PAD_BUTTON_UP;
      backend.Poll(false);
      assert(backend.Scan() && lotus.queryCalls == queries + 2);
      assert(backend.Poll(false).back().state.IsNeutral());
      lotusButtons = 0;
      remote.state = Neutral();
      backend.Poll(false);
      lotusButtons = PS5_REMOTE_LOTUS_REWIND;
      assert(backend.Poll(false).back().state.buttons == PS5_REMOTE_LOTUS_REWIND);
      backend.Close();
      assert(remote.closes == (borrowed ? 0 : 1));
      assert(pads[10].closes == 1);
    }
  }
}

void TestLotusUnavailableAtWake()
{
  Reset({-1, -1, -1, -1});
  CPS5InputBackend backend;
  assert(backend.Scan());
  backend.Poll(false);
  assert(backend.Scan() && lotus.queryCalls == 0);
  remote.state = Neutral();
  backend.Poll(false);
  lotus.records = 0;
  assert(backend.Scan() && lotus.queryCalls == 1);
  assert(backend.Scan() && lotus.queryCalls == 1);
  remote.state.buttons = PAD_BUTTON_UP;
  assert(backend.Poll(false).front().state.buttons == PAD_BUTTON_UP);
  std::this_thread::sleep_for(1100ms);
  lotus.records = 1;
  assert(backend.Scan() && lotus.queryCalls == 2);
  lotusButtons = PS5_REMOTE_LOTUS_REWIND;
  assert(backend.Poll(false).front().state.buttons ==
         (PAD_BUTTON_UP | PS5_REMOTE_LOTUS_REWIND));

  lotusReadRet = static_cast<int>(0x803b0003);
  backend.Poll(false);
  remote.state = Neutral(false);
  backend.Poll(false);
  lotusReadRet = 0;
  lotusButtons = 0;
  remote.state = Neutral();
  backend.Poll(false);
  assert(backend.Scan() && lotus.queryCalls == 3);
  assert(backend.Poll(false).front().connected);
}

void TestRemotePayloadDiagnostics()
{
  Reset({10, -1, -1, -1});
  remote.state.connected = true;
  CPS5InputBackend backend;
  assert(backend.Scan());
  backend.Poll(false);
  assert(inputDebugLogs.size() == 1 && inputDebugLogs.back().ends_with(" 0 0 -"));
  backend.Poll(false);
  assert(inputDebugLogs.size() == 1);

  auto pressed = Neutral();
  pressed.deviceUniqueDataLen = 1;
  pressed.deviceUniqueData[0] = 0;
  remote.reports = {pressed, Neutral(), pressed};
  const auto frames = backend.Poll(false);
  assert(frames.size() == 4 && inputDebugLogs.size() == 4);
  assert(inputDebugLogs[1].ends_with(" 0 1 00"));
  assert(inputDebugLogs[2].ends_with(" 0 0 -"));
  assert(inputDebugLogs[3].ends_with(" 0 1 00"));
  for (const auto& frame : frames)
    assert(frame.state.IsNeutral());
  backend.Poll(false);
  assert(inputDebugLogs.size() == 4);

  remote.state.deviceUniqueData[0] = 0x49;
  backend.Poll(true);
  assert(inputDebugLogs.size() == 5 && inputDebugLogs.back().ends_with(" 0 1 49"));
  remote.state.deviceUniqueData[11] = 0xff;
  backend.Poll(false);
  assert(inputDebugLogs.size() == 5);
  remote.state.deviceUniqueDataLen = 12;
  backend.Poll(false);
  assert(inputDebugLogs.size() == 6);
  assert(inputDebugLogs.back().ends_with(" 0 12 4900000000000000000000ff"));
  remote.state.deviceUniqueDataLen = 255;
  backend.Poll(false);
  assert(inputErrors == 1 && inputDebugLogs.size() == 7);
  assert(inputDebugLogs.back().ends_with(" 0 255 4900000000000000000000ff"));
  backend.Poll(false);
  assert(inputErrors == 1 && inputDebugLogs.size() == 7);

  pads[10].state = pressed;
  backend.Poll(false);
  assert(inputDebugLogs.size() == 7);
  remote.state.connected = false;
  backend.Poll(false);
  remote.state = pressed;
  backend.Poll(false);
  assert(inputDebugLogs.size() == 8 && inputDebugLogs.back().ends_with(" 0 1 00"));
  assert(remote.vibration.empty());
}

void TestIndependentDevices()
{
  Reset({10, 20, 30, 40});
  remote.state.connected = true;
  CPS5InputBackend backend;
  assert(backend.Scan());
  backend.Poll(false);
  assert(backend.GetDevices().size() == 5);
  assert(!backend.SetMotorState(USER_ID_SYSTEM, 0, 1));
  pads[10].state.buttons = PAD_BUTTON_CIRCLE;
  remote.state.buttons = PAD_BUTTON_CROSS | 0x80000000u;
  const auto frames = backend.Poll(false);
  assert(frames[0].userId == 10 && frames[0].state.buttons == PAD_BUTTON_CIRCLE);
  assert(frames.back().userId == USER_ID_SYSTEM);
  assert(frames.back().state.buttons == (PAD_BUTTON_CROSS | 0x80000000u));
  assert(backend.SetMotorState(10, 0, 1));
  remote.state.connected = false;
  backend.Poll(false);
  assert(backend.GetDevices().size() == 4 && remote.vibration.empty());
  remote.state = Neutral();
  backend.Poll(false);
  assert(backend.GetDevices().size() == 5);
  users = {40, 10, 30, 20};
  assert(backend.Scan());
  assert(remote.opens == 1);
  remote.readError = -1;
  backend.Poll(false);
  assert(remote.closes == 1 && backend.GetDevices().size() == 4);
  assert(backend.SetMotorState(10, 0, 1));
  backend.Close();
  assert(remote.closes == 1);
}

void TestRemoteRetryAndBorrowing()
{
  Reset({-1, -1, -1, -1});
  remote.openError = -1;
  CPS5InputBackend backend;
  assert(backend.Scan());
  assert(backend.Poll(false).empty() && backend.GetDevices().empty());
  assert(remote.opens == 1 && inputErrors == 1);
  assert(backend.Scan());
  assert(remote.opens == 1 && inputErrors == 1);
  std::this_thread::sleep_for(1100ms);
  assert(backend.Scan());
  assert(remote.opens == 2 && inputErrors == 1);
  remote.openError = 0;
  remote.borrowed = true;
  remote.state.connected = true;
  std::this_thread::sleep_for(1100ms);
  assert(backend.Scan());
  backend.Poll(false);
  assert(backend.GetDevices().size() == 1);
  assert(!backend.GetDevices()[0].supportsRumble);
  backend.Close();
  assert(remote.closes == 0 && remote.modes == 0 && remote.vibration.empty());
}

void TestUnavailablePorts()
{
  for (bool controllerAbsent : {false, true})
  {
    Reset({10, -1, -1, -1});
    remote.state.connected = true;
    if (controllerAbsent)
      pads[10].openError = -1;
    else
      remote.openError = -1;
    CPS5InputBackend backend;
    assert(backend.Scan());
    backend.Poll(false);
    const auto devices = backend.GetDevices();
    assert(devices.size() == 1);
    assert(devices[0].portType ==
           (controllerAbsent ? PAD_PORT_TYPE_REMOTE_CONTROL : PAD_PORT_TYPE_STANDARD));
    assert(backend.Scan());
    assert(pads[10].opens == 1 && remote.opens == 1 && inputErrors == 1);
    assert(backend.SetMotorState(10, 0, 1) == !controllerAbsent);
    assert(remote.modes == 0 && remote.vibration.empty());
  }
}

FakePad& PadForHandle(int handle)
{
  return handle == REMOTE_HANDLE ? remote : pads.at(handle - 100);
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
  assert(index == 0);
  const bool isRemote = type == PAD_PORT_TYPE_REMOTE_CONTROL;
  assert(isRemote ? user == USER_ID_SYSTEM : type == PAD_PORT_TYPE_STANDARD);
  auto& pad = isRemote ? remote : pads[user];
  ++pad.opens;
  if (pad.openError)
    return pad.openError;
  return pad.borrowed ? static_cast<int>(PAD_ERROR_ALREADY_OPENED)
                      : isRemote ? REMOTE_HANDLE : user + 100;
}
int scePadGetHandle(int user, int type, int index)
{
  assert(index == 0);
  if (type == PAD_PORT_TYPE_REMOTE_CONTROL)
  {
    assert(user == USER_ID_SYSTEM);
    return REMOTE_HANDLE;
  }
  assert(type == PAD_PORT_TYPE_STANDARD);
  return user + 100;
}
int scePadClose(int handle)
{
  ++PadForHandle(handle).closes;
  return 0;
}
int scePadRead(int handle, void* output, int capacity)
{
  assert(capacity == 64);
  auto& pad = PadForHandle(handle);
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
  auto& pad = PadForHandle(handle);
  *static_cast<PadData*>(output) = pad.state;
  return pad.stateError;
}
int scePadSetVibrationMode(int handle, int mode)
{
  assert(mode == 2);
  assert(handle != REMOTE_HANDLE);
  auto& pad = PadForHandle(handle);
  ++pad.modes;
  return pad.modeError;
}
int scePadSetVibration(int handle, const void* vibration)
{
  assert(handle != REMOTE_HANDLE);
  auto& pad = PadForHandle(handle);
  if (pad.vibrationError)
    return pad.vibrationError;
  pad.vibration.push_back(*static_cast<const PadVibration*>(vibration));
  return 0;
}
int scePadLastLotusButtonData(int deviceId, uint32_t lookbackMs, uint32_t* buttons)
{
  assert(deviceId == static_cast<int>(lotus.localId));
  assert(lookbackMs <= 3000);
  ++lotus.reads;
  *buttons = lotusButtons;
  return lotusReadRet;
}
int sceDeviceServiceInitialize(uint32_t version, const void* param)
{
  assert(version == DEVICE_SERVICE_VERSION && param == nullptr);
  ++lotus.initCalls;
  return lotus.initRet;
}
int sceDeviceServiceQueryDeviceInfo_(uint32_t type, const void* filter, uint32_t flags,
                                     void* records, int capacity, int* count, int* extra,
                                     uint32_t recordSize)
{
  assert(type == DEVICE_SERVICE_TYPE_MEDIA_REMOTE && filter == nullptr && flags == 0);
  assert(recordSize == DEVICE_SERVICE_RECORD_SIZE && capacity >= 1);
  ++lotus.queryCalls;
  if (lotus.queryRet < 0)
    return lotus.queryRet;
  const int written = std::min(capacity, lotus.records);
  *count = lotus.reportedCount < 0 ? written : lotus.reportedCount;
  *extra = 0;
  auto* out = static_cast<DeviceServiceRecord*>(records);
  for (int i = 0; i < written; ++i)
  {
    out[i] = {};
    out[i].deviceId = lotus.recordId;
  }
  return lotus.queryRet;
}
int sceMbusConvertToLocalDeviceId2(uint64_t deviceId, uint32_t deviceClass, uint64_t* localId)
{
  assert(deviceClass == 0);
  if (lotus.convertRet)
    return lotus.convertRet;
  *localId = deviceId;
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
  TestRemoteOnly();
  TestRemotePayloadFrames();
  TestRemoteLotusKeys();
  TestInvalidLotusCount();
  TestLotusReconnect();
  TestLotusUnavailableAtWake();
  TestRemotePayloadDiagnostics();
  TestIndependentDevices();
  TestRemoteRetryAndBorrowing();
  TestUnavailablePorts();
  std::cout << "PASS: translation, four-user lifecycle, ordered reports, IME neutrality, "
               "disconnects, retry, borrowed ownership, native errors, independent motors, "
               "remote-only input, payload diagnostics, simultaneous devices and "
               "system-user remote ownership\n";
}
