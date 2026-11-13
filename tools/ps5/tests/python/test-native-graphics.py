#!/usr/bin/env python3
"""Host-side pinned graphics provenance checks; no graphics compilation."""
# SPDX-License-Identifier: GPL-3.0-or-later

import ps5_test_paths
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import native_graphics as GRAPHICS


class GraphicsProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source, self.installed = root / "source", root / "installed"
        cached = self.source / "build/sdk/ps5-opengl-gl46"
        (cached / "lib").mkdir(parents=True)
        (cached / "include").mkdir()
        (cached / "lib/libps5_opengl_core33.a").write_bytes(b"archive")
        (cached / "include/ps5_opengl_display.h").write_text(
            "#define PS5_OPENGL_NATIVE_HEIGHT 2160\n#define PS5_OPENGL_NATIVE_FPS 60\n")
        shutil.copytree(cached, self.installed)
        runtime = self.source / "build/core33-native-runtime"
        runtime.mkdir()
        (runtime / "libps5_opengl_core33.a").write_bytes(b"archive")
        (self.source / "build/.kodi-display-profile").write_text("2160p60")
        self.revision = GRAPHICS.pinned_revision()
        stamp = [self.revision, *[f"{checksum}  /patches/{name}"
                                 for name, checksum in GRAPHICS.patch_identity().items()]]
        (self.source / "build/.kodi-sdk-build").write_text("\n".join(stamp))
        self.git = patch.object(GRAPHICS.subprocess, "run",
                                side_effect=self.git_result).start()
        self.addCleanup(patch.stopall)

    def git_result(self, command, **kwargs):
        value = self.revision + "\n" if "rev-parse" in command else b"patches"
        return subprocess.CompletedProcess(command, 0, stdout=value)

    def test_existing_matching_install_is_qualified_without_building(self):
        proof = GRAPHICS.qualify_graphics(self.source, self.installed)
        self.assertEqual(proof["revision"], self.revision)
        self.assertEqual(proof["profile"], "2160p60")
        self.assertEqual(proof["installed_files"]["lib/libps5_opengl_core33.a"],
                         GRAPHICS.digest(self.installed / "lib/libps5_opengl_core33.a"))
        self.assertTrue(all(call.args[0][0] == "git" for call in self.git.call_args_list))

    def test_wrong_revision_is_rejected_before_other_work(self):
        self.git.side_effect = lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="old-revision\n")
        with self.assertRaisesRegex(ValueError, "expected " + self.revision):
            GRAPHICS.qualify_graphics(self.source, self.installed)
        self.assertEqual(self.git.call_count, 1)

    def test_installed_archive_mismatch_is_rejected(self):
        (self.installed / "lib/libps5_opengl_core33.a").write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "installed graphics differ"):
            GRAPHICS.qualify_graphics(self.source, self.installed)

    def test_changed_integration_patch_stamp_is_rejected(self):
        (self.source / "build/.kodi-sdk-build").write_text(self.revision + "\n")
        with self.assertRaisesRegex(ValueError, "integration patches changed"):
            GRAPHICS.qualify_graphics(self.source, self.installed)

    def test_profile_mismatch_is_rejected(self):
        (self.source / "build/.kodi-display-profile").write_text("1080p60")
        with self.assertRaisesRegex(ValueError, "display profile differs"):
            GRAPHICS.qualify_graphics(self.source, self.installed)

    def test_prepared_sdk_provenance_binds_delivered_module(self):
        sdk = self.installed
        (sdk / "runtimes").mkdir()
        runtime = sdk / "runtimes/libkodi-gl.sprx"
        runtime.write_bytes(b"module")
        report = {"revision": self.revision, "patches": GRAPHICS.patch_identity(),
                  "runtime_sha256": GRAPHICS.digest(runtime)}
        (sdk / "graphics-provenance.json").write_text(json.dumps(report))
        (sdk / "receipt.json").write_text(json.dumps(
            {"files": {"graphics-provenance.json": GRAPHICS.digest(sdk / "graphics-provenance.json")}}))
        with patch.object(GRAPHICS, "validate_sdk"):
            self.assertEqual(GRAPHICS.validate_prepared_graphics(sdk), report)
            runtime.write_bytes(b"stale module")
            with self.assertRaisesRegex(ValueError, "delivered runtime"):
                GRAPHICS.validate_prepared_graphics(sdk)


if __name__ == "__main__":
    unittest.main()
