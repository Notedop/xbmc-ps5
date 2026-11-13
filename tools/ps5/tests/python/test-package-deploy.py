#!/usr/bin/env python3
"""Regressions for explicit title deployment, without contacting a console."""

import ps5_test_paths
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

HERE = ps5_test_paths.HERE


class TestPackageDeploy(unittest.TestCase):
    def setUp(self):
        (HERE.parents[1] / "build").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=HERE.parents[1] / "build")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.capture = self.root / "arguments"
        self.lftp = self.root / "lftp"
        self.lftp.write_text(
            '#!/bin/sh\nprintf "%s\\0" "$@" > "$CAPTURE"\n'
            'exit "${FTP_RESULT:-0}"\n')
        self.lftp.chmod(0o755)
        # Keep metadata parsing real; only the expensive native verifiers are mocked.
        python = self.root / "python3"
        python.write_text('#!/bin/sh\ncase "$1" in\n'
                          '  */verify-shared-title.py|*/native_contract.py|*/native_graphics.py)\n'
                          '    exit "${VERIFY_RESULT:-0}";;\n'
                          '  *) exec /usr/bin/python3 "$@";;\nesac\n')
        python.chmod(0o755)
        self.stage = self.root / "title with spaces"
        self.dist = self.stage / "app/dist/PPSA99420"
        self.dist.mkdir(parents=True)
        (self.stage / ".ps5-package-stage").touch()
        (self.stage / "app/sce_sys").mkdir()
        (self.stage / "app/sce_sys/param.json").write_text(
            json.dumps({"titleId": "PPSA99420"}))
        (self.dist / "ps5-shared-title-proof.json").write_text("{}")
        self.env = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.defpath,
            "CAPTURE": str(self.capture), "STAGE": str(self.stage),
            "PS5_NATIVE_RUNTIME_SDK": str(self.root / "sdk"),
            "PS5_NATIVE_DEPENDS_MAP": str(self.root / "map"),
        }

    def run_deploy(self, host="console.invalid", **env):
        return subprocess.run(
            [shutil.which("bash"), str(HERE / "deploy.sh"), *([host] if host is not None else [])],
            env={**self.env, **env}, text=True, capture_output=True)

    def test_ambient_host_does_not_authorize_upload(self):
        for host in (None, ""):
            result = self.run_deploy(host, PS5_HOST="ambient.invalid")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.capture.exists())

    def test_package_contains_no_ftp_execution(self):
        package = (HERE / "package.sh").read_text()
        self.assertNotIn("lftp", package)
        self.assertNotIn('if [ -n "${PS5_HOST:-}" ]', package)

    def test_upload_preserves_config_and_forces_launcher_and_modules(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.capture.read_bytes().decode().rstrip("\0").split("\0")
        self.assertEqual(args[2:], ["-p", "2121", "console.invalid"])
        commands = [shlex.split(cmd) for cmd in args[1].split(";") if cmd.strip()]
        self.assertEqual(commands[0], ["set", "cmd:fail-exit", "yes"])
        self.assertIn("--delete", commands[1])
        self.assertIn("^config[.]json$", commands[1])
        self.assertIn("--parallel=8", commands[1])
        self.assertEqual(commands[1][-2:],
                         [str(self.dist), "/data/homebrew/PPSA99420"])
        self.assertEqual(commands[2],
                         ["put", "-O", "/data/homebrew/PPSA99420", str(self.dist / "eboot.bin")])
        self.assertEqual(commands[3],
                         ["mirror", "-R", "--transfer-all", "--no-perms",
                          "--parallel=8", "--verbose", str(self.dist / "sce_module"),
                          "/data/homebrew/PPSA99420/sce_module"])
        self.assertEqual(commands[4], ["quit"])

    def test_port_and_parallelism_overrides(self):
        result = self.run_deploy(PS5_FTP_PORT="2021", PS5_DEPLOY_JOBS="3")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.capture.read_bytes().decode().rstrip("\0").split("\0")
        self.assertEqual(args[2:], ["-p", "2021", "console.invalid"])
        self.assertEqual(args[1].count("--parallel=3"), 2)

    def test_invalid_upload_settings_are_rejected(self):
        for setting, values in (
                ("PS5_DEPLOY_JOBS", ("0", "9", "-1", "1; quit")),
                ("PS5_FTP_PORT", ("0", "65536", "bad"))):
            for value in values:
                with self.subTest(setting=setting, value=value):
                    self.assertNotEqual(self.run_deploy(**{setting: value}).returncode, 0)
                    self.assertFalse(self.capture.exists())

    def test_missing_or_invalid_receipts_block_upload(self):
        result = self.run_deploy(VERIFY_RESULT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())
        (self.dist / "ps5-shared-title-proof.json").unlink()
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing completed-title verification receipt", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_unmanaged_and_symlink_stages_block_upload(self):
        marker = self.stage / ".ps5-package-stage"
        marker.unlink()
        self.assertNotEqual(self.run_deploy().returncode, 0)
        marker.touch()
        link = self.root / "linked-title"
        link.symlink_to(self.stage, target_is_directory=True)
        self.assertNotEqual(self.run_deploy(STAGE=str(link) + "/").returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_missing_lftp_is_reported(self):
        self.lftp.unlink()
        # Hide any host-installed lftp while retaining stage validation utilities.
        (self.root / "dirname").symlink_to(shutil.which("dirname"))
        self.env["PATH"] = str(self.root)
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lftp is required for deployment", result.stderr)

    def test_upload_failure_is_not_reported_as_success(self):
        result = self.run_deploy(FTP_RESULT="7")
        self.assertEqual(result.returncode, 7)
        self.assertNotIn("Uploaded title folder", result.stdout)


if __name__ == "__main__":
    unittest.main()
