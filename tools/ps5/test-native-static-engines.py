#!/usr/bin/env python3
"""Verify patched real decoder engines retain separate mutable state."""
# SPDX-License-Identifier: GPL-3.0-or-later

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build/ps5-addon-repository/work"
LOCK = WORK / "source-lock.json"


class NativeStaticEnginesTest(unittest.TestCase):
    def test_nosefart_symbols_and_state_are_isolated(self):
        self.check_engines("audiodecoder.nosefart", "lib/nosefart",
                           ("nosefart_tag", "nosefart_0", "nosefart_1"),
                           "nosefart-engine-isolation.cpp", "nsf_init", "nes6502_mem_access")

    def test_wsr_symbols_and_state_are_isolated(self):
        self.check_engines("audiodecoder.wsr", "lib/in_wsr",
                           ("in_wsr_track", "in_wsr_0", "in_wsr_1"),
                           "wsr-engine-isolation.cpp", "WSRPlayerSetUp", "ChannelMuting")

    def check_engines(self, addon, subdirectory, engines, fixture, entrypoint, state):
        if not all(shutil.which(tool) for tool in ("cmake", "cc", "c++", "nm", "patch")):
            self.skipTest("host C/C++ toolchain, CMake, nm and patch required")
        if not LOCK.is_file():
            self.skipTest("locked source cache required; build " + addon + " first")
        lock = json.loads(LOCK.read_text())
        entry = lock["targets"].get(addon)
        if entry is None:
            self.skipTest("locked source cache required for " + addon)
        source = WORK / "sources" / addon / entry["sha"]
        if not source.is_dir():
            self.skipTest("locked source cache is not present for " + addon)

        def run(*command):
            result = subprocess.run(list(map(str, command)), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode, 0, result.stdout)
            return result.stdout

        with tempfile.TemporaryDirectory(prefix="test-static-engines-", dir=ROOT / "build") as temporary:
            root = Path(temporary)
            patched = root / "source"
            shutil.copytree(source, patched)
            run("cmake", "-DSOURCE_DIR=" + str(patched),
                "-DSERIES_DIR=" + str(ROOT / "tools/ps5/patches/addons" / addon),
                "-P", ROOT / "tools/ps5/apply-patch-series.cmake")
            binary = root / "build"
            run("cmake", "-S", patched / subdirectory, "-B", binary,
                "-DCMAKE_BUILD_TYPE=Debug")
            run("cmake", "--build", binary, "--parallel", "4")
            libraries = []
            inventories = []
            for engine in engines:
                library = binary / ("lib" + engine + ".a")
                self.assertTrue(library.is_file(), library)
                libraries.append(library)
                symbols = set()
                for line in run("nm", "--defined-only", "--extern-only",
                                "--format=posix", library).splitlines():
                    fields = line.split()
                    if len(fields) < 2:
                        continue
                    self.assertTrue(fields[0].startswith(engine + "_"), line)
                    symbols.add(fields[0][len(engine) + 1:])
                self.assertIn(entrypoint, symbols)
                self.assertIn(state, symbols)
                inventories.append(symbols)
            self.assertEqual(inventories[0], inventories[1])
            self.assertEqual(inventories[0], inventories[2])
            executable = root / "isolation"
            run("c++", "-std=c++17", "-I", patched / "lib", ROOT / "tools/ps5/tests" / fixture,
                *libraries, "-lm", "-o", executable)
            run(executable)


if __name__ == "__main__":
    unittest.main()
