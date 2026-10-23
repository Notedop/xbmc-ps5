#!/usr/bin/env python3
"""Exercise native title/add-on toolchains without claiming runtime readiness."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from native_c_link import Elf

ROOT = Path(__file__).resolve().parents[2]
SDK = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
spec = importlib.util.spec_from_file_location(
    "compiler_probe", Path(__file__).with_name("test-native-compiler-abi.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@unittest.skipUnless((SDK / "bin/prospero-clang++").is_file() and shutil.which("cmake")
                     and shutil.which("ninja"), "SDK, CMake and Ninja required")
class SharedToolchainTest(unittest.TestCase):
    def test_native_addon_private_cpp_dependency_uses_libcpp_before_stdio_overlay(self):
        with tempfile.TemporaryDirectory(prefix="ps5-addon-libcpp-") as temporary:
            directory = Path(temporary)
            runtime = directory / "runtime"
            (runtime / "include").mkdir(parents=True)
            shutil.copyfile(ROOT / "tools/ps5/sdk/include/stdio.h", runtime / "include/stdio.h")
            source = directory / "source"
            source.mkdir()
            (source / "probe.cpp").write_text(
                "#include <string>\n#include <cstdio>\n"
                "static_assert(sizeof(fpos_t) == 24);\n"
                "std::string value(const char* text) { return text; }\n")
            (source / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.19)\n"
                "project(PrivateCppProbe LANGUAGES CXX)\n"
                "add_library(probe STATIC probe.cpp)\n")
            build = directory / "build"
            commands = [
                ["cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
                 "-DCMAKE_TOOLCHAIN_FILE=" + str(ROOT / "toolchain/ps5-native-addon.cmake"),
                 "-DPS5_PAYLOAD_SDK=" + str(SDK), "-DPS5_NATIVE_RUNTIME_SDK=" + str(runtime)],
                ["cmake", "--build", str(build)],
            ]
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_addon_toolchain_finds_private_dependencies_without_host_leakage(self):
        for variable in ("ADDON_DEPENDS_PATH", "OUTPUT_DIR"):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory(
                    prefix="ps5-private-depends-") as temporary:
                directory = Path(temporary)
                private, host = directory / "private", directory / "host"
                for prefix in (private, host):
                    (prefix / "include").mkdir(parents=True)
                    (prefix / "lib").mkdir()
                    (prefix / "include/ps5_private_probe.h").touch()
                    (prefix / "lib/libps5_private_probe.a").touch()
                (host / "lib/libps5_host_only_probe.a").touch()
                source = directory / "source"
                source.mkdir()
                (source / "CMakeLists.txt").write_text(
                    "cmake_minimum_required(VERSION 3.19)\n"
                    "project(PrivateDependencyProbe LANGUAGES C CXX)\n"
                    "find_path(PRIVATE_HEADER NAMES ps5_private_probe.h REQUIRED)\n"
                    "find_library(PRIVATE_LIBRARY NAMES ps5_private_probe REQUIRED)\n"
                    "find_library(HOST_ONLY NAMES ps5_host_only_probe)\n")
                build = directory / "build"
                result = subprocess.run([
                    "cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
                    "-DCMAKE_TOOLCHAIN_FILE=" + str(ROOT / "toolchain/ps5-native-addon.cmake"),
                    "-DPS5_PAYLOAD_SDK=" + str(SDK),
                    "-D" + variable + "=" + str(private), "-DCMAKE_PREFIX_PATH=" + str(host),
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                cache = (build / "CMakeCache.txt").read_text()
                self.assertIn("PRIVATE_HEADER:PATH=" + str(private / "include"), cache)
                self.assertIn("PRIVATE_LIBRARY:FILEPATH=" +
                              str(private / "lib/libps5_private_probe.a"), cache)
                self.assertIn("HOST_ONLY:FILEPATH=HOST_ONLY-NOTFOUND", cache)

    def test_kodi_toolchain_preserves_native_source_abi_and_lowers_constructors(self):
        with tempfile.TemporaryDirectory(prefix="ps5-shared-toolchain-") as temporary:
            directory = Path(temporary)
            runtime = directory / "runtime"
            (runtime / "tools").mkdir(parents=True)
            (runtime / "include").mkdir()
            shutil.copyfile(ROOT / "tools/ps5/native-clang.py", runtime / "tools/native-clang.py")
            shutil.copyfile(ROOT / "tools/ps5/sdk/include/stdio.h", runtime / "include/stdio.h")
            (runtime / "PS5NativeRuntimeConfig.cmake").write_text(
                "# Compiler-only fixture: no runtime providers.\n")
            source = directory / "source"
            source.mkdir()
            (source / "probe.cpp").write_text(probe.SOURCE)
            (source / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.19)\n"
                "project(NativeAbiProbe LANGUAGES C CXX)\n"
                "set(CMAKE_CXX_STANDARD 17)\n"
                "set(CMAKE_EXPORT_COMPILE_COMMANDS ON)\n"
                "add_library(probe STATIC probe.cpp)\n")
            build = directory / "build"
            homebrew = directory / "homebrew"
            (homebrew / "lib").mkdir(parents=True)
            (homebrew / "lib/libps5_prefix_probe.a").touch()
            with (source / "CMakeLists.txt").open("a") as stream:
                stream.write("find_library(PS5_PREFIX_PROBE NAMES ps5_prefix_probe REQUIRED)\n")
            commands = [
                ["cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
                 "-DCMAKE_TOOLCHAIN_FILE=" + str(ROOT / "toolchain/ps5-kodi.cmake"),
                 "-DPS5_PAYLOAD_SDK=" + str(SDK), "-DPS5_NATIVE_RUNTIME_SDK=" + str(runtime),
                 "-DPS5_HOMEBREW_PREFIX=" + str(homebrew),
                 "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY"],
                ["cmake", "--build", str(build), "--target", "probe"],
            ]
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            image = Elf((build / "CMakeFiles/probe.dir/probe.cpp.o").read_bytes())
            symbols = dict(image.symbols(".symtab"))
            self.assertEqual(symbols["native_character"][5], 2)
            self.assertEqual(symbols["native_wide"][5], 32)
            self.assertIn(".init_array.101", image.sections)
            self.assertFalse(any(n.startswith((".ctors", ".dtors")) for n in image.sections))
            compile_command = json.loads((build / "compile_commands.json").read_text())[0]["command"]
            self.assertNotIn("--target=x86_64-unknown-freebsd", compile_command)
            self.assertIn(str(runtime / "include"), compile_command)
            cache = (build / "CMakeCache.txt").read_text()
            self.assertIn("PS5_PREFIX_PROBE:FILEPATH=" +
                          str(homebrew / "lib/libps5_prefix_probe.a"), cache)


if __name__ == "__main__":
    unittest.main()
