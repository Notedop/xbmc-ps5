#!/usr/bin/env python3
"""Regressions for pinned graphics checkout selection."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "tools/ps5/bootstrap/ps5-opengl.sh"
PIN = ROOT / "tools/ps5/patches/ps5-opengl/PS5-OPENGL-COMMIT"


class TestOpenGLBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / "tools/ps5"
        (self.tools / "bootstrap").mkdir(parents=True)
        self.patches = self.tools / "patches/ps5-opengl"
        self.patches.mkdir(parents=True)
        shutil.copyfile(BOOTSTRAP, self.tools / "bootstrap/ps5-opengl.sh")
        self.patch = self.patches / "0001-fixture.patch"
        self.patch.write_text(
            "diff --git a/src/platform/local.c b/src/platform/local.c\n"
            "--- a/src/platform/local.c\n+++ b/src/platform/local.c\n"
            "@@ -1 +1 @@\n"
            "-preserve local graphics changes\n+patched graphics fixture\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        make = self.bin / "make"
        make.write_text("#!/bin/sh\necho BUILD_REACHED\nexit 7\n")
        make.chmod(0o755)
        self.work = self.root / "external"
        self.source = self.work / "ps5-opengl"
        (self.source / "src/platform").mkdir(parents=True)
        self.sentinel = self.source / "src/platform/local.c"
        self.sentinel.write_text("preserve local graphics changes\n")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.source), *args], text=True).strip()

    def init_source(self, separate_git_dir=False):
        args = ["init", "--quiet"]
        if separate_git_dir:
            args.append(f"--separate-git-dir={self.root / 'git-metadata'}")
        self.git(*args)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "--allow-empty", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def run_bootstrap(self, pin):
        (self.patches / PIN.name).write_text(pin + "\n")
        env = dict(os.environ, WORK=str(self.work),
                   PS5_OPENGL_PREFIX=str(self.root / "sdk"),
                   PS5_SCANOUT_HEIGHT="2160", PS5_SCANOUT_FPS="60",
                   PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        env.pop("PS5_HOST", None)
        return subprocess.run(
            ["bash", str(self.tools / "bootstrap/ps5-opengl.sh")],
            env=env, text=True, capture_output=True, check=False)

    def test_mismatched_checkout_is_preserved(self):
        self.init_source()
        result = self.run_bootstrap("0" * 40)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Existing sources were preserved", result.stderr)
        self.assertNotIn("BUILD_REACHED", result.stdout)
        self.assertEqual(self.sentinel.read_text(), "preserve local graphics changes\n")

    def test_matching_checkout_applies_patches(self):
        result = self.run_bootstrap(self.init_source())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("BUILD_REACHED", result.stdout)
        self.assertEqual(self.sentinel.read_text(), "patched graphics fixture\n")

    def test_git_file_checkout_applies_patches(self):
        revision = self.init_source(separate_git_dir=True)
        self.assertTrue((self.source / ".git").is_file())
        result = self.run_bootstrap(revision)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("BUILD_REACHED", result.stdout)
        self.assertEqual(self.sentinel.read_text(), "patched graphics fixture\n")

    def test_repeated_application_is_idempotent(self):
        revision = self.init_source()
        self.run_bootstrap(revision)
        result = self.run_bootstrap(revision)
        self.assertIn("integration patches already applied", result.stdout)
        self.assertEqual(self.sentinel.read_text(), "patched graphics fixture\n")

    def test_incompatible_patch_set_leaves_sources_untouched(self):
        revision = self.init_source()
        (self.patches / "0002-incompatible.patch").write_text(
            "diff --git a/src/platform/missing.c b/src/platform/missing.c\n"
            "--- a/src/platform/missing.c\n+++ b/src/platform/missing.c\n"
            "@@ -1 +1 @@\n-before\n+after\n")
        result = self.run_bootstrap(revision)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("BUILD_REACHED", result.stdout)
        self.assertEqual(self.sentinel.read_text(), "preserve local graphics changes\n")

    def test_missing_patches_fail_before_building(self):
        revision = self.init_source()
        self.patch.unlink()
        result = self.run_bootstrap(revision)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no Kodi OpenGL integration patches", result.stderr)
        self.assertNotIn("BUILD_REACHED", result.stdout)

    def test_invalid_pin_fails_before_patching(self):
        result = self.run_bootstrap("not-a-commit")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("expected a full commit hash", result.stderr)
        self.assertNotIn("BUILD_REACHED", result.stdout)

    def test_dependency_manifest_matches_pin_and_fork(self):
        manifest = json.loads((ROOT / "tools/ps5/dependency-manifest.json").read_text())
        dependency = next(item for item in manifest["dependencies"]
                          if item["name"] == "ps5-opengl")
        self.assertEqual(dependency["pin"], PIN.read_text().strip())
        self.assertEqual(dependency["source"], "https://github.com/Notedop/ps5-opengl.git")
        self.assertIn(dependency["source"], BOOTSTRAP.read_text())


if __name__ == "__main__":
    unittest.main()
