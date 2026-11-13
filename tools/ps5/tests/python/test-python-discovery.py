#!/usr/bin/env python3
"""Exercise PS5 target discovery through Kodi's real FindPython, without compiling."""

import ps5_test_paths
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = ps5_test_paths.ROOT
SPEC = importlib.util.spec_from_file_location("payload_sdk_check", ROOT / "tools/ps5/scripts/check-sdk.py")
SDK_CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SDK_CHECK)


@unittest.skipUnless(shutil.which("cmake") and shutil.which("ninja"), "CMake and Ninja required")
class PythonDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".test-python-", dir=ROOT / "tools/ps5")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source"
        self.source.mkdir()
        self.build = self.directory / "build"
        self.target = self.make_python("target")
        self.host = self.make_python("host")
        self.sdk = self.directory / "sdk"
        (self.sdk / "toolchain").mkdir(parents=True)
        (self.sdk / "toolchain/prospero.cmake").write_text(
            "set(CMAKE_SYSTEM_NAME FreeBSD)\n"
            "set(CMAKE_SYSTEM_PROCESSOR x86_64)\n"
            "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)\n"
            "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)\n"
            "set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)\n"
            "set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)\n")
        for name in SDK_CHECK.REQUIRED:
            path = self.sdk / name
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
        for name in ("prospero-clang", "prospero-clang++"):
            (self.sdk / "bin" / name).chmod(0o755)
        SDK_CHECK.record(self.sdk, None)
        self.runtime = self.directory / "runtime"
        for name in ("include/stdio.h", "tools/native-clang.py", "PS5NativeRuntimeConfig.cmake"):
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        modules = self.source / "modules"
        modules.mkdir()
        # Only the transitive libraries are stubbed; both Python find modules are real.
        for package, target in (("EXPAT", "EXPAT::EXPAT"), ("Iconv", "LIBRARY::Iconv"),
                                ("Intl", "Intl::Intl"), ("LibLZMA", "LibLZMA::LibLZMA")):
            (modules / f"Find{package}.cmake").write_text(
                f"set({package}_FOUND TRUE)\nadd_library({target} INTERFACE IMPORTED)\n")
        (self.source / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.19)\n"
            "project(PythonDiscovery NONE)\n"
            "set(CMAKE_SIZEOF_VOID_P 8)\n"
            f'list(PREPEND CMAKE_MODULE_PATH "{modules}" "{ROOT / "cmake/modules"}")\n'
            "set(APP_NAME_LC kodi)\n"
            "if(ENABLE_PYTHON)\n"
            "  find_package(Python MODULE)\n"
            "  if(NOT TARGET kodi::Python)\n"
            '    message(FATAL_ERROR "Python was silently disabled")\n'
            "  endif()\n"
            '  get_target_property(library kodi::Python IMPORTED_LOCATION)\n'
            '  get_target_property(headers kodi::Python INTERFACE_INCLUDE_DIRECTORIES)\n'
            '  file(WRITE "${CMAKE_BINARY_DIR}/observed.txt" '
            '"${library}\\n${headers}\\n${Python3_VERSION}\\n${PYTHON_VERSION}\\n")\n'
            "endif()\n")
        self.environment = os.environ.copy()
        self.environment["CMAKE_PREFIX_PATH"] = str(self.host)
        self.environment["Python3_ROOT_DIR"] = str(self.host)
        self.environment.pop("PS5_DEPENDS_PREFIX", None)
        self.environment.pop("PS5_HOMEBREW_PREFIX", None)
        self.environment.pop("PS5_OPENGL_PREFIX", None)

    def make_python(self, name, version="3.14.2"):
        prefix = self.directory / name
        include = prefix / "include/python3.14"
        include.mkdir(parents=True)
        (include / "Python.h").touch()
        major, minor, patch = version.split(".")
        (include / "patchlevel.h").write_text(
            f"#define PY_MAJOR_VERSION {major}\n#define PY_MINOR_VERSION {minor}\n"
            f'#define PY_MICRO_VERSION {patch}\n#define PY_VERSION "{version}"\n')
        (include / "pyconfig.h").write_text(
            "#define SIZEOF_VOID_P 8\n/* #undef Py_DEBUG */\n/* #undef Py_GIL_DISABLED */\n")
        (prefix / "lib").mkdir()
        (prefix / "lib/libpython3.14.a").touch()
        (prefix / "bin").mkdir()
        for name in ("python3", "python3.14", "python3.14-config"):
            executable = prefix / "bin" / name
            executable.write_text(
                f"#!/bin/sh\ntouch '{prefix / 'executed'}'\nexit 1\n")
            executable.chmod(0o755)
        return prefix

    def run_cmake(self, *arguments, success=True):
        result = subprocess.run(["cmake", *map(str, arguments)], env=self.environment,
                                capture_output=True, text=True)
        output = result.stdout + result.stderr
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        return output

    def configure(self, *arguments, success=True):
        return self.run_cmake(
            "-S", self.source, "-B", self.build, "-G", "Ninja",
            f"-DCMAKE_TOOLCHAIN_FILE={ROOT / 'tools/ps5/toolchain/ps5-kodi.cmake'}",
            f"-DPS5_PAYLOAD_SDK={self.sdk}", f"-DPS5_NATIVE_RUNTIME_SDK={self.runtime}",
            f"-DPS5_HOMEBREW_PREFIX={self.sdk / 'homebrew'}",
            f"-DPS5_OPENGL_PREFIX={self.sdk / 'opengl'}",
            f"-DPS5_DEPENDS_PREFIX={self.target}", "-DENABLE_PYTHON=ON",
            *arguments, success=success)

    def assert_python(self, prefix, version="3.14.2"):
        self.assertEqual((self.build / "observed.txt").read_text().splitlines(), [
            str(prefix / "lib/libpython3.14.a"), str(prefix / "include/python3.14"),
            version, "3.14"])
        cache = (self.build / "CMakeCache.txt").read_text()
        self.assertNotIn("PYTHON_PATH:", cache)
        self.assertNotIn("PYTHON_VER:", cache)
        self.assertIn(f"Python3_ROOT_DIR:PATH={prefix}", cache)
        self.assertIn("PS5_PYTHON_VERSION:STRING=3.14", cache)
        self.assertIn(f"Python3_INCLUDE_DIR:PATH={prefix}/include/python3.14", cache)
        self.assertIn(f"Python3_LIBRARY:FILEPATH={prefix}/lib/libpython3.14.a", cache)
        self.assertFalse((prefix / "executed").exists())
        self.assertFalse((self.host / "executed").exists())

    def test_unqualified_payload_sdk_stops_configuration(self):
        (self.sdk / SDK_CHECK.RECEIPT).unlink()
        output = self.configure(success=False)
        self.assertIn("unqualified SDK preserved", output)
        self.assertFalse((self.host / "executed").exists())

    def test_changed_sdk_wrapper_stops_plain_build_regeneration(self):
        self.configure()
        wrapper = self.sdk / "bin/prospero-clang"
        wrapper.write_text("changed wrapper")
        # Model a later SDK update even on coarse-resolution filesystems.
        changed = (self.build / "build.ninja").stat().st_mtime_ns + 1_000_000_000
        os.utime(wrapper, ns=(changed, changed))
        output = self.run_cmake("--build", self.build, success=False)
        self.assertIn("SDK file changed", output)

    def test_first_repeat_and_build_regeneration(self):
        self.configure()
        self.assert_python(self.target)
        self.run_cmake("-S", self.source, "-B", self.build)
        self.assert_python(self.target)
        with (self.source / "CMakeLists.txt").open("a") as stream:
            stream.write("# Trigger Ninja's plain CMake regeneration.\n")
        self.run_cmake("--build", self.build)
        self.assert_python(self.target)

    def test_changed_prefix_replaces_cached_development_artifacts(self):
        self.configure()
        alternate = self.make_python("alternate", "3.14.3")
        self.run_cmake("-S", self.source, "-B", self.build,
                       f"-DPS5_DEPENDS_PREFIX={alternate}")
        self.assert_python(alternate, "3.14.3")
        cache = (self.build / "CMakeCache.txt").read_text()
        for line in cache.splitlines():
            if line.startswith(("_Python3_", "Python3_")):
                self.assertNotIn(str(self.target), line)

    def test_host_artifact_hints_cannot_replace_target_inputs(self):
        self.configure(f"-DPython3_ROOT_DIR={self.host}",
                       f"-DPython3_EXECUTABLE={self.host}/bin/python3",
                       f"-D_Python3_CONFIG={self.host}/bin/python3.14-config",
                       f"-DPython3_INCLUDE_DIR={self.host}/include/python3.14",
                       f"-DPython3_LIBRARY={self.host}/lib/libpython3.14.a",
                       f"-D_Python3_INCLUDE_DIR={self.host}/include/python3.14",
                       f"-D_Python3_LIBRARY_RELEASE={self.host}/lib/libpython3.14.a")
        self.assert_python(self.target)
        self.assertIn(f"Python3_EXECUTABLE:UNINITIALIZED={self.host}/bin/python3",
                      (self.build / "CMakeCache.txt").read_text())

    def test_missing_target_artifacts_do_not_fall_back_to_host(self):
        for name in ("Python.h", "patchlevel.h", "pyconfig.h", "library"):
            with self.subTest(name=name):
                artifact = (self.target / "lib/libpython3.14.a" if name == "library"
                            else self.target / "include/python3.14" / name)
                contents = artifact.read_bytes()
                artifact.unlink()
                output = self.configure(success=False)
                self.assertIn("Incomplete PS5 target Python", output)
                artifact.write_bytes(contents)

    def test_invalid_version_is_required_not_silently_disabled(self):
        (self.target / "include/python3.14/patchlevel.h").write_text(
            '#define PY_VERSION "3.13.2"\n')
        output = self.configure(success=False)
        self.assertIn("Could NOT find Python3", output)

    def test_explicit_enable_with_missing_prefix_fails(self):
        output = self.configure(f"-DPS5_DEPENDS_PREFIX={self.directory / 'missing'}",
                                success=False)
        self.assertIn("Incomplete PS5 target Python", output)

    def test_invalid_selection_fails(self):
        for argument in ("-DPS5_PYTHON_VERSION=invalid", "-DPS5_DEPENDS_PREFIX=relative",
                         f"-DPYTHON_PATH={self.host}", "-DPYTHON_VER=3.13"):
            with self.subTest(argument=argument):
                self.configure(argument, success=False)
                shutil.rmtree(self.build)

    def test_disabled_python_does_not_require_target_development_files(self):
        self.configure("-DENABLE_PYTHON=OFF",
                       f"-DPS5_DEPENDS_PREFIX={self.directory / 'missing'}")


if __name__ == "__main__":
    unittest.main()
