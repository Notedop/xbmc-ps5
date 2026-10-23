#!/usr/bin/env python3
"""Restricted native linker for single-object, import-free Kodi C add-ons.

SPDX-License-Identifier: GPL-3.0-or-later
Adapted from ps5-native-module-probe/native_module.py.
Metadata conventions: ps5-native-app-boilerplate, Copyright 2026 BlackBearReloaded.
"""
import argparse
import base64
import hashlib
from pathlib import Path
import re
import struct

PAGE = 0x4000
PHDR = struct.Struct("<IIQQQQQQ")
SYM = struct.Struct("<IBBHQQ")
RELA = struct.Struct("<QQq")
SHDR = struct.Struct("<IIQQQQIIQQ")
EXPORTS = {"ADDON_Create", "ADDON_GetTypeVersion", "ADDON_GetTypeMinVersion"}
EXPORT_ORDER = ("ADDON_Create", "ADDON_GetTypeVersion", "ADDON_GetTypeMinVersion")
NOTE_HEADER = struct.pack("<III4s", 4, 20, 3, b"GNU\0")
PLT_HEAD_SIZE = 16
INIT_STUB_SIZE = 47
FINI_STUB_SIZE = 8


def require(condition, message):
    if not condition:
        raise ValueError(message)


def module_identity(module):
    require(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", module) is not None,
            "module identity must be a lowercase add-on ID without path separators")
    return module


def align(value, size=8):
    return (value + size - 1) // size * size


def nid(name):
    salt = bytes.fromhex("518d64a635ded8c1e6b039b1c3e55230")
    digest = hashlib.sha1(name.encode() + salt).digest()[:8][::-1]
    return base64.b64encode(digest).decode().rstrip("=").replace("/", "-")


def hash_table(names):
    count = len(names)
    buckets, chains = [0] * count, [0] * count
    for index in range(count - 1, 0, -1):
        value = 0
        for byte in names[index].encode():
            value = (value << 4) + byte
            high = value & 0xf0000000
            value = (value ^ (high >> 24)) & ~high
        bucket = value % count
        chains[index], buckets[bucket] = buckets[bucket], index
    return struct.pack("<" + "I" * (2 + count * 2), count, count, *buckets, *chains)


def cstring(data, offset):
    return data[offset:data.index(0, offset)].decode()


class Elf:
    def __init__(self, data):
        self.data = data
        require(data[:7] == b"\x7fELF\x02\x01\x01", "expected little-endian ELF64")
        require(struct.unpack_from("<H", data, 18)[0] == 62, "expected x86-64")
        self.kind = struct.unpack_from("<H", data, 16)[0]
        phoff = struct.unpack_from("<Q", data, 32)[0]
        phsize, phcount = struct.unpack_from("<HH", data, 54)
        require(not phcount or phsize == PHDR.size, "unexpected program header size")
        self.phdrs = [list(PHDR.unpack_from(data, phoff + i * phsize)) for i in range(phcount)]
        shoff = struct.unpack_from("<Q", data, 40)[0]
        shsize, shcount, shstr = struct.unpack_from("<HHH", data, 58)
        self.sections = {}
        self.section_headers = []
        self.section_names = []
        if shcount:
            require(shsize == 64, "unexpected section header size")
            headers = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shsize)
                       for i in range(shcount)]
            self.section_headers = headers
            strings = headers[shstr]
            strings = data[strings[4]:strings[4] + strings[5]]
            self.section_names = [cstring(strings, h[0]) for h in headers]
            self.sections = dict(zip(self.section_names, headers))

    def section(self, name):
        header = self.sections.get(name)
        return b"" if header is None else self.data[header[4]:header[4] + header[5]]

    def symbols(self, table):
        strings = self.section(".dynstr" if table == ".dynsym" else ".strtab")
        return [(cstring(strings, s[0]), s) for s in SYM.iter_unpack(self.section(table))]

    def memory(self, address, size):
        for kind, _, offset, va, _, stored, _, _ in self.phdrs:
            if kind == 1 and va <= address and address + size <= va + stored:
                return self.data[offset + address - va:offset + address - va + size]
        raise ValueError(f"unmapped native metadata {address:#x}+{size:#x}")


