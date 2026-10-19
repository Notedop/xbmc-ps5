#!/usr/bin/env python3
"""Focused installed-layout staging and export-policy tests; no title build."""

import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import struct
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
from addon_elf import elf_problems
from addon_catalogue import prepare_manifest


class InstalledAddons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / "build").mkdir(exist_ok=True)
        cls.workspace = tempfile.TemporaryDirectory(prefix="ps5-installed-tests-", dir=ROOT / "build")
        cls.base = Path(cls.workspace.name)
        cls.fixture = cls.base / "fixture.so"
        subprocess.run(["clang++-18", "-shared", "-fPIC", "-nostdlib", "-fuse-ld=lld",
                        str(HERE / "tests/auxiliary-state.cpp"), "-o", str(cls.fixture)], check=True)

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
            'library_freebsd="primary.so.1"/></addon>')
        shutil.copy2(self.fixture, lib / "primary.so.1")
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
        shutil.copy2(self.fixture, repository_lib / "libhelper.so")
        catalogue = self.catalogue("inputstream.adaptive", "screensaver.fake")
        selected, report = staging.discover([prefix], {}, catalogue=catalogue)
        self.assertEqual({entry[0] for entry in selected}, set(catalogue["addons"]))
        self.assertEqual({path for entry in report for path in entry.get("elfs", [])},
                         {str(adaptive_lib / "primary.so.1"), str(repository_lib / "primary.so.1"),
                          str(repository_lib / "libhelper.so")})
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
        self.assertEqual(runtime.get("version"), "1.0.0")
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
        (lib / "primary.so.1").write_text("not ELF")
        with self.assertRaisesRegex(ValueError, "not loadable"):
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
        self.assertEqual(result.stdout.splitlines(), [str(lib / "primary.so.1")])
        self.assertFalse((output / "addons/screensaver.fake").exists())
        self.assertIn("incomplete PS5 runtime export candidate inventory: inputstream.adaptive",
                      result.stderr)
        self.assertIn("rebuild the title before publishing", result.stderr)

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
        marker = staging.ET.parse(output / "addons/resource.ps5.addon-runtime/addon.xml").getroot()
        self.assertEqual(marker.get("version"), "1.0.0")
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
                "./requires/import[@addon='resource.ps5.addon-runtime']").get("version"), "1.0.0")
            self.assertEqual(root.findtext("./extension[@point='xbmc.addon.metadata']/platform"),
                             "ps5")
            self.assertIsNone(root.find("./extension/assets"))

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
        selection = script.split("ADDON_ARGS=(", 1)[1].split(
            'for prefix in "${ADDON_PREFIXES[@]}"; do\n  ADDON_ARGS+=', 1)[0]
        selection = "ADDON_ARGS=(" + selection
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
        from addon_catalogue import load_catalogue
        script = (HERE / "package.sh").read_text()
        verification = script.index('python3 "$ROOT/tools/ps5/verify-addon-exports.py"')
        receipt_start = script.index('python3 - "$ROOT/tools/ps5" "$APP/build/llvm-pie.elf"')
        self.assertGreater(receipt_start, verification)
        payload = script[receipt_start:].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        elf = self.root / "llvm-pie.elf"
        elf.write_bytes(self.fixture.read_bytes())
        failed = subprocess.run([sys.executable, "-", str(HERE), str(elf)],
                                input=payload, text=True, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("regenerate CompileInfo.cpp and rebuild Kodi", failed.stderr)
        self.assertFalse(elf.with_name("ps5-addon-runtime.json").exists())
        with elf.open("ab") as stream:
            stream.write(b"repository.ps5\0https://raw.githubusercontent.com/Notedop/"
                         b"kodi-ps5-binary-addons/main/repo/piers-ps5-1/stable/\0")
        subprocess.run([sys.executable, "-", str(HERE), str(elf)],
                       input=payload, text=True, check=True)
        receipt = json.loads(elf.with_name("ps5-addon-runtime.json").read_text())
        self.assertEqual(receipt, dict(schema=1, runtime=load_catalogue()["runtime"],
                                      title_sha256=hashlib.sha256(elf.read_bytes()).hexdigest()))

    def test_opt_in_probe_staged_platform_is_ps5(self):
        script = (HERE / "package.sh").read_text()
        start = script.index('python3 - "$STAGE/app0/share/kodi/addons/test.binary.ps5/addon.xml"')
        payload = script[start:].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        manifest = self.root / "addon.xml"
        manifest.write_text('<addon id="test.binary.ps5"><extension '
                            'point="xbmc.addon.metadata"><platform>freebsd</platform>'
                            '</extension></addon>')
        subprocess.run([sys.executable, "-", str(manifest)], input=payload, text=True, check=True)
        self.assertEqual(staging.ET.parse(manifest).findtext(
            "./extension[@point='xbmc.addon.metadata']/platform"), "ps5")

    def test_resources_helpers_and_manifest_id(self):
        prefix, data, lib = self.install(addon="screensaver.microcosm.biof",
                                         folder="screensaver.rsxs.microcosm")
        (data / "resources/shaders").mkdir(parents=True)
        (data / "resources/shaders/test.frag").write_text("shader resource")
        shutil.copy2(self.fixture, lib / "libhelper_0.so")
        (lib / "libalias.so").symlink_to("libhelper_0.so")
        selected, report = staging.discover([prefix, prefix], {})
        self.assertEqual(len(selected), 1)
        self.assertEqual(report[0]["status"], "selected")
        output = self.root / "out"
        staging.stage(selected, output)
        addon = output / "addons/screensaver.microcosm.biof"
        self.assertEqual((addon / "resources/shaders/test.frag").read_text(), "shader resource")
        self.assertEqual((addon / "addon.xml").read_bytes(), (data / "addon.xml").read_bytes())
        self.assertEqual((addon / staging.MANIFEST).read_text().splitlines()[1:],
                         ["format 1", "primary primary.so.1", "helper libhelper_0.so"])
        self.assertFalse((addon / "libalias.so").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            staging.stage(selected, output)

    def test_duplicate_id_across_prefixes_or_folders(self):
        for separate_prefix in (False, True):
            with self.subTest(separate_prefix=separate_prefix):
                base = self.root / str(separate_prefix)
                first, _, _ = self.install(prefix=base / "first")
                second, _, _ = self.install(folder="different-folder",
                                             prefix=base / "second" if separate_prefix else first)
                with self.assertRaisesRegex(ValueError, "duplicate add-on ID"):
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
        (data / staging.MANIFEST).write_text("format 1\nhelper arbitrary.so\n")
        with self.assertRaisesRegex(ValueError, "must not provide"):
            staging.discover([prefix], {})
        (data / staging.MANIFEST).unlink()
        (lib / "primary.so.1").unlink()
        with self.assertRaisesRegex(ValueError, "missing binary"):
            staging.discover([prefix], {})

    def test_resource_and_helper_policy(self):
        prefix, data, lib = self.install()
        for filename in ("libbad.so", "unrecognized.so"):
            path = lib / filename
            path.write_text("not ELF")
            with self.assertRaisesRegex(ValueError, "not loadable|unrecognized"):
                staging.discover([prefix], {})
            path.unlink()
        shutil.copy2(self.fixture, data / "hidden.so")
        with self.assertRaisesRegex(ValueError, "ELF must be installed under lib"):
            staging.discover([prefix], {})
        (data / "hidden.so").unlink()
        (data / "escape").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "escapes"):
            staging.discover([prefix], {})
        (data / "escape").unlink()
        (data / "primary.so.1").write_text("collision")
        with self.assertRaisesRegex(ValueError, "collides"):
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

    def test_malformed_elf_and_relocations(self):
        elf = self.root / "bad.so"
        for data in (b"no ELF", self.fixture.read_bytes()[:80]):
            elf.write_bytes(data)
            self.assertTrue(elf_problems(elf))
        data = bytearray(self.fixture.read_bytes())
        shoff = struct.unpack_from("<Q", data, 40)[0]
        shnum = struct.unpack_from("<H", data, 60)[0]
        for index in range(shnum):
            header = struct.unpack_from("<IIQQQQIIQQ", data, shoff + index * 64)
            if header[1] == 4 and header[5]:
                struct.pack_into("<Q", data, header[4] + 8, 37)
                break
        else:
            self.fail("fixture must have relocations")
        elf.write_bytes(data)
        self.assertIn("unsupported relocation type 37", elf_problems(elf))

    def test_emutls_override_cannot_bypass_validation(self):
        prefix, _, lib = self.install(addon="screensaver.rsxs.colorfire")
        subprocess.run(["clang-18", "-x", "c", "-", "-shared", "-fPIC", "-nostdlib",
                        "-fuse-ld=lld", "-o", str(lib / "primary.so.1")],
                       input="extern int __emutls_get_address(void); int x(void) "
                             "{ return __emutls_get_address(); }", text=True, check=True)
        with self.assertRaisesRegex(ValueError, "unsupported runtime import __emutls"):
            staging.discover([prefix], {"screensaver.rsxs.colorfire": "TLS"},
                             include=["screensaver.rsxs.colorfire"])

    def test_cli_export_list_reaches_prepare_exports(self):
        prefix, _, lib = self.install()
        subprocess.run(["clang-18", "-x", "c", "-", "-shared", "-fPIC", "-nostdlib",
                        "-fuse-ld=lld", "-o", str(lib / "primary.so.1")],
                       input="extern void glPolygonMode(unsigned, unsigned); "
                             "void x(void) { glPolygonMode(0, 0); }", text=True, check=True)
        shutil.copy2(self.fixture, lib / "libhelper.so")
        report = self.root / "report.json"
        result = subprocess.run([sys.executable, str(HERE / "stage-installed-addons.py"),
                                 "--prefix", str(prefix), "--report", str(report)],
                                text=True, capture_output=True, check=True)
        paths = result.stdout.splitlines()
        self.assertEqual(paths, [str(lib / "primary.so.1"), str(lib / "libhelper.so")])
        self.assertEqual(json.loads(report.read_text())[0]["elfs"], paths)
        sdk = self.root / "sdk"
        (sdk / "target/lib").mkdir(parents=True)
        (sdk / "bin").mkdir()
        (sdk / "bin/llvm-nm").symlink_to(shutil.which("llvm-nm-18"))
        for name in ("libc++.a", "libc++abi.a", "libunwind.a"):
            subprocess.run(["llvm-ar-18", "rcs", str(sdk / "target/lib" / name)], check=True)
        exports = self.root / "exports"
        subprocess.run([sys.executable, str(HERE / "prepare-addon-exports.py"),
                        "--sdk", str(sdk), "--output", str(exports), *paths], check=True,
                       stdout=subprocess.DEVNULL)
        self.assertIn(".quad glPolygonMode\n", (exports / "addon-imports.S").read_text())
        self.assertIn("glPolygonMode;", (exports / "addon-symbols.map").read_text())
        self.assertIn("glPolygonMode\n", (exports / "addon-imports.txt").read_text())
        (lib / "primary.so.1").write_text("bad")
        failed = subprocess.run([sys.executable, str(HERE / "stage-installed-addons.py"),
                                 "--prefix", str(prefix), "--report", str(report)],
                                text=True, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(report.read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
