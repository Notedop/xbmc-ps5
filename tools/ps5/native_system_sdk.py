#!/usr/bin/env python3
"""Generate a linker-only SDK from extracted native system export metadata.

Usage: native_system_sdk.py --native-elf libkernel.sprx --names-from libkernel.so
       --library libkernel --output fresh-sdk --lld ld.lld
       [--require-symbol sceKernelGetProcessTime]

The ordinary ELF supplies plain names ONLY. NIDs, identity, binding, visibility,
type, value and size are proven against the selected native export library.
This bounded profile requires module == library and versions 257/1, but native
export IDs need not be zero. Names absent from that library are exclusions, not
supported APIs. Unknown implementations are never runtime-qualified.
Unmapped native TLS exports remain excluded evidence; any named TLS/NOTYPE/IFUNC
match is rejected rather than retyped into a usable import.

lib/*.so contains dummy code/data, MUST NEVER be deployed or executed. Original
inputs are retained unchanged under evidence/ for auditing, not deployment.
Output must be a fresh directory; failed generation publishes no SDK.
POSIX hosts require libc renameat2 for atomic, no-replace publication.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import uuid

from native_c_link import Elf, SHDR, SYM, nid, require
from native_runtime_sdk import import_object, sha256, string_at, validate_stub
from native_system_provider import inspect_provider


def publish_directory(staging, output):
    """Publish atomically without replacing even an empty concurrent output."""
    if os.name == "posix":
        libc = ctypes.CDLL(None, use_errno=True)
        require(hasattr(libc, "renameat2"), "atomic no-replace publication requires renameat2")
        rename = libc.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(output), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(output))
    else:
        staging.rename(output)


def name_catalogue(raw):
    """Read names without trusting the catalogue's symbol types or SONAME."""
    require(len(raw) >= 64 and raw[:9] in (
        b"\x7fELF\x02\x01\x01\0\0", b"\x7fELF\x02\x01\x01\x03\0"),
        "names catalogue must be an ordinary little-endian ELF64")
    shoff = struct.unpack_from("<Q", raw, 40)[0]
    shsize, shcount, shstr = struct.unpack_from("<HHH", raw, 58)
    require(shsize == SHDR.size and shcount and shstr < shcount and
            shoff + shcount * shsize <= len(raw), "invalid names section headers")
    for index in range(shcount):
        section = SHDR.unpack_from(raw, shoff + index * shsize)
        require(section[1] == 8 or section[4] + section[5] <= len(raw),
                "truncated names section")
    image = Elf(raw)
    require(image.kind == 3 and len(set(image.section_names)) == len(image.section_names),
            "names catalogue must be ET_DYN with unambiguous sections")
    require(".dynsym" in image.sections and ".dynstr" in image.sections,
            "names catalogue requires .dynsym/.dynstr")
    table, strings = image.section(".dynsym"), image.section(".dynstr")
    header = image.sections[".dynsym"]
    require(header[1] == 11 and header[9] == SYM.size and
            header[6] == image.section_names.index(".dynstr") and
            image.sections[".dynstr"][1] == 3 and
            table and len(table) % SYM.size == 0, "invalid names dynamic symbol table")
    symbols = list(SYM.iter_unpack(table))
    require(symbols[0] == (0, 0, 0, 0, 0, 0), "invalid names null symbol")
    names, identifiers = {}, set()
    for index, symbol in enumerate(symbols[1:], 1):
        if not symbol[3]:
            continue
        name = string_at(strings, symbol[0])
        require(name and "#" not in name and "@" not in name,
                "names catalogue requires nonempty plain unversioned names")
        identifier = nid(name)
        require(name not in names, f"duplicate catalogue name: {name}")
        require(identifier not in identifiers, f"colliding catalogue NID: {name}")
        names[name] = {"name": name, "nid": identifier, "dynsym_index": index}
        identifiers.add(identifier)
    require(names, "names catalogue has no defined names")
    return names


def prove_symbols(native_raw, names_raw, library):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", library) is not None,
            "native library identity must be a nonempty name without path separators")
    inspected = inspect_provider(native_raw, library)
    inventory = inspected["inventory"]
    require(inventory["implementation_status"] != "bootstrap-placeholder",
            "bootstrap-placeholder provider cannot supply a system SDK")
    module = inventory["module"]
    require(module["name"] == library and module["version"] == 257,
            "unsupported native module identity/version: expected selected library, version 257")
    libraries = [entry for entry in inventory["libraries"] if entry["name"] == library]
    require(len(libraries) == 1 and libraries[0]["version"] == 1,
            "unsupported/ambiguous native export library identity/version: expected version 1")
    selected = libraries[0]
    exports = {}
    for export in inspected["exports"]:
        if export["library"]["id"] != selected["id"]:
            continue
        identifier = export["nid"]
        require(identifier not in exports, f"ambiguous/duplicate native export NID: {identifier}")
        require(export["binding_id"] in (1, 2) and export["other"] in (0, 3),
                f"unsupported native export binding/visibility: {export['native_name']}")
        require(export["section"] < 0xFF00 or export["section"] == 0xFFF1,
                f"unsupported native export section: {export['native_name']}")
        exports[identifier] = export
    require(exports, "selected native library has no exports")
    names = name_catalogue(names_raw)
    symbols, matched, excluded = {}, [], []
    for name, row in names.items():
        export = exports.get(row["nid"])
        if export is None:
            excluded.append(dict(row, reason="no export NID in selected native library",
                                 candidates=[entry for entry in inspected["exports"]
                                             if entry["nid"] == row["nid"]]))
            continue
        require(export["type_id"] in (1, 2),
                f"unsupported native export type (TLS/NOTYPE/IFUNC): {export['native_name']}")
        symbols[name] = (0, export["info"], export["other"], export["section"],
                         export["value"], export["size"])
        matched.append(dict(export, name=name, canonical_name=
                            row["nid"] + "#" + library + "#" + library,
                            provider_value=export["value"],
                            names_dynsym_index=row["dynsym_index"]))
    require(symbols, "no catalogue names match selected native export NIDs")
    return symbols, matched, excluded, inspected, selected


