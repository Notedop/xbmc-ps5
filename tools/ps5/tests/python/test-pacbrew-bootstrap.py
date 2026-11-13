#!/usr/bin/env python3
"""Verify pacbrew source fixes are applied safely before building packages."""

import ps5_test_paths
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


HERE = ps5_test_paths.HERE
PATCHES = ("pacbrew-libminizip-source.patch", "pacbrew-lua-source.patch")
CHECKSUM = "bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16"
LUA_CHECKSUM = "7d5ea1b9cb6aa0b59ca3dde1c6adcb57ef83a1ba8e5432c0ecd06bf439b3ad88"


class PacbrewBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tools = self.root / "tools/ps5"
        for relative in ("scripts/prepare/pacbrew.sh", *(f"patches/{name}" for name in PATCHES)):
            destination = tools / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / relative, destination)
        self.script = tools / "scripts/prepare/pacbrew.sh"
        self.repo = self.root / "external/pacbrew-repo"
        self.recipe = self.repo / "libminizip/PKGBUILD"
        self.recipe.parent.mkdir(parents=True)
        self.recipe.write_text(
            "pkgname=ps5-payload-libminizip\npkgver=1.3.2\npkgrel=1\n"
            "pkgdesc='Deflate compression method library'\nurl='http://www.zlib.net/'\n"
            "license=('zlib')\noptions=(!strip libtool staticlibs)\n"
            'source=("https://zlib.net/zlib-$pkgver.tar.gz")\n'
            f"sha256sums=('{CHECKSUM}')\ngroups=('ps5-payload-dev')\n"
            "makedepends=('ps5-payload-sdk' 'ps5-payload-zlib')\n")
        self.lua = self.repo / "lua/PKGBUILD"
        self.lua.parent.mkdir()
        self.lua.write_text(
            "pkgname=ps5-payload-lua\npkgver=5.4.6\npkgrel=1\n"
            "pkgdesc='Lua'\narch=('any')\nurl='https://www.lua.org/'\n"
            "license=('MIT')\noptions=(!strip libtool staticlibs)\n"
            'source=("https://www.lua.org/ftp/lua-$pkgver.tar.gz")\n'
            f"sha256sums=('{LUA_CHECKSUM}')\ngroups=('ps5-payload-dev')\n"
            "makedepends=('ps5-payload-sdk' 'ps5-payload-openlibm')\n")
        (self.repo / "ci-libs.sh").write_text("PKGS=(sdk libminizip lua)\n")
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (
                ("sudo", "exit 0"),
                ("makepkg", '[ "$LC_ALL" = C.UTF-8 ] || exit 87\n'
                 "echo BUILD_REACHED; touch ps5-payload-fixture.pkg.tar.gz")):
            path = self.bin / name
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(0o755)

    def run_bootstrap(self, first="libminizip", last="libminizip"):
        return subprocess.run(
            ["bash", str(self.script), first, last],
            env={**os.environ, "WORK": str(self.repo.parent),
                 "LANG": "C", "LC_ALL": "C",
                 "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]},
            text=True, capture_output=True)

    def test_source_fix_is_repeatable_and_preserves_checksum(self):
        for _ in range(2):
            result = self.run_bootstrap()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("BUILD_REACHED", result.stdout)
            self.assertIn("https://zlib.net/fossils/zlib-", self.recipe.read_text())
            self.assertIn(f"sha256sums=('{CHECKSUM}')", self.recipe.read_text())
            self.assertIn("https://sources.buildroot.net/lua/", self.lua.read_text())
            self.assertIn(f"sha256sums=('{LUA_CHECKSUM}')", self.lua.read_text())

    def test_incompatible_recipe_is_preserved_and_stops_before_building(self):
        self.recipe.write_text("unrecognized recipe\n")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("BUILD_REACHED", result.stdout)
        self.assertEqual(self.recipe.read_text(), "unrecognized recipe\n")

    def test_incompatible_lua_recipe_stops_before_building(self):
        self.lua.write_text("unrecognized Lua recipe\n")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("BUILD_REACHED", result.stdout)
        self.assertEqual(self.lua.read_text(), "unrecognized Lua recipe\n")

    def test_sdk_only_preflight_applies_source_fixes_without_building(self):
        result = self.run_bootstrap("sdk", "sdk")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sdk (skipped)", result.stdout)
        self.assertNotIn("BUILD_REACHED", result.stdout)
        self.assertIn("https://sources.buildroot.net/lua/", self.lua.read_text())


if __name__ == "__main__":
    unittest.main()
