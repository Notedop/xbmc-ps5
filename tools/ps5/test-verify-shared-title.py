#!/usr/bin/env python3
"""Regression checks for strict native title extraction and ownership proof."""
# SPDX-License-Identifier: GPL-3.0-or-later

import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_shared_title", ROOT / "tools/ps5/verify-shared-title.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def title():
    raw = bytearray(216)
    raw[:9] = b"\x7fELF\x02\x01\x01\x09\x02"
    struct.pack_into("<HH", raw, 16, 0xFE10, 62)
    struct.pack_into("<Q", raw, 32, 64)
    struct.pack_into("<HH", raw, 54, 56, 2)
    struct.pack_into("<IIQQQQQQ", raw, 64, 1, 5, 0, 0, 0, 192, 192, 0x4000)
    struct.pack_into("<IIQQQQQQ", raw, 120, 4, 0, 192, 0, 0, 24, 0, 4)
    raw[188] = 0xC3
    raw[192:] = bytes(range(1, 25))
    return raw


def imported_title(module_version=0x101, library_version=1):
    raw = bytearray(4096)
    raw[:9] = b"\x7fELF\x02\x01\x01\x09\x02"
    struct.pack_into("<HH", raw, 16, 0xFE10, 62)
    struct.pack_into("<Q", raw, 32, 64)
    struct.pack_into("<HH", raw, 54, 56, 2)
    struct.pack_into("<IIQQQQQQ", raw, 64, 1, 6, 0, 0, 0,
                     len(raw), len(raw), 0x4000)
    strings = bytearray(b"\0")

    def string(value):
        offset = len(strings)
        strings.extend(value.encode() + b"\0")
        return offset

    soname = string("provider.sprx")
    module = string("provider")
    library = string("provider_exports")
    symbol = string("someNid#B#B")
    tags = [
        (1, soname), (MODULE.NEEDED_MODULE, module | module_version << 32 | 1 << 48),
        (MODULE.IMPORT_LIB, library | library_version << 32 | 1 << 48),
        (5, 0x300), (10, len(strings)), (6, 0x500), (MODULE.SYMTABSZ, 48), (0, 0),
    ]
    struct.pack_into("<IIQQQQQQ", raw, 120, 2, 6, 0x100, 0x100, 0x100,
                     len(tags) * 16, len(tags) * 16, 8)
    for index, (tag, value) in enumerate(tags):
        struct.pack_into("<QQ", raw, 0x100 + index * 16, tag, value)
    raw[0x300:0x300 + len(strings)] = strings
    struct.pack_into("<IBBHQQ", raw, 0x500 + 24, symbol, 0x12, 0, 0, 0, 0)
    return raw


PROVIDER = {
    "soname": "provider.sprx", "module": "provider", "module_version": 0x101,
    "library": "provider_exports", "library_version": 1,
}


