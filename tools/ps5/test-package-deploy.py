#!/usr/bin/env python3
"""Regressions for optional title deployment, without contacting a console."""

from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


class TestPackageDeploy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.capture = self.root / "arguments"
        self.lftp = self.root / "lftp"
        self.lftp.write_text(
            '#!/bin/sh\nprintf "%s\\0" "$@" > "$CAPTURE"\n'
            'exit "${FTP_RESULT:-0}"\n')
        self.lftp.chmod(0o755)
        package = Path(__file__).with_name("package.sh").read_text()
        self.deploy = package.split('if [ -n "${PS5_HOST:-}" ]; then\n', 1)[1]
        self.deploy = ('if [ -n "${PS5_HOST:-}" ]; then\n' +
                       self.deploy.split('\necho\n', 1)[0])
        self.bash = shutil.which("bash")
        self.env = {
            "PATH": str(self.root),
            "CAPTURE": str(self.capture),
            "DIST": str(self.root / "title with spaces"),
            "TITLE_ID": "PPSA99420",
        }

    def run_deploy(self, **env):
        return subprocess.run(
            [self.bash, "-c", "set -euo pipefail\n" + self.deploy],
            env={**self.env, **env}, text=True, capture_output=True)

    def test_unset_or_empty_host_does_not_upload(self):
        for env in ({}, {"PS5_HOST": ""}):
            with self.subTest(env=env):
                result = self.run_deploy(**env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.capture.exists())

    def test_upload_forces_launcher_and_complete_module_tree(self):
        result = self.run_deploy(PS5_HOST="console.invalid")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.capture.read_bytes().decode().rstrip("\0").split("\0")
        self.assertEqual(args[2:], ["-p", "2121", "console.invalid"])
        commands = [shlex.split(cmd) for cmd in args[1].split(";") if cmd.strip()]
        self.assertEqual(commands[0], ["set", "cmd:fail-exit", "yes"])
        self.assertIn("--delete", commands[1])
        self.assertIn("--parallel=8", commands[1])
        self.assertEqual(commands[1][-2:],
                         [self.env["DIST"], "/data/homebrew/PPSA99420"])
        self.assertEqual(commands[2],
                         ["put", "-O", "/data/homebrew/PPSA99420",
                          self.env["DIST"] + "/eboot.bin"])
        self.assertEqual(commands[3],
                         ["mirror", "-R", "--transfer-all", "--no-perms",
                          "--parallel=8", "--verbose",
                          self.env["DIST"] + "/sce_module",
                          "/data/homebrew/PPSA99420/sce_module"])
        self.assertEqual(commands[4], ["quit"])

    def test_port_and_parallelism_overrides(self):
        result = self.run_deploy(PS5_HOST="console.invalid", PS5_FTP_PORT="2021",
                                 PS5_DEPLOY_JOBS="3")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.capture.read_bytes().decode().rstrip("\0").split("\0")
        self.assertEqual(args[2:], ["-p", "2021", "console.invalid"])
        self.assertEqual(args[1].count("--parallel=3"), 2)

    def test_invalid_parallelism_is_rejected_before_upload(self):
        for jobs in ("0", "9", "-1", "1; quit"):
            with self.subTest(jobs=jobs):
                result = self.run_deploy(PS5_HOST="console.invalid",
                                         PS5_DEPLOY_JOBS=jobs)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PS5_DEPLOY_JOBS must be between 1 and 8", result.stderr)
                self.assertFalse(self.capture.exists())

    def test_missing_lftp_is_reported(self):
        self.lftp.unlink()
        result = self.run_deploy(PS5_HOST="console.invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lftp is required for deployment", result.stderr)

    def test_upload_failure_is_not_reported_as_success(self):
        result = self.run_deploy(PS5_HOST="console.invalid", FTP_RESULT="7")
        self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