def generate(native_elf, names_elf, library, output, lld, required=()):
    native_elf, names_elf, output = Path(native_elf), Path(names_elf), Path(output)
    required = set(required)
    require(not output.exists() and not output.is_symlink(),
            "SDK output already exists; use a fresh output directory")
    native_raw, names_raw = native_elf.read_bytes(), names_elf.read_bytes()
    symbols, matched, excluded, inspected, selected = prove_symbols(native_raw, names_raw, library)
    missing = sorted(required - set(symbols))
    require(not missing, "required symbols missing from selected native library: " + ", ".join(missing))
    obj = import_object(symbols)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / ("." + output.name + ".staging-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        (staging / "lib").mkdir()
        (staging / "evidence").mkdir()
        object_path = staging / "import.o"
        object_path.write_bytes(obj)
        stub_path = staging / "lib" / (library + ".so")
        result = subprocess.run([str(lld), "-shared", "--hash-style=sysv",
                                 "--soname=" + library + ".sprx", "-o", str(stub_path),
                                 str(object_path)], capture_output=True, text=True, check=False)
        require(result.returncode == 0, "LLD import-library link failed: " +
                (result.stderr.strip() or result.stdout.strip()))
        stub = stub_path.read_bytes()
        validate_stub(stub, symbols, library)
        object_path.unlink()
        (staging / "evidence" / "native.elf").write_bytes(native_raw)
        (staging / "evidence" / "names.elf").write_bytes(names_raw)
        inventory = inspected["inventory"]
        matched_nids = {symbol["nid"] for symbol in matched}
        manifest = {
            "schema": 1, "schema_version": 1, "format": "ps5-native-runtime-sdk",
            "module": library, "library": library, "module_version": 257, "library_version": 1,
            "soname": library + ".sprx", "linker_only": True, "runtime_loadable": False,
            "runtime_implementation_qualified": False,
            "warning": "Dummy linker-only code/data: MUST NEVER deploy or execute lib/*.so. "
                       "Evidence inputs are not deployment artifacts.",
            "proof_scope": "selected native export metadata and exact name NIDs only; "
                           "not calling conventions, dependency closure, execution or loadability",
            "provider": {
                "module": inventory["module"]["name"], "module_id": inventory["module"]["id"],
                "module_version": inventory["module"]["version"], "export_library": library,
                "library_id": selected["id"], "library_version": selected["version"],
                "native_elf_sha256": sha256(native_raw), "native_elf_path": "evidence/native.elf",
                "native_elf_source": str(native_elf.resolve()),
                "inspection": inspected,
            },
            "name_catalogue": {
                "sha256": sha256(names_raw), "path": "evidence/names.elf",
                "source": str(names_elf.resolve()), "usage": "plain names only",
            },
            "import_library": {
                "path": "lib/" + library + ".so", "soname": library + ".sprx",
                "sha256": sha256(stub), "linker_only": True, "runtime_loadable": False,
            },
            "symbols": matched, "excluded_names": excluded,
            "unnamed_native_exports": [
                dict(export, exclusion_reason=(
                    "no catalogue name; native export type unsupported by linker-only SDK"
                    if export["type_id"] not in (1, 2) else "no matching name in catalogue"))
                for export in inspected["exports"]
                if export["library"]["id"] == selected["id"] and
                export["nid"] not in matched_nids],
            "required_symbols": sorted(required),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        require(not output.exists() and not output.is_symlink(),
                "SDK output already exists; use a fresh output directory")
        publish_directory(staging, output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--native-elf", required=True, type=Path)
    parser.add_argument("--names-from", required=True, type=Path)
    parser.add_argument("--library", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lld", required=True)
    parser.add_argument("--require-symbol", action="append", default=[])
    args = parser.parse_args()
    try:
        generate(args.native_elf, args.names_from, args.library, args.output, args.lld,
                 args.require_symbol)
    except (OSError, ValueError, struct.error, UnicodeError, IndexError) as error:
        parser.exit(1, f"native-system-sdk: {error}\n")


if __name__ == "__main__":
    main()
