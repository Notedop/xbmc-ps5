#!/usr/bin/env python3
"""Read-only native system-provider import coverage; never emits linker stubs.

Usage: native_system_provider.py --consumer linked.elf
       --provider libc=extracted-libc.elf [--provider kernel=extracted-kernel.elf]
       [--library libkernel=libkernel] [--library libc=libc]

Provider labels and ordinary DT_NEEDED names are not native identity evidence.
Plain imports are matched by NID across all supplied native export libraries.
Encoded imports require declared native library/module identities and exact
versions. No version ordering or SDK SONAME-to-native-identity mapping is assumed.
NOTYPE, GNU-versioned imports, unresolved weak imports, and ambiguous matches
block proof. A successful report proves symbol metadata coverage, not calling
conventions, runtime dependency closure, relocation support, or loadability.
The known bootstrap libc placeholder profile blocks even complete metadata
coverage. Unrecognized code is not thereby qualified as a runtime implementation.
Explicit --library MODULE=LIBRARY selectors restrict a module to the listed
actual export libraries; all NID candidates and policy exclusions remain evidence.

SPDX-License-Identifier: GPL-3.0-or-later
"""

import argparse
import json
from pathlib import Path
import re
import struct

from native_c_link import Elf, SHDR, SYM, nid, require
from native_runtime_sdk import (
    DYNAMIC, EXPORT_LIB, IMPORT_LIB, MODULE_INFO, NEEDED_MODULE, SYMTABSZ,
    NativeImage, decode_id, sha256, string_at,
)


NID_PATTERN = re.compile(r"[A-Za-z0-9+\-]{10}[AEIMQUYcgkosw048]\Z")
TYPE_NAMES = {0: "notype", 1: "object", 2: "function", 6: "tls", 10: "ifunc"}
BINDINGS = {0: "local", 1: "global", 2: "weak"}
VISIBILITIES = {0: "default", 1: "internal", 2: "hidden", 3: "protected"}
BOOTSTRAP_CRITICAL_SYMBOLS = ("malloc", "memcpy", "abort", "__cxa_finalize")
BOOTSTRAP_CODE = bytes.fromhex("66 0f ef c0 31 c0 c3")


def implementation_profile(image, exports):
    """Recognize only the known shared zero-return bootstrap libc implementation."""
    critical = [(name, [export for export in exports if export["nid"] == nid(name)])
                for name in BOOTSTRAP_CRITICAL_SYMBOLS]
    evidence = []
    if all(matches and all(export["type_id"] == 2 for export in matches)
           for _, matches in critical):
        addresses = {export["value"] for _, matches in critical for export in matches}
        if len(addresses) == 1:
            address = next(iter(addresses))
            try:
                code = image.memory(address, len(BOOTSTRAP_CODE))
            except ValueError:
                code = None
            if code == BOOTSTRAP_CODE:
                evidence = [
                    {"symbol": name, "nid": export["nid"],
                     "native_name": export["native_name"],
                     "dynsym_index": export["dynsym_index"],
                     "virtual_address": address, "virtual_address_hex": hex(address),
                     "code_hex": code.hex(" ")}
                    for name, matches in critical for export in matches]
    return {"implementation_status": "bootstrap-placeholder" if evidence else "unrecognized",
            "runtime_implementation_qualified": False,
            "implementation_evidence": evidence}


def attributes(symbol):
    _, info, other, section, value, size = symbol
    return {"info": info, "other": other, "binding": BINDINGS.get(info >> 4, "unsupported"),
            "binding_id": info >> 4, "type": TYPE_NAMES.get(info & 15, "unsupported"),
            "type_id": info & 15, "visibility": VISIBILITIES[other & 3],
            "section": section, "value": value, "size": size}


def declarations(tags, strings, tag):
    result = {}
    identities = set()
    for value in tags.get(tag, []):
        identity = value >> 48
        name = string_at(strings, value & 0xFFFFFFFF)
        version = (value >> 32) & 0xFFFF
        require(name and identity not in result and (name, version) not in identities,
                f"duplicate/empty native declaration for tag {tag:#x}")
        result[identity] = {"id": identity, "name": name, "version": version}
        identities.add((name, version))
    return result


