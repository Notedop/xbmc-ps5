#!/usr/bin/env python3
"""Link retained Kodi inputs into a private process-lifetime native SPRX.

No archive rebuild, SDK generation, staging mutation, deployment or software
loading is performed. Successful outputs and failed command logs are durable.
"""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys

import native_c_link as elf
import native_runtime_sdk as runtime
import native_shared_link as native

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
MODULE = "libkodi-app"
ENTRIES = ["kodi_native_run", "kodi_native_finalize"]
IDENTITY = ("soname", "module", "module_version", "library", "library_version")
FORMAT = "ps5-private-native-kodi-app"
NETWORK_SYMBOLS = {"__h_errno", "getaddrinfo", "freeaddrinfo", "getnameinfo",
                   "gai_strerror", "gethostname", "gethostbyname"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def receipt(path):
    return {"path": str(path), "sha256": digest(path)}


def file_path(root, name):
    path = Path(name)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    require(path.is_file(), f"missing input: {path}")
    return path


def response_paths(path, root):
    tokens = shlex.split(path.read_text())
    require(tokens and all(not t.startswith(("-", "@")) for t in tokens),
            f"expected paths-only response file: {path}")
    return [file_path(root, token) for token in tokens]


def group_paths(path, root):
    # Do not execute arbitrary linker-script directives from the retained GROUP.
    text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    text = re.sub(r'SEARCH_DIR\s*\(\s*"[^"]*"\s*\)', "", text)
    match = re.fullmatch(r"\s*GROUP\s*\(([^()]*)\)\s*", text, re.S)
    require(match is not None, f"expected a single retained GROUP: {path}")
    tokens = shlex.split(match[1])
    require(tokens and all(not t.startswith(("-", "@")) for t in tokens),
            f"invalid retained GROUP input: {path}")
    paths = [file_path(root, token) for token in tokens]
    require(all(p.suffix in (".a", ".o", ".so") for p in paths),
            "unsupported retained GROUP input")
    return paths


def providers(app, sdk, inputs):
    manifest_path = sdk / "manifest.json"
    require(digest(manifest_path) == inputs["sdk_manifest_sha256"],
            "staged SDK manifest hash mismatch")
    descriptor = json.loads(manifest_path.read_text())
    require(descriptor.get("schema") == 1 and
            descriptor.get("format") == "ps5-native-addon-sdk",
            "expected verified ps5-native-addon-sdk schema 1")
    require(descriptor["wrap_symbols"] == inputs["wrap_symbols"],
            "staged/SDK wrap_symbols mismatch")
    manifests = []
    for name in descriptor["provider_manifests"]:
        path = file_path(sdk, name)
        require(path.is_relative_to(sdk), "provider manifest escapes SDK")
        manifests.append(path)
    for item in inputs.get("additional_provider_manifests", []):
        path = file_path(app, item["path"])
        require(digest(path) == item["sha256"],
                f"additional provider manifest hash mismatch: {path}")
        manifests.append(path)
    declared = {}
    for path in manifests:
        data = native.load_provider(path)
        require(data["soname"] not in declared, "duplicate provider SONAME")
        library = file_path(path.parent, data["import_library"]["path"])
        require(library.is_relative_to(path.parent), "provider library escapes manifest directory")
        declared[data["soname"]] = (path, library, data)
    ordered = []
    for identity in inputs["providers"]:
        require(identity["soname"] in declared, f"undeclared provider: {identity['soname']}")
        item = declared.pop(identity["soname"])
        require(all(identity[k] == item[2][k] for k in IDENTITY),
                f"staged provider identity mismatch: {identity['soname']}")
        ordered.append(item)
    require(not declared, f"unused provider declarations: {sorted(declared)}")
    require([p[2]["module"] for p in ordered[:2]] == ["libkodi-runtime", "libkodi-gl"],
            "core and GL providers must lead provider order")
    wraps = inputs["wrap_symbols"]
    require(wraps and len(set(wraps)) == len(wraps) and
            all(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", s) for s in wraps),
            "invalid runtime wrap_symbols")
    core = ordered[0][2]
    require(core.get("link_contract", {}).get("wrap_symbols") == wraps and
            core["link_contract"].get("compiler_abi") == "ps5-sdk-native-frontend-v1",
            "missing core compiler/wrapper ABI contract")
    exports = {s["name"] for s in core["symbols"]}
    require(all("__wrap_" + s in exports for s in wraps), "missing core wrapper export")
    for name, expected in inputs["runtime_sha256"].items():
        require(digest(app / "vendor/ps5-runtime" / name) == expected,
                f"staged runtime hash mismatch: {name}")
    return ordered


def shared_network_symbols(core):
    contract = core.get("link_contract", {}).get("network")
    if contract is None:
        return set()
    require(isinstance(contract, dict), "invalid shared network ABI contract")
    names = contract.get("symbols")
    require(contract.get("implementation") == "ps5-libc-net-v1" and
            isinstance(names, list) and all(isinstance(name, str) for name in names) and
            len(names) == len(NETWORK_SYMBOLS) and set(names) == NETWORK_SYMBOLS and
            isinstance(contract.get("source_sha256"), str) and
            re.fullmatch(r"[0-9a-f]{64}", contract["source_sha256"]),
            "invalid shared network ABI contract")
    functions = {s["name"] for s in core["symbols"] if s["info"] & 15 == 2}
    require(NETWORK_SYMBOLS <= functions, "shared network ABI is missing function exports")
    return set(NETWORK_SYMBOLS)


def source_paths(app, core=None):
    excluded = {"title_shared_runtime.cpp", "app_crt.cpp", "app_cpp_runtime.cpp"}
    if core is not None and shared_network_symbols(core):
        network = app / "src/libc_net.c"
        require(not network.exists() or digest(network) ==
                core["link_contract"]["network"]["source_sha256"],
                "private network implementation differs from shared runtime")
        excluded.add("libc_net.c")
    sources = [p for p in sorted((app / "src").rglob("*"))
               if p.suffix in (".c", ".cc", ".cpp", ".cxx") and p.name not in excluded]
    require(sources, "staged app/src has no application shims")
    return sources


def support_paths(app, sdk):
    def first(label, paths):
        for path in paths:
            if path.is_file():
                return path
        raise ValueError(f"missing {label}: " + ", ".join(map(str, paths)))

    return {
        "entry": first("native Kodi entry", [
            app / "tooling/native/kodi_app_entry.cpp",
            ROOT / "shims/native-app/kodi_app_entry.cpp"]),
        "crt": first("native module CRT", [
            app / "tooling/native/native_module_crt.cpp",
            sdk / "tools/src/native_module_crt.cpp",
            sdk / "tools/native_module_crt.cpp",
            TOOLS / "native_module_crt.cpp"]),
        "compiler_wrapper": first("native compiler wrapper", [TOOLS / "native-clang.py"]),
        "linker_script": first("native shared linker script", [TOOLS / "native-shared.ld"]),
    }


def legacy_linker_script(source):
    text = source.read_text()
    anchor = "    .init_array : {"
    require(text.count(anchor) == 1, "unexpected native linker script init-array layout")
    regions = """    .ps5_legacy_init : {
        PROVIDE_HIDDEN(__ps5_legacy_init_start = .);
        KEEP(*(.ctors))
        PROVIDE_HIDDEN(__ps5_legacy_init_end = .);
    } :relro
    .ps5_legacy_fini : {
        PROVIDE_HIDDEN(__ps5_legacy_fini_start = .);
        KEEP(*(.dtors))
        PROVIDE_HIDDEN(__ps5_legacy_fini_end = .);
    } :relro
    .ps5_legacy_priority : { KEEP(*(.ctors.* .dtors.*)) } :relro
"""
    return text.replace(anchor, regions + anchor) + """
ASSERT(SIZEOF(.ps5_legacy_priority) == 0,
       "private Kodi app does not support prioritized legacy .ctors/.dtors")
"""


def legacy_lifecycle(image, map_path):
    """Prove each legacy slot is a local function relocation or ABI sentinel."""
    origins = []
    pattern = re.compile(r"^\s*([0-9a-f]+)\s+[0-9a-f]+\s+([0-9a-f]+)"
                         r"\s+\d+\s+(.+):\((\.ctors|\.dtors)\)\s*$")
    with map_path.open() as source:
        for line in source:
            match = pattern.fullmatch(line.rstrip("\n"))
            if match:
                address, size, owner, section = match.groups()
                origins.append({"address": int(address, 16), "size": int(size, 16),
                                "source": owner, "section": section})
    functions = {}
    for name, symbol in image.symbols(".symtab"):
        if symbol[3] and symbol[1] & 15 == 2:
            functions.setdefault(symbol[4], []).append(name)
    relocations = {}
    for section in (".rela.dyn", ".rela.plt"):
        if section not in image.sections:
            continue
        for offset, info, addend in struct.iter_unpack("<QQq", image.section(section)):
            relocations.setdefault(offset, []).append((info, addend))
    result = {"schema": 1, "format": "ps5-private-legacy-lifecycle-v1",
              "constructor_order": "reverse", "destructor_order": "forward",
              "initialization_phase": "before ordinary init-array callbacks",
              "finalization_phase": "after ordinary fini-array callbacks, before CRT globals finalization",
              "origins": origins}
    for kind, legacy, order in (("init", ".ctors", "reverse"), ("fini", ".dtors", "forward")):
        name = ".ps5_legacy_" + kind
        require(name in image.sections, f"missing bounded legacy lifecycle region: {name}")
        header = image.sections[name]
        require(header[3] % 8 == 0 and header[5] % 8 == 0,
                f"unaligned legacy callback region: {name}")
        data = image.section(name)
        slots, callbacks = [], []
        for index, (value,) in enumerate(struct.iter_unpack("<Q", data)):
            address = header[3] + index * 8
            owners = [o for o in origins if o["section"] == legacy and
                      o["address"] <= address and address + 8 <= o["address"] + o["size"]]
            require(len(owners) == 1, f"legacy slot has ambiguous/missing origin: {address:#x}")
            require(owners[0]["address"] % 8 == 0 and owners[0]["size"] % 8 == 0,
                    f"unaligned legacy input: {owners[0]['source']}")
            entry = {"index": index, "address": address, "source": owners[0]["source"]}
            relocs = relocations.get(address, [])
            if not relocs:
                require(value == 0 and index == len(data) // 8 - 1 or
                        value == 0xFFFFFFFFFFFFFFFF and index == 0,
                        f"unsupported legacy sentinel/unrelocated pointer: {address:#x}={value:#x}")
                entry["sentinel"] = "terminator" if value == 0 else "header"
            else:
                require(len(relocs) == 1 and relocs[0][0] == 8 and value == 0,
                        f"legacy callback requires one local RELATIVE relocation: {address:#x}")
                target = relocs[0][1]
                require(target in functions and any(
                    p[0] == 1 and p[1] & 1 and p[3] <= target < p[3] + p[5]
                    for p in image.phdrs), f"legacy callback is not a local function: {target:#x}")
                entry.update({"target": target, "symbols": functions[target]})
                callbacks.append(entry)
            slots.append(entry)
        # No byte inside a callback may carry another (misaligned) relocation.
        require(all(offset % 8 == 0 for offset in relocations
                    if header[3] <= offset < header[3] + header[5]),
                f"misaligned legacy callback relocation: {name}")
        result[kind] = {"slots": slots,
                        "callbacks": list(reversed(callbacks)) if order == "reverse" else callbacks}
    return result


def validate_private_lifecycle(image):
    symbols = dict(image.symbols(".symtab"))
    names = ("module_start", "ps5_kodi_crt_initialize",
             "module_stop", "ps5_kodi_crt_finalize")
    for name in names:
        symbol = symbols.get(name)
        require(symbol is not None and symbol[3] and symbol[1] & 15 == 2 and symbol[2] == 2,
                f"private app requires a defined hidden lifecycle function: {name}")
    require(len({symbols[name][4] for name in names}) == len(names),
            "native startup/unload entries must be distinct from private CRT lifecycle")


def compile_command(app, sdk, source, target):
    payload = app / ".deps/native/ps5-payload-sdk"
    cxx = source.suffix != ".c"
    return [sys.executable, str(TOOLS / "native-clang.py"),
            str(payload / ("bin/prospero-clang++" if cxx else "bin/prospero-clang")),
            "-std=c++20" if cxx else "-std=c11", "-O2",
            "-fPIC", "-femulated-tls", "-fno-stack-protector",
            *(["-nostdinc++", "-I" + str(payload / "target/include/c++/v1")] if cxx else []),
            "-I" + str(sdk / "include"), "-isystem", str(payload / "target/include"),
            "-ffunction-sections", "-fdata-sections", "-c", str(source), "-o", str(target)]


def run(command, app, log):
    log.write("$ " + shlex.join(map(str, command)) + "\n")
    log.flush()
    result = subprocess.run(command, cwd=app, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "PS5_PAYLOAD_SDK":
                                 str(app / ".deps/native/ps5-payload-sdk")}, check=False)
    if result.returncode:
        log.write(f"COMMAND FAILED: exit {result.returncode}\n")
        log.flush()
        raise subprocess.CalledProcessError(result.returncode, command)


def prepare_output(output):
    require(not output.is_symlink(), "refusing symlink output directory")
    if output.exists():
        require(output.is_dir(), "output is not a directory")
        # Never overwrite artifacts or erase a failed build. Retry in a fresh directory.
        require(not any(output.iterdir()), "output is not empty; choose a fresh --output")
    else:
        output.mkdir(parents=True)


def build(app, sdk, native_tool, output=None):
    app, sdk, native_tool = (Path(p).resolve() for p in (app, sdk, native_tool))
    output = Path(output).absolute() if output is not None else app / "build/kodi-app"
    require(not output.resolve().is_relative_to(app / "src") and
            not output.resolve().is_relative_to(app / "vendor") and
            not output.resolve().is_relative_to(sdk),
            "output must not overwrite application sources, vendor inputs or SDK")
    prepare_output(output)
    with (output / "build.log").open("x") as log:
        # Validation errors as well as command failures are retained in build.log.
        try:
            return _build(app, sdk, native_tool, output, log)
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            log.write(f"BUILD FAILED: {type(error).__name__}: {error}\n")
            raise


def _build(app, sdk, native_tool, output, log):
    require(native_tool.is_file() and os.access(native_tool, os.X_OK),
            "missing executable native tool")
    input_path = app / "vendor/ps5-shared-runtime.json"
    inputs = json.loads(input_path.read_text())
    require(inputs.get("schema") == 1 and inputs.get("format") == "ps5-shared-title-inputs",
            "expected staged ps5-shared-title-inputs schema 1")
    declared = providers(app, sdk, inputs)
    imports_path = app / "vendor/ps5-runtime-imports.rsp"
    imports = response_paths(imports_path, app)
    group_path = app / "vendor/libkodi_group.a"
    group = group_paths(group_path, app)
    # Legacy scripts mix unqualified stubs into GROUP/imports. Only declared
    # libraries may participate, and always before retained archives.
    for manifest, library, provider in declared:
        staged = [p for p in imports if p.name == library.name]
        require(len(staged) == 1 and digest(staged[0]) == digest(library),
                f"staged import library differs from verified provider: {manifest}")
    whole_path = app / "vendor/kodi-whole.rsp"
    whole = response_paths(whole_path, app)
    require(all(p.suffix in (".a", ".o") for p in whole), "non-static whole-archive input")
    static = [p for p in group if p.suffix != ".so"]
    require(static, "retained GROUP contains no static inputs")
    support = support_paths(app, sdk)
    startup_header = support["entry"].with_name("title_native_startup.h")
    require(startup_header.is_file(), "missing native application startup ABI header")
    network_symbols = shared_network_symbols(declared[0][2])
    sources = source_paths(app, declared[0][2]) + [support["entry"], support["crt"]]
    objects = []
    (output / "obj").mkdir()
    for index, source in enumerate(sources):
        obj = output / "obj" / f"{index:02d}-{source.stem}.o"
        command = compile_command(app, sdk, source, obj)
        if source == support["entry"]:
            command.insert(3, "-DPS5_PRIVATE_LEGACY_LIFECYCLE=1")
        elif source == support["crt"]:
            command.insert(3, "-Dmodule_start=ps5_kodi_crt_initialize")
            command.insert(3, "-Dmodule_stop=ps5_kodi_crt_finalize")
        run(command, app, log)
        objects.append(obj)
    local_group = output / "static-group.ld"
    local_group.write_text("GROUP (\n" + "".join(json.dumps(str(p)) + "\n" for p in static) + ")\n")
    exports_path = output / "exports.map"
    exports_path.write_text("{ global: " + "; ".join(ENTRIES) + "; local: *; };\n")
    linker_script = output / "native-app.ld"
    linker_script.write_text(legacy_linker_script(support["linker_script"]))
    link_map = output / "link.map"
    linked, converted, signed, extracted = (output / name for name in
        ("linked.so", "native.elf", MODULE + ".sprx", "extracted.elf"))
    # prospero-lld forcibly appends --hash-style=gnu and defaults to PIE.
    # Use the ordinary ELF linker, as the shared runtime builders do.
    linker = shutil.which("ld.lld")
    require(linker is not None, "missing ld.lld")
    wraps = inputs["wrap_symbols"] + ["pipe", "fcntl", "write"]
    require(len(set(wraps)) == len(wraps), "local/runtime wrap collision")
    command = [str(linker), "-shared", "--soname=" + MODULE + ".sprx",
               "--hash-style=sysv", "-z", "now", "-z", "max-page-size=0x4000",
               "--eh-frame-hdr", "--no-undefined", "--error-limit=0",
               "-init", "module_start", "-fini", "module_stop",
               "-T", str(linker_script), "--Map=" + str(link_map),
               "--version-script", str(exports_path), "-o", str(linked),
               *("--wrap=" + s for s in wraps),
               "--as-needed", *(str(p[1]) for p in declared),
               "--whole-archive", "@" + str(whole_path), "--no-whole-archive",
               *map(str, objects), str(local_group)]
    run(command, app, log)
    linked_bytes = linked.read_bytes()
    image = elf.Elf(linked_bytes)
    validate_private_lifecycle(image)
    legacy = legacy_lifecycle(image, link_map)
    legacy_proof = output / "legacy-lifecycle.json"
    legacy_proof.write_text(json.dumps(legacy, indent=2, sort_keys=True) + "\n")
    defined = {name for name, symbol in image.symbols(".symtab") if symbol[3]}
    protected = {s["name"] for s in declared[0][2]["symbols"]
                 if s["name"].startswith(("__wrap_", "__emutls_", "ps5_tls_", "__cxa_", "_Zn", "_Zd"))}
    protected.update(network_symbols)
    require(not defined & protected,
            f"private application shadows shared runtime ownership: {sorted(defined & protected)}")
    needed = [elf.cstring(image.section(".dynstr"), offset)
              for offset in native.dynamic_entries(image).get(1, [])]
    selected = [p[2] for p in declared if p[2]["soname"] in needed]
    require(needed == [p["soname"] for p in selected], "provider dependency order changed")
    converted.write_bytes(native.convert(linked_bytes, MODULE, selected, process_lifetime=True))
    exports, ownership = runtime.prove_provider(
        linked_bytes, converted.read_bytes(), MODULE, include_dependencies=True)
    require(set(exports) == set(ENTRIES), f"unexpected private app exports: {sorted(exports)}")
    require(all(exports[s][1] & 15 == 2 for s in ENTRIES), "entry is not a function")
    run([str(native_tool), "self", "--sign", "--file", str(converted),
         "--out", str(signed)], app, log)
    run([str(native_tool), "self", "--extract", "--file", str(signed),
         "--out", str(extracted)], app, log)
    require(extracted.read_bytes() == converted.read_bytes(),
            "signed native module extraction differs from converted ELF")
    with signed.open("rb") as stream:
        require(stream.read(4) == b"\x54\x14\xf5\xee", "signer did not produce FSELF")
    record = {
        "schema": 1, "format": FORMAT, "module": MODULE,
        "console_accepted": False, "process_lifetime": True, "native_unload_rejected": True,
        "callable_entries": ENTRIES, "exports": sorted(exports),
        "input_record": receipt(input_path), "sdk_manifest": receipt(sdk / "manifest.json"),
        "response_files": [receipt(p) for p in (whole_path, group_path, imports_path)],
        "whole_archive_inputs": [receipt(p) for p in whole],
        "group_static_inputs": [receipt(p) for p in static],
        "sources": [receipt(p) for p in sources],
        "build_support": [receipt(p) for p in
                          (support["compiler_wrapper"], support["linker_script"],
                           startup_header, Path(__file__).resolve(), native_tool)],
        "startup_header_sha256": digest(startup_header),
        "providers": [{"identity": {k: data[k] for k in IDENTITY},
                       "manifest": receipt(manifest), "import_library": receipt(library)}
                      for manifest, library, data in declared],
        "wrap_symbols": wraps, "import_ownership": ownership,
        "shared_network_symbols": sorted(network_symbols),
        "legacy_lifecycle": legacy,
        "normalized_linker_script": receipt(linker_script),
        "linked_sha256": digest(linked), "native_sha256": digest(converted),
        "signed_sha256": digest(signed),
        "sign_extract": {"exact_match": True, "native_sha256": digest(converted),
                         "extracted_sha256": digest(extracted)},
        "artifacts": {p.name: str(p) for p in
                      (linked, converted, signed, extracted, linker_script, link_map, legacy_proof)},
    }
    (output / "exported-symbols.json").write_text(json.dumps(sorted(exports), indent=2) + "\n")
    (output / "import-ownership.json").write_text(json.dumps(ownership, indent=2) + "\n")
    (output / "proof.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("app", "sdk", "native-tool"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.app, args.sdk, args.native_tool, args.output), indent=2))


if __name__ == "__main__":
    main()
