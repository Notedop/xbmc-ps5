#!/usr/bin/env python3
"""Check locked RTMP recipes and their native installed binary."""
# SPDX-License-Identifier: GPL-3.0-or-later
import ps5_test_paths
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = ps5_test_paths.ROOT
WORK = ROOT / "build/ps5-addon-repository/work"


class RtmpNativeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lock = WORK / "source-lock.json"
        if not lock.is_file() or not shutil.which("cmake") or not shutil.which("patch"):
            raise unittest.SkipTest("locked RTMP source, CMake and patch required")
        targets = json.loads(lock.read_text())["targets"]
        if "inputstream.rtmp" not in targets:
            raise unittest.SkipTest("RTMP has not been source-locked")
        cls.scratch = ROOT / "build" / ("test-native-rtmp-" + uuid.uuid4().hex)
        cls.source = cls.scratch / "source"
        shutil.copytree(WORK / "sources/inputstream.rtmp" / targets["inputstream.rtmp"]["sha"],
                        cls.source, ignore=shutil.ignore_patterns(".git", ".ps5-applied-patches"))
        cls.addClassCleanup(shutil.rmtree, cls.scratch)
        subprocess.run(
            ["cmake", "-DSOURCE_DIR=" + str(cls.source),
             "-DSERIES_DIR=" + str(ROOT / "tools/ps5/patches/addons/inputstream.rtmp"),
             "-P", str(ROOT / "tools/ps5/scripts/apply-patch-series.cmake")],
            check=True, capture_output=True, text=True)

    def test_matching_private_dependencies_and_native_rtmp_compiler(self):
        cmake = (self.source / "CMakeLists.txt").read_text()
        for filename in ("libssl.a", "libcrypto.a", "libz.a", "librtmp.a"):
            self.assertIn("${ADDON_DEPENDS_PATH}/lib/" + filename, cmake)
        self.assertIn('set(RTMP_INCLUDE_DIR "${ADDON_DEPENDS_PATH}/include"', cmake)
        recipe = (self.source / "depends/common/librtmp/CMakeLists.txt").read_text()
        self.assertIn('$ENV{CONFIG_SITE}', recipe)
        self.assertIn('OUTPUT_VARIABLE CMAKE_C_COMPILER', recipe)
        self.assertIn('"CC=${CMAKE_C_COMPILER}"', recipe)
        self.assertIn("SHARED= SYS=${SYS} CRYPTO=OPENSSL", recipe)
        self.assertIn('BUILD_COMMAND "${RTMP_MAKE}"', recipe)
        self.assertNotIn("$(MAKE)", recipe)
        fetch = (self.source / "depends/common/librtmp/FetchSource.cmake").read_text()
        self.assertIn("138fdb258d9fc26f1843fd1b891180416c9dc575", fetch)
        self.assertIn("RTMPDUMP_ACTUAL_SHA256 STREQUAL RTMPDUMP_SOURCE_SHA256", fetch)

    def test_openssl_configuration_preserves_native_compiler_and_portable_crypto(self):
        recipe = self.scratch / "openssl"
        recipe.mkdir()
        shutil.copy2(self.source / "depends/common/openssl/CMakeLists.txt", recipe)
        configure = recipe / "Configure"
        configure.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$CC\" > native-cc.txt\n"
            "printf '%s\\n' \"$@\" > configure-args.txt\n"
            "printf 'build_sw:\\n\\t@true\\ninstall_dev:\\n\\t@true\\n' > Makefile\n")
        configure.chmod(0o755)
        expected = "python3 /native/native-clang.py /sdk/prospero-clang -fPIC"
        site = self.scratch / "config.site"
        site.write_text("CC='" + expected + "'\n")
        env = dict(os.environ, CONFIG_SITE=str(site))
        build = self.scratch / "openssl-build"
        subprocess.run(
            ["cmake", "-S", str(recipe), "-B", str(build),
             "-DCORE_PLATFORM_NAME=ps5", "-DCORE_SYSTEM_NAME=freebsd",
             "-DOUTPUT_DIR=" + str(self.scratch / "depends")],
            check=True, capture_output=True, text=True, env=env)
        subprocess.run(["cmake", "--build", str(build), "--parallel", "2"],
                       check=True, capture_output=True, text=True, env=env)
        self.assertEqual((recipe / "native-cc.txt").read_text().strip(), expected)
        args = (recipe / "configure-args.txt").read_text().splitlines()
        for option in ("BSD-generic64", "no-shared", "no-asm", "no-dso",
                       "no-async", "-DNO_RECVMMSG"):
            self.assertIn(option, args)

    def test_installed_binary_and_opaque_stdio_contract(self):
        import native_addon
        from native_contract import validate_sdk

        record = json.loads((WORK / "build-record.json").read_text())
        target = record["targets"].get("inputstream.rtmp")
        if not target:
            self.skipTest("successful native RTMP install required")
        prefix = Path(target["prefix"])
        sdk = Path(os.environ.get("PS5_NATIVE_RUNTIME_SDK", ROOT / "build/ps5-native/sdk"))
        contract = validate_sdk(sdk, ROOT / "build/ps5-native/depends/archive-map.json")
        self.assertTrue(contract["stdio"]["opaque_file"])
        self.assertEqual(contract["stdio"]["fpos_size"], 24)
        self.assertEqual(contract["stdio"]["fpos_alignment"], 8)
        binary = prefix / "lib/kodi/addons/inputstream.rtmp/inputstream.rtmp.sprx"
        with patch.object(tempfile, "tempdir", str(self.scratch)):
            receipt = native_addon.validate_binary(binary, sdk)
            native_addon.installed(prefix / "share/kodi/addons/inputstream.rtmp", prefix, sdk)
        self.assertEqual(receipt["compiler_abi"], "ps5-sdk-native-frontend-v1")
        self.assertTrue(receipt["sign_extract_equal"])
        self.assertFalse(any("crypto" in name for name in receipt["needed"]))


if __name__ == "__main__":
    unittest.main()
