#!/usr/bin/env python3
"""Build linker-only PS5 provider import libraries from a proven ELF pair.

Usage: native_runtime_sdk.py --linked-elf provider.so --native-elf provider.sprx
       --module provider --output sdk --lld /path/to/ld.lld

The original LLD-linked ELF supplies plain names, not an export allowlist. Every
eligible definition must match the converted provider's native export metadata,
including its address. The resulting lib/<module>.so has SONAME <module>.sprx.
It contains dummy code/data: never package or load it on the console. Only the
original native provider is a runtime candidate. manifest.json records that
distinction and binds the import library to both input files by SHA-256.
Its schema-1 module/library identities and raw symbol attributes can be passed
as a --provider manifest to native_shared_link.py.
Provider imports are proven at their original dynsym indices against declared
native library/module identities; their dependencies are recorded, not stubbed.
The bounded converter contract assigns one library/module ID pair per original
DT_NEEDED, numbered from 1 in needed order, reserving ID 0 for this provider.

No compiler, SDK, addon lists, or third-party Python packages are required.
Existing output files are never replaced. TLS, IFUNC, unusual symbol kinds,
duplicate export identities, and oversized import objects fail closed.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from native_c_link import Elf, PHDR, SHDR, SYM, hash_table, module_identity, nid, require


DYNAMIC = struct.Struct("<QQ")
MODULE_INFO = 0x61000043
EXPORT_LIB = 0x61000047
NEEDED_MODULE = 0x61000045
IMPORT_LIB = 0x61000049
SYMTABSZ = 0x6100003F
HASHSZ = 0x6100003D
MAX_STUB_BYTES = 64 * 1024 * 1024
ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def string_at(strings, offset):
    require(0 <= offset < len(strings), "string offset outside native string table")
    end = strings.find(b"\0", offset)
    require(end >= 0, "unterminated native string")
    return strings[offset:end].decode("utf-8")


def eligible_symbols(symbols, context):
    result = {}
    for name, symbol in symbols:
        _, info, visibility, section, _, _ = symbol
        if section == 0 or info >> 4 not in (1, 2) or visibility & 3 not in (0, 3):
            continue
        require(name and "\0" not in name, f"{context}: empty/invalid export name")
        require(name not in result, f"{context}: duplicate export {name}")
        require(info & 15 != 6, f"{context}: TLS export unsupported: {name}")
        require(info & 15 in (0, 1, 2), f"{context}: unsupported symbol type: {name}")
        require(visibility in (0, 3), f"{context}: unsupported symbol visibility: {name}")
        result[name] = symbol
    require(result, f"{context}: no eligible defined exports")
    return result


class NativeImage:
    """Read native program headers and dynamic data without using section tables."""

    def __init__(self, raw, *, executable=False):
        self.raw = raw
        require(len(raw) >= 64 and raw[:7] == b"\x7fELF\x02\x01\x01",
                "native provider must be little-endian ELF64")
        require(raw[7:9] == b"\x09\x02" and
                struct.unpack_from("<HH", raw, 16) == (0xFE10 if executable else 0xFE18, 62),
                "native image must be a PS5 x86-64 " +
                ("executable" if executable else "shared module"))
        phoff = struct.unpack_from("<Q", raw, 32)[0]
        phsize, phcount = struct.unpack_from("<HH", raw, 54)
        require(phsize == PHDR.size and phcount > 0 and
                phoff + phsize * phcount <= len(raw), "invalid native program headers")
        self.phdrs = [PHDR.unpack_from(raw, phoff + i * phsize) for i in range(phcount)]
        for header in self.phdrs:
            require(header[2] + header[5] <= len(raw), "truncated native segment")
            if header[0] == 1:
                require(header[5] <= header[6], "invalid native LOAD size")
        dynamics = [p for p in self.phdrs if p[0] == 2]
        require(len(dynamics) == 1, "expected exactly one native PT_DYNAMIC")
        dynamic = dynamics[0]
        require(dynamic[5] and dynamic[5] % DYNAMIC.size == 0,
                "invalid native PT_DYNAMIC size")
        data = raw[dynamic[2]:dynamic[2] + dynamic[5]]
        self.tags = {}
        terminated = False
        for tag, value in DYNAMIC.iter_unpack(data):
            if tag == 0:
                terminated = True
                break
            self.tags.setdefault(tag, []).append(value)
        require(terminated, "unterminated native PT_DYNAMIC")

    def tag(self, tag):
        values = self.tags.get(tag, [])
        require(len(values) == 1, f"expected exactly one native dynamic tag {tag:#x}")
        return values[0]

    def validate_metadata_layout(self):
        metadata = [p for p in self.phdrs if p[0] == 1 and p[1] == 0]
        require(len(metadata) == 1, "expected exactly one native metadata LOAD")
        metadata = metadata[0]
        dynamic = next(p for p in self.phdrs if p[0] == 2)
        require(self.phdrs.index(metadata) < self.phdrs.index(dynamic),
                "native metadata LOAD must precede PT_DYNAMIC")
        offset = dynamic[2] - metadata[2]
        require(offset >= 0 and offset + dynamic[5] <= metadata[5] and
                dynamic[3] == metadata[3] + offset,
                "native PT_DYNAMIC must be contained in the metadata LOAD with matching file/VA offsets")
        for address_tag, size_tag in ((5, 10), (6, SYMTABSZ), (4, HASHSZ), (7, 8), (23, 2)):
            if address_tag not in self.tags or size_tag not in self.tags:
                continue
            size = self.tag(size_tag)
            if size:
                address = self.tag(address_tag)
                require(metadata[3] <= address and address + size <= metadata[3] + metadata[5],
                        f"native dynamic table {address_tag:#x} is outside the metadata LOAD")

    def memory(self, address, size):
        matches = [p for p in self.phdrs if p[0] == 1 and
                   p[3] <= address and address + size <= p[3] + p[5]]
        require(len(matches) == 1, f"unmapped/ambiguous native metadata {address:#x}+{size:#x}")
        p = matches[0]
        offset = p[2] + address - p[3]
        return self.raw[offset:offset + size]


def decode_id(encoded):
    require(encoded and (len(encoded) == 1 or encoded[0] != "A"),
            f"invalid native symbol ID: {encoded}")
    value = 0
    for char in encoded:
        require(char in ID_ALPHABET, f"invalid native symbol ID: {encoded}")
        value = value * 64 + ID_ALPHABET.index(char)
    require(value <= 0xFFFF, f"native symbol ID exceeds 16 bits: {encoded}")
    return value


def import_identities(native, strings, tag, own_name):
    identities = {}
    names = {own_name}
    for value in native.tags.get(tag, []):
        identity = value >> 48
        name = string_at(strings, value & 0xFFFFFFFF)
        version = (value >> 32) & 0xFFFF
        require(identity != 0 and identity not in identities and name and name not in names,
                f"duplicate/conflicting native dependency identity for tag {tag:#x}")
        identities[identity] = {"id": identity, "name": name, "version": version}
        names.add(name)
    return identities


def prove_provider(linked_raw, native_raw, module, *, include_dependencies=False):
    module_identity(module)
    linked = Elf(linked_raw)
    require(linked.kind == 3, "linked provider must be an original ET_DYN ELF")
    require(".dynsym" in linked.sections and ".dynstr" in linked.sections,
            "linked provider is missing .dynsym/.dynstr")
    require(linked.sections[".dynsym"][9] == SYM.size,
            "invalid linked dynamic symbol entry size")
    source_symbols = linked.symbols(".dynsym")
    require(source_symbols and source_symbols[0] == ("", (0, 0, 0, 0, 0, 0)),
            "linked null symbol is not empty")
    original = eligible_symbols(source_symbols, "linked provider")
    linked_strings = linked.section(".dynstr")
    linked_dynamic = linked.section(".dynamic")
    require(linked_dynamic and len(linked_dynamic) % DYNAMIC.size == 0,
            "invalid linked dynamic table")
    needed = []
    for tag, value in DYNAMIC.iter_unpack(linked_dynamic):
        if tag == 0:
            break
        if tag == 1:
            soname = string_at(linked_strings, value)
            require(soname and soname not in needed, "duplicate/empty linked DT_NEEDED")
            needed.append(soname)
    native = NativeImage(native_raw)
    native.validate_metadata_layout()
    strings = native.memory(native.tag(5), native.tag(10))
    for tag, version, label in ((MODULE_INFO, 0x101, "module"),
                                (EXPORT_LIB, 1, "export library")):
        value = native.tag(tag)
        require(value >> 32 == version and string_at(strings, value & 0xFFFFFFFF) == module,
                f"native {label} identity/version/id mismatch: expected {module}, "
                f"version {version:#x}, id 0")
    if 0x61000041 in native.tags:
        require(string_at(strings, native.tag(0x61000041)) == module + ".sprx",
                "native provider filename identity mismatch")
    require(native.tag(11) == SYM.size, "invalid native symbol entry size")
    size = native.tag(SYMTABSZ)
    require(size >= SYM.size and size % SYM.size == 0, "invalid native symbol table size")
    symbols = list(SYM.iter_unpack(native.memory(native.tag(6), size)))
    require(symbols[0] == (0, 0, 0, 0, 0, 0), "native null symbol is not empty")
    named = [(string_at(strings, s[0]), s) for s in symbols]
    exports = eligible_symbols(named, "native provider")
    libraries = import_identities(native, strings, IMPORT_LIB, module)
    modules = import_identities(native, strings, NEEDED_MODULE, module)
    require(set(modules) == set(range(1, len(needed) + 1)),
            "native dependency modules do not match original DT_NEEDED indices")
    require(set(libraries) == set(modules),
            "native dependency libraries do not match original DT_NEEDED indices")
    for identity, declaration in modules.items():
        declaration["soname"] = needed[identity - 1]
    expected = {}
    for name, symbol in original.items():
        encoded = nid(name) + "#A#A"
        require(encoded not in expected, f"colliding native export NID: {name}")
        expected[encoded] = name
        require(encoded in exports, f"native provider missing export NID for {name}: {encoded}")
        actual = exports[encoded]
        require(actual[1:3] == symbol[1:3] and actual[4:] == symbol[4:],
                f"native export type/binding/visibility/value/size mismatch: {name}")
    require(set(exports) == set(expected),
            "native provider has unmatched exports: " + ", ".join(sorted(set(exports) - set(expected))))
    require(len(named) == len(source_symbols), "native/source dynsym count mismatch")
    # Native hashes include both imports and exports in the source dynsym order.
    canonical = [""]
    imports = []
    for index, ((source_name, source), (name, symbol)) in enumerate(
            zip(source_symbols[1:], named[1:]), 1):
        require(symbol[1:3] == source[1:3] and symbol[4:] == source[4:] and
                bool(symbol[3]) == bool(source[3]),
                f"native/source dynsym attributes mismatch at index {index}: {source_name}")
        if source[3]:
            require(source_name in original and name == nid(source_name) + "#A#A",
                    f"native/source export index/NID mismatch at index {index}: {source_name}")
            canonical.append(nid(source_name) + "#" + module + "#" + module)
            continue
        require(source_name and source[1] >> 4 in (1, 2) and source[2] in (0, 3),
                f"unsupported original undefined symbol at index {index}")
        require(source[1] & 15 != 6, f"TLS import unsupported: {source_name}")
        require(source[1] & 15 in (0, 1, 2), f"unsupported import symbol type: {source_name}")
        parts = name.split("#")
        require(len(parts) == 3 and parts[0] == nid(source_name),
                f"native/source import index/NID mismatch at index {index}: {source_name}")
        library_id, module_id = decode_id(parts[1]), decode_id(parts[2])
        require(module_id != 0 and module_id in modules and library_id in libraries and
                library_id == module_id,
                f"native import has undeclared/mismatched library/module ID: {source_name}")
        library, dependency = libraries[library_id], modules[module_id]
        canonical.append(parts[0] + "#" + library["name"] + "#" + dependency["name"])
        imports.append({"name": source_name, "dynsym_index": index, "nid": parts[0],
                        "native_name": name, "library_id": library_id, "module_id": module_id,
                        "info": source[1], "other": source[2], "size": source[5],
                        "value": source[4]})
    hashed = hash_table(canonical)
    require(native.tag(HASHSZ) == len(hashed) and
            native.memory(native.tag(4), len(hashed)) == hashed,
            "native symbol hash does not match canonical NID/library/module identities")
    dependencies = {
        "needed": needed,
        "modules": [modules[key] for key in sorted(modules)],
        "libraries": [libraries[key] for key in sorted(libraries)],
        "imports": imports,
    }
    return (original, dependencies) if include_dependencies else original


def import_object(symbols):
    """Emit an ordinary relocatable ELF for LLD, retaining exact export attributes."""
    text = bytearray()
    data_size = 0
    strings = bytearray(b"\0")
    symtab = bytearray(SYM.size)
    for name, symbol in symbols.items():
        _, info, visibility, _, _, size = symbol
        allocation = max(size, 1)
        require(len(text) + data_size + allocation <= MAX_STUB_BYTES,
                "import stub exceeds 64 MiB allocation bound")
        if info & 15 == 2:
            value, section = len(text), 1
            text.extend(b"\xc3" + bytes(allocation - 1))
        else:
            value, section = data_size, 2
            data_size += allocation
        name_offset = len(strings)
        strings.extend(name.encode("utf-8") + b"\0")
        symtab.extend(SYM.pack(name_offset, info, visibility, section, value, size))
    section_names = b"\0.text\0.bss\0.symtab\0.strtab\0.shstrtab\0.note.GNU-stack\0"
    raw = bytearray(64)
    headers = [(0,) * 10]

    def section(name, kind, flags, blob, size=None, link=0, info=0, alignment=1, entsize=0):
        raw.extend(bytes((-len(raw)) % alignment))
        offset = len(raw)
        raw.extend(blob)
        headers.append((section_names.index(name.encode() + b"\0"), kind, flags, 0,
                        offset, len(blob) if size is None else size, link, info, alignment, entsize))

    section(".text", 1, 6, text, alignment=16)
    section(".bss", 8, 3, b"", size=data_size, alignment=16)
    section(".symtab", 2, 0, symtab, link=4, info=1, alignment=8, entsize=SYM.size)
    section(".strtab", 3, 0, strings)
    section(".shstrtab", 3, 0, section_names)
    section(".note.GNU-stack", 1, 0, b"")
    raw.extend(bytes((-len(raw)) % 8))
    shoff = len(raw)
    for header in headers:
        raw.extend(SHDR.pack(*header))
    raw[:64] = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01\x01" + bytes(9),
                           1, 62, 1, 0, 0, shoff, 0, 64, 0, 0, SHDR.size, len(headers), 5)
    return bytes(raw)


def validate_stub(raw, symbols, module):
    image = Elf(raw)
    require(image.kind == 3, "LLD did not produce ET_DYN import library")
    actual = eligible_symbols(image.symbols(".dynsym"), "import library")
    require(set(actual) == set(symbols), "LLD changed import-library export names")
    for name, symbol in symbols.items():
        require(actual[name][1:3] == symbol[1:3] and actual[name][5] == symbol[5],
                f"LLD changed import-library symbol attributes: {name}")
    dynamic = image.section(".dynamic")
    strings = image.section(".dynstr")
    sonames = [value for tag, value in DYNAMIC.iter_unpack(dynamic) if tag == 14]
    require(len(sonames) == 1 and string_at(strings, sonames[0]) == module + ".sprx",
            "incorrect import-library SONAME")


def generate(linked_elf, native_elf, module, output, lld):
    linked_elf, native_elf, output = Path(linked_elf), Path(native_elf), Path(output)
    linked_raw, native_raw = linked_elf.read_bytes(), native_elf.read_bytes()
    symbols, dependencies = prove_provider(linked_raw, native_raw, module, include_dependencies=True)
    obj = import_object(symbols)
    library = output / "lib" / (module + ".so")
    manifest_path = output / "manifest.json"
    object_path = output / ("." + module + ".import.o")
    stub_path = output / ("." + module + ".import.so")
    require(not any(p.exists() or p.is_symlink()
                    for p in (library, manifest_path, object_path, stub_path)),
            "SDK output already exists; use a fresh output directory")
    library.parent.mkdir(parents=True, exist_ok=True)
    owned = []
    published = []
    try:
        for path, content in ((object_path, obj), (stub_path, b"")):
            with path.open("xb") as stream:
                owned.append(path)
                stream.write(content)
        result = subprocess.run([str(lld), "-shared", "--hash-style=sysv",
                                 "--soname=" + module + ".sprx", "-o", str(stub_path),
                                 str(object_path)], capture_output=True, text=True, check=False)
        require(result.returncode == 0, "LLD import-library link failed: " +
                (result.stderr.strip() or result.stdout.strip()))
        stub = stub_path.read_bytes()
        validate_stub(stub, symbols, module)
        manifest = {
            "schema": 1,
            "format": "ps5-native-runtime-sdk",
            "schema_version": 1,
            "module": module,
            "library": module,
            "module_version": 0x101,
            "library_version": 1,
            "soname": module + ".sprx",
            "dependencies": dependencies,
            "linker_only": True,
            "runtime_loadable": False,
            "warning": "Import libraries contain dummy code/data. Deploy only the native provider.",
            "provider": {
                "module": module, "module_id": 0, "module_version": 0x101,
                "export_library": module, "library_id": 0, "library_version": 1,
                "runtime_filename": module + ".sprx",
                "linked_elf_sha256": sha256(linked_raw),
                "native_elf_sha256": sha256(native_raw),
            },
            "import_library": {
                "path": "lib/" + module + ".so", "soname": module + ".sprx",
                "sha256": sha256(stub), "linker_only": True, "runtime_loadable": False,
            },
            "symbols": [
                {"name": name, "nid": nid(name), "native_name": nid(name) + "#A#A",
                 "canonical_name": nid(name) + "#" + module + "#" + module,
                 "info": s[1], "other": s[2], "value": s[4],
                 "binding": "weak" if s[1] >> 4 == 2 else "global",
                 "type": {0: "notype", 1: "object", 2: "function"}[s[1] & 15],
                 "visibility": "protected" if s[2] == 3 else "default",
                 "size": s[5], "provider_value": s[4]}
                for name, s in symbols.items()
            ],
        }
        for path, content in ((library, stub), (manifest_path,
                              (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())):
            with path.open("xb") as stream:
                published.append(path)
                stream.write(content)
        return manifest
    except BaseException:
        for path in reversed(published):
            path.unlink()
        raise
    finally:
        for path in owned:
            path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--linked-elf", required=True, type=Path)
    parser.add_argument("--native-elf", required=True, type=Path)
    parser.add_argument("--module", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lld", required=True, type=Path)
    parser.add_argument("--cc", type=Path, help="accepted for SDK driver compatibility; not needed")
    args = parser.parse_args()
    try:
        generate(args.linked_elf, args.native_elf, args.module, args.output, args.lld)
    except (OSError, ValueError, struct.error, UnicodeError, IndexError) as error:
        parser.exit(1, f"native-runtime-sdk: {error}\n")


if __name__ == "__main__":
    main()
