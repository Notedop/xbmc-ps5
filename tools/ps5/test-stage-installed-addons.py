#!/usr/bin/env python3
"""Native installed-layout, containment, selection and packaging regressions."""

import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("staging", HERE / "stage-installed-addons.py")
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)
from native_addon import validate_binary
from addon_catalogue import prepare_manifest


class InstalledAddons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / "build").mkdir(exist_ok=True)
        cls.workspace = tempfile.TemporaryDirectory(prefix="ps5-installed-tests-", dir=ROOT / "build")
        cls.base = Path(cls.workspace.name)
        cls.fixture = cls.base / "fixture.sprx"
        cls.fixture.write_bytes(b"\x54\x14\xf5\xee" + b"native test module")

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.base)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def install(self, addon="screensaver.fake", folder=None, prefix=None):
        prefix = prefix or self.root / "prefix"
        folder = folder or addon
        data = prefix / "share/kodi/addons" / folder
        lib = prefix / "lib/kodi/addons" / folder
        data.mkdir(parents=True)
        lib.mkdir(parents=True)
        (data / "addon.xml").write_text(
            f'<addon id="{addon}" version="1.2.3"><extension point="kodi.screensaver" '
            'library_freebsd="primary.sprx"/><extension point="xbmc.addon.metadata">'
            '<platform>ps5</platform></extension></addon>')
        shutil.copy2(self.fixture, lib / "primary.sprx")
        requirement = dict(module="libkodi-runtime", soname="libkodi-runtime.sprx",
                           provider=dict(module="libkodi-runtime", library="libkodi-runtime",
                                         module_version=257, library_version=1))
        (lib / "primary.sprx.json").write_text(json.dumps(dict(
            schema=1, format="ps5-native-addon", module="primary",
            compiler_abi="ps5-sdk-native-frontend-v1", sign_extract_equal=True,
            output_sha256=hashlib.sha256(self.fixture.read_bytes()).hexdigest(),
            provider_requirements=[requirement], needed=["libkodi-runtime.sprx"],
            native_dependencies=dict(needed=["libkodi-runtime.sprx"]))))
        return prefix, data, lib

    def catalogue(self, *addons):
        return dict(schema=1, runtime="piers-ps5-1",
                    targets={addon: dict(url=f"https://github.com/xbmc/{addon}",
                                         ref="Piers", addons=[addon]) for addon in addons},
                    addons={addon: dict(target=addon, bundled=addon == "inputstream.adaptive",
                                        channel="testing", revision=1) for addon in addons})

    def test_catalogue_bundles_only_adaptive_but_inventories_all_candidates(self):
        prefix, data, adaptive_lib = self.install("inputstream.adaptive")
        _, _, repository_lib = self.install(prefix=prefix)
        self.install("screensaver.unlisted", prefix=prefix)
        catalogue = self.catalogue("inputstream.adaptive", "screensaver.fake")
        selected, report = staging.discover([prefix], {}, catalogue=catalogue)
        self.assertEqual({entry[0] for entry in selected}, set(catalogue["addons"]))
        self.assertEqual({path for entry in report for path in entry.get("binaries", [])},
                         {str(adaptive_lib / "primary.sprx"), str(repository_lib / "primary.sprx")})
        output = self.root / "out"
        staging.stage(selected, output, catalogue=catalogue, bundled_only=True)
        self.assertEqual([path.name for path in (output / "addons").iterdir()],
                         ["inputstream.adaptive"])
        expected = prepare_manifest(staging.ET.parse(data / "addon.xml").getroot(),
                                    catalogue, "inputstream.adaptive")
        actual = staging.ET.parse(output / "addons/inputstream.adaptive/addon.xml").getroot()
        self.assertEqual(actual.get("version"), "1.2.3+ps5.1")
        self.assertEqual(actual.findtext("./extension[@point='xbmc.addon.metadata']/platform"), "ps5")
        runtime = actual.find("./requires/import[@addon='resource.ps5.addon-runtime']")
        self.assertEqual(runtime.get("version"), "2.0.0")
        self.assertNotIn("optional", runtime.attrib)
        staging.ET.indent(expected, space="  ")
        self.assertEqual(staging.ET.tostring(actual), staging.ET.tostring(expected))
        self.assertEqual(staging.ET.parse(data / "addon.xml").getroot().get("version"), "1.2.3")

    def test_catalogue_never_bypasses_exclusions_and_validates_repository_only(self):
        prefix, _, lib = self.install()
        catalogue = self.catalogue("screensaver.fake")
        selected, report = staging.discover([prefix], {"screensaver.fake": "blocked"},
                                             include=["screensaver.fake"], catalogue=catalogue)
        self.assertFalse(selected)
        self.assertEqual(report[0]["status"], "excluded")
        (lib / "primary.sprx").write_text("not native")
        with self.assertRaisesRegex(ValueError, "signed SPRX"):
            staging.discover([prefix], {}, catalogue=catalogue)
        with self.assertRaisesRegex(ValueError, "not in catalogue"):
            staging.discover([prefix], {}, include=["screensaver.unknown"], catalogue=catalogue)
        with self.assertRaisesRegex(ValueError, "requires a catalogue"):
            staging.stage([], self.root / "out", bundled_only=True)

    def test_versionless_manifest_remains_legacy_only(self):
        prefix, data, _ = self.install()
        manifest = data / "addon.xml"
        manifest.write_text(manifest.read_text().replace(' version="1.2.3"', ""))
        selected, _ = staging.discover([prefix], {})
        output = self.root / "legacy"
        staging.stage(selected, output)
        self.assertEqual((output / "addons/screensaver.fake/addon.xml").read_bytes(),
                         manifest.read_bytes())
        with self.assertRaisesRegex(ValueError, "invalid upstream version"):
            staging.stage(selected, self.root / "catalogued",
                          catalogue=self.catalogue("screensaver.fake"))

    def test_catalogue_inventory_is_not_pruned_by_legacy_audio_selection(self):
        prefix, data, lib = self.install("audiodecoder.fake")
        catalogue = self.catalogue("audiodecoder.fake")
        selected, _ = staging.discover([prefix], {}, audio_ids=[], catalogue=catalogue)
        self.assertEqual([entry[0] for entry in selected], ["audiodecoder.fake"])
        shutil.rmtree(lib)
        (data / "addon.xml").write_text('<addon id="audiodecoder.fake" version="1.2.3"/>')
        with self.assertRaisesRegex(ValueError, "expected one binary extension"):
            staging.discover([prefix], {}, catalogue=catalogue)

    def test_catalogue_cli_reports_incomplete_inventory_and_exports_repository_only(self):
        prefix, _, lib = self.install()
        catalogue = self.root / "catalogue.json"
        policy = self.catalogue("screensaver.fake", "inputstream.adaptive")
        for addon, entry in policy.pop("addons").items():
            target = entry.pop("target")
            policy["targets"][target]["addons"] = {addon: entry}
        catalogue.write_text(json.dumps(policy))
        report = self.root / "report.json"
        output = self.root / "out"
        result = subprocess.run([sys.executable, str(HERE / "stage-installed-addons.py"),
                                 "--prefix", str(prefix), "--report", str(report),
                                 "--catalogue", str(catalogue), "--bundled-only",
                                 "--destination", str(output)], text=True, capture_output=True,
                                check=True)
        self.assertEqual(result.stdout.splitlines(), [str(lib / "primary.sprx")])
        self.assertFalse((output / "addons/screensaver.fake").exists())
        self.assertIn("incomplete native catalogue inventory: inputstream.adaptive",
                      result.stderr)
        self.assertIn("validate every native package before publication", result.stderr)

    def test_ps5_system_addon_registration_and_repository_contract(self):
        output = self.root / "out"
        (output / "system").mkdir(parents=True)
        manifest = output / "system/addon-manifest.xml"
        manifest.write_text('<addons><addon>existing</addon>'
                            '<addon optional="true">repository.ps5</addon>'
                            '<addon>repository.ps5</addon>'
                            '<addon optional="true">repository.ps5.testing</addon>'
                            '<addon>repository.ps5.testing</addon></addons>')
        staging.stage_system_addons(ROOT / "addons", output)
        staging.stage_system_addons(ROOT / "addons", output)
        entries = staging.ET.parse(manifest).getroot().findall("addon")
        self.assertEqual([entry.text for entry in entries],
                         ["existing", "repository.ps5", "resource.ps5.addon-runtime"])
        self.assertTrue(all(not entry.attrib for entry in entries))
        for addon in ("repository.ps5", "repository.ps5.testing"):
            self.assertEqual((output / "addons" / addon / "addon.xml").read_bytes(),
                             (ROOT / "addons" / addon / "addon.xml").read_bytes())
        self.assertFalse(any(entry.text == "repository.ps5.testing" for entry in entries))
        self.assertFalse((output / "addons/plugin.video.ps5.adaptive.test").exists())
        marker = staging.ET.parse(output / "addons/resource.ps5.addon-runtime/addon.xml").getroot()
        self.assertEqual(marker.get("version"), "2.0.0")
        for addon, channel in (("repository.ps5", "stable"),
                               ("repository.ps5.testing", "testing")):
            root = staging.ET.parse(ROOT / "addons" / addon / "addon.xml").getroot()
            directory = root.find("./extension[@point='xbmc.addon.repository']/dir")
            base = ("https://raw.githubusercontent.com/Notedop/kodi-ps5-binary-addons/"
                    f"main/repo/piers-ps5-1/{channel}/")
            self.assertEqual(directory.findtext("info"), base + "addons.xml")
            self.assertEqual(directory.findtext("checksum"), base + "addons.xml.sha256")
            self.assertEqual(directory.find("checksum").get("verify"), "sha256")
            self.assertEqual(directory.findtext("datadir"), base)
            self.assertEqual(directory.find("datadir").get("zip"), "true")
            self.assertEqual(directory.findtext("hashes"), "sha256")
            self.assertEqual(root.find("./requires/import[@addon='xbmc.addon']").get("version"),
                             "12.0.0")
            self.assertEqual(root.find(
                "./requires/import[@addon='resource.ps5.addon-runtime']").get("version"), "2.0.0")
            self.assertEqual(root.findtext("./extension[@point='xbmc.addon.metadata']/platform"),
                             "ps5")
            self.assertIsNone(root.find("./extension/assets"))

    def test_bunny_diagnostic_requires_native_adaptive_and_is_explicit(self):
        output = self.root / "app0/share/kodi"
        (output / "system").mkdir(parents=True)
        manifest = output / "system/addon-manifest.xml"
        manifest.write_text("<addons/>")
        with self.assertRaisesRegex(ValueError, "requires staged native"):
            staging.stage_system_addons(ROOT / "addons", output, bunny_diagnostic=True)
        prefix, _, _ = self.install("inputstream.adaptive")
        selected, _ = staging.discover([prefix], {})
        staging.stage(selected, output)
        staging.stage_system_addons(ROOT / "addons", output, bunny_diagnostic=True)
        staging.stage_system_addons(ROOT / "addons", output, bunny_diagnostic=True)
        diagnostic = "plugin.video.ps5.adaptive.test"
        self.assertEqual((output / "addons" / diagnostic / "default.py").read_bytes(),
                         (ROOT / "addons" / diagnostic / "default.py").read_bytes())
        self.assertEqual([entry.text for entry in staging.ET.parse(manifest).getroot()].count(
            diagnostic), 1)
        (output / "addons/inputstream.adaptive/primary.sprx").write_bytes(b"legacy ELF")
        with self.assertRaisesRegex(ValueError, "signed SPRX"):
            staging.stage_system_addons(ROOT / "addons", output, bunny_diagnostic=True)

    def test_platform_ps5_precedes_freebsd_without_affecting_other_platforms(self):
        source = (ROOT / "xbmc/addons/addoninfo/AddonInfoBuilder.cpp").read_text()
        vector = source.split('std::vector<std::string> supportedPlatforms = {', 1)[1].split("};", 1)[0]
        for defines, expected, absent in (
                (["TARGET_PS5", "TARGET_FREEBSD"], '"ps5"', '"freebsd"'),
                (["TARGET_FREEBSD"], '"freebsd"', '"ps5"'),
                (["TARGET_LINUX"], '"linux"', '"ps5"')):
            result = subprocess.run(["clang-18", "-E", "-P", "-x", "c",
                                     *(f"-D{define}" for define in defines), "-"],
                                    input=vector, text=True, capture_output=True, check=True)
            self.assertIn('"all"', result.stdout)
            self.assertIn(expected, result.stdout)
            self.assertNotIn(absent, result.stdout)

    def test_package_prefix_record_and_explicit_override_validation(self):
        script = (HERE / "package.sh").read_text()
        selection = script.split("# Native add-on prefix selection.\n", 1)[1].split(
            "# End native add-on prefix selection.", 1)[0]
        installed = self.root / "installed"
        installed.mkdir()
        record = self.root / "build/ps5-addon-repository/work/build-record.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps(dict(prefixes=[str(installed)], targets={})))
        command = ('set -euo pipefail\nunset PS5_ADDON_PREFIXES\nROOT="$1"\n' + selection +
                   '\nprintf "%s\\n" "${ADDON_PREFIXES[@]}"')
        result = subprocess.run(["bash", "-c", command, "prefix-test", str(self.root)],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.splitlines(), [str(installed)])
        record.write_text('{"prefixes":["relative/path"]}')
        failed = subprocess.run(["bash", "-c", command, "prefix-test", str(self.root)],
                                text=True, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("invalid or missing installed prefix", failed.stderr)
        override = command.replace('unset PS5_ADDON_PREFIXES', 'PS5_ADDON_PREFIXES="$2"')
        result = subprocess.run(["bash", "-c", override, "prefix-test", str(self.root),
                                 str(installed)], text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.splitlines(), [str(installed)])

    def test_package_emits_matching_title_runtime_receipt_after_verification(self):
        script = (HERE / "package.sh").read_text()
        verification = script.index('python3 "$ROOT/tools/ps5/native_contract.py"')
        receipt_start = script.index('python3 - "$ROOT/tools/ps5" "$APP/build/llvm-pie.elf"')
        self.assertGreater(receipt_start, verification)
        payload = script[receipt_start:].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        elf = self.root / "llvm-pie.elf"
        elf.write_bytes(self.fixture.read_bytes())
        failed = subprocess.run([sys.executable, "-", str(HERE), str(elf), str(elf), str(elf)],
                                input=payload, text=True, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("regenerate CompileInfo.cpp and rebuild Kodi", failed.stderr)
        self.assertFalse(elf.with_name("ps5-addon-runtime.json").exists())
        with elf.open("ab") as stream:
            stream.write(b"repository.ps5\0https://raw.githubusercontent.com/Notedop/"
                         b"kodi-ps5-binary-addons/main/repo/piers-ps5-1/stable/\0")
        subprocess.run([sys.executable, "-", str(HERE), str(elf), str(elf), str(elf)],
                       input=payload, text=True, check=True)
        receipt = json.loads(elf.with_name("ps5-addon-runtime.json").read_text())
        self.assertEqual(receipt["runtime"]["id"], "kodi-ps5-native-sprx-v1")
        self.assertEqual(receipt["title_sha256"], hashlib.sha256(elf.read_bytes()).hexdigest())

    def test_opt_in_probe_staged_platform_is_ps5(self):
        script = (HERE / "package.sh").read_text()
        self.assertIn('PS5_TEST_BINARY_ADDON="${PS5_TEST_BINARY_ADDON:-0}"', script)
        self.assertIn('cp -a "$TEST_ADDON/screensaver.ps5.sprx-test"', script)
        probe = staging.ET.parse(ROOT / "addons/screensaver.ps5.sprx-test/addon.xml")
        self.assertEqual(probe.findtext(
            "./extension[@point='xbmc.addon.metadata']/platform"), "ps5")

    def test_resources_helpers_and_manifest_id(self):
        prefix, data, lib = self.install(addon="screensaver.microcosm.biof")
        (data / "resources/shaders").mkdir(parents=True)
        (data / "resources/shaders/test.frag").write_text("shader resource")
        selected, report = staging.discover([prefix, prefix], {})
        self.assertEqual(len(selected), 1)
        self.assertEqual(report[0]["status"], "selected")
        output = self.root / "out"
        staging.stage(selected, output)
        addon = output / "addons/screensaver.microcosm.biof"
        self.assertEqual((addon / "resources/shaders/test.frag").read_text(), "shader resource")
        self.assertEqual((addon / "addon.xml").read_bytes(), (data / "addon.xml").read_bytes())
        self.assertEqual((addon / "primary.sprx").read_bytes(),
                         (lib / "primary.sprx").read_bytes())
        self.assertEqual((addon / "primary.sprx.json").read_bytes(),
                         (lib / "primary.sprx.json").read_bytes())
        self.assertFalse((addon / "ps5-auxiliaries.list").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            staging.stage(selected, output)

    def test_duplicate_id_across_prefixes_or_folders(self):
        for separate_prefix in (False, True):
            with self.subTest(separate_prefix=separate_prefix):
                base = self.root / str(separate_prefix)
                first, _, _ = self.install(prefix=base / "first")
                second, _, _ = self.install(folder="different-folder",
                                             prefix=base / "second" if separate_prefix else first)
                with self.assertRaisesRegex(ValueError, "duplicate add-on ID|invalid installed native"):
                    staging.discover([first, second], {})

    def test_invalid_manifests(self):
        prefix, data, lib = self.install()
        for xml in ('<addon', '<addon id="../escape"/>',
                    '<addon id="screensaver.fake"><extension library_freebsd="../bad"/></addon>',
                    '<addon id="screensaver.fake"><extension library_linux="primary.so.1"/></addon>',
                    '<addon id="screensaver.fake"><extension library_freebsd="primary.so.1"/>'
                    '<extension library_freebsd="other.so"/></addon>'):
            with self.subTest(xml=xml):
                (data / "addon.xml").write_text(xml)
                with self.assertRaises((ValueError, staging.ET.ParseError)):
                    staging.discover([prefix], {})
        (data / "addon.xml").unlink()
        with self.assertRaisesRegex(ValueError, "without manifest"):
            staging.discover([prefix], {})

    def test_missing_binary_and_forged_helper_manifest(self):
        prefix, data, lib = self.install()
        (data / "ps5-auxiliaries.list").write_text("format 1\nhelper arbitrary.so\n")
        with self.assertRaisesRegex(ValueError, "legacy auxiliary"):
            staging.discover([prefix], {})
        (data / "ps5-auxiliaries.list").unlink()
        (lib / "primary.sprx").unlink()
        with self.assertRaisesRegex(ValueError, "missing or ambiguous native SPRX"):
            staging.discover([prefix], {})

    def test_resource_and_helper_policy(self):
        prefix, data, lib = self.install()
        for filename in ("libbad.so", "unrecognized.so"):
            path = lib / filename
            path.write_text("not ELF")
            with self.assertRaisesRegex(ValueError, "unmodelled"):
                staging.discover([prefix], {})
            path.unlink()
        shutil.copy2(self.fixture, data / "hidden.so")
        with self.assertRaisesRegex(ValueError, "unmodelled"):
            staging.discover([prefix], {})
        (data / "hidden.so").unlink()
        (data / "escape").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "escapes"):
            staging.discover([prefix], {})
        (data / "escape").unlink()
        shutil.copy2(self.fixture, data / "primary.sprx")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            staging.discover([prefix], {})

    def test_exclusions_overrides_and_missing_prefix(self):
        prefix, _, _ = self.install(addon="audiodecoder.dumb")
        selected, report = staging.discover([prefix, self.root / "missing"], {"audiodecoder.dumb": "policy"})
        self.assertFalse(selected)
        self.assertEqual([r["status"] for r in report], ["excluded", "missing-prefix"])
        selected, _ = staging.discover([prefix], {"audiodecoder.dumb": "policy"},
                                       ["audiodecoder.dumb"], ["audiodecoder.dumb"])
        self.assertEqual(len(selected), 1)
        selected, _ = staging.discover([prefix], {}, [])
        self.assertFalse(selected)
        with self.assertRaisesRegex(ValueError, "not installed"):
            staging.discover([prefix], {}, ["audiodecoder.absent"])

    def test_malformed_native_binary_and_receipt(self):
        prefix, _, lib = self.install()
        binary = lib / "primary.sprx"
        for raw in (b"not native", b"\x7fELF" + b"ordinary ELF"):
            binary.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "signed SPRX"):
                staging.discover([prefix], {})
        shutil.copy2(self.fixture, binary)
        receipt = lib / "primary.sprx.json"
        original = json.loads(receipt.read_text())
        for change in (dict(sign_extract_equal=False), dict(output_sha256="0" * 64),
                       dict(compiler_abi="legacy"), dict(needed=[])):
            receipt.write_text(json.dumps(dict(original, **change)))
            with self.assertRaises(ValueError):
                validate_binary(binary)
        receipt.unlink()
        with self.assertRaisesRegex(ValueError, "missing native add-on receipt"):
            staging.discover([prefix], {})

    def test_emutls_override_cannot_bypass_validation(self):
        prefix, _, lib = self.install(addon="screensaver.rsxs.colorfire")
        (lib / "primary.sprx").write_bytes(b"\x7fELF" + b"legacy TLS module")
        with self.assertRaisesRegex(ValueError, "signed SPRX"):
            staging.discover([prefix], {"screensaver.rsxs.colorfire": "TLS"},
                             include=["screensaver.rsxs.colorfire"])

    def test_cli_native_binary_inventory_and_failure_report(self):
        prefix, _, lib = self.install()
        report = self.root / "report.json"
        result = subprocess.run([sys.executable, str(HERE / "stage-installed-addons.py"),
                                 "--prefix", str(prefix), "--report", str(report)],
                                text=True, capture_output=True, check=True)
        paths = result.stdout.splitlines()
        self.assertEqual(paths, [str(lib / "primary.sprx")])
        self.assertEqual(json.loads(report.read_text())[0]["binaries"], paths)
        (lib / "primary.sprx").write_text("bad")
        failed = subprocess.run([sys.executable, str(HERE / "stage-installed-addons.py"),
                                 "--prefix", str(prefix), "--report", str(report)],
                                text=True, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(report.read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
