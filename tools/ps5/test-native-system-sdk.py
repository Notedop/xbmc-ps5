#!/usr/bin/env python3
"""Offline tests: python3 tools/ps5/test-native-system-sdk.py.

Requires clang and ld.lld (NATIVE_SDK_CC/NATIVE_SDK_LLD override discovery).
Set NATIVE_SYSTEM_SDK_REAL_OUTPUT to a generated libkernel SDK directory to also
link and convert an actual sceKernelGetProcessTime consumer. Nothing is executed
or signed. All scratch files are UUID-owned directories beneath the project.
NATIVE_SYSTEM_SDK_REAL_LIBC_OUTPUT adds an actual C function/data consumer.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import os
from pathlib import Path
import runpy
import shutil
import struct
import subprocess
import unittest
from unittest.mock import patch
import uuid

import native_runtime_sdk as runtime
import native_shared_link as link
import native_system_sdk as sdk
from native_c_link import Elf, SYM, nid


ROOT = Path(__file__).resolve().parents[2]
LLD = os.environ.get("NATIVE_SDK_LLD") or shutil.which("ld.lld")
CC = os.environ.get("NATIVE_SDK_CC") or shutil.which("clang")
FIXTURES = runpy.run_path(str(Path(__file__).with_name("test-native-system-provider.py")))
MODULE = "libkernel"
LIFECYCLE = """
__attribute__((visibility("hidden"))) int module_start(void) { return 0; }
__attribute__((visibility("hidden"))) int module_stop(void) { return 0; }
"""


@unittest.skipUnless(CC and LLD, "clang and ld.lld required")
class SystemSdkTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / (".native-system-sdk-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.native = self.root / "provider.native.elf"
        compiled = self.build("provider", """
            __attribute__((visibility("protected"))) int compute(void) { return 17; }
            void *__stderrp;
            __attribute__((weak)) char weak_data[19];
        """)
        raw = bytearray(link.convert(compiled.read_bytes(), MODULE))
        self.native.write_bytes(self.export_id(raw, 8))
        self.names = self.root / "names.so"
        # Deliberately false types, sizes, values, visibility, and provider identity.
        names_obj = self.root / "names.o"
        names_obj.write_bytes(runtime.import_object({
            name: (0, 0x12, 0, 1, 0, 1)
            for name in ("compute", "__stderrp", "weak_data", "not_in_native")}))
        self.run_command([LLD, "-shared", "--soname=untrusted.so", "-o", str(self.names),
                          str(names_obj)])
        self.output = self.root / "sdk"

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_command(self, command):
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def build(self, name, code, libraries=()):
        source = self.root / (name + ".c")
        obj, elf = source.with_suffix(".o"), source.with_suffix(".so")
        source.write_text(LIFECYCLE + code)
        self.run_command([CC, "--target=x86_64-unknown-freebsd", "-fPIC", "-ffreestanding",
                          "-fno-stack-protector", "-O2", "-c", str(source), "-o", str(obj)])
        self.run_command([LLD, "-shared", "--hash-style=sysv", "--eh-frame-hdr", "-z", "now",
                          "-z", "max-page-size=0x4000", "--no-undefined",
                          "-T", str(ROOT / "tools/ps5/native-shared.ld"),
                          "-init", "module_start", "-fini", "module_stop",
                          "-o", str(elf), str(obj), *map(str, libraries)])
        return elf

    def export_id(self, raw, identity):
        image = runtime.NativeImage(raw)
        dynamic = next(header for header in image.phdrs if header[0] == 2)
        for offset in range(dynamic[2], dynamic[2] + dynamic[5], 16):
            tag, value = runtime.DYNAMIC.unpack_from(raw, offset)
            if tag == runtime.EXPORT_LIB:
                runtime.DYNAMIC.pack_into(raw, offset, tag, value | identity << 48)
        return bytes(raw).replace(b"#A#A\0", ("#" + runtime.ID_ALPHABET[identity] + "#A\0").encode())

    def fixture(self, exports=None, **kwargs):
        defaults = {"module": (0, MODULE, 257), "libraries": ((8, MODULE, 1),),
                    "exports": exports if exports is not None else [
                        (FIXTURES["symbol"]("compute", size=5), 8)]}
        defaults.update(kwargs)
        return FIXTURES["native"](**defaults)

    def generate(self, **kwargs):
        return sdk.generate(self.native, self.names, MODULE, self.output, LLD, **kwargs)

    def assert_rejected(self, raw, message):
        self.native.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, message):
            self.generate()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".*.staging-*")))

    def test_compiled_function_data_exact_native_evidence_and_source_immutable(self):
        native_raw, names_raw = self.native.read_bytes(), self.names.read_bytes()
        manifest = self.generate(required=("compute", "__stderrp"))
        loaded = link.load_provider(self.output / "manifest.json")
        self.assertEqual(loaded, manifest)
        self.assertEqual(manifest["provider"]["module_id"], 0)
        self.assertEqual(manifest["provider"]["library_id"], 8)
        actual = {s["name"]: s for s in manifest["symbols"]}
        exports = {s["nid"]: s for s in
                   sdk.inspect_provider(native_raw, MODULE)["exports"]}
        self.assertEqual(set(actual), {"compute", "__stderrp", "weak_data"})
        for name, row in actual.items():
            native = exports[nid(name)]
            for key in ("info", "other", "value", "size", "native_name", "section"):
                self.assertEqual(row[key], native[key])
            self.assertEqual(row["canonical_name"], nid(name) + "#libkernel#libkernel")
            self.assertTrue(row["native_name"].endswith("#I#A"))
        self.assertEqual(actual["__stderrp"]["type"], "object")
        self.assertEqual(actual["__stderrp"]["size"], 8)
        self.assertEqual(actual["weak_data"]["info"], 0x21)
        self.assertEqual(actual["weak_data"]["size"], 19)
        self.assertEqual(actual["compute"]["other"], 3)
        self.assertEqual([s["name"] for s in manifest["excluded_names"]], ["not_in_native"])
        self.assertEqual(self.native.read_bytes(), native_raw)
        self.assertEqual(self.names.read_bytes(), names_raw)
        self.assertEqual((self.output / "evidence/native.elf").read_bytes(), native_raw)
        self.assertEqual((self.output / "evidence/names.elf").read_bytes(), names_raw)
        self.assertEqual(manifest["provider"]["native_elf_sha256"], sdk.sha256(native_raw))
        self.assertEqual(manifest["name_catalogue"]["sha256"], sdk.sha256(names_raw))
        self.assertTrue(manifest["linker_only"])
        self.assertFalse(manifest["runtime_loadable"])
        self.assertFalse(manifest["runtime_implementation_qualified"])
        self.assertEqual(manifest["provider"]["inspection"]["inventory"]["implementation_status"],
                         "unrecognized")
        consumer = self.build("consumer", """
            extern int compute(void);
            extern void *__stderrp;
            void **data_pointer = &__stderrp;
            int run(void) { return compute() + (__stderrp != 0); }
        """, [self.output / "lib/libkernel.so"])
        converted = link.convert(consumer.read_bytes(), "test.consumer", [loaded])
        self.assertEqual(converted[7:9], b"\x09\x02")

    def test_abi3_is_preserved_without_modifying_native_image(self):
        raw = bytearray(self.native.read_bytes())
        raw[8] = 3
        self.native.write_bytes(raw)
        manifest = self.generate()
        self.assertEqual(manifest["provider"]["inspection"]["inventory"]["elf_abi_version"], 3)
        self.assertEqual((self.output / "evidence/native.elf").read_bytes(), raw)
        self.assertEqual(self.native.read_bytes(), raw)
        with self.assertRaisesRegex(ValueError, "PS5 x86-64 shared module"):
            runtime.NativeImage(raw)

    def test_arbitrary_source_ids_are_evidence_not_consumer_import_ids(self):
        self.native.write_bytes(self.fixture([
            (FIXTURES["symbol"]("compute", size=5), 65)],
            module=(7, MODULE, 257), libraries=((65, MODULE, 1),)))
        manifest = self.generate()
        self.assertEqual(manifest["provider"]["module_id"], 7)
        self.assertEqual(manifest["provider"]["library_id"], 65)
        self.assertEqual(manifest["symbols"][0]["native_name"], nid("compute") + "#BB#H")
        self.assertEqual(manifest["symbols"][0]["canonical_name"],
                         nid("compute") + "#libkernel#libkernel")
        provider = link.load_provider(self.output / "manifest.json")
        consumer = self.build("consumer", """
            extern int compute(void);
            int run(void) { return compute(); }
        """, [self.output / "lib/libkernel.so"])
        raw = link.convert(consumer.read_bytes(), "test.consumer", [provider])
        image = runtime.NativeImage(raw)
        strings = image.memory(image.tag(5), image.tag(10))
        imports = [runtime.string_at(strings, row[0]) for row in
                   SYM.iter_unpack(image.memory(image.tag(6), image.tag(runtime.SYMTABSZ)))
                   if row[0] and not row[3]]
        self.assertEqual(imports, [nid("compute") + "#B#B"])

    def test_module_library_version_profile_rejections(self):
        for kwargs in ({"module": (0, "different", 257)},
                       {"module": (0, MODULE, 258)},
                       {"libraries": ((8, "different", 1),), "exports": []},
                       {"libraries": ((8, MODULE, 2),)},
                       {"libraries": ((8, MODULE, 1), (9, MODULE, 2))}):
            with self.subTest(kwargs=kwargs):
                self.assert_rejected(self.fixture(**kwargs), "identity/version")

    def test_mixed_case_native_identity_preserved(self):
        library = "libSceLibcInternal"
        self.native.write_bytes(self.fixture(module=(0, library, 257),
                                             libraries=((2, library, 1),),
                                             exports=[(FIXTURES["symbol"]("compute"), 2)]))
        manifest = sdk.generate(self.native, self.names, library, self.output, LLD)
        self.assertEqual(manifest["module"], library)
        self.assertEqual(manifest["library"], library)
        self.assertEqual(manifest["soname"], library + ".sprx")
        self.assertEqual(manifest["provider"]["library_id"], 2)
        self.assertEqual(manifest["symbols"][0]["canonical_name"],
                         nid("compute") + "#" + library + "#" + library)
        provider = link.load_provider(self.output / "manifest.json")
        self.assertEqual(provider, manifest)
        consumer = self.build("mixed_case_consumer", """
            extern int compute(void);
            int probe(void) { return compute(); }
        """, [self.output / "lib" / (library + ".so")])
        raw = link.convert(consumer.read_bytes(), "test.mixed.consumer", [provider])
        image = runtime.NativeImage(raw)
        strings = image.memory(image.tag(5), image.tag(10))
        self.assertEqual(runtime.string_at(strings, image.tag(1)), library + ".sprx")
        self.assertEqual(runtime.string_at(strings, image.tag(runtime.NEEDED_MODULE) & 0xFFFFFFFF),
                         library)
        self.assertEqual(runtime.string_at(strings, image.tag(runtime.IMPORT_LIB) & 0xFFFFFFFF),
                         library)

    def test_duplicate_and_invalid_native_metadata_rejected(self):
        row = (FIXTURES["symbol"]("compute"), 8)
        for raw, reason in (
            (self.fixture([row, row]), "ambiguous/duplicate"),
            (self.fixture(encoded="malformed"), "malformed encoded"),
            (self.fixture(encoded=nid("compute") + "#J#A"), "undeclared"),
            (self.fixture(module=(2, MODULE, 257),
                          encoded=nid("compute") + "#I#A"), "undeclared"),
            (self.fixture(export_section=0xFFFF), "section"),
            (b"not an elf", "expected extracted"),
            (self.fixture()[:-8], "truncated"),
        ):
            with self.subTest(reason=reason):
                self.assert_rejected(raw, reason)

    def test_untypable_named_or_invisible_exports_rejected(self):
        for info, other in ((0x10, 0), (0x16, 0), (0x1A, 0), (0x32, 0),
                            (0x12, 2), (0x12, 4)):
            with self.subTest(info=info, other=other):
                self.assert_rejected(self.fixture([
                    (FIXTURES["symbol"]("compute", info=info, other=other), 8)]),
                    "unsupported native export")

    def test_unnamed_native_tls_is_evidence_not_an_import_api(self):
        self.native.write_bytes(self.fixture([
            (FIXTURES["symbol"]("compute"), 8),
            (FIXTURES["symbol"]("unnamed_tls", info=0x16), 8)]))
        manifest = self.generate()
        self.assertEqual([row["name"] for row in manifest["symbols"]], ["compute"])
        excluded = manifest["unnamed_native_exports"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["type_id"], 6)
        self.assertIn("unsupported", excluded[0]["exclusion_reason"])

    def test_names_outside_selected_library_are_exclusions_not_apis(self):
        self.native.write_bytes(self.fixture([
            (FIXTURES["symbol"]("compute"), 8),
            (FIXTURES["symbol"]("__stderrp", info=0x11, size=8), 7),
            (FIXTURES["symbol"]("unlisted_name"), 8)],
            libraries=((8, MODULE, 1), (7, "libScePosix", 1))))
        manifest = self.generate()
        self.assertEqual([row["name"] for row in manifest["symbols"]], ["compute"])
        excluded = {row["name"]: row for row in manifest["excluded_names"]}
        self.assertEqual(excluded["__stderrp"]["candidates"][0]["library"]["id"], 7)
        self.assertEqual(len(manifest["unnamed_native_exports"]), 1)

    def test_duplicate_names_and_nid_collisions_rejected(self):
        raw = bytearray(self.names.read_bytes())
        image = Elf(raw)
        table = image.sections[".dynsym"][4]
        SYM.pack_into(raw, table + 2 * SYM.size, *SYM.unpack_from(raw, table + SYM.size))
        with self.assertRaisesRegex(ValueError, "duplicate catalogue name"):
            sdk.name_catalogue(raw)
        with patch.object(sdk, "nid", return_value=nid("collision")):
            with self.assertRaisesRegex(ValueError, "colliding catalogue NID"):
                sdk.name_catalogue(self.names.read_bytes())

    def test_malformed_names_catalogue_rejected(self):
        for raw in (b"bad", self.names.read_bytes()[:-1], self.native.read_bytes()):
            with self.subTest(size=len(raw)):
                with self.assertRaises((ValueError, struct.error)):
                    sdk.name_catalogue(raw)

    def test_required_symbol_missing_fails_before_publication(self):
        for required in (("not_in_native",), ("not_in_catalogue",), ("compute", "missing")):
            with self.subTest(required=required):
                with self.assertRaisesRegex(ValueError, "required symbols missing"):
                    self.generate(required=required)
                self.assertFalse(self.output.exists())

    def test_known_bootstrap_placeholder_rejected(self):
        code = bytes.fromhex("66 0f ef c0 31 c0 c3")
        self.assert_rejected(self.fixture([
            (FIXTURES["symbol"](name), 8)
            for name in ("malloc", "memcpy", "abort", "__cxa_finalize")],
            addresses=[0x10200] * 4, code=code), "bootstrap-placeholder")

    def test_no_overwrite_existing_directory_file_or_symlink(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "fresh output"):
            self.generate()
        self.output.rmdir()
        self.output.write_bytes(b"untouched")
        with self.assertRaisesRegex(ValueError, "fresh output"):
            self.generate()
        self.assertEqual(self.output.read_bytes(), b"untouched")
        self.output.unlink()
        self.output.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "fresh output"):
            self.generate()
        self.assertTrue(self.output.is_symlink())

    def test_link_failure_leaves_no_partial_sdk(self):
        with patch.object(sdk.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, "", "intentional failure")):
            with self.assertRaisesRegex(ValueError, "LLD.*intentional failure"):
                self.generate()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".*.staging-*")))

    def test_validation_and_publication_failures_leave_no_partial_sdk(self):
        for target in ("validate_stub", "publish_directory"):
            with self.subTest(target=target), patch.object(
                    sdk, target, side_effect=ValueError("intentional failure")):
                with self.assertRaisesRegex((ValueError, OSError), "intentional failure"):
                    self.generate()
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob(".*.staging-*")))

    def test_concurrent_empty_output_is_not_overwritten(self):
        publish = sdk.publish_directory

        def concurrent_output(staging, output):
            output.mkdir()
            publish(staging, output)

        with patch.object(sdk, "publish_directory", side_effect=concurrent_output):
            with self.assertRaises(OSError):
                self.generate()
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(list(self.root.glob(".*.staging-*")))

    @unittest.skipUnless(os.environ.get("NATIVE_SYSTEM_SDK_REAL_OUTPUT"),
                         "set NATIVE_SYSTEM_SDK_REAL_OUTPUT for actual libkernel smoke test")
    def test_actual_libkernel_link_and_offline_convert(self):
        output = Path(os.environ["NATIVE_SYSTEM_SDK_REAL_OUTPUT"])
        provider = link.load_provider(output / "manifest.json")
        self.assertEqual(provider["module"], MODULE)
        self.assertEqual(provider["provider"]["library_id"], 8)
        self.assertEqual(provider["provider"]["inspection"]["inventory"]["elf_abi_version"], 3)
        source = self.build("kernel_probe", """
            extern unsigned long sceKernelGetProcessTime(void);
            unsigned long probe(void) { return sceKernelGetProcessTime(); }
        """, [output / "lib/libkernel.so"])
        raw = link.convert(source.read_bytes(), "test.kernel.probe", [provider])
        image = runtime.NativeImage(raw)
        strings = image.memory(image.tag(5), image.tag(10))
        imports = [runtime.string_at(strings, row[0]) for row in
                   SYM.iter_unpack(image.memory(image.tag(6), image.tag(runtime.SYMTABSZ)))
                   if row[0] and not row[3]]
        self.assertEqual(imports, [nid("sceKernelGetProcessTime") + "#B#B"])
        self.assertEqual(runtime.string_at(strings, image.tag(1)), "libkernel.sprx")

    @unittest.skipUnless(os.environ.get("NATIVE_SYSTEM_SDK_REAL_LIBC_OUTPUT"),
                         "set NATIVE_SYSTEM_SDK_REAL_LIBC_OUTPUT for actual libc smoke test")
    def test_actual_libc_function_data_link_and_offline_convert(self):
        output = Path(os.environ["NATIVE_SYSTEM_SDK_REAL_LIBC_OUTPUT"])
        provider = link.load_provider(output / "manifest.json")
        self.assertEqual(provider["module"], "libSceLibcInternal")
        self.assertEqual(provider["provider"]["library_id"], 2)
        runtime.validate_stub((output / "lib/libSceLibcInternal.so").read_bytes(), {
            row["name"]: (0, row["info"], row["other"], 1, 0, row["size"])
            for row in provider["symbols"]}, provider["module"])
        symbols = {row["name"]: row for row in provider["symbols"]}
        self.assertEqual(symbols["_Stderr"]["type_id"], 1)
        self.assertEqual(symbols["_Stderr"]["size"], 456)
        self.assertEqual(symbols["__stderrp"]["type_id"], 1)
        self.assertEqual(symbols["__stderrp"]["size"], 8)
        self.assertTrue(all(row["type_id"] in (1, 2) for row in provider["symbols"]))
        source = self.build("libc_probe", """
            extern void *malloc(unsigned long);
            extern void free(void *);
            extern unsigned char _Stderr[456];
            extern void *__stderrp;
            unsigned char *file_pointer = _Stderr;
            void **stderr_pointer = &__stderrp;
            int probe(void) { void *p = malloc(32); free(p); return _Stderr[0]; }
        """, [output / "lib/libSceLibcInternal.so"])
        raw = link.convert(source.read_bytes(), "test.libc.probe", [provider])
        image = runtime.NativeImage(raw)
        strings = image.memory(image.tag(5), image.tag(10))
        imports = [runtime.string_at(strings, row[0]) for row in
                   SYM.iter_unpack(image.memory(image.tag(6), image.tag(runtime.SYMTABSZ)))
                   if row[0] and not row[3]]
        self.assertEqual(set(imports), {nid(name) + "#B#B"
                                      for name in ("malloc", "free", "_Stderr", "__stderrp")})
        self.assertEqual(runtime.string_at(strings, image.tag(1)), "libSceLibcInternal.sprx")


if __name__ == "__main__":
    unittest.main()
