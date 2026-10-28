#!/usr/bin/env python3
"""Regressions for authoritative title metadata without building or deploying."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


class TestPackageMetadata(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "title/sce_sys/param.json"
        self.source.parent.mkdir(parents=True)
        repo = Path(__file__).resolve().parents[2]
        self.source.write_bytes((repo / "title/sce_sys/param.json").read_bytes())
        self.metadata = json.loads(self.source.read_text())
        self.app = self.root / "app"
        self.dist = self.app / "dist" / self.metadata["titleId"]
        (self.app / "sce_sys").mkdir(parents=True)
        (self.dist / "sce_sys").mkdir(parents=True)
        self.package = Path(__file__).with_name("package.sh").read_text()
        self.header = self.package.split("for setting in TITLE_ID", 1)[1]
        self.header = ("for setting in TITLE_ID" +
                       self.header.split("\nCONFIGURED_HBROOT=", 1)[0])
        self.copy = 'cp "$ROOT/title/sce_sys/param.json" "$APP/sce_sys/param.json"'
        self.assertIn(self.copy, self.package)
        self.verify = self.package.split('cmp -s "$ROOT/title/sce_sys/param.json"', 1)[1]
        self.verify = ('cmp -s "$ROOT/title/sce_sys/param.json"' +
                       self.verify.split('\n\nKODI_IMAGE=', 1)[0])
        self.env = {"PATH": os.defpath, "ROOT": str(self.root),
                    "APP": str(self.app), "DIST": str(self.dist)}

    def run_shell(self, script, **env):
        return subprocess.run(
            [shutil.which("bash"), "-c", "set -euo pipefail\n" + script],
            env={**self.env, **env}, text=True, capture_output=True)

    def test_title_and_version_are_read_from_source(self):
        self.metadata.update(titleId="ABCD12345", contentVersion="23.004.005")
        self.source.write_text(json.dumps(self.metadata))
        result = self.run_shell(self.header)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("title ABCD12345, content version 23.004.005", result.stdout)

    def test_metadata_is_copied_byte_for_byte(self):
        result = self.run_shell(self.header + "\n" + self.copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.source.read_bytes(),
                         (self.app / "sce_sys/param.json").read_bytes())

    def test_missing_source_is_reported(self):
        self.source.unlink()
        result = self.run_shell(self.header)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PS5 title metadata:", result.stderr)

    def test_invalid_metadata_is_reported(self):
        for contents in ("{", "[]",
                         json.dumps({**self.metadata, "titleId": "../bad"}),
                         json.dumps({**self.metadata, "contentVersion": "22.1"}),
                         json.dumps({**self.metadata, "contentVersion": 22})):
            with self.subTest(contents=contents):
                self.source.write_text(contents)
                result = self.run_shell(self.header)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PS5 title metadata:", result.stderr)

    def test_legacy_overrides_are_rejected(self):
        for name in ("TITLE_ID", "KODI_CATEGORY", "KODI_HDR_TITLE"):
            with self.subTest(name=name):
                result = self.run_shell(self.header, **{name: ""})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{name} is no longer supported", result.stderr)

    def test_packaged_metadata_must_match_source(self):
        staged = self.dist / "sce_sys/param.json"
        staged.write_bytes(self.source.read_bytes())
        self.assertEqual(self.run_shell(self.verify).returncode, 0)
        staged.write_text("{}")
        result = self.run_shell(self.verify)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("packaged param.json differs", result.stderr)

    def test_manifest_uses_packaged_metadata(self):
        manifest_script = self.package.split(
            '"$DIST/sce_sys/param.json" "$ZIP_PATH" <<\'PY\'\n', 1)[1].split("\nPY", 1)[0]
        for category, attribute, expected_category, hdr in (
                (0, 0x62000000, "game", True),
                (65536, 0, "media", False)):
            with self.subTest(category=category):
                metadata = {**self.metadata, "contentVersion": "23.004.005",
                            "applicationCategoryType": category, "attribute": attribute}
                staged = self.dist / "sce_sys/param.json"
                staged.write_text(json.dumps(metadata))
                result = subprocess.run(
                    [sys.executable, "-c", manifest_script, str(self.root),
                     str(self.root / "build"), str(self.root), str(staged),
                     str(self.root / "title.zip")],
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(
                    (self.root / (metadata["titleId"] + "-BUILD-MANIFEST.json")).read_text())
                self.assertEqual(manifest["title_id"], metadata["titleId"])
                self.assertEqual(manifest["content_version"], "23.004.005")
                self.assertEqual(manifest["category"], expected_category)
                self.assertEqual(manifest["hdr_capable"], hdr)

    def test_zip_excludes_only_root_build_evidence_with_both_backends(self):
        archive_script = self.package.split('if [ "$PACKAGE_ZIP" = "1" ]; then\n', 1)[1]
        archive_script = archive_script.split("  # Build manifest:", 1)[0]
        evidence = ("ps5-addon-runtime.json", "ps5-kodi-app-proof.json",
                    "ps5-loader-companion-proof.json", "ps5-shared-runtime.json",
                    "ps5-shared-title-proof.json", "sprx-addon-build.txt")
        runtime = {
            "eboot.bin": b"launcher",
            "sce_sys/param.json": self.source.read_bytes(),
            "sce_module/libc.prx": b"loader companion",
            "sce_module/libkodi-runtime.sprx": b"core",
            "sce_module/libkodi-gl.sprx": b"graphics",
            "sce_module/libkodi-app.sprx": b"application",
            "share/kodi/addons/test/config.json": b'{"runtime": true}',
            "share/kodi/ps5-addon-runtime.json": b"nested file is not excluded",
            "assets/file with spaces": b"asset",
            ".hidden": b"hidden runtime file",
        }
        for name, contents in {**runtime, **dict.fromkeys(evidence, b"build evidence")}.items():
            path = self.dist / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("mkdir", "dirname", "rm", "cmake"):
            (tools / name).symlink_to(shutil.which(name))
        for backend, path in (("zip", os.defpath), ("cmake", str(tools))):
            with self.subTest(backend=backend):
                result = self.run_shell(archive_script, PATH=path,
                                        STAGE=str(self.root),
                                        TITLE_ID=self.metadata["titleId"])
                self.assertEqual(result.returncode, 0, result.stderr)
                archive_path = self.root / "artifacts" / (self.metadata["titleId"] + ".zip")
                with zipfile.ZipFile(archive_path) as archive:
                    files = {name for name in archive.namelist() if not name.endswith("/")}
                    prefix = self.metadata["titleId"] + "/"
                    self.assertEqual(files, {prefix + name for name in runtime})
                    for name, contents in runtime.items():
                        self.assertEqual(archive.read(prefix + name), contents)
                for name in evidence:
                    self.assertEqual((self.dist / name).read_bytes(), b"build evidence")


if __name__ == "__main__":
    unittest.main()
