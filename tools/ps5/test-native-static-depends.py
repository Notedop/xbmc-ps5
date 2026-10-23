#!/usr/bin/env python3
"""Check validated dependency mapping and normal CMake target integration."""
# SPDX-License-Identifier: GPL-3.0-or-later

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from native_static_depends import digest, load_verified_map

ROOT = Path(__file__).resolve().parents[2]


class NativeStaticDependsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="native-static-depends-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.header = self.root / "stdio.h"
        self.header.write_text("opaque stdio fixture")
        self.original = self.root / "old.a"
        self.original.write_bytes(b"original")
        self.replacement = self.root / "component/new.a"
        self.replacement.parent.mkdir()
        self.replacement.write_bytes(b"replacement")
        self.mapping = self.root / "map.json"
        self.mapping.write_text(json.dumps({str(self.original): str(self.replacement)}))
        self.receipt = {
            "identity": {"overlay": digest(self.header),
                         "original_archives": {str(self.original): digest(self.original)}},
            "outputs": [{"original": str(self.original), "rebuilt": str(self.replacement),
                         "sha256": digest(self.replacement)}],
            "validation": {"opaque_FILE_rejected": True, "fpos_size": 24, "fpos_align": 8,
                           "all_objects_fresh_with_forced_opaque_overlay": True},
        }
        self.save_receipt()

    def save_receipt(self):
        (self.replacement.parent / "receipt.json").write_text(json.dumps(self.receipt))

    def test_validates_mapping(self):
        self.assertEqual(load_verified_map(self.mapping, self.header),
                         {str(self.original): str(self.replacement)})

    def test_rejects_changed_original(self):
        self.original.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "original dependency changed"):
            load_verified_map(self.mapping, self.header)

    def test_rejects_changed_replacement(self):
        self.replacement.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "replacement changed"):
            load_verified_map(self.mapping, self.header)

    def test_rejects_mismatched_sdk_abi(self):
        self.header.write_text("different")
        with self.assertRaisesRegex(ValueError, "stdio ABI"):
            load_verified_map(self.mapping, self.header)

    def test_rejects_incomplete_compile_evidence(self):
        self.receipt["validation"]["all_objects_fresh_with_forced_opaque_overlay"] = False
        self.save_receipt()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            load_verified_map(self.mapping, self.header)

    @unittest.skipUnless(shutil.which("cmake"), "CMake required")
    def test_cmake_updates_imports_and_interfaces_without_reordering(self):
        source = self.root / "source"
        (source / "tools/ps5").mkdir(parents=True)
        shutil.copyfile(ROOT / "tools/ps5/native_static_depends.py",
                        source / "tools/ps5/native_static_depends.py")
        sdk = self.root / "sdk"
        (sdk / "include").mkdir(parents=True)
        shutil.copyfile(self.header, sdk / "include/stdio.h")
        (source / "CMakeLists.txt").write_text(f'''
cmake_minimum_required(VERSION 3.19)
project(NativeArchives NONE)
set(PS5_NATIVE_PYTHON "{sys.executable}")
set(PS5_NATIVE_DEPENDS_MAP "{self.mapping}")
set(PS5_NATIVE_RUNTIME_SDK "{sdk}")
include("{ROOT / 'cmake/scripts/ps5/NativeStaticDepends.cmake'}")
add_library(provider STATIC IMPORTED)
set_target_properties(provider PROPERTIES IMPORTED_LOCATION_RELEASE "{self.original}")
add_library(consumer INTERFACE)
target_link_libraries(consumer INTERFACE first "{self.original}" -lold "$<LINK_ONLY:-lold>" last)
function(check)
  get_target_property(location provider IMPORTED_LOCATION_RELEASE)
  get_target_property(libraries consumer INTERFACE_LINK_LIBRARIES)
  if(NOT location STREQUAL "{self.replacement}" OR
     NOT libraries STREQUAL "first;{self.replacement};{self.replacement};$<LINK_ONLY:{self.replacement}>;last")
    message(FATAL_ERROR "mapping failed: ${{location}} / ${{libraries}}")
  endif()
endfunction()
cmake_language(DEFER CALL check)
''')
        result = subprocess.run(["cmake", "-S", str(source), "-B", str(self.root / "build")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
