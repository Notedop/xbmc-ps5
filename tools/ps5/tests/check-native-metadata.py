#!/usr/bin/env python3
"""Check preserved LLVM bytes and independent Sony relocation indices."""
from pathlib import Path
import struct
import sys


class Elf:
    def __init__(self, path):
        self.data = Path(path).read_bytes()
        header = struct.unpack_from("<16sHHIQQQIHHHHHH", self.data)
        self.phdr = [struct.unpack_from("<IIQQQQQQ", self.data, header[5] + i * header[9])
                     for i in range(header[10])]
        sections = [struct.unpack_from("<IIQQQQIIQQ", self.data, header[6] + i * header[11])
                    for i in range(header[12])]
        self.sections = {}
        if sections:
            names = sections[header[13]]
            strings = self.data[names[4]:names[4] + names[5]]
            for section in sections:
                name = strings[section[0]:].split(b"\0", 1)[0].decode()
                self.sections[name] = section

    def memory(self, address, size):
        for kind, _, offset, vaddr, _, filesz, _, _ in self.phdr:
            if kind == 1 and address >= vaddr and address + size <= vaddr + filesz:
                return self.data[offset + address - vaddr:offset + address - vaddr + size]
        raise AssertionError(f"unmapped metadata {address:#x}+{size:#x}")

    def section(self, name):
        section = self.sections[name]
        return self.data[section[4]:section[4] + section[5]]


source, converted = map(Elf, sys.argv[1:3])
for name in (".dynsym", ".dynstr", ".dynamic", ".rela.dyn", ".rela.plt", ".gnu.hash"):
    if name in source.sections:
        section = source.sections[name]
        assert converted.memory(section[3], section[5]) == source.section(name), name

dynamic = next(ph for ph in converted.phdr if ph[0] == 2)
tags = dict(struct.iter_unpack("<qQ", converted.data[dynamic[2]:dynamic[2] + dynamic[5]]))
sony_symbols = list(struct.iter_unpack("<IBBHQQ", converted.memory(tags[6], tags[0x6100003f])))
assert all(symbol[3] == 0 for symbol in sony_symbols), "Sony symbols must be imports only"
symbols = list(struct.iter_unpack("<IBBHQQ", source.section(".dynsym")))
relocations = {}
for pointer, size in ((7, 8), (23, 2)):
    for offset, info, addend in struct.iter_unpack("<QQq", converted.memory(tags[pointer], tags[size])):
        assert info >> 32 < len(sony_symbols), "invalid compact Sony symbol index"
        relocations[offset] = (info, addend)
for name in (".rela.dyn", ".rela.plt"):
    if name not in source.sections:
        continue
    for offset, info, addend in struct.iter_unpack("<QQq", source.section(name)):
        actual_info, actual_addend = relocations[offset]
        kind = info & 0xffffffff
        if kind == 8:
            assert (actual_info, actual_addend) == (info, addend)
        elif symbols[info >> 32][3] != 0:
            assert actual_info == 8
            assert actual_addend == symbols[info >> 32][4] + (addend if kind == 1 else 0)
        else:
            assert actual_info & 0xffffffff == kind
            assert actual_info >> 32 != 0
            assert actual_addend == addend
print("PASS: original LLVM metadata mapped byte-for-byte; Sony imports/relocations independent")