def encoded_name(name):
    parts = name.split("#")
    require(len(parts) == 3 and NID_PATTERN.fullmatch(parts[0]),
            f"malformed encoded native symbol: {name}")
    return parts[0], decode_id(parts[1]), decode_id(parts[2])


def inspect_provider(raw, label):
    require(len(raw) >= 64 and raw[7] == 9 and raw[8] in (2, 3),
            "expected extracted PS5 ELF ABI version 2 or 3")
    # Firmware ABI-3 uses the same program-header/dynamic layout. Adapt only
    # NativeImage's ABI-2 header gate; retain original bytes for every memory read.
    image = NativeImage(raw if raw[8] == 2 else raw[:8] + b"\x02" + raw[9:])
    image.raw = raw
    strings = image.memory(image.tag(5), image.tag(10))
    modules = declarations(image.tags, strings, MODULE_INFO)
    require(len(modules) == 1, "expected exactly one native module declaration")
    module = next(iter(modules.values()))
    libraries = declarations(image.tags, strings, EXPORT_LIB)
    require(libraries, "native provider has no export library declarations")
    require(image.tag(11) == SYM.size, "invalid native symbol entry size")
    size = image.tag(SYMTABSZ)
    require(size >= SYM.size and size % SYM.size == 0, "invalid native symbol table size")
    symbols = list(SYM.iter_unpack(image.memory(image.tag(6), size)))
    require(symbols[0] == (0, 0, 0, 0, 0, 0), "native null symbol is not empty")
    exports = []
    for index, symbol in enumerate(symbols[1:], 1):
        if not symbol[3]:
            continue
        name = string_at(strings, symbol[0])
        # Local definitions are not part of the native dynamic export namespace.
        if symbol[1] >> 4 == 0:
            continue
        identifier, library_id, module_id = encoded_name(name)
        require(library_id in libraries and module_id == module["id"],
                f"native export has undeclared library/module ID: {name}")
        exports.append({"provider": label, "native_name": name, "nid": identifier,
                        "dynsym_index": index, "module": dict(module),
                        "library": dict(libraries[library_id]), **attributes(symbol)})
    inventory = {"name": label, "sha256": sha256(raw), "elf_abi_version": raw[8],
                 "module": module,
                 "libraries": [dict(library, export_count=sum(
                     export["library"]["id"] == identity for export in exports))
                               for identity, library in sorted(libraries.items())],
                 "export_count": len(exports),
                 **implementation_profile(image, exports)}
    return {"inventory": inventory, "exports": exports}


