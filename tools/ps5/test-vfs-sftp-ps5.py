#!/usr/bin/env python3
"""Validate the SFTP native recipe against the builder's locked source."""
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build/ps5-addon-repository/work-sftp"
SERIES = ROOT / "tools/ps5/patches/addons/vfs.sftp"


class SftpNativeRecipeTest(unittest.TestCase):
    def run_checked(self, args, **kwargs):
        result = subprocess.run(args, capture_output=True, text=True, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @classmethod
    def setUpClass(cls):
        lock = WORK / "source-lock.json"
        if not lock.is_file() or not shutil.which("cmake") or not shutil.which("patch"):
            raise unittest.SkipTest("locked SFTP source, CMake and patch required")
        revision = json.loads(lock.read_text())["targets"]["vfs.sftp"]["sha"]
        cls.scratch = ROOT / "build" / ("test-vfs-sftp-" + uuid.uuid4().hex)
        cls.source = cls.scratch / "source"
        shutil.copytree(WORK / "sources/vfs.sftp" / revision, cls.source,
                        ignore=shutil.ignore_patterns(".git", ".ps5-applied-patches"))
        cls.addClassCleanup(shutil.rmtree, cls.scratch)
        subprocess.run(
            ["cmake", "-DSOURCE_DIR=" + str(cls.source), "-DSERIES_DIR=" + str(SERIES),
             "-P", str(ROOT / "tools/ps5/apply-patch-series.cmake")],
            check=True, capture_output=True, text=True)

    def test_ps5_ssh_home_and_matching_private_archives(self):
        source = (self.source / "src/SFTPSession.cpp").read_text()
        self.assertIn("defined(TARGET_WINDOWS_STORE) || defined(TARGET_PS5)", source)
        self.assertIn('TranslateSpecialProtocol("special://home/.ssh")', source)
        cmake = (self.source / "CMakeLists.txt").read_text()
        self.assertIn("add_definitions(-DTARGET_PS5)", cmake)
        self.assertIn('${ADDON_DEPENDS_PATH}/lib/libssl.a', cmake)
        self.assertIn('${ADDON_DEPENDS_PATH}/lib/libcrypto.a', cmake)
        flags = (self.source / "depends/common/libssh/flags.txt").read_text()
        for option in ("BUILD_SHARED_LIBS", "WITH_EXAMPLES", "WITH_GSSAPI",
                       "WITH_SYMBOL_VERSIONING", "WITH_DEBUG_CALLTRACE"):
            self.assertIn("-D" + option + "=OFF", flags)

    def test_raw_make_uses_native_config_site_compiler(self):
        recipe = self.scratch / "openssl"
        recipe.mkdir()
        shutil.copy2(self.source / "depends/common/openssl/CMakeLists.txt", recipe)
        configure = recipe / "Configure"
        configure.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$CC\" > native-cc.txt\n"
            "printf '%s\\n' \"$@\" > configure-args.txt\n"
            "printf 'build_sw:\\n\\t@true\\ninstall_dev:\\n\\t@true\\n' > Makefile\n")
        configure.chmod(0o755)
        site = self.scratch / "config.site"
        expected = "python3 /native/native-clang.py /sdk/prospero-clang -fPIC"
        site.write_text("CC='" + expected + "'\n")
        env = dict(os.environ, CONFIG_SITE=str(site))
        build = self.scratch / "openssl-build"
        subprocess.run(
            ["cmake", "-S", str(recipe), "-B", str(build),
             "-DCORE_PLATFORM_NAME=ps5", "-DCORE_SYSTEM_NAME=freebsd",
             "-DOUTPUT_DIR=" + str(self.scratch / "depends")],
            env=env, check=True, capture_output=True, text=True)
        subprocess.run(["cmake", "--build", str(build), "--parallel", "4"],
                       env=env, check=True, capture_output=True, text=True)
        self.assertEqual((recipe / "native-cc.txt").read_text().strip(), expected)
        args = (recipe / "configure-args.txt").read_text().splitlines()
        self.assertIn("BSD-generic64", args)
        self.assertIn("no-asm", args)
        self.assertIn("no-shared", args)
        for option in ("no-dso", "no-async", "-DNO_RECVMMSG"):
            self.assertIn(option, args)

    def test_libssh_patch_pins_private_crypto_without_fuzz(self):
        archives = list((WORK / "build/vfs.sftp").glob(
            "*/build/download/libssh-0.12.2.tar.xz"))
        if not archives:
            self.skipTest("builder's verified libssh source archive required")
        source = self.scratch / "libssh"
        source.mkdir()
        with tarfile.open(archives[0]) as archive:
            cmake = archive.extractfile("libssh-0.12.2/CMakeLists.txt").read()
            checks = archive.extractfile("libssh-0.12.2/ConfigureChecks.cmake").read()
            misc = archive.extractfile("libssh-0.12.2/src/misc.c").read()
        (source / "CMakeLists.txt").write_bytes(cmake)
        (source / "ConfigureChecks.cmake").write_bytes(checks)
        (source / "src").mkdir()
        (source / "src/misc.c").write_bytes(misc)
        patch = self.source / "depends/common/libssh/05-ps5-private-openssl.patch"
        self.run_checked(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)],
                         cwd=source)
        cmake = (source / "CMakeLists.txt").read_text()
        self.assertIn('${OUTPUT_DIR}/lib/libcrypto.a', cmake)
        self.assertIn('${OUTPUT_DIR}/lib/libssl.a', cmake)
        patch = self.source / "depends/common/libssh/06-static-feature-checks.patch"
        self.run_checked(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)],
                         cwd=source)
        checks = (source / "ConfigureChecks.cmake").read_text()
        self.assertIn('check_symbol_exists(memset_explicit "string.h"', checks)
        self.assertIn('check_symbol_exists(FIPS_mode "openssl/crypto.h"', checks)
        patch = self.source / "depends/common/libssh/07-ps5-client-platform.patch"
        self.run_checked(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(patch)],
                         cwd=source)
        cmake = (source / "CMakeLists.txt").read_text()
        self.assertIn('set(WITH_SERVER OFF CACHE BOOL "" FORCE)', cmake)
        self.assertIn('set(HAVE_GLOB 0 CACHE INTERNAL "" FORCE)', cmake)

    def test_installed_native_provider_import_and_opaque_abi(self):
        import native_addon
        from native_contract import validate_sdk

        prefixes = [p for p in (WORK / "install/vfs.sftp").glob("*")
                    if (p / "lib/kodi/addons/vfs.sftp/vfs.sftp.sprx").is_file()]
        sdk = Path(os.environ.get("PS5_NATIVE_RUNTIME_SDK", ROOT / "build/ps5-native/sdk"))
        if not prefixes or not sdk.is_dir():
            self.skipTest("installed native SFTP and stable runtime SDK required")
        prefix = prefixes[0]
        contract = validate_sdk(sdk, ROOT / "build/ps5-native/depends/archive-map.json")
        self.assertTrue(contract["stdio"]["opaque_file"])
        with patch.object(tempfile, "tempdir", str(self.scratch)):
            binary = prefix / "lib/kodi/addons/vfs.sftp/vfs.sftp.sprx"
            receipt = native_addon.validate_binary(binary, sdk)
            native_addon.installed(prefix / "share/kodi/addons/vfs.sftp", prefix, sdk)
        self.assertEqual(receipt["compiler_abi"], "ps5-sdk-native-frontend-v1")
        self.assertFalse(any("crypto" in name for name in receipt["needed"]))
        site = next((WORK / "build/vfs.sftp").glob("*/toolchain/config.site"))
        compiler = subprocess.run(
            ["/bin/sh", "-c", '. "$1"; printf "%s" "$CC"', "sh", str(site)],
            check=True, capture_output=True, text=True).stdout
        probe = self.scratch / "opaque-probe.c"
        probe.write_text(
            '#include <stdio.h>\n'
            '_Static_assert(sizeof(fpos_t) == 24, "native fpos size");\n'
            '_Static_assert(_Alignof(fpos_t) == 8, "native fpos alignment");\n')
        self.run_checked(shlex.split(compiler) +
                         ["-c", str(probe), "-o", str(self.scratch / "opaque-probe.o")])
        probe.write_text('#include <stdio.h>\nint size = sizeof(FILE);\n')
        result = subprocess.run(
            shlex.split(compiler) + ["-c", str(probe), "-o", str(self.scratch / "bad.o")],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incomplete type", result.stderr)


if __name__ == "__main__":
    unittest.main()
