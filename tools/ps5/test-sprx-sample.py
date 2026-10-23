#!/usr/bin/env python3
"""Offline native C-link checks; optional complete comparison with working H006."""
import argparse
import hashlib
from functools import partial
import os
from pathlib import Path
import shutil
import struct
import runpy
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

import native_c_link
from native_c_link import Elf, NOTE_HEADER, PAGE, PHDR, RELA, SHDR, SYM, align

MODULE = "screensaver.ps5.sprx-test"
convert = partial(native_c_link.convert, module=MODULE)
validate = partial(native_c_link.validate, module=MODULE)

ROOT = Path(__file__).resolve().parents[2]
args = argparse.Namespace(build=ROOT / "build/ps5-sprx-sample", reference=None)
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("--reference", type=Path, help="working H006 self-ready.elf")
    args = parser.parse_args()


def with_header(raw, index, field, value):
    altered = bytearray(raw)
    offset = struct.unpack_from("<Q", raw, 32)[0] + index * PHDR.size
    header = list(PHDR.unpack_from(raw, offset))
    header[field] = value
    PHDR.pack_into(altered, offset, *header)
    return altered


def with_section(raw, name, field, value):
    image = Elf(raw)
    altered = bytearray(raw)
    header = list(image.sections[name])
    index = image.section_headers.index(image.sections[name])
    header[field] = value
    offset = struct.unpack_from("<Q", raw, 40)[0] + index * SHDR.size
    SHDR.pack_into(altered, offset, *header)
    return altered


def with_dynamic_value(raw, tag, value):
    dynamic = next(p for p in Elf(raw).phdrs if p[0] == 2)
    altered = bytearray(raw)
    for offset in range(dynamic[2], dynamic[2] + dynamic[5], 16):
        if struct.unpack_from("<Q", raw, offset)[0] == tag:
            struct.pack_into("<Q", altered, offset + 8, value)
            return altered
    raise AssertionError(f"missing dynamic tag {tag}")


def with_initialized_data(raw, payload):
    image = Elf(raw)
    headers = [list(h) for h in image.section_headers]
    strings = image.section(".strtab")
    data = bytearray(raw)
    strings_offset = len(data)
    data.extend(strings + b".data\0")
    string_header = headers[image.section_headers.index(image.sections[".strtab"])]
    string_header[4], string_header[5] = strings_offset, len(strings) + 6
    payload_offset = len(data)
    data.extend(payload)
    headers.append([len(strings), 1, 3, 0, payload_offset, len(payload), 0, 0, 1, 0])
    section_offset = align(len(data))
    data.extend(bytes(section_offset - len(data)))
    data.extend(b"".join(SHDR.pack(*h) for h in headers))
    struct.pack_into("<Q", data, 40, section_offset)
    struct.pack_into("<H", data, 60, len(headers))
    return data


class SampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global SOURCE, NATIVE, ROUNDTRIP
        if __name__ != "__main__":
            sdk = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
            template = Path(os.environ.get("APP_TEMPLATE", ROOT /
                "build/ps5-external/ps5-opengl/build/native-app/PPSA99005"))
            if not (shutil.which("cmake") and (sdk / "bin/prospero-clang").is_file()
                    and (template / "build/host/ps5-native-tool").is_file()):
                raise unittest.SkipTest("native C sample requires the payload SDK and native tool")
            temporary = tempfile.TemporaryDirectory(prefix="native-c-sample-tests-")
            cls.addClassCleanup(temporary.cleanup)
            args.build = Path(temporary.name)
            subprocess.run(["bash", str(ROOT / "tools/ps5/build-sprx-test-addon.sh"),
                            str(args.build)], check=True, capture_output=True, text=True,
                           env=dict(os.environ, PS5_SPRX_START_TRACE_DIAGNOSTIC="0"))
        files = [args.build / name for name in ("main.o", "native.elf", "roundtrip.elf")]
        SOURCE, NATIVE, ROUNDTRIP = [path.read_bytes() for path in files]

    def test_builder_uses_manifest_identity_and_native_library(self):
        identity = runpy.run_path(str(ROOT / "tools/ps5/build-native-c-addon.py"))["manifest_identity"]
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "addon.xml"
            for attributes, expected in (
                ('library_freebsd="screensaver.native-c.sprx"', "screensaver.native-c"),
                ('library_linux="screensaver.native-c.sprx"', "screensaver.native-c"),
            ):
                manifest.write_text(f'<addon id="screensaver.native-c"><extension {attributes}/></addon>')
                self.assertEqual(identity(manifest), expected)
            for attributes in ('library_freebsd="addon.so"',
                               'library_freebsd="addon.so" library_linux="screensaver.native-c.sprx"',
                               ''):
                manifest.write_text(f'<addon id="screensaver.native-c"><extension {attributes}/></addon>')
                with self.subTest(attributes=attributes), self.assertRaisesRegex(ValueError, "native library"):
                    identity(manifest)

    def test_different_addon_identity(self):
        module = "screensaver.native-c"
        raw = native_c_link.convert(SOURCE, module)
        tags = native_c_link.validate(raw, module)
        image = Elf(raw)
        strings = image.memory(tags[5], tags[10])
        self.assertIn((module + ".sprx\0").encode(), strings)
        self.assertNotIn(MODULE.encode(), raw)
        self.assertNotEqual(raw, NATIVE)
        with self.assertRaisesRegex(ValueError, "export hash|identity"):
            native_c_link.validate(raw, MODULE)

    def test_rejects_invalid_addon_identity(self):
        for module in ("", "../addon", "/addon", "addon/name", "addon\\name", "bad#id"):
            with self.subTest(module=module), self.assertRaisesRegex(ValueError, "module identity"):
                native_c_link.convert(SOURCE, module)

    def test_reproducible_link_and_signing_roundtrip(self):
        self.assertEqual(convert(SOURCE), NATIVE)
        self.assertEqual(ROUNDTRIP, NATIVE)
        validate(NATIVE)
        validate(ROUNDTRIP)

    @unittest.skipUnless(args.reference, "pass --reference for complete H006 comparison")
    def test_complete_working_reference_parity(self):
        self.assertEqual(hashlib.sha256(SOURCE).hexdigest(),
                         "9d75768d02cb83d66791d89f2f309edfbf4bef431499aa06d50cf41e1bb7613d",
                         "H006 comparison requires its exact original C object")
        reference = args.reference.read_bytes()
        self.assertEqual(hashlib.sha256(reference).hexdigest(),
                         "d40729cd81234793d37b88682c8697b39bb8b9edfdfa626e2b79f0fa0987ca49")
        validate(reference)
        note = next(p for p in Elf(reference).phdrs if p[0] == 4 and p[6])
        fingerprint = note[2] + 16
        # SharpProspero hashes its unstripped image; this writer hashes its final image.
        self.assertEqual(NATIVE[:fingerprint], reference[:fingerprint])
        self.assertEqual(NATIVE[fingerprint + 16:], reference[fingerprint + 16:])

    def test_loads_and_relro_boundary(self):
        image = Elf(NATIVE)
        loads = [p for p in image.phdrs if p[0] == 1]
        self.assertEqual([p[1] for p in loads], [1, 4, 6, 6, 0])
        relro, data, metadata = loads[2:]
        self.assertEqual(image.phdrs[3][6], align(relro[6], PAGE))
        self.assertEqual(data[3], align(relro[3] + relro[6], PAGE))
        self.assertEqual(metadata[3], align(data[3] + data[6], 16))
        self.assertEqual(metadata[2], data[2] + metadata[3] - data[3])
        self.assertNotEqual(metadata[3] % PAGE, 0)
        self.assertEqual(image.phdrs[7][5:7], [0, 0])

    def test_build_id_is_final_image_fingerprint(self):
        note = next(p for p in Elf(NATIVE).phdrs if p[0] == 4 and p[6])
        self.assertEqual(NATIVE[note[2]:note[2] + 16], NOTE_HEADER)
        blanked = bytearray(NATIVE)
        blanked[note[2] + 16:note[2] + 32] = bytes(16)
        self.assertEqual(NATIVE[note[2] + 16:note[2] + 36],
                         hashlib.sha1(blanked).digest()[:16] + bytes(4))

    def test_reserved_got_and_compatibility_fixups(self):
        image = Elf(NATIVE)
        tags = validate(NATIVE)
        dynamic = next(p for p in image.phdrs if p[0] == 2)
        self.assertEqual(image.memory(tags[3], 32),
                         struct.pack("<4Q", dynamic[3], 0, 0, 32))
        relocs = list(RELA.iter_unpack(image.memory(tags[7], tags[8])))
        self.assertEqual(len(relocs), 8)
        self.assertEqual([t - tags[3] for t, _, _ in relocs],
                         [0x50, 0x58, 0x60, 0x68, 0x70, 0x98, 0x48, 0x58])
        self.assertEqual([v - tags[3] for _, _, v in relocs],
                         [0x38, 0xe0, 0x118, 0x128, 0x1a0, 0x260, 0x298, 0x2a0])
        self.assertTrue(all(info == 8 for _, info, _ in relocs))
        self.assertEqual((tags[23], tags[2]), (tags[7], 0))

    def test_original_c_initializers_not_generated_stubs(self):
        image = Elf(NATIVE)
        tags = validate(NATIVE)
        obj = Elf(SOURCE)
        addresses = {}
        cursor = 16
        for name, section in obj.sections.items():
            if section[2] & 4 and section[5]:
                cursor = align(cursor, section[8])
                addresses[name] = cursor
                cursor += section[5]
        self.assertEqual(tags[12], addresses[".text.module_start"])
        self.assertEqual(tags[13], addresses[".text.module_stop"])
        self.assertNotEqual(image.memory(tags[12], 1), b"\xc3")

    def test_object_content_is_linked_not_replayed(self):
        image = Elf(SOURCE)
        offset = image.sections[".rodata.str1.1"][4]
        altered = bytearray(SOURCE)
        altered[offset] ^= 1
        self.assertNotEqual(convert(altered), NATIVE)

    def test_bss_alignment_after_initialized_data(self):
        raw = convert(with_initialized_data(SOURCE, b"\x11\x22\x33"))
        image = Elf(raw)
        data = image.phdrs[4]
        self.assertEqual(data[5], 8)
        self.assertEqual(data[6], 0x21)
        self.assertEqual(image.memory(data[3], 8), b"\x11\x22\x33" + bytes(5))
        self.assertEqual(image.phdrs[8][3], align(data[3] + 0x21, 16))

    def test_rejects_page_sized_writable_data(self):
        with self.assertRaisesRegex(ValueError, "one native page"):
            convert(with_initialized_data(SOURCE, bytes(PAGE)))

    def test_rejects_linked_or_native_input(self):
        for kind in (3, 0xfe18):
            altered = bytearray(SOURCE)
            struct.pack_into("<H", altered, 16, kind)
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "original ET_REL"):
                convert(altered)

    def test_rejects_import_and_missing_entrypoint(self):
        image = Elf(SOURCE)
        symbols = image.symbols(".symtab")
        index = next(i for i, (name, _) in enumerate(symbols) if name == "ADDON_Create")
        offset = image.sections[".symtab"][4] + index * SYM.size
        altered = bytearray(SOURCE)
        struct.pack_into("<H", altered, offset + 6, 0)
        with self.assertRaisesRegex(ValueError, "imports"):
            convert(altered)
        altered = bytearray(SOURCE)
        altered[offset + 4] = 0x11
        with self.assertRaisesRegex(ValueError, "missing defined function"):
            convert(altered)

    def test_rejects_unsupported_section_flags(self):
        for flags in (7, 0x406):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "flags|TLS"):
                convert(with_section(SOURCE, ".text.GetTypeVersion", 2, flags))
        with self.assertRaisesRegex(ValueError, "alignment"):
            convert(with_section(SOURCE, ".text.GetTypeVersion", 8, 3))

    def test_rejects_absolute_relocations_and_overflow(self):
        image = Elf(SOURCE)
        table = image.sections[".rela.text.GetTypeVersion"]
        for field, value, pattern in ((1, 1, "PC32/PLT32"), (2, 1 << 40, "overflow"),
                                      (0, 0xffffffff, "invalid object relocation")):
            altered = bytearray(SOURCE)
            record = list(RELA.unpack_from(altered, table[4]))
            record[field] = value
            RELA.pack_into(altered, table[4], *record)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, pattern):
                convert(altered)

    def test_rejects_readable_native_text(self):
        for flags in (5, 7):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "read and execute"):
                validate(with_header(NATIVE, 0, 1, flags))

    def test_rejects_changed_native_data_permissions(self):
        for index in (1, 2, 4, 8):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "permissions|read and execute"):
                validate(with_header(NATIVE, index, 1, 5 if index == 8 else 0))

    def test_rejects_bad_relro_and_tls_headers(self):
        for index, field, value, message in ((3, 6, 1, "RELRO"), (7, 6, 8, "TLS")):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, message):
                validate(with_header(NATIVE, index, field, value))

    def test_rejects_wrong_metadata_placement(self):
        for field in (2, 3):
            original = Elf(NATIVE).phdrs[8][field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "metadata placement"):
                validate(with_header(NATIVE, 8, field, original + 16))

    def test_rejects_invalid_got(self):
        image = Elf(NATIVE)
        tags = validate(NATIVE)
        for value in (0, tags[3] + 1, image.phdrs[1][3]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "GOT"):
                validate(with_dynamic_value(NATIVE, 3, value))
        offset = image.phdrs[2][2]
        for word in range(3):
            altered = bytearray(NATIVE)
            altered[offset + word * 8] ^= 1
            with self.subTest(word=word), self.assertRaisesRegex(ValueError, "GOT contents"):
                validate(altered)

    def test_rejects_bad_note(self):
        for field, value in ((1, 4), (5, 35), (6, 0), (7, 8)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "NOTE"):
                validate(with_header(NATIVE, 11, field, value))

    def test_rejects_bad_export_hash_and_relocation(self):
        image = Elf(NATIVE)
        tags = validate(NATIVE)
        metadata = image.phdrs[8]
        altered = bytearray(NATIVE)
        altered[metadata[2] + tags[4] - metadata[3]] ^= 1
        with self.assertRaisesRegex(ValueError, "export hash"):
            validate(altered)
        with self.assertRaisesRegex(ValueError, "relocation profile"):
            validate(with_dynamic_value(NATIVE, 23, 0))

    def test_existing_freebsd_manifest_selection(self):
        manifest = ET.parse(args.build / MODULE / "addon.xml")
        extension = manifest.find("./extension[@point='xbmc.ui.screensaver']")
        self.assertIsNotNone(extension)
        library = extension.get("library_freebsd") or extension.get("library_linux")
        self.assertEqual(library, MODULE + ".sprx")
        self.assertTrue((args.build / MODULE / library).is_file())


if __name__ == "__main__":
    unittest.main(argv=[__file__])
