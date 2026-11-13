#!/usr/bin/env python3
"""Run PS5 unittest regressions, including hyphenated module filenames."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import ast
import importlib.util
from pathlib import Path
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="test-*.py basenames; default: all Python tests")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent / "python"
    sys.path.insert(0, str(here))
    paths = [here / name for name in args.tests] if args.tests else sorted(here.glob("test-*.py"))
    suite = unittest.TestSuite()
    standalone = []
    for path in paths:
        if path.parent != here or not path.is_file() or not path.match("test-*.py"):
            parser.error(f"not a PS5 Python test: {path}")
        tree = ast.parse(path.read_text())
        if not any(isinstance(node, ast.ClassDef) and
                   any(ast.unparse(base) == "unittest.TestCase" for base in node.bases)
                   for node in ast.walk(tree)):
            if args.tests:
                parser.error(f"standalone test; run directly with its required arguments: {path}")
            standalone.append(path.name)
            continue
        spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    if standalone:
        print("Standalone scripts (run separately): " + ", ".join(standalone), file=sys.stderr)
    if not suite.countTestCases():
        parser.error("no tests selected")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
