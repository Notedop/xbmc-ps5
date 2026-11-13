#!/usr/bin/env python3
"""Focused PS5 input lifecycle and packaged-data regressions."""

import ps5_test_paths
import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = ps5_test_paths.ROOT


class ControllerInputTests(unittest.TestCase):
    def test_controller_support_cannot_be_disabled_on_ps5(self):
        source = (ROOT / "xbmc/input/InputManager.cpp").read_text()
        start = source.index("bool CInputManager::IsControllerEnabled() const")
        method = source[start:source.index("\n}", start) + 2]
        compiler = shutil.which("clang++-18") or shutil.which("c++")
        self.assertIsNotNone(compiler, "A host C++20 compiler is required")
        harness = """
#include <cassert>
class CInputManager
{
public:
  bool m_enableController{false};
  bool IsControllerEnabled() const;
};
""" + method + """
int main()
{
  CInputManager input;
#if defined(TARGET_PS5)
  assert(input.IsControllerEnabled());
#else
  assert(!input.IsControllerEnabled());
#endif
  input.m_enableController = true;
  assert(input.IsControllerEnabled());
}
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "controller-support.cpp").write_text(harness)
            for defines in ([], ["-DTARGET_PS5"]):
                with self.subTest(defines=defines):
                    output = path / "controller-support"
                    subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                                    *defines, str(path / "controller-support.cpp"),
                                    "-o", str(output)], check=True)
                    subprocess.run([str(output)], check=True, timeout=30)

    def test_native_backend(self):
        compiler = shutil.which("clang++-18") or shutil.which("c++")
        self.assertIsNotNone(compiler, "A host C++20 compiler is required")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "controller-input"
            subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread",
                            "-fsanitize=address,undefined", "-g",
                            "-I" + str(ROOT / "tools/ps5/tests/input-stubs"),
                            "-I" + str(ROOT / "xbmc"),
                            str(ROOT / "tools/ps5/tests/controller-input.cpp"),
                            str(ROOT / "xbmc/platform/ps5/input/PS5InputBackend.cpp"),
                            "-o", str(output)], check=True)
            subprocess.run([str(output)], check=True, timeout=30)

    def test_missing_lotus_imports_are_logged_without_native_calls(self):
        compiler = shutil.which("clang++-18") or shutil.which("c++")
        self.assertIsNotNone(compiler, "A host C++20 compiler is required")
        source = (ROOT / "xbmc/platform/ps5/input/PS5InputBackend.cpp").read_text()
        start = source.index("bool CPS5InputBackend::InitializeDeviceService()")
        method = source[start:source.index("\nvoid CPS5InputBackend::ResolveLotus", start)]
        method = method.replace("CPS5InputBackend::", "TestBackend::")
        imports = ("sceDeviceServiceInitialize", "sceDeviceServiceQueryDeviceInfo_",
                   "sceMbusConvertToLocalDeviceId2", "scePadLastLotusButtonData")
        harness = """
#include "platform/ps5/sce/SceDeviceService.h"
#include "platform/ps5/sce/SceMbus.h"
#include "platform/ps5/sce/ScePad.h"
#include "utils/log.h"
#include <cassert>
using namespace KODI::PLATFORM::PS5;
struct TestBackend
{
  bool m_deviceServiceInitialized{false};
  bool m_lotusImportsUnavailable{false};
  bool InitializeDeviceService();
};
extern "C" {
int sceDeviceServiceInitialize(uint32_t, const void*) { assert(false); return 0; }
int sceDeviceServiceQueryDeviceInfo_(uint32_t, const void*, uint32_t, void*, int,
                                    int*, int*, uint32_t) { assert(false); return 0; }
int sceMbusConvertToLocalDeviceId2(uint64_t, uint32_t, uint64_t*) { assert(false); return 0; }
int scePadLastLotusButtonData(int, uint32_t, uint32_t*) { assert(false); return 0; }
}
"""
        main = """
