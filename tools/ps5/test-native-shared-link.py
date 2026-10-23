#!/usr/bin/env python3
"""Offline shared-module link/import tests; no Kodi executable or console."""

import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

import native_runtime_sdk as sdk
import native_shared_link as link
from native_c_link import Elf, PAGE, PHDR, RELA, SYM, nid

ROOT = Path(__file__).resolve().parents[2]
LLD = os.environ.get("NATIVE_SDK_LLD") or shutil.which("ld.lld")
CC = os.environ.get("NATIVE_SDK_CC") or shutil.which("clang-18")
LIFECYCLE = """
__attribute__((visibility("hidden"))) int module_start(void) { return 0; }
__attribute__((visibility("hidden"))) int module_stop(void) { return 0; }
"""


@unittest.skipUnless(LLD and CC, "clang-18 and ld.lld required")
class SharedLinkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-shared-link-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build(self, name, code, libraries=(), flags=(), objects=(), lifecycle=True):
        source = self.root / (name + ".c")
        obj = source.with_suffix(".o")
        elf = source.with_suffix(".so")
        source.write_text((LIFECYCLE if lifecycle else "") + code)
        subprocess.run([CC, "--target=x86_64-unknown-freebsd", "-fPIC", "-ffreestanding",
                        "-fno-stack-protector", "-O2", *flags, "-c", str(source), "-o", str(obj)],
                       check=True, capture_output=True)
        result = subprocess.run([LLD, "-shared", "--hash-style=sysv", "--eh-frame-hdr",
                        "-z", "now", "-z", "max-page-size=0x4000", "--no-undefined",
                        "-T", str(ROOT / "tools/ps5/native-shared.ld"),
                        "-init", "module_start", "-fini", "module_stop",
                        "-o", str(elf), str(obj), *map(str, objects), *map(str, libraries)],
                       capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return elf

    def provider(self):
        elf = self.build("provider", """
            int value = 42;
            int *value_pointer = &value;
            char large_storage[100000];
            int compute(int x) { return value + x; }
            __attribute__((weak)) int weak_compute(void) { return value; }
        """)
        native = self.root / "provider.native.elf"
        native.write_bytes(link.convert(elf.read_bytes(), "test.provider"))
        sdk.generate(elf, native, "test.provider", self.root / "sdk", LLD)
        manifest = self.root / "sdk/manifest.json"
        return elf, native, link.load_provider(manifest)

    def test_selects_only_linked_dependencies_without_weakening_conversion(self):
        _, _, provider = self.provider()
        consumer = self.build("consumer", """
            extern int compute(int);
            int run(void) { return compute(1); }
        """, libraries=[self.root / "sdk/lib/test.provider.so"])
        source = consumer.read_bytes()
        available = [{"soname": "unused.sprx"}, provider]
        selected = link.select_providers(source, available)
        self.assertEqual(selected, [provider])
        link.convert(source, "test.consumer", selected)
        with self.assertRaisesRegex(ValueError, "exactly cover DT_NEEDED"):
            link.convert(source, "test.consumer", available)
        with self.assertRaisesRegex(ValueError, "undeclared source DT_NEEDED"):
            link.select_providers(source, [])
        with self.assertRaisesRegex(ValueError, "duplicate provider SONAME"):
            link.select_providers(source, [provider, provider])

    def test_provider_preserves_code_data_exports_and_large_bss(self):
        elf, native, provider = self.provider()
        linked = Elf(elf.read_bytes())
        image = sdk.NativeImage(native.read_bytes())
        symbols = sdk.prove_provider(elf.read_bytes(), native.read_bytes(), "test.provider")
        self.assertEqual(set(symbols), {s["name"] for s in provider["symbols"]})
        self.assertEqual(linked.section(".text"), image.memory(0, len(linked.section(".text"))))
        self.assertEqual(symbols["large_storage"][5], 100000)
        self.assertEqual(symbols["value"][1] & 15, 1)
        self.assertEqual(symbols["weak_compute"][1] >> 4, 2)
        self.assertGreater(next(p[6] for p in image.phdrs if p[0] == 1 and
                                p[3] == linked.sections[".dynamic"][3]), PAGE)
        self.assertEqual(image.tag(2), 0)
        self.assertEqual(image.tag(12), dict(linked.symbols(".symtab"))["module_start"][4])
        self.assertEqual(image.tag(13), dict(linked.symbols(".symtab"))["module_stop"][4])
        self.assertEqual([p[0] for p in image.phdrs][-4:],
                         [2, 0x6FFFFF00, 0x6FFFFF01, 4])

    def test_dynamic_table_uses_native_metadata_mapping(self):
        elf, native, _ = self.provider()
        raw = native.read_bytes()
        image = sdk.NativeImage(raw)
        metadata = next(p for p in image.phdrs if p[0] == 1 and p[1] == 0)
        dynamic = next(p for p in image.phdrs if p[0] == 2)
        self.assertLess(image.phdrs.index(metadata), image.phdrs.index(dynamic))
        loader_offset = metadata[2] + dynamic[3] - metadata[3]
        self.assertEqual(loader_offset, dynamic[2])
        self.assertGreaterEqual(loader_offset, metadata[2])
        self.assertLessEqual(loader_offset + dynamic[5], metadata[2] + metadata[5])
        self.assertEqual(struct.unpack_from("<QQ", raw, loader_offset),
                         sdk.DYNAMIC.unpack_from(raw, dynamic[2]))
        linked = Elf(elf.read_bytes())
        params = linked.sections[".ps5.module"]
        self.assertEqual(struct.unpack_from("<Q", raw, params[4])[0], dynamic[3])
        self.assertEqual(image.memory(linked.sections[".dynamic"][3],
                                      linked.sections[".dynamic"][5]),
                         bytes(linked.sections[".dynamic"][5]))
        image.validate_metadata_layout()

    def test_old_writable_load_dynamic_layout_rejected(self):
        elf, native, _ = self.provider()
        raw = bytearray(native.read_bytes())
        image = sdk.NativeImage(raw)
        index = next(i for i, p in enumerate(image.phdrs) if p[0] == 2)
        dynamic = image.phdrs[index]
        data = next(p for p in image.phdrs if p[0] == 1 and
                    p[3] == Elf(elf.read_bytes()).sections[".dynamic"][3])
        raw[data[2]:data[2] + dynamic[5]] = raw[dynamic[2]:dynamic[2] + dynamic[5]]
        PHDR.pack_into(raw, 64 + index * PHDR.size,
                       2, 6, data[2], data[3], data[3], dynamic[5], dynamic[6], 8)
        with self.assertRaisesRegex(ValueError, "PT_DYNAMIC must be contained"):
            sdk.prove_provider(elf.read_bytes(), raw, "test.provider")

    def test_dynamic_before_metadata_load_rejected(self):
        elf, native, _ = self.provider()
        raw = bytearray(native.read_bytes())
        image = sdk.NativeImage(raw)
        indices = [next(i for i, p in enumerate(image.phdrs) if p[0] == kind and
                        (kind != 1 or p[1] == 0)) for kind in (1, 2)]
        for index, header in zip(indices, (image.phdrs[indices[1]], image.phdrs[indices[0]])):
            PHDR.pack_into(raw, 64 + index * PHDR.size, *header)
        with self.assertRaisesRegex(ValueError, "metadata LOAD must precede"):
            sdk.prove_provider(elf.read_bytes(), raw, "test.provider")

    def test_dynamic_file_virtual_offset_mismatch_rejected(self):
        elf, native, _ = self.provider()
        raw = bytearray(native.read_bytes())
        image = sdk.NativeImage(raw)
        index = next(i for i, p in enumerate(image.phdrs) if p[0] == 2)
        header = list(image.phdrs[index])
        header[3] += 16
        PHDR.pack_into(raw, 64 + index * PHDR.size, *header)
        with self.assertRaisesRegex(ValueError, "matching file/VA offsets"):
            sdk.prove_provider(elf.read_bytes(), raw, "test.provider")

    def test_dynamic_metadata_tables_outside_metadata_load_rejected(self):
        elf, native, _ = self.provider()
        original = native.read_bytes()
        dynamic = next(p for p in sdk.NativeImage(original).phdrs if p[0] == 2)
        for target in (5, 6, 4, 7):
            with self.subTest(tag=target):
                raw = bytearray(original)
                offset = next(offset for offset in range(
                    dynamic[2], dynamic[2] + dynamic[5], sdk.DYNAMIC.size)
                    if sdk.DYNAMIC.unpack_from(raw, offset)[0] == target)
                sdk.DYNAMIC.pack_into(raw, offset, target, 0)
                with self.assertRaisesRegex(ValueError, "outside the metadata LOAD"):
                    sdk.prove_provider(elf.read_bytes(), raw, "test.provider")

    def test_independent_consumer_imports_functions_and_objects(self):
        _, _, provider = self.provider()
        stub = self.root / "sdk/lib/test.provider.so"
        consumer = self.build("consumer", """
            extern int value;
            extern int compute(int);
            int *imported_pointer = &value;
            int run(void) { return compute(value); }
        """, [stub])
        source = consumer.read_bytes()
        self.assertTrue(Elf(source).section(".rela.plt"))
        raw = link.convert(source, "test.consumer", [provider])
        image = sdk.NativeImage(raw)
        strings = image.memory(image.tag(5), image.tag(10))
        imports = [(sdk.string_at(strings, s[0]), s)
                   for s in SYM.iter_unpack(image.memory(image.tag(6), image.tag(sdk.SYMTABSZ)))
                   if not s[3] and s[0]]
        self.assertEqual({n for n, _ in imports},
                         {nid("compute") + "#B#B", nid("value") + "#B#B"})
        self.assertEqual(sdk.string_at(strings, image.tag(1)), "test.provider.sprx")
        self.assertEqual(sdk.string_at(strings, image.tag(0x61000045) & 0xFFFFFFFF),
                         "test.provider")
        rels = list(RELA.iter_unpack(image.memory(image.tag(7), image.tag(8))))
        self.assertTrue(any(info & 0xFFFFFFFF == 1 for _, info, _ in rels))
        self.assertTrue(any(info & 0xFFFFFFFF == 6 for _, info, _ in rels))
        self.assertFalse(any(info & 0xFFFFFFFF == 7 for _, info, _ in rels))
        self.assertEqual(image.tag(2), 0)
        linked_symbols = Elf(source).symbols(".dynsym")
        native_symbols = list(SYM.iter_unpack(image.memory(image.tag(6), image.tag(sdk.SYMTABSZ))))
        for (_, original), converted in zip(linked_symbols, native_symbols):
            self.assertEqual(original[1:3], converted[1:3])
            self.assertEqual(original[4:], converted[4:])
        native = self.root / "consumer.native.elf"
        native.write_bytes(raw)
        manifest = sdk.generate(consumer, native, "test.consumer", self.root / "consumer-sdk", LLD)
        self.assertEqual(manifest["module"], "test.consumer")
        self.assertTrue(manifest["dependencies"])
        second_consumer = self.build(
            "second_consumer", "extern int run(void); int invoke(void) {return run();}",
            [self.root / "consumer-sdk/lib/test.consumer.so"])
        second_native = link.convert(second_consumer.read_bytes(), "test.second",
                                     [link.load_provider(self.root / "consumer-sdk/manifest.json")])
        sdk.prove_provider(second_consumer.read_bytes(), second_native, "test.second")

    def test_native_sign_and_extract_preserves_module(self):
        native_tool = Path(os.environ.get("NATIVE_SDK_SIGN_TOOL", str(
            ROOT / "build/ps5-external/ps5-opengl/build/native-app/PPSA99005/build/host/ps5-native-tool")))
        if not native_tool.is_file():
            self.skipTest("native signing tool not built")
        _, native, _ = self.provider()
        signed, extracted = self.root / "provider.sprx", self.root / "extracted.elf"
        for command in ([str(native_tool), "self", "--sign", "--file", str(native),
                         "--out", str(signed)],
                        [str(native_tool), "self", "--extract", "--file", str(signed),
                         "--out", str(extracted)]):
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(native.read_bytes(), extracted.read_bytes())

    def test_missing_or_duplicate_provider_rejected(self):
        _, _, provider = self.provider()
        consumer = self.build("consumer", "extern int value; int run(void) {return value;}",
                              [self.root / "sdk/lib/test.provider.so"])
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            link.convert(consumer.read_bytes(), "test.consumer")
        with self.assertRaisesRegex(ValueError, "duplicate provider"):
            link.convert(consumer.read_bytes(), "test.consumer", [provider, provider])

    def test_missing_provider_export_rejected(self):
        _, _, provider = self.provider()
        consumer = self.build("consumer", "extern int value; int run(void) {return value;}",
                              [self.root / "sdk/lib/test.provider.so"])
        provider["symbols"] = [s for s in provider["symbols"] if s["name"] != "value"]
        with self.assertRaisesRegex(ValueError, "no declared provider exports value"):
            link.convert(consumer.read_bytes(), "test.consumer", [provider])

    def test_provider_type_mismatch_rejected(self):
        _, _, provider = self.provider()
        consumer = self.build("consumer", "extern int value; int run(void) {return value;}",
                              [self.root / "sdk/lib/test.provider.so"])
        for s in provider["symbols"]:
            if s["name"] == "value":
                s["info"] = 0x12
        with self.assertRaisesRegex(ValueError, "symbol type mismatch"):
            link.convert(consumer.read_bytes(), "test.consumer", [provider])

    def test_provider_import_library_tampering_rejected(self):
        self.provider()
        stub = self.root / "sdk/lib/test.provider.so"
        stub.write_bytes(stub.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            link.load_provider(self.root / "sdk/manifest.json")

    def test_provider_manifest_symbol_tampering_rejected(self):
        self.provider()
        path = self.root / "sdk/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["symbols"][0]["size"] += 1
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "symbol attributes"):
            link.load_provider(path)

    def test_relocation_to_text_rejected(self):
        elf, _, _ = self.provider()
        raw = bytearray(elf.read_bytes())
        h = Elf(raw).sections[".rela.dyn"]
        struct.pack_into("<Q", raw, h[4], 16)
        with self.assertRaisesRegex(ValueError, "non-writable"):
            link.convert(raw, "test.provider")

    def test_unsupported_relocation_rejected(self):
        elf, _, _ = self.provider()
        raw = bytearray(elf.read_bytes())
        h = Elf(raw).sections[".rela.dyn"]
        info = struct.unpack_from("<Q", raw, h[4] + 8)[0]
        struct.pack_into("<Q", raw, h[4] + 8, info & ~0xFFFFFFFF | 37)
        with self.assertRaisesRegex(ValueError, "unsupported dynamic relocation"):
            link.convert(raw, "test.provider")

    def test_source_constructor_array_rejected(self):
        elf = self.build("constructors", """
            volatile int value;
            __attribute__((constructor)) static void initialize(void) {value = 42;}
        """)
        with self.assertRaisesRegex(ValueError, "constructor/destructor arrays"):
            link.convert(elf.read_bytes(), "test.constructors")

    def test_source_native_tls_rejected(self):
        elf = self.build("tls", "__thread int value = 1;")
        with self.assertRaisesRegex(ValueError, "native TLS"):
            link.convert(elf.read_bytes(), "test.tls")

    def build_crt_provider(self, process_lifetime=False, runtime_module="test.runtime",
                           tls_symbol="__cxa_thread_atexit"):
        cxx = Path(os.environ.get("NATIVE_CRT_TARGET_CXX",
                                  "/opt/ps5-payload-sdk/bin/prospero-clang++"))
        if not cxx.is_file():
            self.skipTest("PS5 SDK C++ compiler required")
        source = self.root / "global.cpp"
        source.write_text("""
            volatile int value;
            struct State {
              State() { value = value + 1; }
              ~State() { value = value - 1; }
            };
            State instance;
            extern "C" int read_value() { return value; }
        """)
        if process_lifetime:
            source.write_text(source.read_text() + r'''
                asm(".hidden __ps5_native_process_lifetime_v1\n");
                extern "C" const unsigned char __ps5_native_process_lifetime_v1[17] =
                    "PS5-NATIVE-LIFE\1";
                extern "C" int __cxa_thread_atexit(void (*)(void*), void*, void*);
                extern "C" void* __dso_handle;
                static void cleanup(void*) {}
                extern "C" int register_tls_destructor() {
                  return __cxa_thread_atexit(cleanup, nullptr, __dso_handle);
                }
            ''')
            source.write_text(source.read_text().replace("__cxa_thread_atexit", tls_symbol))
        objects = []
        for name, path in (("crt", ROOT / "tools/ps5/native_module_crt.cpp"), ("global", source)):
            obj = self.root / (name + ".o")
            result = subprocess.run([str(cxx), "--target=x86_64-unknown-freebsd",
                                     "-std=c++20", "-O2", "-fPIC", "-fuse-init-array",
                                     "-fno-stack-protector", "-c", str(path), "-o", str(obj)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            objects.append(obj)
        all_symbols = [entry for obj in objects
                       for entry in Elf(obj.read_bytes()).symbols(".symtab")]
        definitions = {n for n, s in all_symbols if s[3]}
        imports = {n for n, s in all_symbols if n and not s[3] and n not in definitions and
                   not n.startswith("__ps5_") and n != "_GLOBAL_OFFSET_TABLE_"}
        # These are metadata-only ABI fixtures, not a replacement runtime.
        runtime_object = self.root / "abi-exports.o"
        runtime_object.write_bytes(sdk.import_object(
            {n: (0, 0x12, 0, 1, 0, 1) for n in sorted(imports)}))
        runtime = self.build("runtime", "int runtime_fixture;", objects=[runtime_object])
        native = self.root / "runtime.native.elf"
        native.write_bytes(link.convert(runtime.read_bytes(), runtime_module))
        sdk.generate(runtime, native, runtime_module, self.root / "runtime-sdk", LLD)
        provider = link.load_provider(self.root / "runtime-sdk/manifest.json")
        library = self.root / "runtime-sdk/lib" / (runtime_module + ".so")
        elf = self.build("crt_provider", "int anchor;", [library], objects=objects, lifecycle=False)
        return elf, provider

    def test_process_lifetime_profile_requires_explicit_marker(self):
        elf, provider = self.build_crt_provider()
        with self.assertRaisesRegex(ValueError, "valid hidden marker"):
            link.convert(elf.read_bytes(), "test.crt", [provider], process_lifetime=True)

    def test_process_lifetime_marker_cannot_silently_change_addon_policy(self):
        elf, provider = self.build_crt_provider(process_lifetime=True)
        with self.assertRaisesRegex(ValueError, "explicit process-lifetime contract"):
            link.convert(elf.read_bytes(), "test.crt", [provider])

    def test_process_lifetime_tls_destructors_bind_only_to_shared_core(self):
        for name in ("__cxa_thread_atexit", "__cxa_thread_atexit_impl"):
            with self.subTest(name=name):
                self.root = Path(self.temp.name) / name
                self.root.mkdir()
                elf, provider = self.build_crt_provider(True, "libkodi-runtime", name)
                raw = link.convert(elf.read_bytes(), "test.crt", [provider], process_lifetime=True)
                sdk.prove_provider(elf.read_bytes(), raw, "test.crt")
                native = sdk.NativeImage(raw)
                strings = native.memory(native.tag(5), native.tag(10))
                names = {sdk.string_at(strings, symbol[0])
                         for symbol in SYM.iter_unpack(
                             native.memory(native.tag(6), native.tag(sdk.SYMTABSZ)))
                         if symbol[0] and not symbol[3]}
                self.assertIn(nid(name) + "#B#B", names)

    def test_process_lifetime_marker_must_be_hidden_exact_object(self):
        elf, provider = self.build_crt_provider(True, "libkodi-runtime")
        original = elf.read_bytes()
        linked = Elf(original)
        index, (_, marker) = next(
            (index, symbol) for index, symbol in enumerate(linked.symbols(".symtab"))
            if symbol[0] == "__ps5_native_process_lifetime_v1")
        for field, value in ((1, marker[1] & ~15 | 2), (2, 0), (3, 0), (5, 16)):
            with self.subTest(field=field):
                raw = bytearray(original)
                changed = list(marker)
                changed[field] = value
                SYM.pack_into(raw, linked.sections[".symtab"][4] + index * SYM.size, *changed)
                with self.assertRaisesRegex(ValueError, "valid hidden marker"):
                    link.convert(raw, "test.crt", [provider], process_lifetime=True)
        raw = bytearray(original)
        raw[PAGE + marker[4]] ^= 1
        with self.assertRaisesRegex(ValueError, "valid hidden marker"):
            link.convert(raw, "test.crt", [provider], process_lifetime=True)

    def test_process_lifetime_marker_does_not_replace_native_crt(self):
        elf = self.build("no_crt", r'''
            __attribute__((visibility("hidden")))
            const unsigned char __ps5_native_process_lifetime_v1[17] = "PS5-NATIVE-LIFE\1";
            int value;
        ''')
        with self.assertRaisesRegex(ValueError, "valid hidden marker"):
            link.convert(elf.read_bytes(), "test.crt", process_lifetime=True)

    def test_process_lifetime_does_not_allow_another_tls_owner(self):
        elf, provider = self.build_crt_provider(True)
        with self.assertRaisesRegex(ValueError, "must use the shared core"):
            link.convert(elf.read_bytes(), "test.crt", [provider], process_lifetime=True)

    def test_real_target_crt_and_cpp_constructor_arrays(self):
        elf, provider = self.build_crt_provider()
        linked = Elf(elf.read_bytes())
        self.assertGreater(len(linked.section(".init_array")), 0)
        self.assertGreaterEqual(len(linked.section(".fini_array")), 8)
        raw = link.convert(elf.read_bytes(), "test.crt", [provider])
        sdk.prove_provider(elf.read_bytes(), raw, "test.crt")
        native = sdk.NativeImage(raw)
        self.assertNotIn(25, native.tags)
        self.assertNotIn(26, native.tags)
        self.assertEqual(native.tag(12), dict(linked.symbols(".symtab"))["module_start"][4])

    def test_crt_marker_tampering_rejected(self):
        elf, provider = self.build_crt_provider()
        raw = bytearray(elf.read_bytes())
        marker = dict(Elf(raw).symbols(".symtab"))["__ps5_native_lifecycle_v1"]
        raw[PAGE + marker[4]] ^= 1
        with self.assertRaisesRegex(ValueError, "CRT marker"):
            link.convert(raw, "test.crt", [provider])

    def test_crt_finalizer_order_tampering_rejected(self):
        elf, provider = self.build_crt_provider()
        raw = bytearray(elf.read_bytes())
        linked = Elf(raw)
        fini = linked.sections[".fini_array"][3]
        rels = linked.sections[".rela.dyn"]
        for offset in range(rels[4], rels[4] + rels[5], RELA.size):
            target, _, _ = RELA.unpack_from(raw, offset)
            if target == fini:
                struct.pack_into("<q", raw, offset + 16,
                                 dict(linked.symbols(".symtab"))["module_stop"][4])
                break
        else:
            self.fail("CRT finalizer relocation not emitted")
        with self.assertRaisesRegex(ValueError, "first fini-array"):
            link.convert(raw, "test.crt", [provider])

    def test_wrong_layout_rejected(self):
        elf, _, _ = self.provider()
        raw = bytearray(elf.read_bytes())
        struct.pack_into("<Q", raw, 64 + 16, PAGE)
        with self.assertRaisesRegex(ValueError, "text must start"):
            link.convert(bytes(raw), "test.provider")

    def test_unsigned_and_unsupported_identity_rejected(self):
        elf, _, _ = self.provider()
        raw = bytearray(elf.read_bytes())
        struct.pack_into("<H", raw, 16, 1)
        with self.assertRaisesRegex(ValueError, "ET_DYN"):
            link.convert(raw, "test.provider")
        with self.assertRaisesRegex(ValueError, "module identity"):
            link.convert(elf.read_bytes(), "../provider")


if __name__ == "__main__":
    unittest.main()
