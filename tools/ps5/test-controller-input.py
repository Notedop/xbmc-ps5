#!/usr/bin/env python3
"""Focused PS5 input lifecycle and packaged-data regressions."""

import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
        build = ROOT / "build/ps5-controller-tests"
        build.mkdir(parents=True, exist_ok=True)
        compiler = shutil.which("clang++-18") or shutil.which("c++")
        self.assertIsNotNone(compiler, "A host C++20 compiler is required")
        output = build / "controller-input"
        subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-pthread",
                        "-fsanitize=address,undefined", "-g",
                        "-I" + str(ROOT / "tools/ps5/tests/input-stubs"),
                        "-I" + str(ROOT / "xbmc"),
                        str(ROOT / "tools/ps5/tests/controller-input.cpp"),
                        str(ROOT / "xbmc/platform/ps5/input/PS5InputBackend.cpp"),
                        "-o", str(output)], check=True)
        subprocess.run([str(output)], check=True, timeout=30)

    def test_controller_data(self):
        source = ROOT / "xbmc/platform/ps5/input/joystick.xml"
        expected = ET.parse(source).getroot()
        global_map = expected.find("global/joystick")
        self.assertEqual(global_map.get("profile"), "game.controller.default")
        for feature, action in {"a": "Select", "b": "Back", "x": "Info", "y": "ContextMenu",
                                "start": "PlayPause", "back": "PreviousMenu",
                                "lefttrigger": "Rewind", "righttrigger": "FastForward"}.items():
            self.assertEqual(global_map.find(feature).text, action)
        self.assertEqual(expected.find("FullscreenVideo/joystick/leftthumb").text, "PlayerDebug")
        if os.environ.get("PS5_CONTROLLER_PACKAGE"):
            with zipfile.ZipFile(os.environ["PS5_CONTROLLER_PACKAGE"]) as package:
                def member(suffix):
                    matches = [name for name in package.namelist() if name.endswith(suffix)]
                    self.assertEqual(len(matches), 1, suffix)
                    return package.read(matches[0])
                actual = ET.fromstring(member("/share/kodi/system/keymaps/joystick.xml"))
                self.assertEqual(ET.tostring(actual), ET.tostring(expected))
                required = ET.fromstring(member("/share/kodi/system/addon-manifest.xml"))
                for addon in ("game.controller.dualsense", "peripheral.joystick"):
                    node = next((item for item in required if item.text == addon), None)
                    self.assertIsNotNone(node, addon)
                    self.assertNotEqual(node.get("optional"), "true", addon)
                self.assertGreater(len(member("/share/kodi/addons/peripheral.joystick/peripheral.joystick.sprx")),
                                   1000)
                self.assertTrue(member("/share/kodi/addons/game.controller.dualsense/resources/layout.png")
                                .startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
