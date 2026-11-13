#!/usr/bin/env python3
"""Verify relocated unittest discovery does not execute standalone probes."""

import ps5_test_paths
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("ps5_test_runner", ps5_test_paths.HERE / "tests/run.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class TestRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tests = self.root / "python"
        self.tests.mkdir(parents=True)
        (self.tests / "test-tiny.py").write_text(
            "import unittest\nclass Tiny(unittest.TestCase):\n"
            "    def test_passes(self): self.assertTrue(True)\n")
        (self.tests / "test-probe.py").write_text(
            "raise RuntimeError('standalone probe must not run during discovery')\n")
        self.stderr = io.StringIO()

    def run_tests(self, *selectors):
        original_path = list(sys.path)
        try:
            with patch.object(RUNNER, "__file__", str(self.root / "run.py")), \
                    patch.object(sys, "argv", ["run.py", *selectors]), \
                    contextlib.redirect_stderr(self.stderr):
                return RUNNER.main()
        finally:
            sys.path[:] = original_path

    def test_default_discovery_runs_unittests_and_reports_standalone(self):
        self.assertEqual(self.run_tests(), 0)
        self.assertIn("Standalone scripts (run separately): test-probe.py", self.stderr.getvalue())
        self.assertIn("Ran 1 test", self.stderr.getvalue())

    def test_explicit_selector_uses_dedicated_folder(self):
        self.assertEqual(self.run_tests("test-tiny.py"), 0)

    def test_standalone_selector_reports_direct_invocation(self):
        with self.assertRaises(SystemExit) as failure:
            self.run_tests("test-probe.py")
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("standalone test; run directly", self.stderr.getvalue())

    def test_selectors_cannot_escape_dedicated_folder(self):
        with self.assertRaises(SystemExit) as failure:
            self.run_tests("../test-tiny.py")
        self.assertEqual(failure.exception.code, 2)

    def test_test_sources_are_not_mixed_with_build_tools(self):
        self.assertEqual(sorted(path.name for path in ps5_test_paths.SCRIPTS.glob("test-*")), [])

    def test_tooling_root_contains_only_policy_data_and_directories(self):
        self.assertEqual(
            sorted(path.name for path in ps5_test_paths.HERE.iterdir() if path.is_file()),
            ["addon-catalogue.json", "addon-exclusions.json", "dependency-manifest.json"])

    def test_production_scripts_do_not_depend_on_test_fixtures(self):
        for path in ps5_test_paths.SCRIPTS.rglob("*"):
            if path.suffix not in (".py", ".sh", ".cmake"):
                continue
            with self.subTest(script=path.name):
                self.assertNotIn("tools/ps5/tests", path.read_text())


if __name__ == "__main__":
    unittest.main()
