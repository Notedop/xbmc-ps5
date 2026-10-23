#!/usr/bin/env python3
"""Host-only synthetic tests: python3 tools/ps5/test-native-system-provider.py.

No compiler, SDK, console, or third-party dependencies are needed.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import contextlib
import io
import json
from pathlib import Path
import shutil
import struct
import unittest
import uuid

import native_system_provider as proof
from native_c_link import PHDR, SHDR, SYM, nid
from native_runtime_sdk import DYNAMIC, EXPORT_LIB, IMPORT_LIB, MODULE_INFO, NEEDED_MODULE, SYMTABSZ


def encode_id(value):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"
    result = alphabet[value & 63]
    while value >> 6:
        value >>= 6
        result = alphabet[value & 63] + result
    return result


def symbol(name="function", info=0x12, other=0, size=0):
    return (name, info, other, size)


def native(exports=None, libraries=((3, "libc", 1), (65, "libc_setjmp", 2)),
           module=(7, "libc", 0x102), encoded=None, export_section=1,
           addresses=None, code=b""):
    strings = bytearray(b"\0")

    def string(text):
        offset = len(strings)
        strings.extend(text.encode() + b"\0")
        return offset

    def declaration(tag, entry):
        identity, name, version = entry
        return tag, string(name) | version << 32 | identity << 48

    tags = [declaration(MODULE_INFO, module)]
    tags.extend(declaration(EXPORT_LIB, entry) for entry in libraries)
    table = bytearray(SYM.size)
    if exports is None:
        exports = [(symbol(), 3)]
    for index, ((name, info, other, size), library_id) in enumerate(exports):
        name = (encoded if encoded is not None else
                nid(name) + "#" + encode_id(library_id) + "#" + encode_id(module[0]))
        address = addresses[index] if addresses is not None else 0x400 + index * 8
        table.extend(SYM.pack(string(name), info, other, export_section, address, size))
    raw = bytearray(0x200)
    base = 0x10000

    def append(data):
        raw.extend(bytes(-len(raw) % 8))
        address = base + len(raw)
        raw.extend(data)
        return address

    append(code)
    strva = append(strings)
    symva = append(table)
    tags += [(5, strva), (10, len(strings)), (6, symva), (11, SYM.size),
             (SYMTABSZ, len(table)), (0, 0)]
    dynamic = b"".join(DYNAMIC.pack(*tag) for tag in tags)
    dynva = append(dynamic)
    # Deliberately stale section headers: providers must use program headers only.
    raw[:64] = struct.pack("<16sHHIQQQIHHHHHH",
                           b"\x7fELF\x02\x01\x01\x09\x02" + bytes(7),
                           0xFE18, 62, 1, 0, 64, 0xFFFFFFFF, 0, 64, PHDR.size, 2,
                           SHDR.size, 27, 26)
    PHDR.pack_into(raw, 64, 1, 4, 0, base, base, len(raw), len(raw), 0x1000)
    PHDR.pack_into(raw, 64 + PHDR.size, 2, 4, dynva - base, dynva, dynva,
                   len(dynamic), len(dynamic), 8)
    return bytes(raw)


def consumer(imports=None, libraries=(), modules=(), needed="misleading-sdk-name.so",
             version=None):
    if imports is None:
        imports = [symbol()]
    strings = bytearray(b"\0")

    def string(text):
        offset = len(strings)
        strings.extend(text.encode() + b"\0")
        return offset

    tags = [(1, string(needed))]
    for tag, rows in ((IMPORT_LIB, libraries), (NEEDED_MODULE, modules)):
        for identity, name, revision in rows:
            tags.append((tag, string(name) | revision << 32 | identity << 48))
    tags.append((0, 0))
    symbols = bytearray(SYM.size)
    for name, info, other, size in imports:
        symbols.extend(SYM.pack(string(name), info, other, 0, 0, size))
    names = b"\0.dynstr\0.dynsym\0.dynamic\0.shstrtab\0.gnu.version\0"
    sections = [("", b"", 0, 0), (".dynstr", strings, 3, 0),
                (".dynsym", symbols, 11, SYM.size),
                (".dynamic", b"".join(DYNAMIC.pack(*tag) for tag in tags), 6, DYNAMIC.size),
                (".shstrtab", names, 3, 0)]
    if version is not None:
        sections.append((".gnu.version", struct.pack("<" + "H" * (len(imports) + 1),
                                                    0, *([version] * len(imports))), 0x6FFFFFFF, 2))
    raw = bytearray(64)
    headers = []
    for name, data, kind, entry_size in sections:
        headers.append((names.index(name.encode() + b"\0"), kind, 0, 0, len(raw),
                        len(data), 1 if kind in (6, 11) else 0, 0, 1, entry_size))
        raw.extend(data)
    shoff = len(raw)
    raw.extend(b"".join(SHDR.pack(*header) for header in headers))
    raw[:64] = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + bytes(9),
                           3, 62, 1, 0, 0, shoff, 0, 64, PHDR.size, 0,
                           SHDR.size, len(headers), 4)
    return bytes(raw)


class ProviderTests(unittest.TestCase):
    def bootstrap(self, names=proof.BOOTSTRAP_CRITICAL_SYMBOLS, code=proof.BOOTSTRAP_CODE,
                  addresses=None, info=0x12):
        return proof.inspect_provider(native(
            [(symbol(name, info=info), 3) for name in names], code=code,
            addresses=[0x10200] * len(names) if addresses is None else addresses), "bootstrap")

    def test_known_bootstrap_profile_preserves_exact_evidence(self):
        inventory = self.bootstrap()["inventory"]
        self.assertEqual(inventory["implementation_status"], "bootstrap-placeholder")
        self.assertFalse(inventory["runtime_implementation_qualified"])
        evidence = inventory["implementation_evidence"]
        self.assertEqual([row["symbol"] for row in evidence],
                         list(proof.BOOTSTRAP_CRITICAL_SYMBOLS))
        for row in evidence:
            self.assertEqual(row["nid"], nid(row["symbol"]))
            self.assertEqual(row["virtual_address"], 0x10200)
            self.assertEqual(row["virtual_address_hex"], "0x10200")
            self.assertEqual(row["code_hex"], "66 0f ef c0 31 c0 c3")

    def test_different_code_or_addresses_are_not_bootstrap_profile(self):
        for provider in (
            self.bootstrap(code=b"\x90" + proof.BOOTSTRAP_CODE[1:]),
            self.bootstrap(code=proof.BOOTSTRAP_CODE * 4,
                           addresses=[0x10200 + 7 * index for index in range(4)]),
            self.bootstrap(code=b"\xc3" * 7),
        ):
            self.assertEqual(provider["inventory"]["implementation_status"], "unrecognized")
            self.assertFalse(provider["inventory"]["runtime_implementation_qualified"])
            self.assertEqual(provider["inventory"]["implementation_evidence"], [])

    def test_bootstrap_requires_all_critical_function_nids_and_readable_code(self):
        for provider in (
            self.bootstrap(names=proof.BOOTSTRAP_CRITICAL_SYMBOLS[:-1]),
            self.bootstrap(names=("ordinary_alias",) * 4),
            self.bootstrap(info=0x11),
            self.bootstrap(addresses=[0x400] * 4),
        ):
            self.assertEqual(provider["inventory"]["implementation_status"], "unrecognized")

    def test_bootstrap_blocks_perfect_metadata_coverage_with_or_without_selection(self):
        provider = self.bootstrap()
        source = consumer([symbol(name) for name in proof.BOOTSTRAP_CRITICAL_SYMBOLS])
        for selectors in ((), ("libc=libc",)):
            result = proof.prove_imports(source, [provider], selectors=selectors)
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["proof_complete"])
            self.assertTrue(result["symbol_metadata_complete"])
            self.assertEqual(result["verified_count"], 4)
            self.assertTrue(all(row["proof_complete"] for row in result["imports"]))
            self.assertFalse(result["runtime_implementation_qualified"])
            self.assertIn("bootstrap-placeholder", result["blockers"][0]["reason"])
            self.assertEqual(result["blockers"][0]["implementation_evidence"],
                             provider["inventory"]["implementation_evidence"])

    def prove(self, source=None, providers=None):
        if providers is None:
            providers = [proof.inspect_provider(native(), "untrusted-label")]
        return proof.prove_imports(consumer() if source is None else source, providers)

    def blocked(self, result, reason):
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["proof_complete"])
        self.assertIn(reason, "; ".join(result["imports"][0]["reasons"]))

    def test_plain_names_ignore_sdk_hints_and_record_actual_identities(self):
        report = self.prove()
        self.assertTrue(report["proof_complete"])
        self.assertTrue(report["symbol_metadata_complete"])
        self.assertFalse(report["runtime_implementation_qualified"])
        self.assertEqual(report["providers"][0]["implementation_status"], "unrecognized")
        target = report["imports"][0]["matches"][0]
        self.assertEqual(target["module"], {"id": 7, "name": "libc", "version": 258})
        self.assertEqual(target["library"], {"id": 3, "name": "libc", "version": 1})
        self.assertEqual(report["providers"][0]["libraries"][1]["id"], 65)

    def test_multiple_libraries_and_arbitrary_ids(self):
        exports = [(symbol("function"), 3), (symbol("jump"), 65)]
        report = self.prove(consumer([symbol("jump")]),
                            [proof.inspect_provider(native(exports), "libc")])
        self.assertTrue(report["proof_complete"])
        self.assertEqual(report["imports"][0]["matches"][0]["library"]["name"], "libc_setjmp")

    def test_firmware_abi_three_keeps_original_hash_and_sdk_gate(self):
        from native_runtime_sdk import NativeImage, sha256
        raw = bytearray(native())
        raw[8] = 3
        original = bytes(raw)
        provider = proof.inspect_provider(original, "p")
        self.assertEqual(provider["inventory"]["elf_abi_version"], 3)
        self.assertEqual(provider["inventory"]["sha256"], sha256(original))
        self.assertTrue(self.prove(providers=[provider])["proof_complete"])
        with self.assertRaises(ValueError):
            NativeImage(original)
        for abi in (0, 1, 4):
            raw[8] = abi
            with self.assertRaises(ValueError):
                proof.inspect_provider(bytes(raw), "p")

    def test_encoded_import_ids_are_local_not_provider_ids(self):
        source = consumer([symbol(nid("function") + "#BA#C")],
                          libraries=((64, "libc", 1),), modules=((2, "libc", 258),))
        self.assertTrue(self.prove(source)["proof_complete"])

    def test_module_and_library_version_mismatches_preserve_evidence(self):
        for module_version, library_version in ((257, 1), (258, 2)):
            with self.subTest(module=module_version, library=library_version):
                source = consumer([symbol(nid("function") + "#A#B")],
                                  libraries=((0, "libc", library_version),),
                                  modules=((1, "libc", module_version),))
                report = self.prove(source)
                self.blocked(report, "incompatible native module/library version")
                self.assertEqual(report["imports"][0]["matches"][0]["module"]["version"], 258)

    def test_same_library_name_distinct_versions_require_exact_selection(self):
        provider = proof.inspect_provider(
            native([(symbol(), 3), (symbol(), 65)],
                   libraries=((3, "libc", 1), (65, "libc", 2))), "p")
        self.blocked(self.prove(providers=[provider]), "ambiguous native export")
        source = consumer([symbol(nid("function") + "#A#B")],
                          libraries=((0, "libc", 2),), modules=((1, "libc", 258),))
        report = self.prove(source, [provider])
        self.assertTrue(report["proof_complete"])
        self.assertEqual(report["imports"][0]["matches"][0]["library"]["version"], 2)

    def test_identity_mismatch_is_missing_even_if_nid_exists(self):
        source = consumer([symbol(nid("function") + "#A#B")],
                          libraries=((0, "not-libc", 1),), modules=((1, "libc", 258),))
        self.blocked(self.prove(source), "missing native export")

    def test_ambiguity_across_libraries_and_providers(self):
        for providers in (
            [proof.inspect_provider(native([(symbol(), 3), (symbol(), 65)]), "one")],
            [proof.inspect_provider(native(), label) for label in ("one", "two")],
            [proof.inspect_provider(native([(symbol(), 3), (symbol(), 3)]), "one")],
        ):
            report = self.prove(providers=providers)
            self.blocked(report, "ambiguous native export")
            self.assertEqual(len(report["imports"][0]["matches"]), 2)

    def test_function_data_mismatch_both_directions(self):
        for source_info, export_info in ((0x12, 0x11), (0x11, 0x12)):
            self.blocked(self.prove(consumer([symbol(info=source_info)]),
                         [proof.inspect_provider(native([(symbol(info=export_info), 3)]), "p")]),
                         "function/data type mismatch")

    def test_notype_source_is_incomplete_with_matched_evidence(self):
        report = self.prove(consumer([symbol(info=0x10)]))
        self.blocked(report, "source STT_NOTYPE")
        row = report["imports"][0]
        self.assertEqual(row["source"]["type"], "notype")
        self.assertEqual(row["matches"][0]["type"], "function")
        self.assertEqual(report["verified_count"], 0)

    def test_notype_export_is_incomplete(self):
        self.blocked(self.prove(providers=[
            proof.inspect_provider(native([(symbol(info=0x10), 3)]), "p")]),
            "export STT_NOTYPE")

    def test_missing_export_never_succeeds(self):
        self.blocked(self.prove(consumer([symbol("absent")])), "missing native export")
        self.blocked(self.prove(providers=[]), "missing native export")
        self.blocked(self.prove(providers=[
            proof.inspect_provider(native(export_section=0), "p")]), "missing native export")

    def test_missing_weak_is_explicitly_unsupported(self):
        self.blocked(self.prove(consumer([symbol("absent", info=0x22)])),
                     "unresolved weak import unsupported")
        self.assertTrue(self.prove(consumer([symbol(info=0x22)]))["proof_complete"])

    def test_binding_visibility_and_unsupported_types(self):
        for info, other in ((0x32, 0), (0x12, 1), (0x12, 2), (0x12, 4),
                            (0x16, 0), (0x1A, 0)):
            with self.subTest(info=info, other=other):
                report = self.prove(providers=[
                    proof.inspect_provider(native([(symbol(info=info, other=other), 3)]), "p")])
                self.assertFalse(report["proof_complete"])
                target = report["imports"][0]["matches"][0]
                self.assertEqual((target["info"], target["other"]), (info, other))
        for info, other in ((0x02, 0), (0x12, 3), (0x16, 0)):
            self.assertFalse(self.prove(consumer([symbol(info=info, other=other)]))["proof_complete"])
        for info, other in ((0x22, 0), (0x12, 3)):
            self.assertTrue(self.prove(providers=[
                proof.inspect_provider(native([(symbol(info=info, other=other), 3)]), "p")
            ])["proof_complete"])

    def test_object_size_mismatch(self):
        self.blocked(self.prove(consumer([symbol(info=0x11, size=8)]), [
            proof.inspect_provider(native([(symbol(info=0x11, size=4), 3)]), "p")
        ]), "object size mismatch")

    def test_gnu_versioned_import_is_not_silently_unversioned(self):
        self.blocked(self.prove(consumer(version=2)), "GNU-versioned import unsupported")

    def test_malformed_encoded_names(self):
        for name in ("bad#D#H", nid("function") + "#AA#H",
                     nid("function") + "#!#H", nid("function") + "#EAAA#H",
                     nid("function") + "##H", nid("function") + "#D#H#A"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                proof.inspect_provider(native(encoded=name), "p")
            with self.subTest(consumer=name), self.assertRaises(ValueError):
                self.prove(consumer([symbol(name)]))

    def test_undeclared_provider_ids(self):
        for encoded in (nid("function") + "#C#H", nid("function") + "#D#A"):
            with self.assertRaisesRegex(ValueError, "undeclared"):
                proof.inspect_provider(native(encoded=encoded), "p")

    def test_common_export_is_not_a_resolved_definition(self):
        self.blocked(self.prove(providers=[
            proof.inspect_provider(native(export_section=0xFFF2), "p")]),
            "unsupported export section index")

    def test_explicit_library_selection_disambiguates_and_preserves_candidates(self):
        providers = [proof.inspect_provider(native([(symbol(), 3), (symbol(), 65)]), "p")]
        result = proof.prove_imports(consumer(), providers, selectors=["libc=libc"])
        self.assertTrue(result["proof_complete"])
        self.assertFalse(result["abi_layouts_verified"])
        self.assertEqual(result["library_selection"], [{"module": "libc", "libraries": ["libc"]}])
        row = result["imports"][0]
        self.assertEqual(len(row["candidates"]), 2)
        self.assertEqual(row["matches"][0]["library"]["id"], 3)
        self.assertEqual(row["excluded_by_library_policy"][0]["library"]["id"], 65)
        self.assertFalse(row["policy_excluded_only"])
        result = proof.prove_imports(consumer(), providers,
                                    selectors=["libc=libc", "libc=libc_setjmp"])
        self.blocked(result, "ambiguous native export")

    def test_selection_does_not_hide_exports_only_in_other_libraries(self):
        providers = [proof.inspect_provider(native([(symbol(), 65)]), "p")]
        result = proof.prove_imports(consumer(), providers, selectors=["libc=libc"])
        self.blocked(result, "outside selected library policy")
        row = result["imports"][0]
        self.assertTrue(row["policy_excluded_only"])
        self.assertEqual(len(row["candidates"]), 1)
        self.assertEqual(len(row["excluded_by_library_policy"]), 1)
        self.assertEqual(row["matches"], [])

    def test_selection_preserves_missing_notype_and_weak_blockers(self):
        providers = [proof.inspect_provider(native(), "p")]
        for source, reason in ((symbol(info=0x10), "source STT_NOTYPE"),
                               (symbol("missing"), "missing native export"),
                               (symbol("missing", info=0x22), "unresolved weak import unsupported")):
            self.blocked(proof.prove_imports(consumer([source]), providers,
                                            selectors=["libc=libc"]), reason)

    def test_invalid_library_selection_is_rejected(self):
        providers = [proof.inspect_provider(native(), "p")]
        for selectors in ([""], ["libc"], ["=libc"], ["libc="], ["libc=libc=extra"],
                          ["sdk-label=libc"], ["libc=missing"], ["libc=libc", "libc=libc"]):
            with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                proof.prove_imports(consumer(), providers, selectors=selectors)

    def test_library_selection_uses_actual_module_not_provider_label(self):
        providers = [proof.inspect_provider(native(), "libkernel")]
        with self.assertRaisesRegex(ValueError, "unknown native module"):
            proof.prove_imports(consumer(), providers, selectors=["libkernel=libc"])
        self.assertTrue(proof.prove_imports(consumer(), providers,
                                           selectors=["libc=libc"])["proof_complete"])

    def test_undeclared_consumer_ids(self):
        with self.assertRaisesRegex(ValueError, "undeclared"):
            self.prove(consumer([symbol(nid("function") + "#A#B")]))

    def test_duplicate_empty_or_missing_declarations(self):
        for libraries in ((), ((3, "libc", 1), (3, "other", 2)),
                          ((3, "libc", 1), (4, "libc", 1)), ((3, "", 1),)):
            with self.subTest(libraries=libraries), self.assertRaises(ValueError):
                proof.inspect_provider(native(libraries=libraries), "p")
        with self.assertRaises(ValueError):
            self.prove(consumer(libraries=((0, "libc", 1), (0, "other", 1))))

    def test_truncated_provider_and_consumer(self):
        for raw in (native()[:30], native()[:-1]):
            with self.assertRaises(ValueError):
                proof.inspect_provider(raw, "p")
        for raw in (consumer()[:30], consumer()[:-1]):
            with self.assertRaises(ValueError):
                self.prove(raw)

    def test_native_consumer_and_et_rel_are_rejected(self):
        with self.assertRaises(ValueError):
            self.prove(native())
        raw = bytearray(consumer())
        struct.pack_into("<H", raw, 16, 1)
        with self.assertRaisesRegex(ValueError, "ET_DYN"):
            self.prove(bytes(raw))

    def test_cli_json_exit_codes_and_no_input_writes(self):
        directory = Path(__file__).resolve().parents[2] / (".system-provider-test-" + uuid.uuid4().hex)
        directory.mkdir()
        try:
            provider = directory / "provider.elf"
            source = directory / "consumer.elf"
            provider.write_bytes(native())
            for imports, expected in (([symbol()], 0), ([symbol(info=0x10)], 1),
                                      ([symbol("missing")], 1)):
                source.write_bytes(consumer(imports))
                original = source.read_bytes(), provider.read_bytes()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = proof.main(["--consumer", str(source),
                                       "--provider", "p=" + str(provider)])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(output.getvalue())["proof_complete"], expected == 0)
                self.assertEqual(original, (source.read_bytes(), provider.read_bytes()))
            for arguments in ([], ["--consumer", str(source), "--provider", "bad"],
                              ["--consumer", str(source), "--provider", "p=" + str(provider),
                               "--provider", "p=" + str(provider)],
                              ["--consumer", str(source), "--provider", "p=" + str(provider),
                               "--library", "libc=missing"]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(proof.main(arguments), 1)
                self.assertEqual(json.loads(output.getvalue())["status"], "blocked")
            source.write_bytes(consumer())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(proof.main(["--consumer", str(source),
                                             "--provider", "p=" + str(provider),
                                             "--library", "libc=libc"]), 0)
            self.assertEqual(json.loads(output.getvalue())["library_selection"],
                             [{"module": "libc", "libraries": ["libc"]}])
            provider.write_bytes(native(
                [(symbol(name), 3) for name in proof.BOOTSTRAP_CRITICAL_SYMBOLS],
                addresses=[0x10200] * 4, code=proof.BOOTSTRAP_CODE))
            source.write_bytes(consumer([symbol("malloc")]))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(proof.main(["--consumer", str(source),
                                             "--provider", "p=" + str(provider)]), 1)
            report = json.loads(output.getvalue())
            self.assertTrue(report["symbol_metadata_complete"])
            self.assertFalse(report["proof_complete"])
            self.assertIn("bootstrap-placeholder", report["blockers"][0]["reason"])
        finally:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