def convert(source, module):
    module_identity(module)
    image = Elf(source)
    require(image.kind == 1, "expected original ET_REL C object; ET_DYN conversion is disabled")
    symbols = image.symbols(".symtab")
    require(symbols, "missing object symbol table")
    require(all(s[3] != 0 for _, s in symbols[1:]), "sample imports are unsupported")
    defined = {}
    for name, symbol in symbols:
        if symbol[1] >> 4 == 1:
            require(name not in defined, f"duplicate symbol {name}")
            defined[name] = symbol
    for name in (*EXPORT_ORDER, "module_start", "module_stop"):
        require(name in defined and defined[name][1] == 0x12,
                f"missing defined function {name}")

    groups = {name: [] for name in ("text", "rodata", "data", "bss")}
    expected_flags = {"text": 6, "rodata": 2, "data": 3, "bss": 3}
    for index, (name, section) in enumerate(zip(image.section_names, image.section_headers)):
        if not section[2] & 2:
            continue
        require(not section[2] & 0x400, "TLS unsupported")
        require(section[1] in (1, 8) and section[3] == 0,
                f"unsupported sample section {name}")
        group = next((g for g in groups if name == "." + g or name.startswith("." + g + ".")), None)
        require(group is not None, f"unsupported sample section {name}")
        require(section[2] & 7 == expected_flags[group] and
                (section[1] == 8) == (group == "bss"),
                f"unsupported sample section flags {name}")
        require(section[8] == 0 or section[8] & (section[8] - 1) == 0,
                f"invalid section alignment {name}")
        require(section[8] <= PAGE, f"unsupported section alignment {name}")
        if section[5]:
            groups[group].append(index)

    addresses = {}
    blobs = {}

    def place(group, base, initial=b""):
        blob = bytearray(initial)
        for index in groups[group]:
            section = image.section_headers[index]
            offset = align(base + len(blob), max(1, section[8])) - base
            addresses[index] = base + offset
            blob.extend(bytes(offset - len(blob)))
            if section[1] == 8:
                blob.extend(bytes(section[5]))
            else:
                require(section[4] + section[5] <= len(source), "truncated object section")
                blob.extend(source[section[4]:section[4] + section[5]])
        blobs[group] = blob
        return base + len(blob)

    text_end = place("text", 0, b"\xcc" * 16)
    stub_va = align(text_end, 16)
    blobs["text"].extend(bytes(stub_va + PLT_HEAD_SIZE + INIT_STUB_SIZE + FINI_STUB_SIZE - text_end))
    blobs["text"][stub_va + PLT_HEAD_SIZE] = 0xc3
    blobs["text"][stub_va + PLT_HEAD_SIZE + INIT_STUB_SIZE] = 0xc3
    ro_va = align(len(blobs["text"]), PAGE)
    ro_end = place("rodata", ro_va)
    require(ro_end > ro_va, "sample must have read-only data")
    relro_va = align(ro_end, PAGE)
    params = bytearray(0x2a8)
    struct.pack_into("<QIIIIII", params, 24, 32, 0x3c13f4bf, 3,
                     0x08050001, 0x02000009, 1, 0)
    blocks = ((0x38, 0xa8, 0x10000000e), (0xe0, 0x38, 0),
              (0x118, 0x10, 0), (0x128, 0x78, 2),
              (0x1a0, 0xc0, 3), (0x260, 0x38, 1))
    for offset, size, revision in blocks:
        struct.pack_into("<QQ", params, offset, size, revision)
    struct.pack_into("<QQ", params, 0x298, 0xffffffffffffffff, 1)
    data_va = align(relro_va + len(params), PAGE)
    data_end = place("data", data_va)
    data_size = max(8, data_end - data_va)
    data_end = place("bss", data_end)
    data_mem = max(data_size, data_end - data_va)
    require(data_mem < PAGE, "sample writable data must fit within one native page")

    def symbol_address(symbol):
        require(symbol[3] in addresses, "symbol refers to an unsupported/empty section")
        section = image.section_headers[symbol[3]]
        require(symbol[4] + symbol[5] <= section[5], "symbol outside object section")
        return addresses[symbol[3]] + symbol[4]

    # Resolve fixups only after all section addresses are final; strings are not tail-merged.
    for section in image.section_headers:
        if section[1] not in (4, 9):
            continue
        require(section[1] == 4 and section[9] == RELA.size and
                section[6] == image.section_headers.index(image.sections[".symtab"]),
                "unsupported object relocation table")
        target_index = section[7]
        require(target_index in addresses, "relocations target an unsupported section")
        target = image.section_headers[target_index]
        require(target[1] == 1, "relocations into BSS unsupported")
        group = next(g for g in groups if target_index in groups[g])
        base = {"text": 0, "rodata": ro_va, "data": data_va}[group]
        require(section[4] + section[5] <= len(source), "truncated relocation table")
        for offset, info, addend in RELA.iter_unpack(source[section[4]:section[4] + section[5]]):
            require(info & 0xffffffff in (2, 4), "only PC32/PLT32 object relocations supported")
            require(offset + 4 <= target[5] and info >> 32 < len(symbols),
                    "invalid object relocation")
            value = symbol_address(symbols[info >> 32][1]) + addend - addresses[target_index] - offset
            require(-(1 << 31) <= value < (1 << 31), "object relocation overflow")
            struct.pack_into("<i", blobs[group], addresses[target_index] - base + offset, value)

    # Preserve the working writer's complete compatibility profile, not a claimed minimum ABI.
    # In particular, two generated relocations intentionally share target +0x58.
    relocations = b"".join(RELA.pack(relro_va + target, 8, relro_va + value)
                           for target, value in ((0x50, 0x38), (0x58, 0xe0), (0x60, 0x118),
                                                 (0x68, 0x128), (0x70, 0x1a0), (0x98, 0x260),
                                                 (0x48, 0x298), (0x58, 0x2a0)))
    meta_va = align(data_va + data_mem, 16)
    metadata = bytearray()
    strings = bytearray(b"\0")

    def add_string(text):
        offset = len(strings)
        strings.extend(text.encode() + b"\0")
        return offset

    module_name = add_string(module)
    filename = add_string(module + ".sprx")
    encoded = bytearray(SYM.size)
    canonical = [""]
    for name in EXPORT_ORDER:
        symbol = defined[name]
        encoded.extend(SYM.pack(add_string(nid(name) + "#A#A"), 0x12, 0, 1,
                                symbol_address(symbol), symbol[5]))
        canonical.append(nid(name) + "#" + module + "#" + module)

    def append(blob):
        metadata.extend(bytes(align(len(metadata)) - len(metadata)))
        address = meta_va + len(metadata)
        metadata.extend(blob)
        return address

    str_va, sym_va = append(strings), append(encoded)
    rel_va = append(relocations)
    hashed = hash_table(canonical)
    hash_va = append(hashed)
    note = NOTE_HEADER + bytes(20)
    note_va = append(note)
    tags = [
        (0x61000043, module_name | 0x0101 << 32),
        (0x61000011, 0), (0x61000041, filename),
        (0x61000047, module_name | 1 << 32), (0x61000017, 1), (21, 0),
        (7, rel_va), (8, len(relocations)), (9, 24), (0x6ffffff9, len(relocations) // 24),
        (23, rel_va), (2, 0), (3, relro_va), (20, 7),
        (6, sym_va), (11, 24), (5, str_va), (10, len(strings)), (4, hash_va),
        (32, 0), (33, 0), (25, 0), (27, 0), (26, 0), (28, 0),
        (12, symbol_address(defined["module_start"])),
        (13, symbol_address(defined["module_stop"])),
        (0x6100003f, len(encoded)), (0x6100003d, len(hashed)), (0, 0),
    ]
    dynamic = b"".join(struct.pack("<QQ", *tag) for tag in tags)
    dyn_va = append(dynamic)
    struct.pack_into("<Q", params, 0, dyn_va)
    comment_text = (module + ".sprx\0").encode()
    comment = bytearray(align(12 + len(comment_text), 4))
    struct.pack_into("<4sII", comment, 0, b"PATH", len(comment) - 8, len(comment_text))
    comment[12:12 + len(comment_text)] = comment_text
    version = (bytes.fromhex("0000160008") + b"crt1:" +
               struct.pack(">IIII", 0x02000009, 1, 0x02000009, 1))
    meta_file = PAGE + meta_va
    comment_file = align(meta_file + len(metadata), 16)
    version_file = align(comment_file + len(comment), 16)
    tail_file = align(version_file + len(version), 16)
    raw = bytearray(tail_file + 24)
    headers = []

    def ph(kind, flags, offset, va, stored, memory, alignment):
        headers.append([kind, flags, offset, va, va, stored, memory, alignment])

    ph(1, 1, PAGE, 0, len(blobs["text"]), len(blobs["text"]), PAGE)
    ph(1, 4, PAGE + ro_va, ro_va, len(blobs["rodata"]), len(blobs["rodata"]), PAGE)
    ph(1, 6, PAGE + relro_va, relro_va, len(params), len(params), PAGE)
    ph(0x6474e552, 4, PAGE + relro_va, relro_va, len(params), align(len(params), PAGE), 1)
    ph(1, 6, PAGE + data_va, data_va, data_size, data_mem, PAGE)
    ph(0x61000002, 4, PAGE + relro_va + 24, relro_va + 24, 32, 32, 8)
    ph(2, 6, PAGE + dyn_va, dyn_va, len(dynamic), len(dynamic), 8)
    ph(7, 4, PAGE + relro_va, relro_va, 0, 0, 1)
    ph(1, 0, meta_file, meta_va, len(metadata), len(metadata), PAGE)
    ph(0x6fffff00, 0, comment_file, 0, len(comment), 0, 16)
    ph(0x6fffff01, 0, version_file, 0, len(version), len(version), 1)
    ph(4, 0, PAGE + note_va, note_va, len(note), len(note), 4)
    ph(4, 0, tail_file, 0, 24, 0, 4)
    for offset, blob in ((PAGE, blobs["text"]), (PAGE + ro_va, blobs["rodata"]),
                         (PAGE + relro_va, params), (PAGE + data_va, blobs["data"]),
                         (meta_file, metadata), (comment_file, comment), (version_file, version)):
        raw[offset:offset + len(blob)] = blob
    ident = b"\x7fELF\x02\x01\x01\x09\x02" + bytes(7)
    raw[:64] = struct.pack("<16sHHIQQQIHHHHHH", ident, 0xfe18, 62, 1,
                           0, 64, 0, 0, 64, 56, len(headers), 64, 0, 0)
    for index, header in enumerate(headers):
        PHDR.pack_into(raw, 64 + index * PHDR.size, *header)
    raw[PAGE + note_va + 16:PAGE + note_va + 32] = hashlib.sha1(raw).digest()[:16]
    validate(bytes(raw), module)
    return raw


def validate(raw, module):
    module_identity(module)
    image = Elf(raw)
    require(image.kind == 0xfe18 and raw[7:9] == b"\x09\x02", "incorrect native ELF identity")
    loads = [p for p in image.phdrs if p[0] == 1]
    require(not any(p[1] & 5 == 5 for p in loads),
            "native LOAD must not combine read and execute")
    require([p[1] for p in loads] == [1, 4, 6, 6, 0],
            "unexpected native text/ro/relro/data/metadata permissions")
    relro, data, metadata = loads[2:]
    require([p[0] for p in image.phdrs] ==
            [1, 1, 1, 0x6474e552, 1, 0x61000002, 2, 7, 1, 0x6fffff00, 0x6fffff01, 4, 4],
            "unexpected native program-header profile")
    protection = image.phdrs[3]
    require(protection == [0x6474e552, 4, *relro[2:6], align(relro[6], PAGE), 1] and
            data[3] == align(relro[3] + relro[6], PAGE),
            "invalid native RELRO boundary")
    require(image.phdrs[7] == [7, 4, relro[2], relro[3], relro[3], 0, 0, 1],
            "invalid empty TLS header")
    parameter = image.phdrs[5]
    require(parameter == [0x61000002, 4, relro[2] + 24, relro[3] + 24,
                          relro[3] + 24, 32, 32, 8] and
            image.memory(parameter[3], 32) ==
            struct.pack("<QIIIIII", 32, 0x3c13f4bf, 3, 0x08050001, 0x02000009, 1, 0),
            "invalid module parameters")
    require(metadata[3] == align(data[3] + data[6], 16) and
            metadata[2] == data[2] + metadata[3] - data[3],
            "invalid native metadata placement")
    notes = [p for p in image.phdrs if p[0] == 4 and p[1] == 0 and p[6]]
    require(len(notes) == 1, "expected one qualifying native NOTE")
    note = notes[0]
    require(note[7] == 4 and note[5] == note[6] == len(NOTE_HEADER) + 20 and
            metadata[3] <= note[3] and note[3] + note[5] <= metadata[3] + metadata[5] and
            note[2] == metadata[2] + note[3] - metadata[3],
            "invalid native NOTE placement")
    require(image.memory(note[3], note[5]).startswith(NOTE_HEADER),
            "invalid native build-id NOTE")
    require(image.memory(note[3] + 32, 4) == bytes(4),
            "invalid native build-id padding")
    dynamic = next(p for p in image.phdrs if p[0] == 2)
    tags = dict(struct.iter_unpack("<QQ", raw[dynamic[2]:dynamic[2] + dynamic[5]]))
    require(not any(tag in tags for tag in (1, 0x61000045, 0x61000049)), "unexpected import")
    got = tags.get(3, 0)
    require(got and got % 8 == 0 and any(
            p[1] == 6 and p[3] <= got and got + 32 <= p[3] + p[5] and
            p[2] + got - p[3] + 32 <= len(raw) for p in loads),
            "invalid native reserved GOT placement")
    require(got == relro[3] and
            image.memory(got, 32) == struct.pack("<4Q", dynamic[3], 0, 0, 32),
            "invalid native reserved GOT contents")
    strings = image.memory(tags[5], tags[10])
    symbols = list(SYM.iter_unpack(image.memory(tags[6], tags[0x6100003f])))
    require(len(symbols) == 4, "incorrect native symbol count")
    require({cstring(strings, s[0]) for s in symbols[1:]} ==
            {nid(name) + "#A#A" for name in EXPORTS}, "incorrect native export identities")
    require(all(s[3] == 1 and s[1] == 0x12 and
                loads[0][3] < s[4] and s[4] + s[5] <= loads[0][3] + loads[0][5]
                for s in symbols[1:]), "invalid native export type/address")
    canonical = [""] + [cstring(strings, s[0]).split("#")[0] + "#" + module + "#" + module
                         for s in symbols[1:]]
    require(image.memory(tags[4], tags[0x6100003d]) == hash_table(canonical),
            "incorrect native export hash")
    for tag, version, ident in ((0x61000043, 0x0101, 0), (0x61000047, 1, 0)):
        value = tags[tag]
        require(cstring(strings, value & 0xffffffff) == module and
                value >> 48 == ident and (value >> 32) & 0xffff == version,
                "incorrect native module/library identity")
    for tag in (12, 13):
        require(tags[tag] and any(p[0] == 1 and p[1] & 1 and
                p[3] <= tags[tag] < p[3] + p[5] for p in image.phdrs), "invalid native start/stop")
    for target, info, _ in RELA.iter_unpack(image.memory(tags[7], tags[8])):
        require(info == 8 and any(p[0] == 1 and p[1] & 2 and p[3] <= target and
                target + 8 <= p[3] + p[6] for p in image.phdrs), "invalid native relocation")
    require(tags[23] == tags[7] and tags[2] == 0 and tags[8] == 8 * RELA.size and
            tags[0x6ffffff9] == 8 and tags[20] == 7 and tags[9] == tags[11] == 24,
            "invalid native relocation profile")
    for p in (p for p in image.phdrs if p[0] == 1):
        require(p[7] == PAGE and p[2] % PAGE == p[3] % PAGE and p[5] <= p[6] and
                p[2] + p[5] <= len(raw), "invalid native LOAD geometry")
    return tags


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--module", required=True)
    args = parser.parse_args()
    args.output.write_bytes(convert(args.input.read_bytes(), args.module))
    print("PASS: restricted reference-compatible native C link; console acceptance NOT established")
