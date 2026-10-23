#!/usr/bin/env python3
"""Assemble a fresh, relocatable native add-on SDK; never deploy or package firmware."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from native_c_link import SYM, nid, require
import native_runtime_sdk as sdk
from native_shared_link import load_provider
from native_contract import validate_providers

ROOT = Path(__file__).resolve().parents[2]
WRAPS = ("malloc", "calloc", "realloc", "free", "posix_memalign",
         "malloc_usable_size", "aligned_alloc", "memalign", "reallocf")
TOOLS = ("build-native-shared-addon.py", "native_shared_link.py", "native_runtime_sdk.py",
         "native_c_link.py", "native_system_provider.py", "native_module_crt.cpp",
         "native-shared.ld", "native-clang.py", "native_static_depends.py")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative_file(root, name):
    path = (root / name).resolve()
    require(path.is_relative_to(root) and path.is_file(), f"invalid provider file: {name}")
    return path


def provider_directory(root):
    root = root.resolve()
    manifest = relative_file(root, "manifest.json")
    raw = json.loads(manifest.read_text())
    relative_file(root, raw["import_library"]["path"])
    return {"root": root, "manifest": manifest, "data": load_provider(manifest)}


def dependency_closure(providers):
    by_soname = {}
    by_module = {}
    for provider in providers:
        data = provider["data"]
        require(data["soname"] not in by_soname and data["module"] not in by_module,
                f"duplicate/conflicting provider: {data['module']}")
        by_soname[data["soname"]] = data
        by_module[data["module"]] = data
    edges = {}
    for provider in providers:
        data = provider["data"]
        deps = data.get("dependencies", {})
        needed = deps.get("needed", [])
        require(isinstance(needed, list) and len(set(needed)) == len(needed),
                f"invalid dependencies: {data['module']}")
        modules = {d["id"]: d for d in deps.get("modules", [])}
        libraries = {d["id"]: d for d in deps.get("libraries", [])}
        require(len(modules) == len(deps.get("modules", [])) and
                len(libraries) == len(deps.get("libraries", [])) and
                set(modules) == set(libraries) == set(range(1, len(needed) + 1)),
                f"invalid dependency identity table: {data['module']}")
        for index, soname in enumerate(needed, 1):
            require(soname in by_soname,
                    f"incomplete dependency closure: {data['module']} needs {soname}")
            target = by_soname[soname]
            module, library = modules[index], libraries[index]
            require(module["name"] == target["module"] and
                    module["version"] == target["module_version"] and
                    module.get("soname", soname) == soname and
                    library["name"] == target["library"] and
                    library["version"] == target["library_version"],
                    f"conflicting dependency identity/version: {data['module']} -> {soname}")
        for symbol in deps.get("imports", []):
            identity = symbol["module_id"]
            require(identity in modules and symbol["library_id"] == identity,
                    f"undeclared native import identity: {data['module']}")
            target = by_soname[needed[identity - 1]]
            require(any(s["name"] == symbol["name"] and s["nid"] == symbol["nid"] and
                        s["info"] & 15 == symbol["info"] & 15
                        for s in target["symbols"]),
                    f"missing dependency export: {data['module']} -> {symbol['name']}")
        edges[data["module"]] = list(needed)
    return edges


def core_wrap_symbols(core):
    contract = core.get("link_contract", {})
    require(contract.get("compiler_abi") == "ps5-sdk-native-frontend-v1",
            "core link_contract must attest the native PS5 SDK frontend ABI")
    wraps = contract.get("wrap_symbols")
    require(isinstance(wraps, list) and
            all(isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name)
                for name in wraps) and len(set(wraps)) == len(wraps) and
            set(WRAPS) <= set(wraps),
            "core link_contract.wrap_symbols must declare all nine heap wraps")
    exports = {s["name"] for s in core["symbols"] if s["info"] & 15 == 2}
    missing = ["__wrap_" + name for name in wraps if "__wrap_" + name not in exports]
    require(not missing, f"core is missing finalized common wrapper exports: {missing}")
    return list(wraps)


def publish_fresh(source, destination):
    if os.name == "nt":
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    require(hasattr(libc, "renameat2"), "atomic fresh-root publication requires renameat2")
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def verify_native(raw, provider):
    require(hashlib.sha256(raw).hexdigest() == provider["provider"]["native_elf_sha256"],
            f"own runtime native ELF hash mismatch: {provider['module']}")
    image = sdk.NativeImage(raw)
    image.validate_metadata_layout()
    strings = image.memory(image.tag(5), image.tag(10))
    for tag, name, version in ((sdk.MODULE_INFO, provider["module"], provider["module_version"]),
                               (sdk.EXPORT_LIB, provider["library"], provider["library_version"])):
        value = image.tag(tag)
        require(value >> 32 == version and sdk.string_at(strings, value & 0xFFFFFFFF) == name,
                f"own runtime native identity mismatch: {name}")
    rows = list(SYM.iter_unpack(image.memory(image.tag(6), image.tag(sdk.SYMTABSZ))))
    symbols = {sdk.string_at(strings, s[0]): s for s in rows
               if s[0] and s[3] and s[1] >> 4 in (1, 2)}
    require(len(symbols) == len(provider["symbols"]), "own runtime export count mismatch")
    for symbol in provider["symbols"]:
        actual = symbols.get(symbol["nid"] + "#A#A")
        require(actual and (actual[1], actual[2], actual[4], actual[5]) ==
                (symbol["info"], symbol["other"], symbol["value"], symbol["size"]),
                f"own runtime export metadata mismatch: {symbol['name']}")
    deps = provider.get("dependencies", {})
    imports = {s["dynsym_index"]: s for s in deps.get("imports", [])}
    undefined = {index: s for index, s in enumerate(rows) if s[0] and not s[3]}
    require(len(imports) == len(deps.get("imports", [])) and imports.keys() == undefined.keys(),
            f"own runtime native import metadata mismatch: {provider['module']}")
    for index, actual in undefined.items():
        declared = imports[index]
        parts = sdk.string_at(strings, actual[0]).split("#")
        require(len(parts) == 3 and parts[0] == declared["nid"] == nid(declared["name"]) and
                sdk.decode_id(parts[1]) == declared["library_id"] and
                sdk.decode_id(parts[2]) == declared["module_id"] and
                (actual[1], actual[2], actual[4], actual[5]) ==
                (declared["info"], declared["other"], declared["value"], declared["size"]),
                f"own runtime native import metadata mismatch at index {index}")
    actual_modules = sdk.import_identities(image, strings, sdk.NEEDED_MODULE, provider["module"])
    actual_libraries = sdk.import_identities(image, strings, sdk.IMPORT_LIB, provider["module"])
    for actual, declared in ((actual_modules, deps.get("modules", [])),
                              (actual_libraries, deps.get("libraries", []))):
        require(actual == {entry["id"]: {k: v for k, v in entry.items() if k != "soname"}
                           for entry in declared},
                f"own runtime dependency metadata mismatch: {provider['module']}")


def assemble(args):
    output = args.output.absolute()
    require(not output.exists() and not output.is_symlink(),
            "SDK output already exists; use a fresh output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.parent / ("." + output.name + "-build-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    report = {"schema": 1, "format": "ps5-native-addon-sdk-assembly",
              "status": "building", "console_accepted": False, "process_lifetime": True,
              "closure_scope": "declared provider module/library dependencies",
              "system_runtime_policy": "OS-owned; no firmware binaries packaged",
              "required_allocator_wraps": list(WRAPS)}
    try:
        tool = args.native_tool.resolve()
        require(tool.is_file() and os.access(tool, os.X_OK), "missing executable native tool")
        core, graphics = provider_directory(args.core), provider_directory(args.graphics)
        require(core["data"]["module"] == "libkodi-runtime", "core provider must be libkodi-runtime")
        require(graphics["data"]["module"] == "libkodi-gl", "graphics provider must be libkodi-gl")
        systems = [provider_directory(root) for root in args.system_sdk]
        require(all(p["data"]["module"] not in ("libkodi-runtime", "libkodi-gl") for p in systems),
                "system provider cannot replace an own runtime")
        providers = [core, graphics, *systems]
        edges = dependency_closure(providers)
        wraps = core_wrap_symbols(core["data"])
        validate_providers([p["data"] for p in providers], wraps)
        report["wrap_symbols"] = wraps
        report["link_contract"] = core["data"]["link_contract"]
        descriptor = {"schema": 1, "format": "ps5-native-addon-sdk",
                      "provider_manifests": [], "wrap_symbols": wraps,
                      "process_lifetime": True, "console_accepted": False,
                      "qualification": "metadata-verified-console-unqualified"}
        report["dependencies"] = edges
        for provider in providers:
            module = provider["data"]["module"]
            destination = ("runtime" if provider is core else "graphics" if provider is graphics
                           else "system/" + module)
            dest = work / destination
            library_path = provider["data"]["import_library"]["path"]
            require(library_path == "lib/" + module + ".so",
                    f"expected canonical linker-only import library: {module}")
            library = relative_file(provider["root"], library_path)
            require(not Path(library_path).is_absolute() and
                    ".." not in Path(library_path).parts, "nonportable provider import library path")
            (dest / library_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(provider["manifest"], dest / "manifest.json")
            shutil.copyfile(library, dest / library_path)
            require(load_provider(dest / "manifest.json") == provider["data"],
                    f"provider changed during SDK assembly: {module}")
            descriptor["provider_manifests"].append(destination + "/manifest.json")
        tools = work / "tools"
        tools.mkdir()
        for name in TOOLS:
            shutil.copyfile(ROOT / "tools/ps5" / name, tools / name)
        shutil.copy2(tool, tools / "ps5-native-tool")
        for name in ("PS5NativeRuntimeConfig.cmake", "NativeSharedAddon.cmake", "PS5NativeABI.cmake",
                     "NativeStaticDepends.cmake"):
            shutil.copyfile(ROOT / "cmake/scripts/ps5" / name, work / name)
        (work / "toolchain").mkdir()
        shutil.copyfile(ROOT / "toolchain/ps5-native-addon.cmake",
                        work / "toolchain/ps5-native-addon.cmake")
        (work / "include").mkdir()
        shutil.copyfile(ROOT / "tools/ps5/sdk/include/stdio.h", work / "include/stdio.h")
        shutil.copyfile(ROOT / "tools/ps5/native_abi_check.h", work / "include/ps5_native_abi.h")
        diagnostics = work / "diagnostics"
        diagnostics.mkdir()

        def run_tool(label, *command):
            result = subprocess.run([str(tool), "self", *map(str, command)],
                                    capture_output=True, text=True)
            (diagnostics / (label + ".stdout")).write_text(result.stdout)
            (diagnostics / (label + ".stderr")).write_text(result.stderr)
            require(result.returncode == 0, f"native tool failed: {label}")

        (work / "runtimes").mkdir()
        report["runtime_proofs"] = {}
        for provider in (core, graphics):
            data = provider["data"]
            module = data["module"]
            native = diagnostics / (module + ".elf")
            supplied = provider["root"] / "runtimes" / (module + ".sprx")
            if supplied.is_file():
                require(supplied.read_bytes()[:4] == b"\x54\x14\xf5\xee",
                        f"own runtime is not PS5 FSELF: {module}")
                run_tool(module + "-input-extract", "--extract", "--file", supplied, "--out", native)
            else:
                relative = ("native/" + module + ".elf"
                            if (provider["root"] / "native" / (module + ".elf")).is_file()
                            else "native-runtime-link.elf")
                source = relative_file(provider["root"], relative)
                shutil.copyfile(source, native)
            raw = native.read_bytes()
            verify_native(raw, data)
            signed = work / "runtimes" / (module + ".sprx")
            run_tool(module + "-sign", "--sign", "--file", native, "--out", signed)
            require(signed.read_bytes()[:4] == b"\x54\x14\xf5\xee", "signer did not produce PS5 FSELF")
            extracted = diagnostics / (module + "-roundtrip.elf")
            run_tool(module + "-output-extract", "--extract", "--file", signed, "--out", extracted)
            require(extracted.read_bytes() == raw, f"own runtime signing roundtrip differs: {module}")
            report["runtime_proofs"][module] = {
                "native_elf_sha256": hashlib.sha256(raw).hexdigest(),
                "fself_sha256": digest(signed), "sign_extract_equal": True,
                "native_metadata_layout_verified": True,
                "process_lifetime": True}
        shutil.rmtree(diagnostics)
        (work / "manifest.json").write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n")
        report["status"] = "metadata-verified-console-unqualified"
        report["files"] = {p.relative_to(work).as_posix(): digest(p)
                           for p in sorted(work.rglob("*")) if p.is_file()}
        (work / "receipt.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        checksums = "".join(f"{digest(p)}  {p.relative_to(work).as_posix()}\n"
                            for p in sorted(work.rglob("*")) if p.is_file())
        (work / "checksums.sha256").write_text(checksums)
        publish_fresh(work, output)
        return output
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = str(error)
        (work / "failure.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        raise ValueError(f"{error}; diagnostics retained at {work}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--graphics", type=Path, required=True)
    parser.add_argument("--system-sdk", type=Path, action="append", default=[])
    parser.add_argument("--native-tool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(assemble(args))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"prepare-native-addon-sdk: {error}\n")


if __name__ == "__main__":
    main()