def consumer_imports(raw):
    require(len(raw) >= 64, "truncated consumer ELF")
    require(raw[7] in (0, 3) and raw[8] == 0,
            "consumer must be an ordinary ELF, not a native module")
    shoff = struct.unpack_from("<Q", raw, 40)[0]
    shsize, shcount, shstr = struct.unpack_from("<HHH", raw, 58)
    require(shcount and shsize == SHDR.size and shstr < shcount and
            shoff + shsize * shcount <= len(raw), "invalid consumer section headers")
    for index in range(shcount):
        section = SHDR.unpack_from(raw, shoff + index * shsize)
        require(section[1] == 8 or section[4] + section[5] <= len(raw),
                "truncated consumer section")
    image = Elf(raw)
    require(len(image.section_names) == len(set(image.section_names)),
            "duplicate consumer section names")
    require(image.kind == 3, "consumer must be an ordinary ET_DYN ELF")
    require(all(name in image.sections for name in (".dynsym", ".dynstr", ".dynamic")),
            "consumer requires .dynsym/.dynstr/.dynamic")
    require(all(image.sections[name][1] == kind for name, kind in
                ((".dynsym", 11), (".dynstr", 3), (".dynamic", 6))),
            "invalid consumer dynamic section types")
    require(image.sections[".dynsym"][9] == SYM.size, "invalid consumer dynsym entry size")
    require(image.sections[".dynsym"][6] == image.section_names.index(".dynstr") and
            image.sections[".dynamic"][6] == image.section_names.index(".dynstr"),
            "consumer dynamic string table link mismatch")
    table = image.section(".dynsym")
    require(table and len(table) % SYM.size == 0, "invalid consumer dynsym size")
    symbols = list(SYM.iter_unpack(table))
    require(symbols[0] == (0, 0, 0, 0, 0, 0), "consumer null symbol is not empty")
    strings = image.section(".dynstr")
    dynamic = image.section(".dynamic")
    require(dynamic and len(dynamic) % DYNAMIC.size == 0, "invalid consumer dynamic table")
    tags = {}
    for tag, value in DYNAMIC.iter_unpack(dynamic):
        if not tag:
            break
        tags.setdefault(tag, []).append(value)
    else:
        raise ValueError("unterminated consumer dynamic table")
    libraries = declarations(tags, strings, IMPORT_LIB)
    modules = declarations(tags, strings, NEEDED_MODULE)
    versions = image.section(".gnu.version")
    require(0x6FFFFFF0 not in tags or versions,
            "consumer GNU version tag has no version table")
    require(not versions or len(versions) == 2 * len(symbols),
            "invalid consumer GNU version table")
    imports = []
    for index, symbol in enumerate(symbols[1:], 1):
        if symbol[3]:
            continue
        name = string_at(strings, symbol[0])
        require(name, f"empty consumer import at index {index}")
        row = {"name": name, "dynsym_index": index, **attributes(symbol),
               "required_module": None, "required_library": None}
        if "#" in name:
            identifier, library_id, module_id = encoded_name(name)
            require(library_id in libraries and module_id in modules,
                    f"consumer import has undeclared library/module ID: {name}")
            row.update(nid=identifier, required_library=libraries[library_id],
                       required_module=modules[module_id])
        else:
            row["nid"] = nid(name)
        row["gnu_version_index"] = (
            struct.unpack_from("<H", versions, index * 2)[0] if versions else 1)
        imports.append(row)
    return imports


def library_policy(selectors, providers):
    policy = {}
    for selector in selectors:
        module, separator, library = selector.partition("=")
        require(separator and module and library and "=" not in library,
                "library selectors require nonempty MODULE=LIBRARY")
        require(library not in policy.get(module, []), "duplicate library selector")
        inventories = [provider["inventory"] for provider in providers
                       if provider["inventory"]["module"]["name"] == module]
        require(inventories, f"library selector has unknown native module: {module}")
        require(any(entry["name"] == library for inventory in inventories
                    for entry in inventory["libraries"]),
                f"library selector has unknown export library: {module}={library}")
        policy.setdefault(module, []).append(library)
    return policy


