#!/usr/bin/env python3
"""Regression tests for native PS5 RAR metadata and libc adaptations."""
# SPDX-License-Identifier: GPL-3.0-or-later
import ps5_test_paths
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from native_addon import validate_binary
from native_shared_link import load_provider

ROOT = ps5_test_paths.ROOT
WORK = ROOT / "build/ps5-addon-repository/work-rar"
SERIES = ROOT / "tools/ps5/patches/addons/vfs.rar"


class RarPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lock = WORK / "source-lock.json"
        if not lock.is_file() or not all(shutil.which(tool) for tool in
                                        ("patch", "cc", "c++", "nm")):
            raise unittest.SkipTest("Cached RAR source, patch, C/C++ compiler and nm required")
        sha = json.loads(lock.read_text())["targets"]["vfs.rar"]["sha"]
        cls.original = WORK / "sources/vfs.rar" / sha
        cls.scratch = ROOT / "build" / ("test-vfs-rar-ps5-" + uuid.uuid4().hex)
        cls.scratch.mkdir(parents=True)
        cls.addClassCleanup(shutil.rmtree, cls.scratch)
        cls.source = cls.scratch / "source"
        shutil.copytree(cls.original, cls.source)
        for patch in sorted(SERIES.glob("*.patch")):
            output = cls.run_command("patch", "--fuzz=0", "-p1", "-i", patch,
                                     cwd=cls.source)
            if "offset" in output or "fuzz" in output:
                raise AssertionError(output)

    @classmethod
    def run_command(cls, *args, **kwargs):
        kwargs.setdefault("env", dict(os.environ, TMPDIR=str(cls.scratch)))
        result = subprocess.run([str(arg) for arg in args], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
        if result.returncode:
            raise AssertionError(result.stdout)
        return result.stdout

    def test_multibyte_adapter_reads_changing_native_value(self):
        cmake = (self.source / "CMakeLists.txt").read_text()
        adapter = "int ___mb_cur_max(void) { return __mb_cur_max; }"
        self.assertIn(adapter, cmake)
        probe = self.scratch / "multibyte.c"
        probe.write_text("int __mb_cur_max;\n" + adapter + """
int main(void) {
  __mb_cur_max = 1;
  if (___mb_cur_max() != 1) return 1;
  __mb_cur_max = 6;
  return ___mb_cur_max() != 6;
}
""")
        binary = self.scratch / "multibyte"
        self.run_command("cc", probe, "-o", binary)
        self.run_command(binary)

    def test_optional_metadata_preserves_link_code_and_host_defaults(self):
        unrar = self.source / "lib/UnrarXLib"
        common = ["c++", "-DRARDLL", "-DSILENT", "-I",
                  ROOT / "xbmc/addons/kodi-dev-kit/include", "-c", unrar / "extinfo.cpp"]
        native_object = self.scratch / "native-metadata.o"
        self.run_command(*common, "-DRAR_NO_UNIX_OWNERS", "-DRAR_NO_LUTIMES",
                         "-o", native_object)
        symbols = self.run_command("nm", "-u", native_object)
        for name in ("getgrnam", "getpwnam", "chown", "lchown", "lutimes"):
            self.assertNotIn(" U " + name + "\n", symbols)
        self.assertIn(" U symlink\n", symbols)
        self.assertIn("IsRelativeSymlinkSafe", self.run_command("nm", native_object))
        host_object = self.scratch / "host-metadata.o"
        self.run_command(*common, "-o", host_object)
        host_symbols = self.run_command("nm", "-u", host_object)
        self.assertIn(" U getgrnam\n", host_symbols)
        self.assertIn(" U getpwnam\n", host_symbols)
        self.assertIn(" U symlink\n", host_symbols)
        self.assertIn(" U utimensat\n", host_symbols)
        native_definitions = self.run_command(
            "c++", "-DRARDLL", "-DSILENT", "-DRAR_NO_UNIX_OWNERS", "-DRAR_NO_LUTIMES",
            "-I", ROOT / "xbmc/addons/kodi-dev-kit/include",
            "-dM", "-E", unrar / "extinfo.cpp")
        self.assertIn("#define SAVE_LINKS", native_definitions)
        self.assertNotIn("#define USE_LUTIMES", native_definitions)
        self.assertNotIn("RAR_SMP", (unrar / "CMakeLists.txt").read_text())
        for name in ("arcread.cpp", "extract.cpp", "unpack.cpp", "unicode.cpp",
                     "file.cpp", "ulinks.cpp"):
            self.assertEqual((unrar / name).read_bytes(),
                             (self.original / "lib/UnrarXLib" / name).read_bytes())

    def test_native_capabilities_and_installed_receipt(self):
        sdk = ROOT / "build/ps5-native/sdk"
        descriptor = json.loads((sdk / "manifest.json").read_text())
        symbols = set()
        for manifest in descriptor["provider_manifests"]:
            symbols.update(s["name"] for s in load_provider(sdk / manifest)["symbols"])
        for name in ("getgrnam", "lutimes", "___mb_cur_max"):
            self.assertNotIn(name, symbols)
        self.assertIn("__mb_cur_max", symbols)
        state = json.loads((WORK / "build-state/vfs.rar.json").read_text())
        self.assertIn("-include ", state["context"]["native_site"])
        self.assertIn("/include/ps5_native_abi.h", state["context"]["native_site"])
        self.assertIn("-femulated-tls", state["context"]["native_site"])
        binary = Path(state["install_prefix"]) / "lib/kodi/addons/vfs.rar/vfs.rar.sprx"
        old_tempdir = tempfile.tempdir
        try:
            tempfile.tempdir = str(self.scratch)
            receipt = validate_binary(binary, sdk)
        finally:
            tempfile.tempdir = old_tempdir
        imports = receipt["native_dependencies"]["imports"]
        names = {s["name"] for s in imports}
        self.assertIn("__mb_cur_max", names)
        for name in ("___mb_cur_max", "getgrnam", "lutimes", "getpwnam", "chown", "lchown"):
            self.assertNotIn(name, names)
        self.assertTrue(all(s["info"] & 15 != 6 for s in imports))
        self.assertTrue(receipt["sign_extract_equal"])


if __name__ == "__main__":
    unittest.main()
