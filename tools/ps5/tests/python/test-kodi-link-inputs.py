#!/usr/bin/env python3
"""Regressions for native-title archive selection."""

import ps5_test_paths
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "kodi_link_inputs", (ps5_test_paths.HERE / 'kodi-link-inputs.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TestKodiLinkInputs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build = self.root / "build"
        self.hb = self.root / "homebrew"
        self.sdk = self.root / "sdk"
        self.gl = self.root / "opengl"
        self.depends = self.root / "depends with spaces"
        for path in (self.build, self.hb / "lib", self.sdk / "target/lib",
                     self.gl / "lib", self.depends):
            path.mkdir(parents=True)

    def write_rule(self, libraries, search_path=""):
        (self.build / "build.ninja").write_text(
            "build kodi.bin: LINK objects\n"
            f"  LINK_PATH = {search_path}\n"
            f"  LINK_LIBRARIES = -Wl,--whole-archive core.a "
            f"-Wl,--no-whole-archive {libraries}\n")

    def extract(self):
        return module.extract(self.build, self.hb, self.sdk, self.gl)

    def test_explicit_search_path_wins_over_stale_sdk_copy(self):
        for directory in (self.depends, self.hb / "lib"):
            (directory / "libsmb2.a").touch()
        self.write_rule("-lsmb2 -lsmb2", f'-L"{self.depends}"')
        whole, deps = self.extract()
        self.assertEqual([self.build / "core.a"], whole)
        self.assertEqual([self.depends / "libsmb2.a"], deps)

    def test_separate_search_path_argument_and_order(self):
        for directory in (self.depends, self.gl / "lib"):
            (directory / "libsmb2.a").touch()
        self.write_rule("-lsmb2", f'-L "{self.depends}" -L"{self.gl / "lib"}"')
        self.assertEqual([self.depends / "libsmb2.a"], self.extract()[1])

    def test_fallback_archives_and_native_stub(self):
        (self.hb / "lib/libfallback.a").touch()
        (self.sdk / "target/lib/libSceNet.so").touch()
        self.write_rule("-lfallback -lSceNet relative.a")
        self.assertEqual(
            [self.hb / "lib/libfallback.a", self.build / "relative.a"],
            self.extract()[1])

    def test_payload_runtime_archives_remain_excluded(self):
        (self.sdk / "target/lib/libpthread.a").touch()
        self.write_rule("-lpthread")
        self.assertEqual([], self.extract()[1])

    def test_missing_library_is_not_silently_dropped(self):
        self.write_rule("-lmissing")
        with self.assertRaisesRegex(FileNotFoundError, "-lmissing"):
            self.extract()

    def test_conflicting_iconv_archives_are_rejected(self):
        sdk_iconv = self.hb / "lib/libiconv.a"
        depends_iconv = self.depends / "libiconv.a"
        sdk_iconv.touch()
        depends_iconv.touch()
        self.write_rule(f'"{sdk_iconv}" "{depends_iconv}"')
        with self.assertRaisesRegex(ValueError, "conflicting iconv archives"):
            self.extract()

    def test_repeated_selected_iconv_archive_is_deduplicated(self):
        iconv = self.depends / "libiconv.a"
        iconv.touch()
        self.write_rule(f'"{iconv}" -liconv', f'-L"{self.depends}"')
        self.assertEqual([iconv], self.extract()[1])


@unittest.skipUnless(all(shutil.which(tool) for tool in ("cmake", "ninja", "pkg-config", "cc", "ar")),
                     "CMake, Ninja, pkg-config and a host C toolchain required")
class TestIconvDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ps5-iconv-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.build = self.root / "build"
        self.sdk = self.root / "sdk"
        self.depends = self.root / "depends"
        for directory in (self.source, self.sdk / "lib/pkgconfig",
                          self.depends / "lib", self.depends / "include"):
            directory.mkdir(parents=True)
        helpers = self.source / "cmake/scripts/common"
        helpers.mkdir(parents=True)
        shutil.copyfile(ps5_test_paths.ROOT / "cmake/scripts/common/ModuleHelpers.cmake",
                        helpers / "ModuleHelpers.cmake")
        self.archive(self.sdk / "lib/libass.a", "void ass_fixture(void) {}")
        self.archive(self.sdk / "lib/libiconv.a",
                     "void *libiconv_open(const char *to, const char *from) { return (void *)-1; }")
        self.archive(self.depends / "lib/libiconv.a", """
#include <string.h>
void *libiconv_open(const char *to, const char *from) {
    return !strcmp(to, "UTF-8") && !strcmp(from, "CP437") ? (void *)1 : (void *)-1;
}
""")
        (self.depends / "include/iconv.h").write_text(
            "#define _LIBICONV_VERSION 0x0112\n"
            "#define iconv_open libiconv_open\n"
            "void *libiconv_open(const char *, const char *);\n")
        (self.sdk / "lib/pkgconfig/libass.pc").write_text(
            f"prefix={self.sdk}\nlibdir=${{prefix}}/lib\n"
            "Name: libass\nDescription: iconv dependency fixture\nVersion: 99.0.0\n"
            "Libs: -L${libdir} -lass -liconv\n")
        (self.source / "core.c").write_text("void core_fixture(void) {}\n")
        (self.source / "main.c").write_text(
            '#include <iconv.h>\n'
            'int main(void) { return iconv_open("UTF-8", "CP437") == (void *)-1; }\n')
        (self.source / "CMakeLists.txt").write_text(f"""
cmake_minimum_required(VERSION 3.19)
project(IconvDiscovery C)
set(APP_NAME_LC kodi)
set(PROJECTSOURCE "{ps5_test_paths.ROOT}")
list(PREPEND CMAKE_MODULE_PATH "{ps5_test_paths.ROOT / 'cmake/modules'}")
set(ENABLE_INTERNAL_ASS OFF)
find_package(ASS REQUIRED MODULE)
find_package(Iconv REQUIRED MODULE)
add_library(core STATIC core.c)
add_executable(kodi.bin main.c)
target_link_libraries(kodi.bin PRIVATE
  -Wl,--whole-archive core -Wl,--no-whole-archive kodi::ASS kodi::Iconv)
""")

    def run_command(self, command, **kwargs):
        result = subprocess.run(command, capture_output=True, text=True, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def archive(self, path, source):
        c_file = path.with_suffix(".c")
        obj = path.with_suffix(".o")
        c_file.write_text(source)
        self.run_command(["cc", "-c", str(c_file), "-o", str(obj)])
        self.run_command(["ar", "rcs", str(path), str(obj)])

    def configure(self, platform):
        environment = os.environ.copy()
        environment["PKG_CONFIG_LIBDIR"] = str(self.sdk / "lib/pkgconfig")
        environment["PKG_CONFIG_PATH"] = ""
        environment.pop("PKG_CONFIG_SYSROOT_DIR", None)
        self.run_command([
            "cmake", "-S", str(self.source), "-B", str(self.build), "-G", "Ninja",
            f"-DCORE_SYSTEM_NAME={platform}", "-DIconv_IS_BUILT_IN=OFF",
            f"-DIconv_INCLUDE_DIR={self.depends / 'include'}",
            f"-DIconv_LIBRARY={self.depends / 'lib/libiconv.a'}"], env=environment)
        self.run_command(["cmake", "--build", str(self.build)])

    def test_ps5_transitive_iconv_uses_selected_target_on_repeat_configure(self):
        for _ in range(2):
            self.configure("ps5")
            _, deps = module.extract(self.build, self.sdk, self.sdk, self.sdk)
            self.assertIn(self.depends / "lib/libiconv.a", deps)
            self.assertNotIn(self.sdk / "lib/libiconv.a", deps)
            self.assertIn(self.sdk / "lib/libass.a", deps)
            self.run_command([str(self.build / "kodi.bin")])

    def test_other_platform_pkgconfig_interface_is_unchanged(self):
        self.configure("linux")
        result = subprocess.run([str(self.build / "kodi.bin")], capture_output=True)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
