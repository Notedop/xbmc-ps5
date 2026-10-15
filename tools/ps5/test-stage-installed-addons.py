#!/usr/bin/env python3
"""Focused installed-layout staging and export-policy tests; no title build."""

import importlib.util
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
            f'<addon id="{addon}"><extension point="kodi.screensaver" '
            'library_freebsd="primary.so.1"/></addon>')
        shutil.copy2(self.fixture, lib / "primary.so.1")
        return prefix, data, lib

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
