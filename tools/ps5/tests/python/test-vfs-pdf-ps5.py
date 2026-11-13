#!/usr/bin/env python3
"""Check the PDF native private dependency and locale ABI patches."""
# SPDX-License-Identifier: GPL-3.0-or-later

import ps5_test_paths
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = ps5_test_paths.ROOT
WORK = ROOT / "build/ps5-addon-repository/work-pdf"
LIBRARIES = ("poppler-cpp", "poppler", "freetype", "png16", "openjp2", "iconv", "jpeg", "z")


class PdfNativeTest(unittest.TestCase):
    def run_command(self, *command, success=True):
        result = subprocess.run(list(map(str, command)), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def setUp(self):
        if not all(shutil.which(tool) for tool in ("cmake", "patch", "cc")):
            self.skipTest("host C compiler, CMake and patch required")
        lock_path = WORK / "source-lock.json"
        if not lock_path.is_file():
            self.skipTest("build vfs.pdf in work-pdf to populate the source lock")
        entry = json.loads(lock_path.read_text())["targets"]["vfs.pdf"]
        source = WORK / "sources/vfs.pdf" / entry["sha"]
        self.temporary = tempfile.TemporaryDirectory(prefix="test-vfs-pdf-", dir=ROOT / "build")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        shutil.copytree(source, self.source)
        self.run_command("cmake", "-DSOURCE_DIR=" + str(self.source),
                         "-DSERIES_DIR=" + str(ROOT / "tools/ps5/patches/addons/vfs.pdf"),
                         "-P", ROOT / "tools/ps5/apply-patch-series.cmake")
        self.prefix = self.root / "private"
        (self.prefix / "lib").mkdir(parents=True)
        (self.prefix / "include/poppler/cpp").mkdir(parents=True)
        (self.prefix / "include/poppler/cpp/poppler-document.h").touch()
        for library in LIBRARIES:
            (self.prefix / "lib" / f"lib{library}.a").touch()
        self.probe = self.root / "find.cmake"
        self.probe.write_text(
            'set(PS5_NATIVE_SHARED_ADDONS TRUE)\n'
            f'set(ADDON_DEPENDS_PATH "{self.prefix}")\n'
            'set(PC_POPPLER_STATIC_LDFLAGS "-lwrong-core-poppler")\n'
            f'include("{self.source}/cmake/FindPoppler.cmake")\n'
            'if(NOT POPPLER_FOUND)\n  message(FATAL_ERROR "Poppler not found")\nendif()\n'
            f'file(WRITE "{self.root}/libraries" "${{POPPLER_LIBRARIES}}")\n'
        )

    def test_private_archives_are_absolute_and_ordered(self):
        self.run_command("cmake", "-P", self.probe)
        self.assertEqual((self.root / "libraries").read_text().split(";"),
                         [str(self.prefix / "lib" / f"lib{name}.a") for name in LIBRARIES])
        self.run_command("cmake", "-DSOURCE_DIR=" + str(self.source),
                         "-DSERIES_DIR=" + str(ROOT / "tools/ps5/patches/addons/vfs.pdf"),
                         "-P", ROOT / "tools/ps5/apply-patch-series.cmake")

    def test_missing_private_archive_never_falls_back(self):
        for name in LIBRARIES:
            with self.subTest(library=name):
                archive = self.prefix / "lib" / f"lib{name}.a"
                archive.unlink()
                output = self.run_command("cmake", "-P", self.probe, success=False)
                self.assertIn("Missing private Poppler dependency", output)
                self.assertIn(str(archive), output)
                archive.touch()

    def test_large_file_probes_select_native_off_t_functions(self):
        flags = (self.source / "depends/common/poppler/flags.txt").read_text().split()
        self.assertIn("-DHAVE_PREAD64=0", flags)
        self.assertIn("-DHAVE_LSEEK64=0", flags)

    def test_multibyte_accessor_reads_mutable_native_data(self):
        (self.root / "stdlib.h").write_text("extern int __mb_cur_max;\n")
        cmake = self.root / "adapter.cmake"
        # project() cannot run in script mode; retain the adapter block verbatim.
        text = (self.source / "CMakeLists.txt").read_text()
        start = text.index("if(PS5_NATIVE_SHARED_ADDONS)\n  set(_multibyte_adapter")
        end = text.index("\nendif()", start) + len("\nendif()")
        cmake.write_text(
            'set(PS5_NATIVE_SHARED_ADDONS TRUE)\n'
            f'set(CMAKE_CURRENT_BINARY_DIR "{self.root}")\n'
            'function(target_sources)\nendfunction()\n' + text[start:end] + "\n")
        self.run_command("cmake", "-P", cmake)
        fixture = self.root / "locale.c"
        fixture.write_text(
            "int __mb_cur_max = 1;\nint ___mb_cur_max(void);\n"
            "int main(void) {\n"
            "  if (___mb_cur_max() != 1) return 1;\n"
            "  __mb_cur_max = 6;\n"
            "  return ___mb_cur_max() != 6;\n}\n")
        binary = self.root / "locale"
        self.run_command("cc", "-I", self.root, self.root / "ps5-multibyte-limit.c",
                         fixture, "-o", binary)
        self.run_command(binary)


if __name__ == "__main__":
    unittest.main()
