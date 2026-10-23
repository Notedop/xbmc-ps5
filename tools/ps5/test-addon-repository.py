#!/usr/bin/env python3
"""Host-only catalogue, deterministic ZIP and publication tests."""

import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from addon_catalogue import DEFAULT_CATALOGUE, load_catalogue, prepare_manifest
import addon_repository as repository


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.catalogue = load_catalogue()

    def test_catalogue_whitelist_and_exclusions(self):
        raw = json.loads(DEFAULT_CATALOGUE.read_text())
        self.assertNotIn("addons", raw)
        self.assertTrue(all(isinstance(t["addons"], dict) for t in raw["targets"].values()))
        self.assertEqual(len(self.catalogue["addons"]), 34)
        self.assertIn("pvr.hts", self.catalogue["addons"])
        self.assertEqual([addon for addon, entry in self.catalogue["addons"].items()
                          if entry["bundled"]], ["inputstream.adaptive"])
        self.assertTrue(all(p["channel"] == "testing" for p in self.catalogue["addons"].values()))
        invalid = json.loads(DEFAULT_CATALOGUE.read_text())
        policy = invalid["targets"]["inputstream.adaptive"]["addons"]["inputstream.adaptive"]
        policy["channel"] = "stable"
        path = self.base / "catalogue.json"
        path.write_text(json.dumps(invalid))
        with self.assertRaisesRegex(ValueError, "needs an accepted"):
            load_catalogue(path)
        policy["channel"] = "testing"
        invalid["targets"]["inputstream.adaptive"]["addons"]["unknown"] = None
        path.write_text(json.dumps(invalid))
        with self.assertRaisesRegex(ValueError, "invalid policy"):
            load_catalogue(path)

    def test_manifest_policy_shared_with_title(self):
        root = ET.fromstring('<addon id="inputstream.adaptive" version="22.3.23">'
                             '<requires><import addon="kodi.binary.instance.inputstream" '
                             'version="2.0.0"/></requires></addon>')
        prepare_manifest(root, self.catalogue, "inputstream.adaptive")
        self.assertEqual(root.get("version"), "22.3.23+ps5.2")
        self.assertEqual(root.find("./extension/platform").text, "ps5")
        self.assertEqual(root.find("./requires/import[@addon='resource.ps5.addon-runtime']")
                         .get("version"), "2.0.0")
        before = ET.tostring(root)
        prepare_manifest(root, self.catalogue, "inputstream.adaptive")
        self.assertEqual(ET.tostring(root), before)

    def folder(self, addon="inputstream.adaptive"):
        folder = self.base / addon
        folder.mkdir(exist_ok=True)
        (folder / "addon.xml").write_text(
            f'<addon id="{addon}" version="22.3.23+ps5.1"/>')
        (folder / "binary.so").write_bytes(b"example ELF placeholder")
        return folder

    def test_deterministic_zip_and_immutable_write(self):
        folder = self.folder()
        first = repository.zip_directory(folder)
        (folder / "binary.so").touch()
        self.assertEqual(first, repository.zip_directory(folder))
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(archive.namelist(),
                             ["inputstream.adaptive/addon.xml", "inputstream.adaptive/binary.so"])
            self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in archive.infolist()))
        out = self.base / "package.zip"
        self.assertTrue(repository.write_changed(out, first, immutable=True))
        stamp = out.stat().st_mtime_ns
        self.assertFalse(repository.write_changed(out, first, immutable=True))
        self.assertEqual(stamp, out.stat().st_mtime_ns)
        with self.assertRaisesRegex(ValueError, "increment catalogue revision"):
            repository.write_changed(out, b"changed", immutable=True)
        (folder / "empty").mkdir()
        (folder / "binary.so").chmod(0o755)
        with zipfile.ZipFile(io.BytesIO(repository.zip_directory(folder))) as archive:
            self.assertIn("inputstream.adaptive/empty/", archive.namelist())
            self.assertEqual(archive.getinfo("inputstream.adaptive/binary.so").external_attr >> 16,
                             0o100755)

    def test_symlink_escape_rejected(self):
        folder = self.folder()
        (self.base / "external").write_text("outside")
        (folder / "escape").symlink_to(self.base / "external")
        with self.assertRaisesRegex(ValueError, "escapes"):
            repository.zip_directory(folder)

    def test_workflow_lock_releases_after_failure(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with repository.workflow_lock(self.base):
                with self.assertRaisesRegex(ValueError, "another repository command"):
                    with repository.workflow_lock(self.base):
                        self.fail("concurrent workflow acquired the same lock")
                raise RuntimeError("fixture")
        with repository.workflow_lock(self.base):
            pass

    def test_package_channels_approval_and_unchanged_outputs(self):
        catalogue = copy.deepcopy(self.catalogue)
        catalogue["targets"] = {"inputstream.adaptive":
                                catalogue["targets"]["inputstream.adaptive"]}
        catalogue["addons"] = {"inputstream.adaptive":
                               catalogue["addons"]["inputstream.adaptive"]}
        fixture_root = self.base / "root"
        for channel, repo in repository.REPOSITORIES.items():
            folder = fixture_root / "addons" / repo
            folder.mkdir(parents=True)
            url = (f"https://raw.githubusercontent.com/{repository.REMOTE}/main/repo/"
                   f"{catalogue['runtime']}/{channel}/")
            (folder / "addon.xml").write_text(
                f'<addon id="{repo}" version="1.0.0">'
                '<extension point="xbmc.addon.repository"><dir>'
                f'<info>{url}addons.xml</info>'
                f'<checksum verify="sha256">{url}addons.xml.sha256</checksum>'
                f'<datadir>{url}</datadir><hashes>sha256</hashes>'
                '</dir></extension></addon>')
        installed = self.folder()
        (installed / "addon.xml").write_text('<addon id="inputstream.adaptive" version="22.3.23"/>')
        title = self.base / "title.elf"
        title.write_bytes(b"original ELF fixture")
        receipt = title.with_name("ps5-addon-runtime.json")
        receipt.write_bytes(repository.json_bytes({
            "schema": 1, "runtime": catalogue["runtime"],
            "title_sha256": repository.digest(title.read_bytes())}))
        output = self.base / "output"
        record = {"targets": {"inputstream.adaptive": {
            "source_revision": "a" * 40, "url": "https://github.com/xbmc/inputstream.adaptive",
            "fingerprint": "b" * 64}}}

        def stage(selected, destination):
            import shutil

            shutil.copytree(installed, destination / "addons" / "inputstream.adaptive")

        with patch.object(repository.staging, "discover",
                          return_value=([("inputstream.adaptive", installed, "binary.so",
                                          installed / "binary.so", [])], [])), \
                patch.object(repository.staging, "stage", side_effect=stage), \
                patch.object(repository, "validate_sdk", return_value={"shared_dns": "ps5-libc-net-v1"}), \
                patch.object(repository, "validate_binary", return_value={"format": "ps5-native-addon"}), \
                patch.object(repository.subprocess, "run") as verify:
            def package():
                return repository.package(fixture_root, catalogue, [], output, title, record,
                                          self.base / "sdk", self.base / "archive-map.json")
            result = package()
            self.assertEqual(verify.call_count, 1)
            self.assertIn("--app", verify.call_args.args[0])
            self.assertIn(str(title), verify.call_args.args[0])
            base = output / "repo" / catalogue["runtime"]
            stable = ET.parse(base / "stable/addons.xml").getroot()
            self.assertEqual([entry.get("id") for entry in stable], ["repository.ps5"])
            testing = ET.parse(base / "testing/addons.xml").getroot()
            self.assertEqual(testing[0].get("version"), "22.3.23+ps5.2")
            instructions = (output / "README.md").read_text()
            self.assertIn("package --title-app <stage>/app", instructions)
            self.assertIn("build --jobs 4 --package --title-app <stage>/app", instructions)
            self.assertIn("--apply-native-migration --commit", instructions)
            self.assertEqual(
                (output / "index.html").read_text(),
                '<!doctype html><html><body>\n'
                '<a href="repository.ps5-1.0.0.zip">repository.ps5-1.0.0.zip</a><br>\n'
                '<a href="repository.ps5.testing-1.0.0.zip">'
                'repository.ps5.testing-1.0.0.zip</a><br>\n'
                '</body></html>\n')
            for repo in repository.REPOSITORIES.values():
                self.assertTrue((output / f"{repo}-1.0.0.zip").is_file())
            snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in output.rglob("*") if p.is_file()}
            package()
            self.assertTrue(all(p.read_bytes() == data and p.stat().st_mtime_ns == timestamp
                                for p, (data, timestamp) in snapshot.items()))
            policy = catalogue["addons"]["inputstream.adaptive"]
            policy.update(channel="stable", acceptance="Explicit console acceptance",
                          accepted_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "approval"):
                package()
            policy["accepted_sha256"] = result["addons"]["inputstream.adaptive"]["sha256"]
            package()
            stable = ET.parse(base / "stable/addons.xml").getroot()
            self.assertEqual(stable[0].get("id"), "inputstream.adaptive")
            (installed / "binary.so").write_bytes(b"changed binary")
            with self.assertRaisesRegex(ValueError, "approval|increment catalogue revision"):
                package()
            title.write_bytes(b"replaced ELF")
            verify.side_effect = subprocess.CalledProcessError(1, ["verify-shared-title.py"])
            with self.assertRaises(subprocess.CalledProcessError):
                package()

    def make_publication(self):
        checkout = self.base / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"),
                           ("commit.gpgsign", "false")):
            repository.git(checkout, "config", key, value)
        repository.git(checkout, "remote", "add", "origin",
                       "https://github.com/Notedop/kodi-ps5-binary-addons.git")
        (checkout / "README").write_text("publication fixture")
        repository.git(checkout, "add", "README")
        repository.git(checkout, "commit", "-qm", "fixture")
        output = self.base / "output"
        output.mkdir()
        relative = "repo/piers-ps5-1/testing/inputstream.adaptive/inputstream.adaptive-1.0.zip"
        archive = output / relative
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"zip")
        (output / "publication.json").write_bytes(repository.json_bytes({
            "schema": 2, "format": "ps5-native-publication", "runtime": "piers-ps5-1",
            "qualified_addons": sorted(self.catalogue["addons"]),
            "catalogue_sha256": repository.digest(repository.json_bytes(self.catalogue)),
            "files": {relative: repository.digest(b"zip")}}))
        return output, checkout, relative

    def test_explicit_publication_preserves_history_and_noop(self):
        output, checkout, relative = self.make_publication()
        old = checkout / "repo/old.zip"
        old.parent.mkdir()
        old.write_bytes(b"old")
        repository.git(checkout, "add", "repo/old.zip")
        repository.git(checkout, "commit", "-qm", "old")
        with patch("addon_repository.git", wraps=repository.git) as git:
            self.assertTrue(repository.publish(output, checkout, commit=True, catalogue=self.catalogue))
            self.assertFalse(any("push" in call.args[1:] for call in git.call_args_list))
        self.assertEqual(old.read_bytes(), b"old")
        self.assertIn("Co-authored-by: Copilot", repository.git(checkout, "log", "-1", "--format=%B"))
        self.assertFalse(repository.publish(output, checkout, commit=True, catalogue=self.catalogue))
        (output / relative).write_bytes(b"tamper")
        with self.assertRaisesRegex(ValueError, "modified"):
            repository.publish(output, checkout, catalogue=self.catalogue)

    def test_publication_dirty_wrong_remote_and_immutable(self):
        output, checkout, relative = self.make_publication()
        (checkout / "local").write_text("user change")
        with self.assertRaisesRegex(ValueError, "dirty"):
            repository.publish(output, checkout, catalogue=self.catalogue)
        (checkout / "local").unlink()
        repository.git(checkout, "remote", "set-url", "origin", "https://github.com/another/repo")
        with self.assertRaisesRegex(ValueError, "unexpected publication remote"):
            repository.publish(output, checkout, catalogue=self.catalogue)
        repository.git(checkout, "remote", "set-url", "origin",
                       "https://github.com/Notedop/kodi-ps5-binary-addons")
        collision = checkout / relative
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"older bytes")
        repository.git(checkout, "add", relative)
        repository.git(checkout, "commit", "-qm", "collision")
        with self.assertRaisesRegex(ValueError, "published version changed"):
            repository.publish(output, checkout, catalogue=self.catalogue)

    def test_native_migration_dry_run_scoped_removal_and_idempotence(self):
        output, checkout, relative = self.make_publication()
        old = checkout / relative.replace("-1.0.zip", "-0.9.zip")
        old.parent.mkdir(parents=True)
        old.write_bytes(b"legacy")
        helper = old.parent / "libhelper.so.1"
        helper.write_bytes(b"legacy helper")
        runtime = checkout / "repo/piers-ps5-1/testing/runtime/libc.prx"
        runtime.parent.mkdir()
        runtime.write_bytes(b"old runtime")
        unrelated = old.parent / "settings.txt"
        unrelated.write_text("preserve")
        repository.git(checkout, "add", ".")
        repository.git(checkout, "commit", "-qm", "legacy fixture")
        before = repository.git(checkout, "rev-parse", "HEAD")
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertTrue(repository.publish(output, checkout, catalogue=self.catalogue, dry_run=True))
        inventory = json.loads(stdout.getvalue())
        change = next(entry for entry in inventory["native_publication_changes"]
                      if entry["path"] == relative)
        self.assertEqual(change["sha256"], repository.digest((output / relative).read_bytes()))
        self.assertIsNone(change["previous_sha256"])
        self.assertIn(old.relative_to(checkout).as_posix(),
                      [entry["path"] for entry in inventory["native_migration_removals"]])
        self.assertTrue(old.exists())
        self.assertFalse((checkout / relative).exists())
        self.assertFalse(repository.git(checkout, "status", "--porcelain"))
        with self.assertRaisesRegex(ValueError, "apply-native-migration"):
            repository.publish(output, checkout, catalogue=self.catalogue)
        self.assertTrue(repository.publish(output, checkout, catalogue=self.catalogue,
                                           apply_migration=True, commit=True))
        self.assertFalse(old.exists())
        self.assertFalse(helper.exists())
        self.assertFalse(runtime.exists())
        self.assertEqual(unrelated.read_text(), "preserve")
        self.assertEqual(repository.git(checkout, "rev-parse", "HEAD^"), before)
        self.assertFalse(repository.publish(output, checkout, catalogue=self.catalogue,
                                            apply_migration=True, commit=True))

    def test_migration_rejects_downgrade_from_existing_index(self):
        output, checkout, _ = self.make_publication()
        index = checkout / "repo/piers-ps5-1/testing/addons.xml"
        index.parent.mkdir(parents=True)
        index.write_text('<addons><addon id="inputstream.adaptive" version="9.0"/></addons>')
        repository.git(checkout, "add", ".")
        repository.git(checkout, "commit", "-qm", "newer legacy index")
        with self.assertRaisesRegex(ValueError, "does not supersede"):
            repository.publish(output, checkout, catalogue=self.catalogue, dry_run=True)

    def test_same_version_different_channel_cannot_replace_immutable_bytes(self):
        output, checkout, relative = self.make_publication()
        collision = checkout / relative.replace("/testing/", "/stable/")
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"same version, different binary")
        repository.git(checkout, "add", ".")
        repository.git(checkout, "commit", "-qm", "cross-channel collision")
        with self.assertRaisesRegex(ValueError, "increment catalogue revision"):
            repository.publish(output, checkout, catalogue=self.catalogue, dry_run=True)

    def test_publication_rejects_symlink_components_and_traversal(self):
        output, checkout, relative = self.make_publication()
        (checkout / "repo").symlink_to(self.base)
        repository.git(checkout, "add", "repo")
        repository.git(checkout, "commit", "-qm", "symlink fixture")
        with self.assertRaisesRegex(ValueError, "symlink"):
            repository.publish(output, checkout, catalogue=self.catalogue)
        (checkout / "repo").unlink()
        repository.git(checkout, "add", "repo")
        repository.git(checkout, "commit", "-qm", "remove symlink fixture")
        inventory = json.loads((output / "publication.json").read_text())
        inventory["files"]["repo/piers-ps5-1/testing/../../outside"] = "0" * 64
        (output / "publication.json").write_bytes(repository.json_bytes(inventory))
        with self.assertRaisesRegex(ValueError, "invalid publication path"):
            repository.publish(output, checkout, catalogue=self.catalogue)

    def test_interrupted_apply_restores_previous_publication(self):
        output, checkout, relative = self.make_publication()
        old = checkout / relative.replace("-1.0.zip", "-0.9.zip")
        old.parent.mkdir(parents=True)
        old.write_bytes(b"legacy")
        repository.git(checkout, "add", ".")
        repository.git(checkout, "commit", "-qm", "legacy")
        original_unlink = Path.unlink

        def fail_removal(path, *args, **kwargs):
            if path == old:
                raise OSError("interrupted apply")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_removal):
            with self.assertRaisesRegex(OSError, "interrupted"):
                repository.publish(output, checkout, catalogue=self.catalogue,
                                   apply_migration=True)
        self.assertEqual(old.read_bytes(), b"legacy")
        self.assertFalse((checkout / relative).exists())
        self.assertFalse(repository.git(checkout, "status", "--porcelain"))

    def test_publication_requires_full_native_qualification(self):
        output, checkout, _ = self.make_publication()
        inventory = json.loads((output / "publication.json").read_text())
        inventory["qualified_addons"].remove("pvr.hts")
        (output / "publication.json").write_bytes(repository.json_bytes(inventory))
        with self.assertRaisesRegex(ValueError, "complete qualified catalogue"):
            repository.publish(output, checkout, catalogue=self.catalogue)

    def test_native_revision_order(self):
        for earlier, later in (("1.0+ps5.1", "1.0+ps5.2"),
                               ("1.0+ps5.9", "1.0+ps5.10"),
                               ("1.0~beta", "1.0"), ("1.0", "1.1"),
                               ("9.0+ps5.100", "10.0+ps5.2")):
            self.assertLess(repository.compare_version(earlier, later), 0)
            self.assertGreater(repository.compare_version(later, earlier), 0)
        self.assertEqual(repository.compare_version("1.01+ps5.2", "1.1+ps5.2"), 0)


if __name__ == "__main__":
    unittest.main()
