#!/usr/bin/env python3
"""Verify aggregate SDK assembly with compiled metadata fixtures and real FSELF signing."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
import uuid

import native_runtime_sdk as sdk
import native_shared_link as link
from native_contract import validate_sdk

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assembler = load("sdk_assembler", "prepare-native-addon-sdk.py")
fixtures = load("addon_sdk_fixtures", "test-native-shared-addon.py")


@unittest.skipUnless(fixtures.CC and fixtures.CXX and fixtures.LLD and fixtures.CMAKE and
                     fixtures.SIGNER.is_file() and
                     (fixtures.PAYLOAD / "target/include/c++/v1/__config").is_file(),
                     "PS5 SDK compilers, LLD, CMake and native signer required")
class PrepareSDKTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = fixtures.NativeAddonTest
        cls.fixture.setUpClass()
        cls.root = cls.fixture.root
        cls.core = cls.fixture.sdk / "libkodi-runtime"
        cls.kernel = cls.fixture.sdk / "libkernel"
        cls.extra = cls.fixture.sdk / "libextra"
        elf = cls.fixture.shared_source("libextra", """
            extern int unused_kernel(void);
            int extra_value(void) { return unused_kernel(); }
        """, libraries=[cls.kernel / "lib/libkernel.so"])
        native = elf.with_suffix(".elf")
        native.write_bytes(link.convert(elf.read_bytes(), "libextra",
                                        [link.load_provider(cls.kernel / "manifest.json")]))
        sdk.generate(elf, native, "libextra", cls.extra, fixtures.LLD)
        cls.graphics = cls.root / "graphics-input"
        elf = cls.fixture.shared_source("graphics-fixture", """
            extern int runtime_compute(int);
            extern int extra_value(void);
            void glFixture(void) { runtime_compute(extra_value()); }
        """, libraries=[cls.core / "lib/libkodi-runtime.so", cls.extra / "lib/libextra.so"])
        native = elf.with_suffix(".elf")
        native.write_bytes(link.convert(elf.read_bytes(), "libkodi-gl", [
            link.load_provider(cls.core / "manifest.json"), link.load_provider(cls.extra / "manifest.json")]))
        sdk.generate(elf, native, "libkodi-gl", cls.graphics, fixtures.LLD)
        for source, destination, module in (
                (cls.root / "libkodi-runtime.elf", cls.core, "libkodi-runtime"),
                (native, cls.graphics, "libkodi-gl")):
            (destination / "native").mkdir()
            shutil.copyfile(source, destination / "native" / (module + ".elf"))
        (cls.core / "runtimes").mkdir()
        cls.fixture.run_command(fixtures.SIGNER, "self", "--sign", "--file",
                                cls.core / "native/libkodi-runtime.elf", "--out",
                                cls.core / "runtimes/libkodi-runtime.sprx")
        for system in (cls.kernel, cls.extra):
            (system / "evidence").mkdir()
            (system / "evidence/native.elf").write_bytes(b"FORBIDDEN-FIRMWARE")
            (system / "evidence/names.elf").write_bytes(b"FORBIDDEN-NAME-CATALOGUE")
            (system / "firmware.sprx").write_bytes(b"FORBIDDEN-FIRMWARE")

    @classmethod
    def tearDownClass(cls):
        cls.fixture.tearDownClass()

    def args(self):
        return argparse.Namespace(core=self.core, graphics=self.graphics,
                                  system_sdk=[self.kernel, self.extra],
                                  native_tool=fixtures.SIGNER,
                                  output=self.root / ("assembled-" + uuid.uuid4().hex))

    def test_assembly_relocation_and_normal_addon_build(self):
        args = self.args()
        assembled = assembler.assemble(args)
        relocated = assembled.with_name(assembled.name + "-relocated")
        assembled.rename(relocated)
        descriptor = json.loads((relocated / "manifest.json").read_text())
        self.assertEqual(descriptor["wrap_symbols"], list(fixtures.CORE_WRAP_SYMBOLS))
        self.assertTrue(descriptor["process_lifetime"])
        self.assertFalse(descriptor["console_accepted"])
        for name in descriptor["provider_manifests"]:
            self.assertFalse(Path(name).is_absolute())
            link.load_provider(relocated / name)
        receipt = json.loads((relocated / "receipt.json").read_text())
        self.assertEqual(validate_sdk(relocated)["shared_dns"], "ps5-libc-net-v1")
        self.assertEqual(receipt["dependencies"]["libextra"], ["libkernel.sprx"])
        self.assertEqual(receipt["status"], "metadata-verified-console-unqualified")
        for name, expected in receipt["files"].items():
            self.assertFalse(Path(name).is_absolute())
            self.assertEqual(assembler.digest(relocated / name), expected)
        for line in (relocated / "checksums.sha256").read_text().splitlines():
            expected, name = line.split("  ", 1)
            self.assertEqual(assembler.digest(relocated / name), expected)
        self.assertFalse(list(relocated.rglob("*.elf")))
        self.assertFalse(list(relocated.rglob("evidence")))
        for path in relocated.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"FORBIDDEN-FIRMWARE", path.read_bytes())
        self.assertEqual({p.name for p in (relocated / "runtimes").iterdir()},
                         {"libkodi-runtime.sprx", "libkodi-gl.sprx"})
        depends = self.root / ("relocated-depends-" + uuid.uuid4().hex)
        self.fixture.run_command(fixtures.CMAKE, "-DCORE_SOURCE_DIR=" + str(fixtures.ROOT),
                                "-DADDON_DEPENDS_PATH=" + str(depends),
                                "-DCORE_SYSTEM_NAME=freebsd", "-DCORE_PLATFORM_NAME=ps5",
                                "-DCMAKE_CXX_STANDARD=17", "-DCMAKE_CXX_STANDARD_REQUIRED=ON",
                                "-DPS5_NATIVE_RUNTIME_SDK=" + str(relocated),
                                "-P", fixtures.ROOT / "cmake/scripts/common/PrepareEnv.cmake")
        fixture = self.fixture()
        fixture.sdk, fixture.depends = relocated, depends
        project, _, addon = fixture.project("CXX", graphics=True)
        with (project / "main.cpp").open("a") as stream:
            stream.write("\n#include <cstdio>\n")
        binary = project / "relocated-toolchain-build"
        fixture.run_command(fixtures.CMAKE, "-S", project, "-B", binary,
                            "-DCMAKE_TOOLCHAIN_FILE=" + str(relocated / "toolchain/ps5-native-addon.cmake"),
                            "-DCMAKE_BUILD_TYPE=Release")
        fixture.run_command(fixtures.CMAKE, "--build", binary, "-j2")
        fixture.inspect(binary, addon, graphics=True)
        prefix = self.root / "relocated-install"
        fixture.run_command(fixtures.CMAKE, "--install", binary, "--prefix", prefix)
        self.assertFalse(list(prefix.rglob("*.so*")))
        self.assertEqual(len(list(prefix.rglob("*.sprx"))), 1)

    def test_incomplete_transitive_closure_and_conflicts(self):
        args = self.args()
        args.system_sdk = [self.extra]
        with self.assertRaisesRegex(ValueError, "libextra needs libkernel.sprx"):
            assembler.assemble(args)
        self.assertFalse(args.output.exists())
        diagnostics = next(self.root.glob("." + args.output.name + "-build-*"))
        self.assertEqual(json.loads((diagnostics / "failure.json").read_text())["status"], "failed")
        args = self.args()
        args.system_sdk.append(self.kernel)
        with self.assertRaisesRegex(ValueError, "duplicate/conflicting provider"):
            assembler.assemble(args)
        self.assertFalse(args.output.exists())
        args = self.args()
        manifest = self.graphics / "manifest.json"
        original = manifest.read_bytes()
        try:
            provider = json.loads(original)
            provider["dependencies"]["libraries"][0]["version"] = 2
            manifest.write_text(json.dumps(provider))
            with self.assertRaisesRegex(ValueError, "conflicting dependency identity/version"):
                assembler.assemble(args)
            self.assertFalse(args.output.exists())
        finally:
            manifest.write_bytes(original)

    def test_missing_or_wrong_identity_graphics_cannot_be_published(self):
        for graphics, diagnostic in ((self.root / "missing-graphics", "invalid provider file"),
                                     (self.extra, "graphics provider must be libkodi-gl")):
            with self.subTest(graphics=graphics):
                args = self.args()
                args.graphics = graphics
                with self.assertRaisesRegex(ValueError, diagnostic):
                    assembler.assemble(args)
                self.assertFalse(args.output.exists())
                diagnostics = next(self.root.glob("." + args.output.name + "-build-*"))
                self.assertEqual(json.loads((diagnostics / "failure.json").read_text())["status"],
                                 "failed")

    def test_tampered_import_library(self):
        args = self.args()
        library = self.kernel / "lib/libkernel.so"
        original = library.read_bytes()
        try:
            library.write_bytes(original + b"tampered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                assembler.assemble(args)
            self.assertFalse(args.output.exists())
        finally:
            library.write_bytes(original)

    def test_contract_rejects_missing_dns_and_mismatched_runtime_receipt(self):
        manifest = self.core / "manifest.json"
        original = manifest.read_bytes()
        try:
            data = json.loads(original)
            del data["link_contract"]["network"]
            manifest.write_text(json.dumps(data))
            args = self.args()
            with self.assertRaisesRegex(ValueError, "shared DNS capability"):
                assembler.assemble(args)
            self.assertFalse(args.output.exists())
        finally:
            manifest.write_bytes(original)
        assembled = assembler.assemble(self.args())
        receipt_path = assembled / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["runtime_proofs"]["libkodi-runtime"]["native_elf_sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "runtime receipt mismatch"):
            validate_sdk(assembled)

    def test_contract_rejects_incomplete_sdk_and_changed_tools(self):
        with self.assertRaisesRegex(ValueError, "missing prepared native SDK receipt"):
            validate_sdk(self.root / "no-sdk")
        assembled = assembler.assemble(self.args())
        (assembled / "tools/native-clang.py").write_text("# stale compiler launcher\n")
        with self.assertRaisesRegex(ValueError, "native SDK input changed"):
            validate_sdk(assembled)

    def test_native_import_proof_cannot_omit_dependency_symbols(self):
        args = self.args()
        manifest = self.graphics / "manifest.json"
        original = manifest.read_bytes()
        try:
            provider = json.loads(original)
            provider["dependencies"]["imports"] = [
                symbol for symbol in provider["dependencies"]["imports"]
                if symbol["name"] != "extra_value"]
            manifest.write_text(json.dumps(provider))
            with self.assertRaisesRegex(ValueError, "native import metadata mismatch"):
                assembler.assemble(args)
            self.assertFalse(args.output.exists())
        finally:
            manifest.write_bytes(original)

    def test_all_nine_wrappers_are_mandatory(self):
        args = self.args()
        body = (self.root / "libkodi-runtime.c").read_text()
        body = body[body.index("void __cxa_finalize"):]
        body = "\n".join(line for line in body.splitlines() if "__wrap_reallocf" not in line)
        elf = self.fixture.shared_source("missing-wrapper", body)
        native = elf.with_suffix(".elf")
        native.write_bytes(link.convert(elf.read_bytes(), "libkodi-runtime"))
        args.core = self.root / "missing-core"
        sdk.generate(elf, native, "libkodi-runtime", args.core, fixtures.LLD)
        manifest = args.core / "manifest.json"
        provider = json.loads(manifest.read_text())
        provider["link_contract"] = json.loads(
            (self.core / "manifest.json").read_text())["link_contract"]
        manifest.write_text(json.dumps(provider))
        with self.assertRaisesRegex(ValueError, "__wrap_reallocf"):
            assembler.assemble(args)
        self.assertFalse(args.output.exists())
        self.assertEqual(len(assembler.WRAPS), 9)

    def test_all_core_declared_wraps_are_preserved(self):
        args = self.args()
        manifest = self.core / "manifest.json"
        original = manifest.read_bytes()
        try:
            provider = json.loads(original)
            provider["link_contract"]["wrap_symbols"].append("fixture_extra")
            manifest.write_text(json.dumps(provider))
            output = assembler.assemble(args)
            descriptor = json.loads((output / "manifest.json").read_text())
            self.assertEqual(descriptor["wrap_symbols"],
                             [*fixtures.CORE_WRAP_SYMBOLS, "fixture_extra"])
        finally:
            manifest.write_bytes(original)

    def test_generic_frontend_core_cannot_be_published(self):
        args = self.args()
        manifest = self.core / "manifest.json"
        original = manifest.read_bytes()
        try:
            provider = json.loads(original)
            provider["link_contract"]["compiler_abi"] = "generic-freebsd"
            manifest.write_text(json.dumps(provider))
            with self.assertRaisesRegex(ValueError, "native PS5 SDK frontend ABI"):
                assembler.assemble(args)
            self.assertFalse(args.output.exists())
        finally:
            manifest.write_bytes(original)

    def test_core_contract_cannot_omit_mandatory_heap_wrap(self):
        args = self.args()
        manifest = self.core / "manifest.json"
        original = manifest.read_bytes()
        try:
            provider = json.loads(original)
            provider["link_contract"]["wrap_symbols"].remove("reallocf")
            manifest.write_text(json.dumps(provider))
            with self.assertRaisesRegex(ValueError, "must declare all nine heap wraps"):
                assembler.assemble(args)
            self.assertFalse(args.output.exists())
        finally:
            manifest.write_bytes(original)

    def test_tampered_own_runtime_and_failed_signing(self):
        args = self.args()
        runtime = self.core / "runtimes/libkodi-runtime.sprx"
        original = runtime.read_bytes()
        try:
            runtime.write_bytes(b"not an own runtime")
            with self.assertRaisesRegex(ValueError, "not PS5 FSELF"):
                assembler.assemble(args)
            self.assertFalse(args.output.exists())
        finally:
            runtime.write_bytes(original)
        args = self.args()
        native = self.graphics / "native/libkodi-gl.elf"
        original = native.read_bytes()
        try:
            native.write_bytes(original + b"tampered")
            with self.assertRaisesRegex(ValueError, "native ELF hash mismatch"):
                assembler.assemble(args)
            self.assertFalse(args.output.exists())
        finally:
            native.write_bytes(original)
        args = self.args()
        args.native_tool = Path("/bin/false")
        with self.assertRaisesRegex(ValueError, "native tool failed"):
            assembler.assemble(args)
        self.assertFalse(args.output.exists())
        diagnostics = next(self.root.glob("." + args.output.name + "-build-*"))
        self.assertTrue(list((diagnostics / "diagnostics").glob("*.stderr")))

    def test_never_overwrites_existing_output(self):
        args = self.args()
        args.output.mkdir()
        marker = args.output / "owned"
        marker.write_text("preserve")
        with self.assertRaisesRegex(ValueError, "already exists"):
            assembler.assemble(args)
        self.assertEqual(marker.read_text(), "preserve")
        source = self.root / ("publish-source-" + uuid.uuid4().hex)
        source.mkdir()
        with self.assertRaises(OSError):
            assembler.publish_fresh(source, args.output)
        self.assertTrue(source.is_dir())
        self.assertEqual(marker.read_text(), "preserve")
        empty = self.root / ("empty-destination-" + uuid.uuid4().hex)
        empty.mkdir()
        with self.assertRaises(OSError):
            assembler.publish_fresh(source, empty)
        self.assertTrue(source.is_dir())
        self.assertTrue(empty.is_dir())


if __name__ == "__main__":
    unittest.main()