def prove_imports(consumer_raw, providers, *, selectors=()):
    imports = consumer_imports(consumer_raw)
    policy = library_policy(selectors, providers)
    exports = [symbol for provider in providers for symbol in provider["exports"]]
    results = []
    for source in imports:
        candidates = [symbol for symbol in exports if symbol["nid"] == source["nid"]]
        all_candidates = candidates
        reasons = []
        if source["required_module"] is not None:
            candidates = [symbol for symbol in candidates if all(
                symbol[key]["name"] == source["required_" + key]["name"]
                for key in ("module", "library"))]
            version_matches = [symbol for symbol in candidates if all(
                symbol[key]["version"] == source["required_" + key]["version"]
                for key in ("module", "library"))]
            if candidates and not version_matches:
                reasons.append("incompatible native module/library version")
            else:
                candidates = version_matches
        excluded = [symbol for symbol in all_candidates
                    if symbol["module"]["name"] in policy and
                    symbol["library"]["name"] not in policy[symbol["module"]["name"]]]
        policy_excluded_only = bool(candidates) and all(symbol in excluded for symbol in candidates)
        candidates = [symbol for symbol in candidates if symbol not in excluded]
        if policy_excluded_only:
            reasons.append("native export exists only outside selected library policy")
        if not candidates:
            reasons.append("missing native export")
            if source["binding_id"] == 2:
                reasons.append("unresolved weak import unsupported")
        elif len(candidates) != 1:
            reasons.append("ambiguous native export")
        if source["binding_id"] not in (1, 2) or source["other"] != 0:
            reasons.append("unsupported import binding/visibility")
        if source["gnu_version_index"] not in (0, 1):
            reasons.append("GNU-versioned import unsupported")
        if source["type_id"] == 0:
            reasons.append("source STT_NOTYPE cannot prove function/data ABI")
        elif source["type_id"] not in (1, 2):
            reasons.append("unsupported import type")
        if len(candidates) == 1:
            target = candidates[0]
            if target["binding_id"] not in (1, 2) or target["other"] not in (0, 3):
                reasons.append("incompatible export binding/visibility")
            if target["section"] >= 0xFF00 and target["section"] != 0xFFF1:
                reasons.append("unsupported export section index")
            if target["type_id"] == 0:
                reasons.append("export STT_NOTYPE cannot prove function/data ABI")
            elif target["type_id"] not in (1, 2):
                reasons.append("unsupported export type")
            elif source["type_id"] in (1, 2) and source["type_id"] != target["type_id"]:
                reasons.append("function/data type mismatch")
            if source["type_id"] == 1 and source["size"] and source["size"] != target["size"]:
                reasons.append("object size mismatch")
        results.append({"source": source, "status": "blocked" if reasons else "verified",
                        "proof_complete": not reasons, "reasons": reasons,
                        "matches": candidates, "candidates": all_candidates,
                        "excluded_by_library_policy": excluded,
                        "policy_excluded_only": policy_excluded_only})
    metadata_complete = all(row["proof_complete"] for row in results)
    blockers = [
        {"provider": provider["inventory"]["name"],
         "reason": "bootstrap-placeholder: critical C functions share known zero-return code",
         "implementation_evidence": provider["inventory"]["implementation_evidence"]}
        for provider in providers
        if provider["inventory"]["implementation_status"] == "bootstrap-placeholder"]
    complete = metadata_complete and not blockers
    return {"schema_version": 1, "status": "verified" if complete else "blocked",
            "proof_complete": complete, "consumer_sha256": sha256(consumer_raw),
            "proof_scope": "dynamic symbol metadata coverage only; not execution, loadability or full ABI",
            "symbol_metadata_complete": metadata_complete,
            "runtime_implementation_qualified": False,
            "blockers": blockers,
            "abi_layouts_verified": False,
            "library_selection": [{"module": module, "libraries": sorted(libraries)}
                                  for module, libraries in sorted(policy.items())],
            "providers": [provider["inventory"] for provider in providers],
            "imports": results, "import_count": len(results),
            "verified_count": sum(row["proof_complete"] for row in results)}


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    parser = JsonParser(description=__doc__)
    parser.add_argument("--consumer", required=True, type=Path)
    parser.add_argument("--provider", required=True, action="append", metavar="NAME=EXTRACTED.ELF")
    parser.add_argument("--library", action="append", default=[], metavar="MODULE=LIBRARY",
                        help="restrict an actual native module to selected export libraries")
    try:
        args = parser.parse_args(argv)
        providers = []
        labels = set()
        for specification in args.provider:
            label, separator, filename = specification.partition("=")
            require(separator and label and filename and label not in labels,
                    "providers require unique nonempty NAME=EXTRACTED.ELF")
            labels.add(label)
            provider = inspect_provider(Path(filename).read_bytes(), label)
            provider["inventory"]["path"] = filename
            providers.append(provider)
        report = prove_imports(args.consumer.read_bytes(), providers, selectors=args.library)
        report["consumer"] = str(args.consumer)
    except (ValueError, OSError, struct.error, IndexError, UnicodeError) as error:
        report = {"schema_version": 1, "status": "blocked", "proof_complete": False,
                  "error": str(error)}
    print(json.dumps(report, indent=2))
    return 0 if report["proof_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