int main()
{
  TestBackend backend;
  assert(!backend.InitializeDeviceService());
  assert(backend.m_lotusImportsUnavailable);
  assert(inputErrors == 1);
  assert(!backend.InitializeDeviceService());
  assert(inputErrors == 1);
}
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            for name in imports:
                with self.subTest(missing=name):
                    test_method = method.replace(
                        "&" + name + ";", "static_cast<decltype(&" + name + ")>(nullptr);")
                    self.assertNotEqual(test_method, method)
                    fixture = path / "missing-import.cpp"
                    fixture.write_text(harness + test_method + main)
                    output = path / "missing-import"
                    subprocess.run([compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror",
                                    "-I" + str(ROOT / "tools/ps5/tests/input-stubs"),
                                    "-I" + str(ROOT / "xbmc"), str(fixture), "-o", str(output)],
                                   check=True)
                    subprocess.run([str(output)], check=True, timeout=30)

    def test_obsolete_probe_is_removed(self):
        source = (ROOT / "xbmc/platform/ps5/main.cpp").read_text()
        cmake = (ROOT / "xbmc/platform/ps5/CMakeLists.txt").read_text()
        self.assertNotIn("MediaRemoteProbe", source + cmake)
        self.assertNotIn("KODI_PS5_MEDIA_PROBE", source)
        self.assertNotIn("kodi-media-probe", source)
        self.assertFalse((ROOT / "xbmc/platform/ps5/MediaRemoteProbe.cpp").exists())
        self.assertFalse((ROOT / "kodi-media-probe").exists())

    def test_controller_data(self):
        source = ROOT / "xbmc/platform/ps5/input/joystick.xml"
        expected = ET.parse(source).getroot()
        global_map = expected.find("global/joystick[@profile='game.controller.default']")
        self.assertEqual(global_map.get("profile"), "game.controller.default")
        for feature, action in {"a": "Select", "b": "Back", "x": "Info", "y": "ContextMenu",
                                "start": "PlayPause", "back": "PreviousMenu",
                                "lefttrigger": "Rewind", "righttrigger": "FastForward"}.items():
            self.assertEqual(global_map.find(feature).text, action)
        self.assertEqual(expected.find(
            "FullscreenVideo/joystick[@profile='game.controller.default']/leftthumb").text,
            "PlayerDebug")
        if os.environ.get("PS5_CONTROLLER_PACKAGE"):
            with zipfile.ZipFile(os.environ["PS5_CONTROLLER_PACKAGE"]) as package:
                def member(suffix):
                    matches = [name for name in package.namelist() if name.endswith(suffix)]
                    self.assertEqual(len(matches), 1, suffix)
                    return package.read(matches[0])
                actual = ET.fromstring(member("/share/kodi/system/keymaps/joystick.xml"))
                self.assertEqual(ET.tostring(actual), ET.tostring(expected))
                required = ET.fromstring(member("/share/kodi/system/addon-manifest.xml"))
                for addon in ("game.controller.dualsense", "game.controller.ps5.mediaremote",
                              "peripheral.joystick"):
                    node = next((item for item in required if item.text == addon), None)
                    self.assertIsNotNone(node, addon)
                    self.assertNotEqual(node.get("optional"), "true", addon)
                self.assertGreater(len(member("/share/kodi/addons/peripheral.joystick/peripheral.joystick.sprx")),
                                   1000)
                self.assertTrue(member("/share/kodi/addons/game.controller.dualsense/resources/layout.png")
                                .startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertTrue(member(
                    "/share/kodi/addons/game.controller.ps5.mediaremote/resources/layout.png")
                    .startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(ET.tostring(ET.fromstring(member(
                    "/share/kodi/addons/game.controller.ps5.mediaremote/resources/layout.xml"))),
                    ET.tostring(ET.parse(
                        ROOT / "addons/game.controller.ps5.mediaremote/resources/layout.xml").getroot()))

    def test_remote_profile(self):
        addon_id = "game.controller.ps5.mediaremote"
        addon = ROOT / "addons" / addon_id
        metadata = ET.parse(addon / "addon.xml").getroot()
        self.assertEqual(metadata.get("id"), addon_id)
        layout = ET.parse(addon / metadata.find(
            "extension[@point='kodi.game.controller']").get("library")).getroot()
        features = {button.get("name") for button in layout.findall("category/button")}
        expected = {"select": "Select", "back": "Back", "options": "ContextMenu",
                    "up": "Up", "right": "Right", "down": "Down", "left": "Left",
                    "playpause": "PlayPause", "rewind": "Rewind", "fastforward": "FastForward",
                    "volumeup": "VolumeUp", "volumedown": "VolumeDown", "mute": "Mute"}
        self.assertEqual(features, set(expected))
        self.assertTrue(all(button.get("type") == "digital"
                            for button in layout.findall("category/button")))
        keymap = ET.parse(ROOT / "xbmc/platform/ps5/input/joystick.xml").getroot()
        global_map = keymap.find(f"global/joystick[@profile='{addon_id}']")
        for feature, action in expected.items():
            self.assertEqual(global_map.find(feature).text, action)
        self.assertTrue((addon / "resources" / layout.get("image")).read_bytes()
                        .startswith(b"\x89PNG\r\n\x1a\n"))
        manifest = ET.parse(ROOT / "system/addon-manifest.xml").getroot()
        entry = next(item for item in manifest if item.text == addon_id)
        self.assertNotEqual(entry.get("optional"), "true")
        self.assertIn(f"addons/{addon_id}/*",
                      (ROOT / "cmake/installdata/common/addons.txt").read_text().splitlines())
        strings = (addon / "resources/language/resource.language.en_gb/strings.po").read_text()
        for element in [layout, *layout.findall("category"), *layout.findall("category/button")]:
            self.assertIn(f'msgctxt "#{element.get("label")}"', strings)

    def test_native_peripheral_mapping(self):
        source = (ROOT / "xbmc/platform/ps5/input/PeripheralBusPS5.cpp").read_text()
        methods = [
            source[source.index("std::string CPeripheralBusPS5::Location("):
                   source.index("bool CPeripheralBusPS5::ScanForDevices(")],
            source[source.index("bool CPeripheralBusPS5::InitializeButtonMap("):
                   source.index("void CPeripheralBusPS5::ProcessEvents(")],
        ]
        harness = r"""
#include "platform/ps5/input/PS5InputBackend.h"
#include "utils/log.h"
#include <array>
#include <cassert>
#include <cstdio>
#include <map>
#include <string>
#include <utility>
#include <vector>
using namespace KODI::PLATFORM::PS5;
namespace GAME { constexpr auto DEFAULT_CONTROLLER_ID = "game.controller.default"; }
enum { PERIPHERAL_JOYSTICK, PERIPHERAL_OTHER };
enum class PRIMITIVE_TYPE { BUTTON, MOTOR };
enum class SEMIAXIS_DIRECTION { NEGATIVE, POSITIVE };
enum class ANALOG_STICK_DIRECTION { LEFT, RIGHT, UP, DOWN };
struct Primitive {
  unsigned int index;
  Primitive(PRIMITIVE_TYPE, unsigned int i) : index(i) {}
  Primitive(unsigned int i, int, SEMIAXIS_DIRECTION, int) : index(i) {}
};
struct IButtonMap {
  std::string id, appearance;
  std::map<std::string, unsigned int> features;
  std::vector<Primitive> ignored;
  bool saved{false}, allowAppearance{true};
  explicit IButtonMap(std::string controller) : id(std::move(controller)) {}
  const std::string& ControllerID() const { return id; }
  std::string Location() const { return "test"; }
  void AddScalar(const char* name, Primitive primitive) { features[name] = primitive.index; }
  void AddAnalogStick(const char*, ANALOG_STICK_DIRECTION, Primitive) {}
  void SetIgnoredPrimitives(std::vector<Primitive> value) { ignored = std::move(value); }
  bool SetAppearance(const char* value) { appearance = value; return allowAppearance; }
  void SaveButtonMap() { saved = true; }
};
struct StringUtils {
  static std::string Format(const char*, uint32_t user, int type) {
    char buffer[80];
    std::snprintf(buffer, sizeof(buffer), "ps5/user/%08x/port/%d/index/0", user, type);
    return buffer;
  }
};
struct CPeripheral {
  std::string location;
  int type{PERIPHERAL_JOYSTICK};
  explicit CPeripheral(std::string value) : location(std::move(value)) {}
  int Type() const { return type; }
  const std::string& Location() const { return location; }
};
struct CPeripheralJoystick : CPeripheral {
  unsigned int buttons, axes, motors;
  unsigned int axisEvents{0}, frames{0};
  std::vector<std::pair<unsigned int, bool>> events;
  CPeripheralJoystick(std::string location, unsigned int b, unsigned int a, unsigned int m)
    : CPeripheral(std::move(location)), buttons(b), axes(a), motors(m) {}
  unsigned int ButtonCount() const { return buttons; }
  unsigned int AxisCount() const { return axes; }
  unsigned int MotorCount() const { return motors; }
  void OnButtonMotion(unsigned int index, bool down) { events.emplace_back(index, down); }
  void OnAxisMotion(unsigned int, float) { ++axisEvents; }
  void OnInputFrame() { ++frames; }
};
struct CPeripheralBusPS5 {
  std::map<std::string, PS5InputState> m_states;
  static std::string Location(int user, int type);
  std::string GetAppearance(const CPeripheral&) const;
  bool InitializeButtonMap(const CPeripheral&, IButtonMap&) const;
  void Dispatch(CPeripheralJoystick&, const PS5InputState&);
};
""" + "\n".join(methods) + r"""
int main() {
  CPeripheralBusPS5 bus;
  CPeripheralJoystick remote(bus.Location(USER_ID_SYSTEM, PAD_PORT_TYPE_REMOTE_CONTROL), 32, 0, 0);
  CPeripheralJoystick controller(bus.Location(10, PAD_PORT_TYPE_STANDARD), 16, 6, 2);
  assert(controller.Location() == "ps5/user/0000000a/port/0/index/0");
  assert(remote.Location() != controller.Location());
  assert(bus.GetAppearance(remote) == PS5_REMOTE_ID);
  assert(bus.GetAppearance(controller) == PS5_CONTROLLER_ID);
  IButtonMap remoteMap(PS5_REMOTE_ID);
  assert(bus.InitializeButtonMap(remote, remoteMap));
  assert(remoteMap.saved && remoteMap.appearance == PS5_REMOTE_ID);
  assert(remoteMap.features.size() == 10);
  for (auto [name, index] : std::map<std::string, unsigned int>{
         {"select", 14}, {"back", 13}, {"options", 3}, {"up", 4},
         {"right", 5}, {"down", 6}, {"left", 7}, {"playpause", 12},
         {"rewind", 11}, {"fastforward", 10}})
    assert(remoteMap.features.at(name) == index);
  assert(remoteMap.ignored.size() == 1 && remoteMap.ignored[0].index == 0);
  IButtonMap defaultMap(GAME::DEFAULT_CONTROLLER_ID);
  assert(bus.InitializeButtonMap(remote, defaultMap));
  assert(defaultMap.features.size() == 7 && defaultMap.features.at("y") == 3);
  assert(defaultMap.appearance == PS5_REMOTE_ID);
  IButtonMap controllerMap(GAME::DEFAULT_CONTROLLER_ID);
  assert(bus.InitializeButtonMap(controller, controllerMap));
  assert(controllerMap.features.at("a") == 0 && controllerMap.features.at("start") == 4);
  assert(controllerMap.features.at("leftmotor") == 0);
  assert(controllerMap.appearance == PS5_CONTROLLER_ID);
  IButtonMap unsupported("game.controller.snes");
  assert(!bus.InitializeButtonMap(remote, unsupported) && !unsupported.saved);
  IButtonMap invalid(PS5_REMOTE_ID);
  invalid.allowAppearance = false;
  assert(!bus.InitializeButtonMap(remote, invalid) && !invalid.saved);
  PS5InputState pressed;
  pressed.buttons = PAD_BUTTON_CROSS | 0x80000000u;
  bus.Dispatch(remote, pressed);
  assert((remote.events == std::vector<std::pair<unsigned int, bool>>{{14, true}, {31, true}}));
  assert(remote.axisEvents == 0 && remote.frames == 1);
  bus.Dispatch(remote, pressed);
  assert(remote.events.size() == 2);
  bus.Dispatch(controller, pressed);
  assert(controller.events.front() == std::make_pair(0u, true));
  assert(controller.axisEvents == 6 && remote.events.size() == 2);
  bus.Dispatch(remote, {});
  assert(remote.events.size() == 4 && !remote.events.back().second);
  assert(bus.m_states[controller.Location()].buttons == pressed.buttons);
  PS5InputState backPressed;
  backPressed.buttons = PAD_BUTTON_CIRCLE;
  bus.Dispatch(remote, backPressed);
  assert(remote.events.size() == 5);
  assert(remote.events.back() == std::make_pair(remoteMap.features.at("back"), true));
  assert(defaultMap.features.at("b") == remote.events.back().first);
  bus.Dispatch(remote, {});
  assert(remote.events.size() == 6 && !remote.events.back().second);
  assert(remoteMap.features.at("select") == remote.events.front().first);
  assert(defaultMap.features.at("a") == remote.events.front().first);
}
"""
        compiler = shutil.which("clang++-18") or shutil.which("c++")
        self.assertIsNotNone(compiler, "A host C++20 compiler is required")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            (path / "peripheral-mapping.cpp").write_text(harness)
            output = path / "peripheral-mapping"
            subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined",
                            "-I" + str(ROOT / "tools/ps5/tests/input-stubs"),
                            "-I" + str(ROOT / "xbmc"),
                            str(path / "peripheral-mapping.cpp"), "-o", str(output)], check=True)
            subprocess.run([str(output)], check=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
