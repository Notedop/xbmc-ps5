#!/usr/bin/env python3
"""Host-only SDK tests. Run with python3 tools/ps5/test-native-runtime-sdk.py.

Requires ld.lld on PATH (or set NATIVE_SDK_LLD); no compiler or console needed.
Fixtures are generated beneath the project, and only their own files are removed.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
import uuid

import native_runtime_sdk as sdk
from native_c_link import Elf, PHDR, SYM, hash_table, nid


ROOT = Path(__file__).resolve().parents[2]
LLD = os.environ.get("NATIVE_SDK_LLD") or shutil.which("ld.lld")
MODULE = "test.provider"


def native_fixture(symbols, module=MODULE, missing=None, mutate=None, duplicate=False,
                   source_symbols=None, libraries=(), modules=(), import_ids=None,
                   omit_tags=()):
    strings = bytearray(b"\0")

    def string(text):
        offset = len(strings)
        strings.extend(text.encode() + b"\0")
        return offset

    module_offset = string(module)
    filename_offset = string(module + ".sprx")
    dependency_tags = []
    for tag, declarations in ((sdk.IMPORT_LIB, libraries), (sdk.NEEDED_MODULE, modules)):
        for identity, name, version in declarations:
            if tag not in omit_tags:
                dependency_tags.append((tag, string(name) | version << 32 | identity << 48))
    encoded = bytearray(SYM.size)
    canonical = [""]
    entries = source_symbols[1:] if source_symbols is not None else symbols.items()
    for name, symbol in entries:
        if name == missing:
            continue
        s = list(symbol)
        if symbol[3] == 0:
            library_id, module_id = import_ids[name]
            s[0] = string(nid(name) + "#" + encode_id(library_id) + "#" + encode_id(module_id))
            library = next(row[1] for row in libraries if row[0] == library_id)
            dependency = next(row[1] for row in modules if row[0] == module_id)
            canonical.append(nid(name) + "#" + library + "#" + dependency)
        else:
            s[0] = string(nid(name) + "#A#A")
            canonical.append(nid(name) + "#" + module + "#" + module)
        if mutate:
            mutate(name, s)
        encoded.extend(SYM.pack(*s))
        if duplicate:
            encoded.extend(SYM.pack(*s))
            canonical.append(canonical[-1])
    raw = bytearray(0x200)
    base = 0x10000

    def append(blob):
        raw.extend(bytes((-len(raw)) % 8))
        address = base + len(raw)
        raw.extend(blob)
        return address

    str_va = append(strings)
    sym_va = append(encoded)
    hashed = hash_table(canonical)
    hash_va = append(hashed)
    tags = [(sdk.MODULE_INFO, module_offset | 0x101 << 32),
            (sdk.EXPORT_LIB, module_offset | 1 << 32),
            (0x61000041, filename_offset), (5, str_va), (10, len(strings)),
            (6, sym_va), (11, SYM.size), (sdk.SYMTABSZ, len(encoded)),
            (4, hash_va), (sdk.HASHSZ, len(hashed)), *dependency_tags, (0, 0)]
    dynamic = b"".join(sdk.DYNAMIC.pack(*tag) for tag in tags)
    dyn_va = append(dynamic)
    raw[:64] = struct.pack("<16sHHIQQQIHHHHHH",
                           b"\x7fELF\x02\x01\x01\x09\x02" + bytes(7),
                           0xFE18, 62, 1, 0, 64, 0, 0, 64, PHDR.size, 2, 64, 0, 0)
    PHDR.pack_into(raw, 64, 1, 0, 0, base, base, len(raw), len(raw), 0x1000)
    PHDR.pack_into(raw, 64 + PHDR.size, 2, 4, dyn_va - base, dyn_va, dyn_va,
                   len(dynamic), len(dynamic), 8)
    return bytes(raw)


def encode_id(value):
    result = sdk.ID_ALPHABET[value & 63]
    while value >> 6:
        value >>= 6
        result = sdk.ID_ALPHABET[value & 63] + result
    return result


@unittest.skipUnless(LLD, "ld.lld required (set NATIVE_SDK_LLD)")
class RuntimeSdkTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / (".native-runtime-sdk-test-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.symbols = {
            "plain_function": (0, 0x12, 0, 1, 0, 9),
            "plain_object": (0, 0x11, 0, 2, 0, 24),
            "weak_function": (0, 0x22, 0, 1, 0, 3),
            "weak_object": (0, 0x21, 0, 2, 0, 7),
            "protected_function": (0, 0x12, 3, 1, 0, 1),
            "zero_object": (0, 0x11, 0, 2, 0, 0),
            "untyped.symbol": (0, 0x10, 0, 2, 0, 4),
        }
        obj = self.directory / "provider.o"
        obj.write_bytes(sdk.import_object(self.symbols))
        self.linked = self.directory / "provider.so"
        subprocess.run([LLD, "-shared", "--hash-style=sysv", "-o", str(self.linked), str(obj)],
                       check=True, capture_output=True)
        self.linked_raw = self.linked.read_bytes()
        self.symbols = sdk.eligible_symbols(Elf(self.linked_raw).symbols(".dynsym"), "fixture")
        self.native_raw = native_fixture(self.symbols)
        self.native = self.directory / "provider.sprx"
        self.native.write_bytes(self.native_raw)

    def tearDown(self):
        # This UUID-owned fixture directory is never a caller-supplied SDK output.
        shutil.rmtree(self.directory)

    def test_generate_attributes_soname_and_manifest(self):
        output = self.directory / "sdk"
        manifest = sdk.generate(self.linked, self.native, MODULE, output, LLD)
        library = output / "lib" / (MODULE + ".so")
        raw = library.read_bytes()
        sdk.validate_stub(raw, self.symbols, MODULE)
        self.assertEqual(json.loads((output / "manifest.json").read_text()), manifest)
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["module"], MODULE)
        self.assertEqual(manifest["library"], MODULE)
        self.assertEqual(manifest["module_version"], 257)
        self.assertEqual(manifest["library_version"], 1)
        self.assertEqual(manifest["soname"], MODULE + ".sprx")
        self.assertEqual(manifest["schema_version"], 1)
        self.assertFalse(manifest["runtime_loadable"])
        self.assertTrue(manifest["linker_only"])
        self.assertEqual(manifest["provider"]["native_elf_sha256"], sdk.sha256(self.native_raw))
        self.assertEqual(manifest["import_library"]["sha256"], sdk.sha256(raw))
        exports = {s["name"]: s for s in manifest["symbols"]}
        for name, symbol in self.symbols.items():
            self.assertEqual(exports[name]["info"], symbol[1])
            self.assertEqual(exports[name]["other"], symbol[2])
            self.assertEqual(exports[name]["value"], symbol[4])
            self.assertEqual(exports[name]["size"], symbol[5])
        self.assertEqual(exports["plain_object"]["size"], 24)
        self.assertEqual(exports["plain_object"]["type"], "object")
        self.assertEqual(exports["weak_object"]["binding"], "weak")
        self.assertEqual(exports["weak_function"]["type"], "function")
        self.assertEqual(exports["protected_function"]["visibility"], "protected")
        self.assertEqual(exports["zero_object"]["size"], 0)
        image = Elf(raw)
        for name, symbol in image.symbols(".dynsym"):
            if symbol[1] & 15 == 2 and symbol[3]:
                self.assertEqual(image.memory(symbol[4], 1), b"\xc3", name)
        self.assertEqual(set(p.name for p in output.iterdir()), {"lib", "manifest.json"})

    def test_consumer_gets_native_needed_name(self):
        output = self.directory / "sdk"
        sdk.generate(self.linked, self.native, MODULE, output, LLD)
        obj = self.directory / "consumer.o"
        # Convert an ordinary exported function to an undefined function reference.
        raw = bytearray(sdk.import_object({"plain_function": (0, 0x12, 0, 1, 0, 9)}))
        image = Elf(raw)
        symoff = image.sections[".symtab"][4] + SYM.size
        symbol = list(SYM.unpack_from(raw, symoff))
        symbol[3:6] = [0, 0, 0]
        SYM.pack_into(raw, symoff, *symbol)
        obj.write_bytes(raw)
        consumer = self.directory / "consumer.so"
        subprocess.run([LLD, "-shared", "--no-as-needed", "-z", "defs", "-o", str(consumer),
                        str(obj), str(output / "lib" / (MODULE + ".so"))],
                       capture_output=True, check=True)
        image = Elf(consumer.read_bytes())
        needed = [sdk.string_at(image.section(".dynstr"), value)
                  for tag, value in sdk.DYNAMIC.iter_unpack(image.section(".dynamic")) if tag == 1]
        self.assertEqual(needed, [MODULE + ".sprx"])

    def test_module_mismatch(self):
        with self.assertRaisesRegex(ValueError, "module identity/version/id mismatch"):
            sdk.prove_provider(self.linked_raw, native_fixture(self.symbols, "other"), MODULE)

    def test_missing_nid(self):
        with self.assertRaisesRegex(ValueError, "missing export NID"):
            sdk.prove_provider(self.linked_raw,
                               native_fixture(self.symbols, missing="plain_function"), MODULE)

    def test_duplicate_native_exports(self):
        with self.assertRaisesRegex(ValueError, "duplicate export"):
            sdk.prove_provider(self.linked_raw, native_fixture(self.symbols, duplicate=True), MODULE)

    def test_native_attributes_must_match(self):
        for field in (1, 2, 4, 5):
            with self.subTest(field=field):
                def mutate(name, symbol):
                    if name == "plain_function":
                        symbol[field] = {1: 0x22, 2: 3, 4: symbol[4] + 1, 5: 99}[field]
                with self.assertRaisesRegex(ValueError, "type/binding/visibility/value/size mismatch"):
                    sdk.prove_provider(self.linked_raw,
                                       native_fixture(self.symbols, mutate=mutate), MODULE)

    def test_no_unmatched_native_exports(self):
        extra = dict(self.symbols, unexpected=(0, 0x12, 0, 1, 0, 1))
        with self.assertRaisesRegex(ValueError, "unmatched exports"):
            sdk.prove_provider(self.linked_raw, native_fixture(extra), MODULE)

    def test_library_identity_and_versions(self):
        for tag in (sdk.MODULE_INFO, sdk.EXPORT_LIB):
            for change in (1 << 32, 1 << 48, 1):
                with self.subTest(tag=tag, change=change):
                    raw = bytearray(self.native_raw)
                    image = sdk.NativeImage(raw)
                    dynamic = next(p for p in image.phdrs if p[0] == 2)
                    for offset in range(dynamic[2], dynamic[2] + dynamic[5], 16):
                        if sdk.DYNAMIC.unpack_from(raw, offset)[0] == tag:
                            value = sdk.DYNAMIC.unpack_from(raw, offset)[1]
                            sdk.DYNAMIC.pack_into(raw, offset, tag, value ^ change)
                            break
                    with self.assertRaisesRegex(ValueError, "identity/version/id mismatch"):
                        sdk.prove_provider(self.linked_raw, raw, MODULE)

    def test_canonical_hash_required(self):
        raw = bytearray(self.native_raw)
        image = sdk.NativeImage(raw)
        load = next(p for p in image.phdrs if p[0] == 1)
        raw[load[2] + image.tag(4) - load[3] + 8] ^= 1
        with self.assertRaisesRegex(ValueError, "canonical"):
            sdk.prove_provider(self.linked_raw, raw, MODULE)

    def test_native_section_headers_are_not_used(self):
        raw = bytearray(self.native_raw)
        struct.pack_into("<Q", raw, 40, 0xFFFFFFFFFFFF)
        struct.pack_into("<HHH", raw, 58, 64, 99, 88)
        self.assertEqual(sdk.prove_provider(self.linked_raw, raw, MODULE), self.symbols)

    def test_native_missing_metadata_is_explicit(self):
        raw = bytearray(self.native_raw)
        image = sdk.NativeImage(raw)
        dynamic = next(p for p in image.phdrs if p[0] == 2)
        for offset in range(dynamic[2], dynamic[2] + dynamic[5], 16):
            if sdk.DYNAMIC.unpack_from(raw, offset)[0] == sdk.EXPORT_LIB:
                sdk.DYNAMIC.pack_into(raw, offset, 0x60001234, 0)
                break
        with self.assertRaisesRegex(ValueError, "expected exactly one native dynamic tag"):
            sdk.prove_provider(self.linked_raw, raw, MODULE)

    def test_stub_allocation_bound(self):
        with self.assertRaisesRegex(ValueError, "64 MiB"):
            sdk.import_object({"oversized": (0, 0x11, 0, 1, 0, sdk.MAX_STUB_BYTES + 1)})

    def import_pair(self, **fixture_overrides):
        dependencies = [
            (1, "runtime.api", 3, "runtime.module", 0x102, "host_function", 0x12),
            (2, "kernel.api", 1, "kernel.module", 0x101, "kernel_object", 0x11),
        ]
        entries = dict(self.symbols)
        for _, _, _, _, _, name, info in dependencies:
            entries[name] = (0, info, 0, 1, 0, 4)
        raw = bytearray(sdk.import_object(entries))
        image = Elf(raw)
        for index, (name, symbol) in enumerate(image.symbols(".symtab")):
            if name in ("host_function", "kernel_object"):
                symbol = list(symbol)
                symbol[3:6] = [0, 0, 0]
                SYM.pack_into(raw, image.sections[".symtab"][4] + index * SYM.size, *symbol)
        obj = self.directory / "import-provider.o"
        obj.write_bytes(raw)
        link_args = [LLD, "-shared", "--no-as-needed", "-u", "host_function",
                     "-u", "kernel_object", "-o", str(self.linked), str(obj)]
        for _, _, _, module, _, name, info in dependencies:
            dep_obj = self.directory / (module + ".o")
            dep_obj.write_bytes(sdk.import_object({name: (0, info, 0, 1, 0, 4)}))
            dep_so = self.directory / (module + ".so")
            subprocess.run([LLD, "-shared", "--soname=" + module + ".sprx",
                            "-o", str(dep_so), str(dep_obj)], capture_output=True, check=True)
            link_args.append(str(dep_so))
        subprocess.run(link_args, capture_output=True, check=True)
        linked = self.linked.read_bytes()
        source_symbols = Elf(linked).symbols(".dynsym")
        self.assertEqual({name for name, s in source_symbols if s[3] == 0 and name},
                         {"host_function", "kernel_object"})
        options = {
            "source_symbols": source_symbols,
            "libraries": [(row[0], row[1], row[2]) for row in dependencies],
            "modules": [(row[0], row[3], row[4]) for row in dependencies],
            "import_ids": {row[5]: (row[0], row[0]) for row in dependencies},
        }
        options.update(fixture_overrides)
        return linked, native_fixture(self.symbols, **options)

    def test_import_bearing_provider_and_dependency_manifest(self):
        linked, native = self.import_pair()
        exports, dependencies = sdk.prove_provider(linked, native, MODULE, include_dependencies=True)
        self.assertEqual(set(exports), set(self.symbols))
        self.assertEqual(dependencies["needed"], ["runtime.module.sprx", "kernel.module.sprx"])
        self.assertEqual(dependencies["modules"][0],
                         {"id": 1, "name": "runtime.module", "version": 0x102,
                          "soname": "runtime.module.sprx"})
        self.assertEqual(dependencies["libraries"][0],
                         {"id": 1, "name": "runtime.api", "version": 3})
        source = Elf(linked).symbols(".dynsym")
        for symbol in dependencies["imports"]:
            self.assertEqual(source[symbol["dynsym_index"]][0], symbol["name"])
        self.native.write_bytes(native)
        manifest = sdk.generate(self.linked, self.native, MODULE, self.directory / "import-sdk", LLD)
        self.assertEqual(manifest["dependencies"], dependencies)
        stub = Elf((self.directory / "import-sdk" / "lib" / (MODULE + ".so")).read_bytes())
        self.assertFalse(any(s[3] == 0 and name for name, s in stub.symbols(".dynsym")))

    def test_import_missing_declarations(self):
        for tag in (sdk.IMPORT_LIB, sdk.NEEDED_MODULE):
            with self.subTest(tag=tag):
                linked, native = self.import_pair(omit_tags=(tag,))
                with self.assertRaisesRegex(ValueError, "undeclared|dependency modules|dependency libraries"):
                    sdk.prove_provider(linked, native, MODULE)

    def test_import_hash_uses_declared_identity_names(self):
        linked, native = self.import_pair()
        raw = bytearray(native)
        image = sdk.NativeImage(raw)
        load = next(p for p in image.phdrs if p[0] == 1)
        raw[load[2] + image.tag(4) - load[3] + 8] ^= 1
        with self.assertRaisesRegex(ValueError, "canonical"):
            sdk.prove_provider(linked, raw, MODULE)

    def test_import_indices_nids_and_attributes_are_proven(self):
        for field in (0, 1, 2, 4, 5):
            with self.subTest(field=field):
                def mutate(name, symbol):
                    if name == "host_function":
                        symbol[field] = {0: 0, 1: 0x22, 2: 3, 4: 7, 5: 99}[field]
                linked, native = self.import_pair(mutate=mutate)
                with self.assertRaisesRegex(ValueError, "import index/NID|dynsym attributes"):
                    sdk.prove_provider(linked, native, MODULE)

    def test_import_ids_must_be_declared_and_module_nonzero(self):
        linked, native = self.import_pair()
        image = sdk.NativeImage(native)
        strings = image.memory(image.tag(5), image.tag(10))
        needle = (nid("host_function") + "#B#B").encode()
        for suffix in (b"#C#B", b"#B#A"):
            with self.subTest(suffix=suffix):
                raw = native.replace(needle, nid("host_function").encode() + suffix)
                # Library C exists but belongs to a different DT_NEEDED dependency.
                with self.assertRaisesRegex(ValueError, "undeclared|canonical"):
                    sdk.prove_provider(linked, raw, MODULE)
        self.assertIn(needle, strings)

    def test_duplicate_dependency_declarations(self):
        declarations = [(1, "runtime.api", 3), (2, "kernel.api", 1)]
        for duplicate in ((1, "extra.api", 1), (3, "runtime.api", 3),
                          (0, "extra.api", 1)):
            with self.subTest(duplicate=duplicate):
                linked, native = self.import_pair(libraries=declarations + [duplicate])
                with self.assertRaisesRegex(ValueError, "duplicate/conflicting"):
                    sdk.prove_provider(linked, native, MODULE)
        modules = [(1, "runtime.module", 0x102), (2, "kernel.module", 0x101),
                   (3, "runtime.module", 0x102)]
        linked, native = self.import_pair(modules=modules)
        with self.assertRaisesRegex(ValueError, "duplicate/conflicting"):
            sdk.prove_provider(linked, native, MODULE)

    def test_undeclared_import_library_id(self):
        linked, native = self.import_pair()
        needle = (nid("host_function") + "#B#B").encode()
        raw = native.replace(needle, (nid("host_function") + "#D#B").encode())
        with self.assertRaisesRegex(ValueError, "undeclared"):
            sdk.prove_provider(linked, raw, MODULE)

    def test_imported_tls_fails_closed(self):
        linked, native = self.import_pair()
        linked, native = bytearray(linked), bytearray(native)
        image = Elf(linked)
        for index, (name, symbol) in enumerate(image.symbols(".dynsym")):
            if name == "host_function":
                source_offset = image.sections[".dynsym"][4] + index * SYM.size
                source = list(SYM.unpack_from(linked, source_offset))
                source[1] = 0x16
                SYM.pack_into(linked, source_offset, *source)
                image = sdk.NativeImage(native)
                load = next(p for p in image.phdrs if p[0] == 1)
                native_offset = load[2] + image.tag(6) - load[3] + index * SYM.size
                target = list(SYM.unpack_from(native, native_offset))
                target[1] = 0x16
                SYM.pack_into(native, native_offset, *target)
                break
        with self.assertRaisesRegex(ValueError, "TLS import unsupported"):
            sdk.prove_provider(linked, native, MODULE)

    def test_native_cannot_invent_needed_modules(self):
        modules = [(1, "runtime.module", 0x102), (2, "kernel.module", 0x101),
                   (3, "invented.module", 0x101)]
        linked, native = self.import_pair(modules=modules)
        with self.assertRaisesRegex(ValueError, "dependency modules"):
            sdk.prove_provider(linked, native, MODULE)

    def test_tls_explicitly_unsupported(self):
        raw = bytearray(self.linked_raw)
        image = Elf(raw)
        offset = image.sections[".dynsym"][4] + SYM.size
        symbol = list(SYM.unpack_from(raw, offset))
        symbol[1] = 0x16
        SYM.pack_into(raw, offset, *symbol)
        with self.assertRaisesRegex(ValueError, "TLS export unsupported"):
            sdk.prove_provider(raw, self.native_raw, MODULE)

    def test_unmodified_output_cannot_be_reused(self):
        output = self.directory / "sdk"
        sdk.generate(self.linked, self.native, MODULE, output, LLD)
        before = (output / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            sdk.generate(self.linked, self.native, MODULE, output, LLD)
        self.assertEqual((output / "manifest.json").read_bytes(), before)

    def test_link_failure_cleans_only_owned_files(self):
        output = self.directory / "sdk"
        output.mkdir()
        unrelated = output / "keep"
        unrelated.write_text("do not delete")
        with self.assertRaises(OSError):
            sdk.generate(self.linked, self.native, MODULE, output, output / "missing-lld")
        self.assertEqual(unrelated.read_text(), "do not delete")
        self.assertFalse((output / "manifest.json").exists())
        self.assertFalse((output / ("." + MODULE + ".import.o")).exists())
        self.assertFalse((output / ("." + MODULE + ".import.so")).exists())

    def test_cli(self):
        result = subprocess.run(
            [sys.executable, str(Path(sdk.__file__)), "--linked-elf", str(self.linked),
             "--native-elf", str(self.native), "--module", MODULE,
             "--output", str(self.directory / "cli-sdk"), "--lld", LLD],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

if __name__ == "__main__":
    unittest.main()
