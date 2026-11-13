#!/usr/bin/env python3
"""Check fail-closed package freshness using tiny native CMake projects."""

import ps5_test_paths
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = ps5_test_paths.HERE
SPEC = importlib.util.spec_from_file_location("package_freshness", HERE / "check-package-build.py")
FRESHNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FRESHNESS)


class PackageFreshnessTests(unittest.TestCase):
    def setUp(self):
        (HERE.parents[1] / "build").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=HERE.parents[1] / "build")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_generators_reject_pending_link_and_accept_completed_build(self):
        for generator in ("Ninja", "Unix Makefiles", "Ninja Multi-Config"):
            with self.subTest(generator=generator):
                source = self.root / generator
                source.mkdir()
                (source / "CMakeLists.txt").write_text(
                    "cmake_minimum_required(VERSION 3.20)\nproject(Freshness C)\n"
                    "add_executable(kodi.bin main.c)\n")
                code = source / "main.c"
                code.write_text("int main(void) { return 0; }\n")
                build = source / "out"
                subprocess.run(["cmake", "-S", str(source), "-B", str(build), "-G", generator],
                               check=True, capture_output=True)
                config = "Debug" if generator == "Ninja Multi-Config" else ""
                with self.assertRaisesRegex(ValueError, "pending link work"):
                    FRESHNESS.check(build, config)
                subprocess.run(["cmake", "--build", str(build), "--config", "Debug",
                                "--target", "kodi.bin"],
                               check=True, capture_output=True)
                FRESHNESS.check(build, config)
                project = source / "CMakeLists.txt"
                graph = build / ("Makefile" if generator == "Unix Makefiles" else "build.ninja")
                graph_before = graph.read_bytes()
                project.write_text(project.read_text() + "add_compile_definitions(CHANGED=1)\n")
                with self.assertRaisesRegex(ValueError, "pending .*work"):
                    FRESHNESS.check(build, config)
                self.assertEqual(graph.read_bytes(), graph_before,
                                 "freshness checking must not regenerate the build graph")
                subprocess.run(["cmake", "--build", str(build), "--config", "Debug",
                                "--target", "kodi.bin"], check=True, capture_output=True)
                FRESHNESS.check(build, config)
                code.write_text("int main(void) { return 1; }\n")
                with self.assertRaisesRegex(ValueError, "pending link work"):
                    FRESHNESS.check(build, config)

    def test_graph_tool_failures_and_unsupported_generators_are_not_fresh(self):
        for generator, program, message in (
                ("Ninja", "/bin/false", "cannot verify"),
                ("Ninja", "/does/not/exist", None),
                ("Unknown", "/bin/true", "unsupported")):
            (self.root / "CMakeCache.txt").write_text(
                f"CMAKE_GENERATOR:INTERNAL={generator}\n"
                f"CMAKE_MAKE_PROGRAM:FILEPATH={program}\n")
            with self.subTest(generator=generator, program=program):
                if message:
                    with self.assertRaisesRegex(ValueError, message):
                        FRESHNESS.check(self.root)
                else:
                    with self.assertRaises(OSError):
                        FRESHNESS.check(self.root)


if __name__ == "__main__":
    unittest.main()
