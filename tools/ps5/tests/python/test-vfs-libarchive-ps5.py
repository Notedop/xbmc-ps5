#!/usr/bin/env python3
"""Host regression tests for the native libarchive add-on patch series."""
# SPDX-License-Identifier: GPL-3.0-or-later
import ps5_test_paths
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET

from native_shared_link import load_provider
from native_addon import validate_binary

ROOT = ps5_test_paths.ROOT
WORK = ROOT / "build/ps5-addon-repository/work-libarchive"
SERIES = ROOT / "tools/ps5/patches/addons/vfs.libarchive"


class LibarchivePatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lock = WORK / "source-lock.json"
        if not lock.is_file() or not shutil.which("patch") or not shutil.which("cmake"):
            raise unittest.SkipTest("Cached source, patch and CMake required")
        sha = json.loads(lock.read_text())["targets"]["vfs.libarchive"]["sha"]
        source = WORK / "sources/vfs.libarchive" / sha
        cls.scratch = ROOT / "build" / ("test-vfs-libarchive-ps5-" + uuid.uuid4().hex)
        cls.scratch.mkdir(parents=True)
        cls.addClassCleanup(shutil.rmtree, cls.scratch)
        cls.source = cls.scratch / "source"
        shutil.copytree(source, cls.source)
        for patch in sorted(SERIES.glob("*.patch")):
            cls.run_command("patch", "--fuzz=0", "-p1", "-i", patch, cwd=cls.source)

    @staticmethod
    def run_command(*args, **kwargs):
        result = subprocess.run([str(arg) for arg in args], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
        if result.returncode:
            raise AssertionError(result.stdout)
        return result.stdout

    def test_native_openssl_driver_preserves_abi_and_private_zlib(self):
        site = self.scratch / "config.site"
        compiler = self.scratch / "fake-cc"
        compiler.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        compiler.chmod(0o755)
        site.write_text(f"CC='{compiler} -include native-abi.h -femulated-tls'\n")
        env = dict(os.environ, CONFIG_SITE=str(site))
        build = self.scratch / "openssl-build"
        prefix = self.scratch / "private"
        self.run_command("cmake", "-S", self.source / "depends/common/openssl",
                         "-B", build, "-DPS5_NATIVE_SHARED_ADDONS=ON",
                         "-DCORE_SYSTEM_NAME=freebsd", f"-DOUTPUT_DIR={prefix}", env=env)
        output = self.run_command(build / "ps5-openssl-cc", "-c", "crypto.c",
                                  "-lz", "-o", "crypto.o", env=env).splitlines()
        self.assertEqual(output, ["-include", "native-abi.h", "-femulated-tls",
                                  "-c", "crypto.c", str(prefix / "lib/libz.a"),
                                  "-o", "crypto.o"])
        commands = "\n".join(p.read_text() for p in build.rglob("*cfgcmd.txt"))
        self.assertIn("BSD-generic64", commands)
        for feature in ("asm", "dso", "async", "dgram"):
            self.assertIn(f"no-{feature}", commands)
        self.assertIn("no-apps", commands)
        self.assertIn("no-tests", commands)

    def test_private_link_and_manifest_scope(self):
        cmake = (self.source / "CMakeLists.txt").read_text()
        self.assertIn('set(_private_prefix "${ADDON_DEPENDS_PATH}")', cmake)
        self.assertIn('foreach(_library IN ITEMS archive bz2 lz4 lzo2 lzma ssl crypto z)', cmake)
        self.assertIn('list(APPEND DEPLIBS "${_archive}")', cmake)
        self.assertIn('"-Wl,-Bsymbolic"', cmake)
        self.assertIn("int ___mb_cur_max(void) { return __mb_cur_max; }", cmake)
        template = (self.source / "vfs.libarchive/addon.xml.in").read_text()
        manifest = ET.fromstring(template.replace("@PLATFORM@", "ps5"))
        vfs = manifest.find("./extension[@point='kodi.vfs']")
        self.assertIsNotNone(vfs)
        self.assertNotIn(".rar", vfs.attrib.get("extensions", ""))

    def test_libc_capabilities_match_native_providers(self):
        sdk = ROOT / "build/ps5-native/sdk"
        if not (sdk / "manifest.json").is_file():
            self.skipTest("Qualified native provider SDK required")
        descriptor = json.loads((sdk / "manifest.json").read_text())
        symbols = set()
        for manifest in descriptor["provider_manifests"]:
            symbols.update(s["name"] for s in load_provider(sdk / manifest)["symbols"])
        symbols.update(s.removeprefix("__wrap_") for s in list(symbols)
                       if s.startswith("__wrap_"))
        flags = (self.source / "depends/common/libarchive/flags.txt").read_text()
        self.assertEqual(len(flags.splitlines()), 1)
        self.assertIn("-DENABLE_ACL=OFF", flags)
        for function in ("close_range", "ctime_r", "futimesat", "getgrgid_r",
                         "getgrnam_r", "getline", "getvfsbyname", "lutimes",
                         "readpassphrase", "vfork", "_fseeki64", "_get_timezone",
                         "cygwin_conv_path"):
            self.assertNotIn(function, symbols)
            self.assertIn(f"-DHAVE_{function.upper()}=OFF", flags)
        for function in ("fork", "posix_spawn", "posix_spawnp", "fseeko", "isalnum"):
            self.assertIn(function, symbols)
            self.assertNotIn(f"-DHAVE_{function.upper()}=OFF", flags)

    def test_installed_native_provider_receipt(self):
        state_path = WORK / "build-state/vfs.libarchive.json"
        if not state_path.is_file():
            self.skipTest("Completed native libarchive build required")
        state = json.loads(state_path.read_text())
        binary = (Path(state["install_prefix"]) /
                  "lib/kodi/addons/vfs.libarchive/vfs.libarchive.sprx")
        if not binary.is_file():
            self.skipTest("Completed native libarchive install required")
        old_tempdir = tempfile.tempdir
        try:
            tempfile.tempdir = str(self.scratch)
            receipt = validate_binary(binary, ROOT / "build/ps5-native/sdk")
        finally:
            tempfile.tempdir = old_tempdir
        imports = {s["name"] for s in receipt["native_dependencies"]["imports"]}
        self.assertTrue(all(s["info"] & 15 != 6
                            for s in receipt["native_dependencies"]["imports"]))
        self.assertIn("__mb_cur_max", imports)
        for name in ("___mb_cur_max", "dladdr", "_setjmp", "_longjmp",
                     "sendmmsg", "recvmmsg", "__emutls_v._ThreadRuneLocale"):
            self.assertNotIn(name, imports)
        self.assertTrue(receipt["sign_extract_equal"])
        elf_dir = Path(state["build_dir"]) / "vfs.libarchive-prefix/src/vfs.libarchive-build"
        elf = next(elf_dir.glob("vfs.libarchive.so.*")).read_bytes()
        phoff = struct.unpack_from("<Q", elf, 32)[0]
        phsize, phnum = struct.unpack_from("<HH", elf, 54)
        for index in range(phnum):
            header = struct.unpack_from("<IIQQQQQQ", elf, phoff + index * phsize)
            # The native linker template retains an empty PT_TLS header.
            if header[0] == 7:
                self.assertEqual(header[5:7], (0, 0))


if __name__ == "__main__":
    unittest.main()
