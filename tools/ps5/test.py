#!/usr/bin/env python3
"""Run PS5 Python regressions, including hyphenated test module filenames."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import importlib.util
from pathlib import Path
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="test-*.py basenames; default: all Python tests")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    paths = [here / name for name in args.tests] if args.tests else sorted(here.glob("test-*.py"))
    suite = unittest.TestSuite()
    for path in paths:
        if path.parent != here or not path.is_file() or not path.match("test-*.py"):
            parser.error(f"not a PS5 Python test: {path}")
        spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    if not suite.countTestCases():
        parser.error("no tests selected")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
