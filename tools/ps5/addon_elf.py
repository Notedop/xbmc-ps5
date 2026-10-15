"""Structural preflight shared by installed PS5 add-on staging tools."""

import struct


def elf_problems(path):
    try:
        return _inspect(path.read_bytes())
    except (ValueError, struct.error, IndexError, UnicodeError) as error:
        return [f"malformed ELF: {error}"]


def _inspect(data):
    def unpack(fmt, offset):
        return struct.unpack(fmt, block(offset, struct.calcsize(fmt)))

    def block(offset, size):
        if offset < 0 or size < 0 or offset + size > len(data):
            raise ValueError("table outside file")
        return data[offset:offset + size]

    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        return ["not a little-endian ELF64 file"]
    e_type, machine = unpack("<HH", 16)
    phoff, shoff = unpack("<QQ", 32)
    if unpack("<I", 20)[0] != 1 or unpack("<H", 52)[0] != 64:
        raise ValueError("invalid ELF header version/size")
    phsize, phnum, shsize, shnum = unpack("<HHHH", 54)
    problems = []
    if e_type != 3 or machine != 62:
        problems.append("not an x86-64 shared object")
    if not phnum or phsize != 56:
        raise ValueError("invalid program header size")
    loadable = False
    for index in range(phnum):
        header = unpack("<IIQQQQQQ", phoff + index * phsize)
        block(header[2], header[5])
        if header[0] == 1:
            loadable = True
            if header[5] > header[6]:
                raise ValueError("segment file size exceeds memory size")
        if header[0] == 7:
            problems.append("contains PT_TLS")
    if not loadable:
        raise ValueError("no loadable segments")
    if not shnum:
        return problems + ["has no section headers to inspect"]
    if shsize != 64:
        raise ValueError("invalid section header size")
    sections = [unpack("<IIQQQQIIQQ", shoff + index * shsize) for index in range(shnum)]
    for _, kind, _, _, offset, size, link, _, _, entsize in sections:
        if kind != 8:  # SHT_NOBITS
            block(offset, size)
        if kind in (4, 6, 11):
            expected = {4: 24, 6: 16, 11: 24}[kind]
            if entsize != expected or size % expected:
                raise ValueError("invalid dynamic, symbol or relocation entry size")
        if kind == 6:
            for entry in range(size // 16):
                tag, _ = unpack("<qQ", offset + entry * 16)
                if tag == 1:
                    problems.append("has DT_NEEDED dependencies")
                if tag == 0:
                    break
        elif kind == 4:
            for entry in range(size // 24):
                _, info, _ = unpack("<QQq", offset + entry * 24)
                relocation = info & 0xFFFFFFFF
                if relocation not in (1, 6, 7, 8):
                    problems.append(f"unsupported relocation type {relocation}")
                    break
        elif kind == 9:
            problems.append("unsupported SHT_REL relocations")
        elif kind == 11:
            if link >= len(sections):
                raise ValueError("invalid symbol string table")
            strings = block(sections[link][4], sections[link][5])
            for entry in range(size // 24):
                name, info, _, section, _, _ = unpack("<IBBHQQ", offset + entry * 24)
                if name >= len(strings) or b"\0" not in strings[name:]:
                    raise ValueError("invalid symbol name")
                symbol = strings[name:].split(b"\0", 1)[0].decode("ascii")
                if section == 0 and (symbol.startswith("__emutls_") or
                                     symbol in ("__tls_get_addr", "__cxa_thread_atexit",
                                                "__cxa_thread_atexit_impl", "atexit")):
                    problems.append(f"unsupported runtime import {symbol}")
                if info & 15 in (6, 10):
                    problems.append("unsupported TLS/IFUNC symbol")
    return list(dict.fromkeys(problems))
