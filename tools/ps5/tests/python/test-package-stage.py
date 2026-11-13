#!/usr/bin/env python3
"""Regressions for bounded, reusable title output without building a title."""

import ps5_test_paths
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class PackageStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build = self.root / "build"
        script = (ps5_test_paths.HERE / 'package.sh').read_text()
        self.default = next(line for line in script.splitlines() if line.startswith("STAGE="))
        self.prepare = script.split('echo "==> 1. staging Kodi\'s data into $STAGE"\n', 1)[1]
        self.prepare = self.prepare.split('DESTDIR="$STAGE"', 1)[0]
        self.env = {"PATH": os.defpath, "BUILD": str(self.build)}

    def run_stage(self, stage=None):
        env = dict(self.env)
        if stage is not None:
            env["STAGE"] = str(stage)
        return subprocess.run(
            [shutil.which("bash"), "-c",
             "set -euo pipefail\n" + self.default + "\n" + self.prepare],
            env=env, text=True, capture_output=True)

    def test_install_uses_selected_build_configuration(self):
        script = (ps5_test_paths.HERE / "package.sh").read_text()
        install = "INSTALL_ARGS=()" + script.split("INSTALL_ARGS=()", 1)[1]
        install = install.split('[ -d "$STAGE/app0/share/kodi" ]', 1)[0]
        stage = self.root / "stage"
        stage.mkdir()
        executable = self.root / "cmake"
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
        executable.chmod(0o755)
        capture = self.root / "arguments"
        for config in ("", "Debug"):
            result = subprocess.run(
                ["bash", "-c", "set -euo pipefail\n" + install],
                env={**self.env, "STAGE": str(stage), "CAPTURE": str(capture),
                     "PS5_BUILD_CONFIG": config,
                     "PATH": str(self.root) + os.pathsep + os.defpath},
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text().splitlines(),
                             ["--install", str(self.build), *(["--config", config] if config else [])])

    def test_repeated_builds_share_one_clean_output(self):
        for override in (None, self.root / "custom output"):
            with self.subTest(override=override):
                stage = override or self.build / "title"
                for attempt in range(3):
                    result = self.run_stage(override)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if attempt == 0:
                        siblings = set(stage.parent.iterdir())
                    self.assertEqual(list(stage.iterdir()), [stage / ".ps5-package-stage"])
                    for name in ("app/dist/TEST/obsolete.sprx", "app0/old-addon",
                                 "artifacts/old.zip", "app-build.log"):
                        path = stage / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(str(attempt))
                self.assertEqual(set(stage.parent.iterdir()), siblings)

    def test_existing_unmanaged_directory_is_not_deleted(self):
        for stage in (self.build, self.root / "other"):
            with self.subTest(stage=stage):
                stage.mkdir()
                evidence = stage / "keep"
                evidence.write_text("not package output")
                result = self.run_stage(stage)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to replace an unmanaged stage", result.stderr)
                self.assertEqual(evidence.read_text(), "not package output")

    def test_symlink_stage_is_rejected(self):
        target = self.root / "target"
        target.mkdir()
        marker = target / ".ps5-package-stage"
        marker.touch()
        stage = self.root / "link"
        stage.symlink_to(target, target_is_directory=True)
        for path in (str(stage), str(stage) + "/"):
            with self.subTest(path=path):
                result = self.run_stage(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Stage must not be a symlink", result.stderr)
                self.assertTrue(marker.exists())

    def test_cleanup_does_not_follow_nested_symlinks(self):
        self.assertEqual(self.run_stage().returncode, 0)
        target = self.root / "keep"
        target.write_text("outside output")
        (self.build / "title/link").symlink_to(target)
        self.assertEqual(self.run_stage().returncode, 0)
        self.assertEqual(target.read_text(), "outside output")

    def test_concurrent_build_is_rejected_without_cleaning(self):
        stage = self.build / "title"
        self.assertEqual(self.run_stage().returncode, 0)
        (stage / "evidence").write_text("keep")
        with Path(str(stage) + ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Stage is in use by another package build", result.stderr)
        self.assertEqual((stage / "evidence").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
