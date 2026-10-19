#!/usr/bin/env python3
"""Regressions for native-title archive selection."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "kodi_link_inputs", Path(__file__).with_name("kodi-link-inputs.py"))
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


if __name__ == "__main__":
    unittest.main()
