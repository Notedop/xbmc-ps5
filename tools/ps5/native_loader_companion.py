#!/usr/bin/env python3
"""Build the mandatory app-local libc.prx loader companion, not a C API provider."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from native_c_link import SYM, nid, require
from native_runtime_sdk import NativeImage, SYMTABSZ, decode_id, prove_provider, string_at
from native_shared_link import convert, load_provider

ROOT = Path(__file__).resolve().parents[2]
MARKER = "ps5_native_loader_companion_v1"
IMPORTS = {
    "malloc": "libSceLibcInternal",
    "free": "libSceLibcInternal",
    "posix_memalign": "libSceLibcInternal",
    "_sceKernelRtldSetApplicationHeapAPI": "libkernel",
    "sceKernelDebugOutText": "libkernel",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def providers_from_sdk(sdk):
    descriptor = json.loads((sdk / "manifest.json").read_text())
    selected = {}
    for name in descriptor["provider_manifests"]:
        path = sdk / name
        module = json.loads(path.read_text())["module"]
        if module in set(IMPORTS.values()):
            require(module not in selected, "duplicate loader companion provider")
            selected[module] = load_provider(path)
            selected[module]["_library_path"] = str(
                path.parent / selected[module]["import_library"]["path"])
    require(set(selected) == set(IMPORTS.values()), "missing native loader companion providers")
    return [selected[name] for name in ("libSceLibcInternal", "libkernel")]


def verify_native(linked, native, providers):
    exports = prove_provider(linked, native, "libc")
    require(set(exports) == {MARKER}, "loader companion must not export replacement C APIs")
    require(exports[MARKER][1] & 15 == 1 and exports[MARKER][5] == 4,
            "invalid loader companion version marker")
    require(native == convert(linked, "libc", providers), "loader companion conversion changed")
    image = NativeImage(native)
    require(image.memory(exports[MARKER][4], 4) == b"\x01\0\0\0",
            "unsupported loader companion version")
    strings = image.memory(image.tag(5), image.tag(10))
    modules = {value >> 48: string_at(strings, value & 0xFFFFFFFF)
               for value in image.tags[0x61000045]}
    libraries = {value >> 48: string_at(strings, value & 0xFFFFFFFF)
                 for value in image.tags[0x61000049]}
    actual = {}
    for symbol in SYM.iter_unpack(image.memory(image.tag(6), image.tag(SYMTABSZ))):
        if symbol[0] and not symbol[3]:
            identifier, library, module = string_at(strings, symbol[0]).split("#")
            require(identifier not in actual, "duplicate loader companion import")
            actual[identifier] = (modules[decode_id(module)], libraries[decode_id(library)])
    require(actual == {nid(name): (module, module) for name, module in IMPORTS.items()},
            "loader companion must import real system allocation and loader APIs only")
    return {"exports": [MARKER], "import_owners": IMPORTS,
            "native_metadata_layout_verified": True,
            "replacement_c_exports": False, "owns_heap_tls_or_cxx": False}


def verify_artifacts(directory, sdk, tool, signed=None):
    proof = json.loads((directory / "proof.json").read_text())
    require(proof["format"] == "ps5-native-loader-companion" and
            proof["sdk_manifest_sha256"] == digest(sdk / "manifest.json"),
            "loader companion SDK/proof mismatch")
    linked, native = directory / "linked.so", directory / "native.elf"
    signed = signed or directory / "libc.prx"
    for path, key in ((linked, "linked_sha256"), (native, "native_sha256"),
                      (signed, "signed_sha256")):
        require(digest(path) == proof[key], f"loader companion artifact changed: {path}")
    require(proof["source_sha256"] == digest(ROOT / "shims/native-app/loader_companion.c"),
            "loader companion source changed; rebuild required")
    verified = verify_native(linked.read_bytes(), native.read_bytes(), providers_from_sdk(sdk))
    extracted = directory / "verified.elf"
    subprocess.run([str(tool), "self", "--extract", "--file", str(signed),
                    "--out", str(extracted)], check=True, capture_output=True, text=True)
    require(extracted.read_bytes() == native.read_bytes(),
            "loader companion signing changed native bytes")
    require(proof["verification"] == verified, "loader companion verification receipt changed")
    return proof


def build(sdk, output, tool, payload_sdk=None):
    sdk, output, tool = sdk.resolve(), output.resolve(), tool.resolve()
    require(not output.exists(), "loader companion output already exists; use a fresh directory")
    require(tool.is_file(), "native signing tool missing")
    providers = providers_from_sdk(sdk)
    payload_sdk = payload_sdk or Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
    lld = shutil.which("ld.lld-18") or shutil.which("ld.lld")
    require(lld is not None, "LLD is required for the native shared-module layout")
    source = ROOT / "shims/native-app/loader_companion.c"
    output.mkdir(parents=True)
    command = [
        sys.executable, str(ROOT / "tools/ps5/native-clang.py"),
        str(payload_sdk / "bin/prospero-clang"), "-std=c11", "-O2", "-fPIC",
        "-femulated-tls", "-fno-stack-protector", "-ffreestanding",
        "-isystem", str(payload_sdk / "target/include"),
        "-c", str(source), "-o", str(output / "companion.o"),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    linked = output / "linked.so"
    subprocess.run([
        lld, "-shared", "--hash-style=sysv",
        "--eh-frame-hdr", "-z", "now", "-z", "max-page-size=0x4000", "--no-undefined",
        "-T", str(ROOT / "tools/ps5/native-shared.ld"),
        "-init", "module_start", "-fini", "module_stop", "-o", str(linked),
        str(output / "companion.o"),
        *[provider["_library_path"] for provider in providers],
    ], check=True, capture_output=True, text=True)
    native = output / "native.elf"
    native.write_bytes(convert(linked.read_bytes(), "libc", providers))
    verification = verify_native(linked.read_bytes(), native.read_bytes(), providers)
    signed = output / "libc.prx"
    subprocess.run([str(tool), "self", "--sign", "--in", str(native),
                    "--out", str(signed)], check=True, capture_output=True, text=True)
    signed.chmod(0o755)
    proof = {
        "schema": 1, "format": "ps5-native-loader-companion",
        "console_accepted": False, "process_lifetime": True,
        "source_sha256": digest(source), "sdk_manifest_sha256": digest(sdk / "manifest.json"),
        "linked_sha256": digest(linked), "native_sha256": digest(native),
        "signed_sha256": digest(signed), "verification": verification,
    }
    (output / "proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    return verify_artifacts(output, sdk, tool)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-tool", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.sdk, args.output, args.native_tool)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        parser.exit(1, f"native-loader-companion: {detail}\n")


if __name__ == "__main__":
    main()
