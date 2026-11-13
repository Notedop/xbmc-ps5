#!/usr/bin/env python3
"""Check normal title packaging's native installed-prefix selection."""

import ps5_test_paths
import json
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = (ps5_test_paths.HERE / 'package.sh')


class PackageInputsTests(unittest.TestCase):
    def test_videoout_uses_qualified_stub_without_legacy_backup(self):
        script = SCRIPT.read_text().split('VO_EXT=', 1)[1]
        script = "VO_EXT=" + script.split('grep -q -- "--wrap=malloc"', 1)[0]
        with tempfile.TemporaryDirectory(prefix="native-videoout-inputs-") as directory:
            root = Path(directory)
            sdk = root / "sdk"
            (sdk / "bin").mkdir(parents=True)
            (sdk / "target/lib").mkdir(parents=True)
            nm = sdk / "bin/llvm-nm"
            nm.write_text('#!/bin/sh\ncat "$3"\n')
            nm.chmod(0o755)
            template = root / "template"
            template.mkdir()
            stub = sdk / "target/lib/libSceVideoOut.so"
            original = "000 T sceVideoOutOpen\n"
            for exports, success in (
                    (original + "001 T sceVideoOutVrrUnpegFromFixedRate\n", True),
                    ("001 T sceVideoOutVrrUnpegFromFixedRate\n", False)):
                with self.subTest(success=success):
                    stub.write_text(exports)
                    destination = template / stub.name
                    destination.write_text(original)
                    result = subprocess.run(
                        ["bash", "-c", "set -euo pipefail\n" + script],
                        env={**os.environ, "PS5_PAYLOAD_SDK": str(sdk),
                             "STUBDIR": str(template)},
                        text=True, capture_output=True)
                    self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                    self.assertEqual(destination.read_text(), exports if success else original)
                    self.assertFalse(stub.with_suffix(".so.sdk").exists())

    def test_title_app_is_required_only_when_packaging(self):
        script = SCRIPT.with_name("addon-repository.py")
        spec = importlib.util.spec_from_file_location("addon_repository_cli", script)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(cli, "execute") as execute, \
                patch.object(cli, "workflow_lock", side_effect=lambda path: contextlib.nullcontext()):
            with patch.object(sys, "argv", [str(script), "build"]):
                cli.main()
            self.assertEqual(execute.call_count, 1)
            for arguments in (["package"], ["build", "--package"]):
                with self.subTest(arguments=arguments), \
                        patch.object(sys, "argv", [str(script), *arguments]), \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as failure:
                    cli.main()
                self.assertEqual(failure.exception.code, 2)

    def test_explicit_empty_prefix_override_disables_record_discovery(self):
        selection = SCRIPT.read_text().split('PREFIX_LIST=', 1)[1]
        selection = selection.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory(prefix="native-package-inputs-") as directory:
            root = Path(directory)
            prefix = root / "install"
            prefix.mkdir()
            record = root / "build/ps5-addon-repository/work/build-record.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"prefixes": [str(prefix)]}))
            for value, defined, expected in (("", "", str(prefix) + "\n"),
                                             ("", "x", ""),
                                             (str(prefix), "x", str(prefix) + "\n")):
                with self.subTest(value=value, defined=defined):
                    result = subprocess.run(
                        [sys.executable, "-c", selection, str(root), value, defined],
                        check=True, capture_output=True, text=True)
                    self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