class SharedTitleProofTest(unittest.TestCase):
    def test_shared_network_has_one_owner_and_preserves_old_sdk_support(self):
        symbol = (0, 0x12, 0, 0, 0, 0)
        symbols = {"getaddrinfo": symbol}
        core = {"link_contract": {"network": {"symbols": ["getaddrinfo", "freeaddrinfo"]}}}
        owner = {"module": "libkodi-runtime", "library": "libkodi-runtime"}
        imports = {MODULE.nid("getaddrinfo"): owner}
        self.assertEqual(MODULE.shared_network_imports(symbols, imports, core),
                         {"getaddrinfo": owner})
        self.assertEqual(MODULE.shared_network_imports(symbols, imports, {}), {})
        with self.assertRaisesRegex(ValueError, "outside the shared core"):
            MODULE.shared_network_imports(symbols, {}, core)
        with self.assertRaisesRegex(ValueError, "defines shared network service"):
            MODULE.shared_network_imports({"getaddrinfo": (0, 0x12, 0, 1, 1, 1)}, imports, core)

    def application_receipt(self, directory):
        paths = [Path(directory) / name for name in ("app.sprx", "native.elf", "linked.so")]
        for path, contents in zip(paths, (b"signed", b"native", b"linked")):
            path.write_bytes(contents)
        signed, native, linked = paths
        proof = {
            "schema": 1, "format": "ps5-private-native-kodi-app", "module": "libkodi-app",
            "process_lifetime": True, "native_unload_rejected": True,
            "exports": ["kodi_native_finalize", "kodi_native_run"],
            "callable_entries": ["kodi_native_run", "kodi_native_finalize"],
            "signed_sha256": MODULE.digest(signed), "native_sha256": MODULE.digest(native),
            "linked_sha256": MODULE.digest(linked),
            "sign_extract": {"exact_match": True, "native_sha256": MODULE.digest(native),
                             "extracted_sha256": MODULE.digest(native)},
        }
        return proof, paths

    def test_application_proof_binds_actual_artifacts_and_conversion_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            proof, paths = self.application_receipt(temporary)
            with patch.object(MODULE, "convert", return_value=b"native") as convert:
                MODULE.verify_application_proof(proof, *paths, [PROVIDER])
            convert.assert_called_once_with(
                b"linked", "libkodi-app", [PROVIDER], process_lifetime=True)

    def test_application_proof_rejects_changed_artifacts(self):
        for index in range(3):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                proof, paths = self.application_receipt(temporary)
                paths[index].write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "artifact hash changed"):
                    MODULE.verify_application_proof(proof, *paths, [PROVIDER])

    def test_application_proof_rejects_unloadable_or_unproven_profile(self):
        for field in ("process_lifetime", "native_unload_rejected"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                proof, paths = self.application_receipt(temporary)
                proof[field] = False
                with self.assertRaisesRegex(ValueError, "process-lifetime unload rejection"):
                    MODULE.verify_application_proof(proof, *paths, [PROVIDER])

    def test_application_proof_rejects_changed_conversion_or_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            proof, paths = self.application_receipt(temporary)
            with patch.object(MODULE, "convert", return_value=b"different"):
                with self.assertRaisesRegex(ValueError, "conversion changed"):
                    MODULE.verify_application_proof(proof, *paths, [PROVIDER])
            proof["sign_extract"]["exact_match"] = False
            with self.assertRaisesRegex(ValueError, "signing roundtrip changed"):
                MODULE.verify_application_proof(proof, *paths, [PROVIDER])

    def provider_receipt(self, directory):
        sdk = Path(directory)
        (sdk / "manifest.json").write_text(json.dumps({"provider_manifests": []}))
        manifest = sdk / "provider.json"
        manifest.write_text(json.dumps(PROVIDER))
        record = {
            "providers": [PROVIDER],
            "additional_provider_manifests": [
                {"path": str(manifest), "sha256": MODULE.digest(manifest)}],
        }
        return sdk, manifest, record

    def test_loads_hash_bound_additional_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk, manifest, record = self.provider_receipt(temporary)
            with patch.object(MODULE, "load_provider", return_value=PROVIDER) as load:
                self.assertEqual(MODULE.title_providers(sdk, record), [PROVIDER])
            load.assert_called_once_with(manifest)

    def test_rejects_changed_additional_provider_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk, manifest, record = self.provider_receipt(temporary)
            manifest.write_text("{}")
            with self.assertRaisesRegex(ValueError, "provider manifest changed"):
                MODULE.title_providers(sdk, record)

    def test_rejects_changed_identity_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk, manifest, record = self.provider_receipt(temporary)
            record["providers"] = []
            with patch.object(MODULE, "load_provider", return_value=PROVIDER):
                with self.assertRaisesRegex(ValueError, "identity receipt changed"):
                    MODULE.title_providers(sdk, record)

    def test_exact_provider_identity_and_versions(self):
        imports = MODULE.native_imports(imported_title(), [PROVIDER])
        self.assertEqual(imports["someNid"],
                         {"module": "provider", "library": "provider_exports"})

    def test_private_application_uses_native_shared_module_imports(self):
        raw = imported_title()
        struct.pack_into("<H", raw, 16, 0xFE18)
        imports = MODULE.native_imports(raw, [PROVIDER], executable=False)
        self.assertEqual(imports["someNid"]["module"], "provider")

    def test_rejects_truncated_module_version(self):
        with self.assertRaisesRegex(ValueError, "module identity/version mismatch"):
            MODULE.native_imports(imported_title(module_version=1), [PROVIDER])

    def test_rejects_wrong_library_version(self):
        with self.assertRaisesRegex(ValueError, "library identity/version mismatch"):
            MODULE.native_imports(imported_title(library_version=2), [PROVIDER])

    def test_rejects_wrong_published_library_name(self):
        with self.assertRaisesRegex(ValueError, "library identity/version mismatch"):
            MODULE.native_imports(imported_title(), [{**PROVIDER, "library": "provider"}])

    def test_rejects_undeclared_provider(self):
        with self.assertRaisesRegex(ValueError, "undeclared title provider"):
            MODULE.native_imports(imported_title(), [])

    def test_exact_roundtrip(self):
        raw = bytes(title())
        proof = MODULE.check_roundtrip(raw, raw)
        self.assertTrue(proof["whole_file_equal"])
        self.assertEqual(proof["omitted_nonloadable_notes"], [])

    def test_only_existing_signer_note_omission_is_accepted(self):
        source = title()
        extracted = source.copy()
        extracted[192:] = bytes(24)
        proof = MODULE.check_roundtrip(source, extracted)
        self.assertFalse(proof["whole_file_equal"])
        self.assertTrue(proof["all_loadable_bytes_equal"])
        self.assertEqual(proof["omitted_nonloadable_notes"], [{"offset": 192, "size": 24}])

    def test_loaded_byte_change_fails(self):
        source = title()
        extracted = source.copy()
        extracted[188] = 0
        with self.assertRaisesRegex(ValueError, "differs outside"):
            MODULE.check_roundtrip(source, extracted)

    def test_nonzero_note_change_fails(self):
        source = title()
        extracted = source.copy()
        extracted[192] = 7
        with self.assertRaisesRegex(ValueError, "note transformation"):
            MODULE.check_roundtrip(source, extracted)

    def test_changed_length_fails(self):
        with self.assertRaisesRegex(ValueError, "ELF size"):
            MODULE.check_roundtrip(title(), title()[:-1])

    def test_completed_local_title(self):
        if not os.environ.get("PS5_SHARED_TITLE_APP"):
            self.skipTest("set PS5_SHARED_TITLE_APP to a freshly packaged native title")
        app = Path(os.environ["PS5_SHARED_TITLE_APP"])
        sdk = Path(os.environ.get("PS5_NATIVE_RUNTIME_SDK", ROOT / "build/ps5-native/sdk"))
        mapping = Path(os.environ.get(
            "PS5_NATIVE_DEPENDS_MAP", ROOT / "build/ps5-native/depends/archive-map.json"))
        if not (app / "dist/PPSA99420/eboot.bin").is_file():
            self.skipTest("completed shared title required")
        proof = MODULE.verify(app, sdk, mapping)
        self.assertTrue(proof["no_duplicate_runtime_owner"])
        self.assertTrue(proof["only_own_runtimes_packaged"])
        self.assertFalse(proof["console_accepted"])
        self.assertTrue(proof["provider_identities_verified"])
        self.assertTrue(proof["loader_companion_verified"])
        self.assertTrue(proof["runtime_metadata_layout_verified"])
        self.assertEqual(proof["startup"]["model"], "native-two-stage")
        self.assertTrue(proof["startup"]["launcher_has_no_shared_imports"])
        self.assertFalse(proof["startup"]["software_relocation"])
        self.assertEqual(proof["startup"]["startup_abi"], "ps5-kodi-startup-v1")
        self.assertEqual(proof["startup"]["entry_handoff"], "module-start-arguments")
        self.assertFalse(proof["startup"]["executable_symbol_lookup"])
        self.assertTrue(proof["application"]["native_unload_rejected"])
        self.assertTrue(proof["application"]["process_lifetime_conversion_verified"])
        self.assertEqual(len(proof["dependency_archives"]), 7)


if __name__ == "__main__":
    unittest.main()
