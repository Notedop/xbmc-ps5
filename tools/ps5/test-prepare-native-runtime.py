#!/usr/bin/env python3
"""Verify system import contracts preserve the shared core's DNS ownership."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "native_preparation", Path(__file__).with_name("prepare-native-runtime.py"))
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


@unittest.skipUnless(shutil.which("clang-18") and shutil.which("ld.lld"),
                     "requires clang-18 and ld.lld")
class SystemContractTests(unittest.TestCase):
    def test_system_imports_do_not_compete_with_shared_dns(self):
        with tempfile.TemporaryDirectory(prefix="native-system-contract-") as directory:
            root = Path(directory)
            source = root / "imports.c"
            source.write_text("\n".join(
                f"int {name}(void) {{ return 0; }}"
                for name in sorted(PREPARE.NETWORK_SYMBOLS | {"keep_sdk_import"})))
            library = root / "imports.so"
            subprocess.run(["clang-18", "-shared", "-fPIC", "-nostdlib", str(source),
                            "-o", str(library)], check=True, capture_output=True)
            original = PREPARE.digest(library)
            manifest = PREPARE.system_contract(
                library, "libSystemTest", root / "contract", shutil.which("ld.lld"))
            provider = PREPARE.load_provider(manifest)
            self.assertEqual({symbol["name"] for symbol in provider["symbols"]},
                             {"keep_sdk_import"})
            self.assertEqual(provider["source_import_library_sha256"], original)
            self.assertEqual(PREPARE.digest(library), original)
            self.assertFalse(provider["runtime_implementation_qualified"])
            self.assertEqual(provider["module_version"], 257)
            self.assertEqual(provider["library_version"], 1)


if __name__ == "__main__":
    unittest.main()
