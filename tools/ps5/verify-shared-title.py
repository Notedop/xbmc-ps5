#!/usr/bin/env python3
"""Verify the packaged shared title without executing it on a console."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from native_c_link import Elf, PHDR, SYM, cstring, nid, require
from native_runtime_sdk import DYNAMIC, IMPORT_LIB, NEEDED_MODULE, SYMTABSZ, NativeImage, decode_id
from native_static_depends import digest, load_verified_map
from native_shared_link import convert, dynamic_entries, load_provider
from native_loader_companion import verify_artifacts as verify_loader_companion
from native_title_launcher import verify as verify_launcher
from native_title_launcher import STARTUP_SOURCES
from native_runtime_sdk import prove_provider

CORE_IMPORTS = (
    "__emutls_get_address", "__wrap_malloc", "__wrap_free", "__wrap_pthread_create",
    "__wrap_exit", "__cxa_throw", "__gxx_personality_v0",
    "__unw_add_dynamic_eh_frame_section",
)


def program_headers(raw, *, executable=True):
    require(len(raw) >= 64 and raw[:7] == b"\x7fELF\x02\x01\x01",
            "expected little-endian ELF64 title")
    require(struct.unpack_from("<HH", raw, 16) == (0xFE10 if executable else 0xFE18, 62),
            "expected native x86-64 executable/module")
    offset = struct.unpack_from("<Q", raw, 32)[0]
    size, count = struct.unpack_from("<HH", raw, 54)
    require(size == PHDR.size and count and offset + count * size <= len(raw),
            "invalid native title program headers")
    headers = [PHDR.unpack_from(raw, offset + index * size) for index in range(count)]
    require(all(p[2] + p[5] <= len(raw) for p in headers), "truncated native title segment")
    return headers


def native_imports(raw, providers=None, *, executable=True):
    headers = program_headers(raw, executable=executable)

    def memory(address, length):
        matches = [p for p in headers if p[0] == 1 and
                   p[3] <= address and address + length <= p[3] + p[5]]
        require(len(matches) == 1, "native title metadata must be mapped exactly once")
        start = matches[0][2] + address - matches[0][3]
        return raw[start:start + length]

    dynamics = [p for p in headers if p[0] == 2]
    require(len(dynamics) == 1, "expected one native title dynamic table")
    dynamic = dynamics[0]
    tags = {}
    for tag, value in DYNAMIC.iter_unpack(raw[dynamic[2]:dynamic[2] + dynamic[5]]):
        if not tag:
            break
        tags.setdefault(tag, []).append(value)
    strings = memory(tags[5][0], tags[10][0])

    def string(at):
        require(at < len(strings), "invalid native title string offset")
        return strings[at:strings.index(0, at)].decode()

    modules = {value >> 48: string(value & 0xFFFFFFFF)
               for value in tags[NEEDED_MODULE]}
    libraries = {value >> 48: string(value & 0xFFFFFFFF)
                 for value in tags[IMPORT_LIB]}
    expected = {}
    if providers is not None:
        by_soname = {p["soname"]: p for p in providers}
        require(len(by_soname) == len(providers), "duplicate provider SONAME")
        require(len(tags.get(1, [])) == len(tags[NEEDED_MODULE]),
                "native title dependency/identity count mismatch")
        for soname, value in zip(tags[1], tags[NEEDED_MODULE]):
            name = string(soname)
            require(name in by_soname, f"undeclared title provider: {name}")
            provider = by_soname[name]
            module_id = value >> 48
            require(modules[module_id] == provider["module"] and
                    (value >> 32) & 0xFFFF == provider["module_version"],
                    f"native title provider module identity/version mismatch: {name}")
            expected[module_id] = provider
        require(len(expected) == len(tags[NEEDED_MODULE]), "duplicate native module ID")
    library_versions = {value >> 48: (value >> 32) & 0xFFFF
                        for value in tags[IMPORT_LIB]}
    imports = {}
    for symbol in SYM.iter_unpack(memory(tags[6][0], tags[SYMTABSZ][0])):
        if not symbol[0] or symbol[3]:
            continue
        parts = string(symbol[0]).split("#")
        require(len(parts) == 3, "unencoded native title import")
        library_id, module_id = decode_id(parts[1]), decode_id(parts[2])
        library, module = libraries[library_id], modules[module_id]
        if providers is not None:
            provider = expected[module_id]
            require(library == provider["library"] and
                    library_versions[library_id] == provider["library_version"],
                    f"native title provider library identity/version mismatch: {module}")
        require(parts[0] not in imports, "duplicate native title import NID")
        imports[parts[0]] = {"module": module, "library": library}
    return imports


def check_roundtrip(source, extracted):
    require(len(source) == len(extracted), "signed title extraction changed ELF size")
    notes = [p for p in program_headers(source) if p[0] == 4 and p[6] == 0]
    canonical = bytearray(source)
    omitted = []
    for note in notes:
        start, length = note[2], note[5]
        if source[start:start + length] != extracted[start:start + length]:
            require(not any(extracted[start:start + length]),
                    "unexpected nonloadable note transformation")
            canonical[start:start + length] = bytes(length)
            omitted.append({"offset": start, "size": length})
    require(canonical == extracted,
            "signed title differs outside the signer's omitted nonloadable notes")
    return {"all_loadable_bytes_equal": True, "whole_file_equal": source == extracted,
            "omitted_nonloadable_notes": omitted}


def title_providers(sdk, record):
    descriptor = json.loads((sdk / "manifest.json").read_text())
    providers = [load_provider(sdk / name) for name in descriptor["provider_manifests"]]
    for receipt in record.get("additional_provider_manifests", []):
        path = Path(receipt["path"])
        require(digest(path) == receipt["sha256"],
                f"additional title provider manifest changed: {path}")
        providers.append(load_provider(path))
    identities = [{key: provider[key] for key in
                   ("soname", "module", "module_version", "library", "library_version")}
                  for provider in providers]
    require(len({p["soname"] for p in providers}) == len(providers),
            "duplicate title provider SONAME")
    require(record["providers"] == identities, "title provider identity receipt changed")
    return providers


def verify_application_proof(proof, signed, native, linked, providers):
    require(proof["schema"] == 1 and proof["format"] == "ps5-private-native-kodi-app" and
            proof["module"] == "libkodi-app", "invalid private application proof profile")
    entries = ["kodi_native_finalize", "kodi_native_run"]
    require(proof["exports"] == entries and sorted(proof["callable_entries"]) == entries,
            "private application proof has unexpected callable entries")
    require(proof.get("process_lifetime") is True and
            proof.get("native_unload_rejected") is True,
            "private application must declare process-lifetime unload rejection")
    hashes = {name: digest(path) for name, path in
              (("signed_sha256", signed), ("native_sha256", native), ("linked_sha256", linked))}
    require(all(proof[name] == value for name, value in hashes.items()),
            "private application proof artifact hash changed")
    require(proof["sign_extract"] == {
        "exact_match": True, "native_sha256": hashes["native_sha256"],
        "extracted_sha256": hashes["native_sha256"],
    }, "private application proof signing roundtrip changed")
    require(convert(linked.read_bytes(), "libkodi-app", providers, process_lifetime=True) ==
            native.read_bytes(), "private application process-lifetime conversion changed")


def shared_network_imports(symbols, imports, core):
    names = core.get("link_contract", {}).get("network", {}).get("symbols", [])
    owners = {}
    for name in names:
        if name not in symbols:
            continue
        require(not symbols[name][3], f"private application defines shared network service: {name}")
        owner = imports.get(nid(name))
        require(owner == {"module": "libkodi-runtime", "library": "libkodi-runtime"},
                f"title binds network service outside the shared core: {name}")
        owners[name] = owner
    return owners


def verify(app, sdk, archive_map):
    title_id = json.loads((app / "sce_sys/param.json").read_text())["titleId"]
    dist = app / "dist" / title_id
    record = json.loads((app / "vendor/ps5-shared-runtime.json").read_text())
    require(set(record.get("startup_sources", {})) == STARTUP_SOURCES,
            "missing shared title startup source receipt")
    for name, expected in record["startup_sources"].items():
        require(digest(app / "tooling/native" / name) == expected,
                f"shared title startup source changed: {name}")
    require(json.loads((dist / "ps5-shared-runtime.json").read_text()) == record,
            "packaged shared title input receipt changed")
    require(record["sdk_manifest_sha256"] == digest(sdk / "manifest.json"),
            "title SDK manifest changed")
    mapping = load_verified_map(archive_map, sdk / "include/stdio.h")
    inputs = set((app / "vendor/kodi-deps.txt").read_text().splitlines())
    require(not inputs.intersection(mapping) and set(mapping.values()) <= inputs,
            "title dependency list contains old or missing native replacements")
    expected_modules = {"libkodi-runtime.sprx", "libkodi-gl.sprx"}
    require({p.name for p in (dist / "sce_module").iterdir()} ==
            expected_modules | {"libc.prx", "libkodi-app.sprx"},
            "shared title must package core, graphics, private application and loader companion")
    require(not any(dist.rglob("*.so")), "linker-only shared objects must not be packaged")
    for name in expected_modules:
        require(digest(dist / "sce_module" / name) == record["runtime_sha256"][name] ==
                digest(sdk / "runtimes" / name), f"packaged runtime changed: {name}")
    with tempfile.TemporaryDirectory(prefix="ps5-title-runtime-layout-") as temporary:
        for name in sorted(expected_modules):
            extracted_runtime = Path(temporary) / (name + ".elf")
            subprocess.run([str(app / "build/host/ps5-native-tool"), "self", "--extract",
                            "--file", str(dist / "sce_module" / name),
                            "--out", str(extracted_runtime)], check=True,
                           capture_output=True, text=True)
            NativeImage(extracted_runtime.read_bytes()).validate_metadata_layout()
    companion_path = dist / "sce_module/libc.prx"
    companion = verify_loader_companion(
        app / "vendor/ps5-loader-companion", sdk, app / "build/host/ps5-native-tool",
        signed=companion_path)
    require(companion["signed_sha256"] == record["loader_companion_sha256"],
            "title loader companion hash mismatch")
    require(json.loads((dist / "ps5-loader-companion-proof.json").read_text()) == companion,
            "packaged loader companion proof changed")
    source_path = app / "build/eboot.elf"
    extracted = app / "build/eboot.verified.elf"
    subprocess.run([str(app / "build/host/ps5-native-tool"), "self", "--extract",
                    "--file", str(dist / "eboot.bin"), "--out", str(extracted)], check=True,
                   capture_output=True, text=True)
    source, raw = source_path.read_bytes(), extracted.read_bytes()
    roundtrip = check_roundtrip(source, raw)
    providers = title_providers(sdk, record)
    imports = native_imports(raw, providers)
    linked_path = app / "build/llvm-pie.elf"
    linked = Elf(linked_path.read_bytes())
    require(json.loads((dist / "ps5-addon-runtime.json").read_text())["title_sha256"] ==
            digest(linked_path), "packaged add-on runtime title hash changed")
    startup = verify_launcher(linked, imports)
    module_directory = app / "build/kodi-app"
    application_source = module_directory / "native.elf"
    application_linked = module_directory / "linked.so"
    application_proof = json.loads((module_directory / "proof.json").read_text())
    require(application_proof.get("startup_header_sha256") ==
            record["startup_sources"]["title_native_startup.h"],
            "application startup ABI differs from staged launcher ABI")
    require(json.loads((dist / "ps5-kodi-app-proof.json").read_text()) == application_proof,
            "packaged application proof changed")
    app_linked = Elf(application_linked.read_bytes())
    needed = {cstring(app_linked.section(".dynstr"), offset)
              for offset in dynamic_entries(app_linked).get(1, [])}
    application_providers = [provider for provider in providers if provider["soname"] in needed]
    verify_application_proof(application_proof, dist / "sce_module/libkodi-app.sprx",
                             application_source, application_linked, application_providers)
    with tempfile.TemporaryDirectory(prefix="ps5-kodi-app-verify-") as temporary:
        extracted_app = Path(temporary) / "application.elf"
        subprocess.run([str(app / "build/host/ps5-native-tool"), "self", "--extract",
                        "--file", str(dist / "sce_module/libkodi-app.sprx"),
                        "--out", str(extracted_app)], check=True, capture_output=True, text=True)
        application_raw = extracted_app.read_bytes()
    require(application_raw == application_source.read_bytes(),
            "private application signing changed native bytes")
    require(json.loads((dist / "ps5-addon-runtime.json").read_text()).get("application_sha256") ==
            digest(dist / "sce_module/libkodi-app.sprx"),
            "packaged add-on runtime application hash changed")
    NativeImage(application_raw).validate_metadata_layout()
    published, dependencies = prove_provider(application_linked.read_bytes(), application_raw,
                                             "libkodi-app", include_dependencies=True)
    imports = native_imports(application_raw, providers, executable=False)
    symbols = dict(app_linked.symbols(".symtab"))
    core = next(provider for provider in providers if provider["module"] == "libkodi-runtime")
    network_owners = shared_network_imports(symbols, imports, core)
    lifecycle = ("module_start", "ps5_kodi_crt_initialize", "module_stop", "ps5_kodi_crt_finalize")
    for name in lifecycle:
        require(name in symbols and symbols[name][3] and symbols[name][2] == 2 and
                symbols[name][1] & 15 == 2,
                f"private application requires a defined hidden lifecycle function: {name}")
    require(len({symbols[name][4] for name in lifecycle}) == len(lifecycle),
            "private application startup/unload entries must be separate from CRT lifecycle")
    exported = {name for name, symbol in app_linked.symbols(".dynsym")
                if name and symbol[3] and symbol[1] >> 4 in (1, 2) and symbol[2] & 3 in (0, 3)}
    require(exported == {"kodi_native_run", "kodi_native_finalize"},
            "private application must not export a title-to-addon symbol bridge")
    owners = {}
    for name in CORE_IMPORTS:
        require(name in symbols and not symbols[name][3], f"title must import {name}")
        owner = imports[nid(name)]
        require(owner == {"module": "libkodi-runtime", "library": "libkodi-runtime"},
                f"title binds {name} outside the shared core")
        owners[name] = owner
    forbidden = {*CORE_IMPORTS, "__cxa_thread_atexit_impl", "ps5_tls_thread_exit",
                 *("__wrap_" + name for name in record["wrap_symbols"])}
    require(not any(symbols[name][3] for name in forbidden if name in symbols),
            "private application defines a duplicate runtime owner")
    require(symbols["__dso_handle"][3] and symbols["__dso_handle"][2] & 3 == 2,
            "private application must own its hidden DSO tag")
    require(not app_linked.section(".preinit_array"),
            "private application must initialize through the native shared CRT")
    for name in ("eglMakeCurrent", "glClear"):
        require(imports[nid(name)] ==
                {"module": "libkodi-gl", "library": "libkodi-gl"},
                f"title graphics must bind through the shared provider: {name}")
    proof = {
        "schema": 1, "format": "ps5-shared-title-local-proof", "console_accepted": False,
        "eboot_sha256": digest(dist / "eboot.bin"),
        "native_source_sha256": digest(source_path),
        "native_extracted_sha256": digest(extracted), "sign_extract": roundtrip,
        "runtime_sha256": record["runtime_sha256"], "core_import_owners": owners,
        "shared_network_import_owners": network_owners,
        "dependency_archives": [{"name": Path(new).name, "sha256": digest(Path(new))}
                                for new in sorted(set(mapping.values()))],
        "no_duplicate_runtime_owner": True, "only_own_runtimes_packaged": True,
        "linker_stubs_packaged": False,
        "provider_identities_verified": True,
        "runtime_metadata_layout_verified": True,
        "loader_companion_verified": True,
        "loader_companion_sha256": companion["signed_sha256"],
        "startup": startup,
        "application": {
            "module": "libkodi-app", "process_lifetime": True,
            "native_unload_rejected": True, "process_lifetime_conversion_verified": True,
            "signed_sha256": digest(dist / "sce_module/libkodi-app.sprx"),
            "native_sha256": digest(application_source),
            "linked_sha256": digest(application_linked),
            "exact_sign_extract": True, "native_metadata_layout_verified": True,
            "private_exports": sorted(exported),
            "proven_export_count": len(published),
            "proven_import_count": len(dependencies["imports"]),
        },
    }
    (dist / "ps5-shared-title-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--archive-map", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args.app.resolve(), args.sdk.resolve(), args.archive_map.resolve())
    except (OSError, ValueError, KeyError, struct.error, subprocess.CalledProcessError) as error:
        parser.exit(1, f"verify-shared-title: {error}\n")


if __name__ == "__main__":
    main()
