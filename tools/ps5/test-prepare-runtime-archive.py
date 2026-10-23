#!/usr/bin/env python3
"""Host-only bounded runtime archive tests; no SDK modification or deployment.

Run: python3 tools/ps5/test-prepare-runtime-archive.py
Requires native clang-18, llvm-ar-18, llvm-objcopy-18 and ld.lld-18.
Fixtures are created under the repository, never in a system temporary directory.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
import uuid

from native_c_link import Elf, RELA, SHDR
from prepare_runtime_archive import archive_members, prepare_archive


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ("clang-18", "llvm-ar-18", "llvm-objcopy-18", "ld.lld-18")


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "native LLVM 18 required")
class PrepareArchiveTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".prepare-runtime-test-" + uuid.uuid4().hex)
        self.work.mkdir()
        self.source = self.work / "source.a"
        self.output = self.work / "prepared.a"
        self.ctor = self.compile(
            "constructor.o",
            "int initialized;\n"
            "__attribute__((constructor(101))) void ctor_fixture(void) { initialized = 7; }\n")
        self.unchanged = self.compile("unchanged.o", "int unchanged(void) { return 9; }\n")

    def tearDown(self):
        shutil.rmtree(self.work)

    def run_tool(self, *argv):
        return subprocess.run([str(a) for a in argv], cwd=self.work,
                              capture_output=True, text=True, check=True)

    def compile(self, name, code, *options):
        source = self.work / (name + ".c")
        source.write_text(code)
        output = self.work / name
        self.run_tool("clang-18", "-c", "-fPIC", "-fno-use-init-array",
                      "-fno-asynchronous-unwind-tables", *options, source, "-o", output)
        return output

    def archive(self, *members):
        self.run_tool("llvm-ar-18", "rcsD", self.source, *members)
        return self.source.read_bytes()

    def rename_ctor(self, name):
        self.run_tool("llvm-objcopy-18", "--rename-section", f".ctors.65434={name}", self.ctor)

    def reject(self, pattern):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, pattern):
            prepare_archive(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.work.glob(".prepare-runtime-*")), [])

    def patch_section(self, name, field, value):
        raw = bytearray(self.ctor.read_bytes())
        elf = Elf(raw)
        index = elf.section_names.index(name)
        header = list(elf.section_headers[index])
        header[field] = value
        shoff = struct.unpack_from("<Q", raw, 40)[0]
        SHDR.pack_into(raw, shoff + SHDR.size * index, *header)
        self.ctor.write_bytes(raw)

    def test_prepares_and_links_with_real_native_shared_script(self):
        before = self.archive(self.ctor, self.unchanged)
        report = prepare_archive(self.source, self.output)
        json.dumps(report)
        self.assertEqual(report["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(report["prepared_sha256"],
                         hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(self.source.read_bytes(), before)
        old, _ = archive_members(before)
        members, indexed = archive_members(self.output.read_bytes())
        self.assertTrue(indexed)
        self.assertEqual(list(old), list(members))
        self.assertEqual(old["unchanged.o"], members["unchanged.o"])
        obj = Elf(members["constructor.o"])
        self.assertNotIn(".ctors.65434", obj.sections)
        section = obj.sections[".init_array.00101"]
        self.assertEqual((section[1], section[2], section[5]), (14, 3, 8))
        relocation = obj.sections[".rela.init_array.00101"]
        self.assertEqual(relocation[7], obj.section_names.index(".init_array.00101"))
        self.assertEqual(obj.section_headers[relocation[6]][1], 2)
        self.assertEqual(obj.section(".rela.init_array.00101"),
                         Elf(old["constructor.o"]).section(".rela.ctors.65434"))
        change = report["changes"][0]
        self.assertEqual((change["source_priority"], change["init_priority"]), (65434, 101))
        self.assertEqual(change["function"], "ctor_fixture")
        self.assertTrue(report["commands"])
        linked = self.work / "linked.so"
        # No --whole-archive: successful extraction also exercises the rebuilt index.
        self.run_tool("ld.lld-18", "-shared", "-Bsymbolic", "-u", "ctor_fixture",
                      "--hash-style=sysv", "-T", ROOT / "tools/ps5/native-shared.ld",
                      self.output, "-o", linked)
        elf = Elf(linked.read_bytes())
        init = elf.sections[".init_array"]
        self.assertEqual((init[1], init[2], init[5]), (14, 3, 8))
        self.assertFalse(any(".ctors" in n for n in elf.section_names))
        function = dict(elf.symbols(".symtab"))["ctor_fixture"][4]
        relas = [r for r in RELA.iter_unpack(elf.section(".rela.dyn")) if r[0] == init[3]]
        self.assertEqual(len(relas), 1)
        self.assertEqual(relas[0][1:], (8, function))

    def test_priority_transform_boundaries_and_sdk_priority(self):
        original = self.ctor.read_bytes()
        for priority in (0, 1, 65435, 65535):
            with self.subTest(priority=priority):
                self.ctor.write_bytes(original)
                self.rename_ctor(f".ctors.{priority}")
                if self.source.exists():
                    self.source.unlink()
                self.archive(self.ctor)
                report = prepare_archive(self.source, self.output)
                change = report["changes"][0]
                self.assertEqual(change["init_priority"], 65535 - priority)
                members, _ = archive_members(self.output.read_bytes())
                self.assertIn(f".init_array.{65535 - priority:05d}",
                              Elf(members["constructor.o"]).sections)
                self.output.unlink()

    def test_no_legacy_verified_copy(self):
        before = self.archive(self.unchanged)
        report = prepare_archive(self.source, self.output)
        self.assertEqual(report["mode"], "verified-copy")
        self.assertEqual(report["changes"], [])
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), before)

    def test_section_symbol_resolves_to_defined_function(self):
        obj = self.compile(
            "local.o", "int state;\n"
            "__attribute__((constructor(100))) static void ctor_local(void) { state = 1; }\n")
        elf = Elf(obj.read_bytes())
        relocation = elf.sections[".rela.ctors.65435"]
        _, info, _ = RELA.unpack(elf.section(".rela.ctors.65435"))
        symtab = elf.section_headers[relocation[6]]
        symbol_offset = symtab[4] + (info >> 32) * 24
        self.assertEqual(elf.data[symbol_offset + 4] & 15, 3)
        self.archive(obj)
        report = prepare_archive(self.source, self.output)
        self.assertEqual(report["changes"][0]["function"], "ctor_local")

    def test_duplicate_comdat_section_names_and_staging_name_collision(self):
        self.output = self.work / "result.a"
        obj = self.compile(
            "prepared.a",
            "template<class T> int identity(T) { return 1; }\n"
            "template int identity<int>(int);\n"
            "template int identity<long>(long);\n", "-x", "c++")
        elf = Elf(obj.read_bytes())
        self.assertGreater(elf.section_names.count(".group"), 1)
        self.archive(self.ctor, obj)
        prepare_archive(self.source, self.output)
        members, _ = archive_members(self.output.read_bytes())
        self.assertEqual(members["prepared.a"], obj.read_bytes())

    def test_malformed_and_plain_priorities(self):
        original = self.ctor.read_bytes()
        for name in (".ctors", ".ctors.foo", ".ctors.-1", ".ctors.65536", ".ctors.123456"):
            with self.subTest(name=name):
                self.ctor.write_bytes(original)
                self.rename_ctor(name)
                if self.source.exists():
                    self.source.unlink()
                self.archive(self.ctor)
                self.reject("numeric|priority")

    def test_multiple_across_members(self):
        other = self.compile("other.o",
                             "__attribute__((constructor(102))) void second(void) {}\n")
        self.archive(self.ctor, other)
        self.reject("multiple legacy constructor pointers")

    def test_multiple_within_section(self):
        obj = self.compile("many.o", "__attribute__((constructor(101))) void a(void) {}\n"
                           "__attribute__((constructor(101))) void b(void) {}\n")
        self.archive(obj)
        self.reject("one writable.*8-byte")

    def test_multiple_sections(self):
        obj = self.compile("many.o", "__attribute__((constructor(101))) void a(void) {}\n"
                           "__attribute__((constructor(102))) void b(void) {}\n")
        self.archive(obj)
        self.reject("multiple legacy constructor sections")

    def test_sentinel_rejected(self):
        raw = bytearray(self.ctor.read_bytes())
        offset = Elf(raw).sections[".ctors.65434"][4]
        raw[offset:offset + 8] = b"\xff" * 8
        self.ctor.write_bytes(raw)
        self.archive(self.ctor)
        self.reject("sentinels")

    def test_bad_relocation_type_offset_and_undefined_target(self):
        original = self.ctor.read_bytes()
        for offset, kind, undefined in ((1, 1, False), (0, 2, False), (0, 1, True)):
            with self.subTest(offset=offset, kind=kind, undefined=undefined):
                raw = bytearray(original)
                elf = Elf(raw)
                section = elf.sections[".rela.ctors.65434"]
                _, info, addend = RELA.unpack(elf.section(".rela.ctors.65434"))
                RELA.pack_into(raw, section[4], offset, (info & ~0xffffffff) | kind, addend)
                if undefined:
                    table = elf.section_headers[section[6]]
                    struct.pack_into("<H", raw, table[4] + (info >> 32) * 24 + 6, 0)
                self.ctor.write_bytes(raw)
                if self.source.exists():
                    self.source.unlink()
                self.archive(self.ctor)
                self.reject("R_X86_64_64|defined function")

    def test_dtors_and_native_tls_rejected(self):
        for name, code, pattern in (
                ("tls.o", "__thread int state;\n", "native TLS"),
                ("dtor.o", "__attribute__((destructor(101))) void dtor(void) {}\n",
                 "legacy destructors")):
            with self.subTest(name=name):
                obj = self.compile(name, code)
                if self.source.exists():
                    self.source.unlink()
                self.archive(self.ctor, obj)
                self.reject(pattern)

    def test_duplicate_member_names(self):
        self.archive(self.ctor)
        self.run_tool("llvm-ar-18", "q", self.source, self.ctor)
        self.reject("duplicate archive member")

    def test_thin_archive(self):
        self.run_tool("llvm-ar-18", "rcsT", self.source, self.ctor)
        self.reject("thin archives")

    def test_unsafe_member_path(self):
        raw = bytearray(self.archive(self.ctor))
        offset = raw.index(b"constructor.o/")
        raw[offset:offset + 16] = b"../evil.o/".ljust(16)
        self.source.write_bytes(raw)
        self.reject("unsafe/unsupported")

    def test_preexisting_output_and_source_as_output(self):
        self.archive(self.ctor)
        self.output.write_bytes(b"do not overwrite")
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare_archive(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"do not overwrite")
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare_archive(self.source, self.source)

    def test_tool_failure_never_publishes(self):
        before = self.archive(self.ctor)
        with self.assertRaises(OSError):
            prepare_archive(self.source, self.output, objcopy="missing-native-objcopy")
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.work.glob(".prepare-runtime-*")), [])
        with self.assertRaisesRegex(ValueError, "command failed"):
            prepare_archive(self.source, self.output, objcopy="/bin/false")
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.work.glob(".prepare-runtime-*")), [])

    def test_cli_json_and_unsupported_exit(self):
        self.archive(self.ctor)
        helper = ROOT / "tools/ps5/prepare_runtime_archive.py"
        result = self.run_tool(sys.executable, helper, "--source", self.source,
                               "--output", self.output)
        self.assertEqual(json.loads(result.stdout)["mode"], "single-legacy-constructor")
        result = subprocess.run([sys.executable, str(helper), "--source", str(self.source),
                                 "--output", str(self.output)],
                                capture_output=True, text=True, cwd=self.work)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
