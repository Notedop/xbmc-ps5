#!/usr/bin/env python3
"""Package an ordinary SDK-linked add-on ELF as a native module; never deploy."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inside(root, name):
    require(isinstance(name, str) and name, "missing SDK-relative path")
    path = (root / name).resolve()
    require(path.is_relative_to(root), f"path escapes SDK: {name}")
    require(path.is_file(), f"missing SDK file: {path}")
    return path


def describe(sdk):
    root = sdk.resolve()
    descriptor = json.loads((root / "manifest.json").read_text())
    require(descriptor.get("schema") == 1 and
            descriptor.get("format") == "ps5-native-addon-sdk",
            "expected prepared ps5-native-addon-sdk schema 1")
    for name in ("native_shared_link.py", "native_runtime_sdk.py", "native_c_link.py",
                 "native_system_provider.py", "native_module_crt.cpp", "native-shared.ld",
                 "ps5-native-tool", "native-clang.py"):
        inside(root, "tools/" + name)
    inside(root, "include/stdio.h")
    inside(root, "include/ps5_native_abi.h")
    inside(root, "PS5NativeABI.cmake")
    sys.path.insert(0, str(root / "tools"))
    link = importlib.import_module("native_shared_link")
    manifests = descriptor.get("provider_manifests")
    require(isinstance(manifests, list) and manifests, "missing provider_manifests")
    providers = []
    for name in manifests:
        path = inside(root, name)
        provider = json.loads(path.read_text())
        library = inside(root, str(path.parent.relative_to(root) /
                                   provider["import_library"]["path"]))
        provider = link.load_provider(path)
        providers.append({"manifest": str(path), "library": str(library),
                          "module": provider["module"], "soname": provider["soname"]})
    require(len({p["soname"] for p in providers}) == len(providers),
            "duplicate provider SONAME")
    wraps = descriptor.get("wrap_symbols")
    require(isinstance(wraps, list) and wraps and
            all(isinstance(s, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", s)
                for s in wraps) and len(set(wraps)) == len(wraps),
            "invalid wrap_symbols")
    core = next((p for p in providers if p["module"] == "libkodi-runtime"), None)
    require(core is not None, "missing libkodi-runtime provider")
    core_data = link.load_provider(core["manifest"])
    core_exports = {s["name"] for s in core_data["symbols"] if s["info"] & 15 == 2}
    missing = ["__wrap_" + symbol for symbol in wraps
               if "__wrap_" + symbol not in core_exports]
    require(not missing, f"core provider is missing allocator wrapper exports: {missing}")
    contract = core_data.get("link_contract", {})
    require(contract.get("compiler_abi") == "ps5-sdk-native-frontend-v1",
            "core link_contract must attest the native PS5 SDK frontend ABI")
    require(contract.get("wrap_symbols") == wraps,
            "aggregate wrap_symbols must exactly match core link_contract.wrap_symbols")
    return {"root": str(root), "providers": providers, "wrap_symbols": wraps}


def build(args):
    sdk = describe(args.sdk)
    link = importlib.import_module("native_shared_link")
    elf = importlib.import_module("native_c_link")
    source = args.source.read_bytes()
    image = elf.Elf(source)
    needed = [elf.cstring(image.section(".dynstr"), offset)
              for offset in link.dynamic_entries(image).get(1, [])]
    declared = {p["soname"]: p for p in sdk["providers"]}
    require(len(set(needed)) == len(needed), "duplicate source DT_NEEDED")
    require(set(needed) <= declared.keys(),
            f"undeclared source DT_NEEDED: {sorted(set(needed) - declared.keys())}")
    selected = [declared[name] for name in needed]
    providers = [link.load_provider(p["manifest"]) for p in selected]
    converted = link.convert(source, args.module, providers)
    runtime = importlib.import_module("native_runtime_sdk")
    _, dependencies = runtime.prove_provider(
        source, converted, args.module, include_dependencies=True)
    output = args.output.absolute()
    receipt = output.with_suffix(output.suffix + ".json")
    require(args.source.resolve() not in (output.resolve(), receipt.resolve()),
            "source ELF must be separate from native output and receipt")
    output.parent.mkdir(parents=True, exist_ok=True)
    require(not output.is_symlink() and not receipt.is_symlink(),
            "refusing symlink output or receipt")
    previous_output = previous_receipt = None
    if output.exists() or receipt.exists():
        require(output.is_file() and receipt.is_file(), "unowned native output/receipt")
        previous_output, previous_receipt = output.read_bytes(), receipt.read_bytes()
        previous = json.loads(previous_receipt)
        require(previous.get("format") == "ps5-native-addon" and
                previous.get("module") == args.module and
                previous.get("output_sha256") == digest(previous_output),
                "refusing to overwrite unowned/modified native output")
    stage = output.parent / (".native-addon-" + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        native, signed, extracted = (stage / name for name in
                                     ("native.elf", "signed.sprx", "extracted.elf"))
        native.write_bytes(converted)
        tool = args.native_tool or Path(sdk["root"]) / "tools/ps5-native-tool"
        require(tool.is_file() and os.access(tool, os.X_OK), "missing executable native tool")
        subprocess.run([str(tool), "self", "--sign", "--file", str(native),
                        "--out", str(signed)], check=True)
        subprocess.run([str(tool), "self", "--extract", "--file", str(signed),
                        "--out", str(extracted)], check=True)
        require(extracted.read_bytes() == converted,
                "signed native module extraction differs from converted ELF")
        signed_bytes = signed.read_bytes()
        require(signed_bytes[:4] == b"\x54\x14\xf5\xee",
                f"signer did not produce FSELF: {signed_bytes[:4].hex()}")
        record = {"schema": 1, "format": "ps5-native-addon", "module": args.module,
                  "source_sha256": digest(source), "native_elf_sha256": digest(converted),
                  "output_sha256": digest(signed_bytes), "needed": needed,
                  "compiler_abi": "ps5-sdk-native-frontend-v1",
                  "sign_extract_equal": True,
                  "native_dependencies": dependencies,
                  "provider_requirements": [
                      {key: p[key] for key in
                       ("module", "module_version", "library", "library_version", "soname")}
                      for p in providers],
                  "providers": [{"module": p["module"],
                                 "manifest_sha256": digest(Path(p["manifest"]).read_bytes())}
                                for p in selected]}
        staged_receipt = stage / "receipt.json"
        staged_receipt.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        os.replace(signed, output)
        try:
            os.replace(staged_receipt, receipt)
        except BaseException:
            if previous_output is None:
                output.unlink()
            else:
                native.write_bytes(previous_output)
                os.replace(native, output)
            raise
    finally:
        shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--module")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--native-tool", type=Path)
    args = parser.parse_args()
    if args.describe:
        print(json.dumps(describe(args.sdk)))
    else:
        require(args.source and args.module and args.output,
                "--source, --module and --output are required")
        build(args)


if __name__ == "__main__":
    main()
