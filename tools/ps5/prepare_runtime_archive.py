#!/usr/bin/env python3
"""Prepare a private runtime archive for the modern native shared-module CRT.

Only one legacy constructor pointer in the entire archive is supported. General
.ctors reversal (within sections and across archive members) is NOT implemented.
No SDK files are edited, no startup is discarded, and this is not deployment
qualification. Requires native LLVM ar/objcopy; uses no target executables.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import uuid

from native_c_link import Elf, RELA, SYM, cstring, require


def archive_members(raw):
    """Read ordinary GNU ar members without extracting untrusted member paths."""
    require(raw[:8] != b"!<thin>\n", "thin archives are unsupported")
    require(raw[:8] == b"!<arch>\n", "expected an ordinary GNU archive")
    members = {}
    strings = b""
    offset = 8
    indexed = False
    while offset < len(raw):
        require(offset + 60 <= len(raw), "truncated archive member header")
        header = raw[offset:offset + 60]
        require(header[58:] == b"`\n", "invalid archive member header")
        size = int(header[48:58].decode("ascii").strip())
        require(size >= 0, "negative archive member size")
        start = offset + 60
        require(start + size <= len(raw), "truncated archive member")
        data = raw[start:start + size]
        name = header[:16].decode("ascii").rstrip()
        offset = start + size + (size % 2)
        require(offset <= len(raw), "missing archive member padding")
        if name in ("/", "/SYM64/"):
            indexed = True
            continue
        if name == "//":
            strings = data
            continue
        if name.startswith("/") and name[1:].isdigit():
            index = int(name[1:])
            require(index < len(strings) and (index == 0 or strings[index - 1] == 10),
                    "invalid archive long-name offset")
            end = strings.find(b"/\n", index)
            require(end >= index, "unterminated archive long name")
            name = strings[index:end].decode("ascii")
        else:
            require(name.endswith("/"), "unsupported archive member name encoding")
            name = name[:-1]
        require(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", name) is not None
                and name not in (".", ".."),
                f"unsafe/unsupported archive member name: {name!r}")
        require(name not in members, f"duplicate archive member name: {name}")
        members[name] = data
    require(members, "archive has no object members")
    return members, indexed


def constructor(raw, member):
    image = Elf(raw)
    require(image.kind == 1, f"{member}: expected ET_REL object")
    for name, section in zip(image.section_names, image.section_headers):
        require(section[1] == 8 or section[4] + section[5] <= len(raw),
                f"{member}: truncated section {name}")
        require(not section[2] & 0x400, f"{member}: native TLS is unsupported ({name})")
        require(not name.startswith((".dtors", ".rel.dtors", ".rela.dtors")),
                f"{member}: legacy destructors are unsupported ({name})")
    for name, section in zip(image.section_names, image.section_headers):
        if section[1] == 2:
            require(section[9] == SYM.size and section[5] % SYM.size == 0,
                    f"{member}: malformed symbol table {name}")
            require(all(s[1] & 15 != 6 for s in SYM.iter_unpack(
                raw[section[4]:section[4] + section[5]])),
                    f"{member}: native TLS symbols are unsupported")
    names = [name for name in image.section_names if name.startswith(".ctors")]
    legacy_relocs = [name for name in image.section_names
                     if name.startswith((".rel.ctors", ".rela.ctors"))]
    require(len(names) <= 1, f"{member}: multiple legacy constructor sections unsupported")
    if not names:
        require(not legacy_relocs, f"{member}: orphan legacy constructor relocations")
        return None
    name = names[0]
    match = re.fullmatch(r"\.ctors\.([0-9]{1,5})", name)
    require(match is not None, f"{member}: expected standard numeric .ctors.N, got {name}")
    priority = int(match[1])
    require(0 <= priority <= 65535, f"{member}: invalid constructor priority {priority}")
    section = image.sections[name]
    require(section[1] == 1 and section[2] == 3 and section[5] == 8
            and section[8] == 8 and section[9] == 0,
            f"{member}: expected one writable, allocated 8-byte PROGBITS constructor pointer")
    require(image.section(name) == bytes(8),
            f"{member}: constructor sentinels/nonzero pointer contents unsupported")
    relocation_name = ".rela" + name
    require(legacy_relocs == [relocation_name],
            f"{member}: expected one RELA constructor section")
    target_index = image.section_names.index(name)
    relocations = [s for s in image.section_headers if s[1] in (4, 9)
                   and s[7] == target_index]
    require(len(relocations) == 1, f"{member}: expected exactly one relocation table")
    relocation = relocations[0]
    require(relocation == image.sections[relocation_name] and relocation[1] == 4
            and relocation[5] == RELA.size and relocation[9] == RELA.size,
            f"{member}: expected exactly one RELA pointer")
    offset, info, addend = RELA.unpack(image.section(relocation_name))
    require(offset == 0 and info & 0xffffffff == 1,
            f"{member}: expected R_X86_64_64 at constructor offset 0")
    require(0 < relocation[6] < len(image.section_headers),
            f"{member}: invalid relocation symbol table link")
    symtab = image.section_headers[relocation[6]]
    require(symtab[1] == 2 and symtab[9] == SYM.size
            and 0 < symtab[6] < len(image.section_headers),
            f"{member}: invalid constructor symbol table")
    symbols = list(SYM.iter_unpack(raw[symtab[4]:symtab[4] + symtab[5]]))
    require(info >> 32 < len(symbols), f"{member}: invalid constructor symbol index")
    symbol = symbols[info >> 32]
    require(0 < symbol[3] < len(image.section_headers),
            f"{member}: constructor must reference a defined function")
    code = image.section_headers[symbol[3]]
    require(code[1] == 1 and code[2] & 6 == 6,
            f"{member}: constructor target must be executable code")
    # LLVM commonly relocates against a SECTION symbol plus an addend, not FUNC.
    require(symbol[1] & 15 in (2, 3), f"{member}: unsupported constructor symbol type")
    address = symbol[4] + addend
    functions = [s for s in symbols if s[1] & 15 == 2 and s[3] == symbol[3]
                 and s[4] == address and s[5] > 0 and s[4] + s[5] <= code[5]]
    require(functions and 0 <= address < code[5],
            f"{member}: relocation does not resolve to a defined function entry")
    strings_header = image.section_headers[symtab[6]]
    strings = raw[strings_header[4]:strings_header[4] + strings_header[5]]
    return {
        "member": member, "source_section": name,
        "source_priority": priority, "init_priority": 65535 - priority,
        "output_section": f".init_array.{65535 - priority:05d}",
        "function": cstring(strings, functions[0][0]),
        "relocation_section": relocation_name,
    }


def prepare_archive(source, destination, ar="llvm-ar-18", objcopy="llvm-objcopy-18"):
    """Validate, stage and exclusively publish an indexed private archive.

    Raise ValueError for unsupported input; failures never publish destination.
    The returned JSON-compatible report includes exact tool argv and working dirs.
    """
    source = Path(source).resolve()
    destination = Path(destination).absolute()
    require(not os.path.lexists(destination), f"output already exists: {destination}")
    raw = source.read_bytes()
    members, _ = archive_members(raw)
    changes = []
    for member, data in members.items():
        try:
            change = constructor(data, member)
        except (IndexError, struct.error, UnicodeError) as error:
            raise ValueError(f"{member}: malformed ELF: {error}") from error
        if change:
            changes.append(change)
    require(len(changes) <= 1,
            "multiple legacy constructor pointers across archive members unsupported")
    commands = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = destination.parent / (".prepare-runtime-" + uuid.uuid4().hex)
    work.mkdir()

    def run(argv):
        argv = [str(arg) for arg in argv]
        commands.append({"argv": argv, "cwd": str(work)})
        try:
            subprocess.run(argv, cwd=work, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            raise ValueError(f"command failed: {argv!r}: {error.stderr.strip()}") from error

    try:
        staged = work / "prepared.a"
        while staged.name in members:
            staged = staged.with_name("_" + staged.name)
        staged.write_bytes(raw)
        if changes:
            run([ar, "x", staged])
            change = changes[0]
            old, new = change["source_section"], change["output_section"]
            require(new not in Elf(members[change["member"]]).sections,
                    f"constructor destination section already exists: {new}")
            member_path = work / change["member"]
            run([objcopy, "--rename-section", f"{old}={new},alloc,load,data,contents",
                 member_path])
            run([objcopy, "--set-section-type", f"{new}=14", member_path])
            converted = Elf(member_path.read_bytes())
            section = converted.sections.get(new)
            require(section and section[1] == 14 and section[2] == 3
                    and section[5] == 8 and section[8] == 8,
                    "objcopy did not produce a writable allocated SHT_INIT_ARRAY pointer")
            require(converted.section(new) == bytes(8)
                    and converted.section(".rela" + new)
                    == Elf(members[change["member"]]).section(".rela" + old),
                    "objcopy changed constructor pointer/relocation contents")
            rela = converted.sections[".rela" + new]
            require(rela[7] == converted.section_names.index(new)
                    and converted.section_headers[rela[6]][1] == 2,
                    "objcopy did not preserve constructor relocation links")
            require(not any(n.startswith((".ctors", ".rela.ctors", ".rel.ctors"))
                            for n in converted.section_names),
                    "objcopy left a legacy constructor section")
            staged.unlink()
            run([ar, "rcsD", staged, *members])
        else:
            run([ar, "sD", staged])
        prepared = staged.read_bytes()
        output_members, indexed = archive_members(prepared)
        require(indexed, "prepared archive has no symbol index")
        require(list(output_members) == list(members), "archive member order changed")
        changed_names = {c["member"] for c in changes}
        require(all(output_members[n] == data for n, data in members.items()
                    if n not in changed_names), "unmodified archive member bytes changed")
        require(source.read_bytes() == raw, "source changed during preparation")
        report = {
            "schema": 1, "source": str(source), "output": str(destination),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "prepared_sha256": hashlib.sha256(prepared).hexdigest(),
            "mode": "single-legacy-constructor" if changes else "verified-copy",
            "member_count": len(members), "changes": changes, "commands": commands,
            "indexed": indexed,
            "support": "at most one legacy function pointer; no dtors or native TLS",
        }
        # link() is atomic and refuses an output created concurrently, unlike replace().
        os.link(staged, destination)
        return report
    finally:
        shutil.rmtree(work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ar", default="llvm-ar-18")
    parser.add_argument("--objcopy", default="llvm-objcopy-18")
    args = parser.parse_args()
    try:
        report = prepare_archive(args.source, args.output, args.ar, args.objcopy)
    except (OSError, ValueError, struct.error) as error:
        print(f"runtime archive preparation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
