#!/usr/bin/env python3
"""Validate the reusable native runtime contract, separately from title provenance."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import hashlib
import json
from pathlib import Path

from native_shared_link import load_provider
from native_static_depends import load_verified_map

COMPILER_ABI = "ps5-sdk-native-frontend-v1"
NETWORK_SYMBOLS = frozenset((
    "getaddrinfo", "freeaddrinfo", "getnameinfo", "gai_strerror",
    "gethostname", "gethostbyname", "__h_errno",
))
WRAPS = frozenset((
    "malloc", "calloc", "realloc", "free", "posix_memalign",
    "malloc_usable_size", "aligned_alloc", "memalign", "reallocf",
    "chdir", "fopen", "pthread_create", "exit",
))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sdk_file(root, relative):
    require(isinstance(relative, str) and relative and
            not Path(relative).is_absolute() and ".." not in Path(relative).parts,
            f"invalid SDK-relative path: {relative}")
    path = root / relative
    require(not path.is_symlink() and path.resolve().is_relative_to(root) and path.is_file(),
            f"missing or escaping native SDK input: {path}")
    return path


def validate_providers(providers, wraps):
    by_module = {provider["module"]: provider for provider in providers}
    require(len(by_module) == len(providers) and
            len({p["soname"] for p in providers}) == len(providers),
            "duplicate native provider identity")
    require({"libkodi-runtime", "libkodi-gl"} <= by_module.keys(),
            "native SDK requires shared core and graphics providers")
    require(not any("libkodi-app" in p["module"] or "libkodi-app" in p["soname"]
                    for p in providers), "private Kodi application cannot be an SDK provider")
    core = by_module["libkodi-runtime"]
    contract = core.get("link_contract", {})
    require(contract.get("compiler_abi") == COMPILER_ABI and
            contract.get("process_lifetime") is True,
            "core requires the native compiler ABI and process-lifetime ownership")
    require(contract.get("stdio") ==
            {"opaque_file": True, "fpos_size": 24, "fpos_alignment": 8},
            "core does not attest the opaque native stdio ABI")
    require(isinstance(wraps, list) and all(isinstance(s, str) for s in wraps) and
            len(wraps) == len(set(wraps)) and WRAPS <= set(wraps) and
            contract.get("wrap_symbols") == wraps,
            "SDK and core must declare the complete common wrapper contract")
    functions = {s["name"] for s in core["symbols"] if s["info"] & 15 == 2}
    require({"__wrap_" + name for name in wraps} <= functions,
            "core is missing common wrapper function exports")
    network = contract.get("network", {})
    require(network.get("implementation") == "ps5-libc-net-v1" and
            set(network.get("symbols", [])) == NETWORK_SYMBOLS and
            NETWORK_SYMBOLS <= functions,
            "native SDK lacks the shared DNS capability; rebuild the shared core and SDK")
    for provider in providers:
        if provider is core or "name_catalogue" in provider:
            continue
        exports = {s["name"] for s in provider["symbols"]}
        require(not exports.intersection(NETWORK_SYMBOLS),
                f"duplicate shared DNS owner: {provider['module']}")
    return {"compiler_abi": COMPILER_ABI, "shared_dns": "ps5-libc-net-v1",
            "stdio": contract["stdio"],
            "providers": [{key: p[key] for key in
                           ("module", "module_version", "library", "library_version", "soname")}
                          for p in providers]}


def validate_sdk(sdk, archive_map=None):
    root = Path(sdk).resolve()
    require((root / "receipt.json").is_file(),
            f"missing prepared native SDK receipt at {root}; "
            "run prepare-native-addon-sdk.py with freshly built core/graphics providers")
    receipt = json.loads((root / "receipt.json").read_text())
    require(receipt.get("schema") == 1 and
            receipt.get("format") == "ps5-native-addon-sdk-assembly" and
            receipt.get("status") == "metadata-verified-console-unqualified" and
            receipt.get("process_lifetime") is True,
            "incomplete native SDK assembly receipt")
    files = receipt.get("files")
    require(isinstance(files, dict) and files, "native SDK receipt lacks input hashes")
    for relative in (
        "include/stdio.h", "include/ps5_native_abi.h", "PS5NativeRuntimeConfig.cmake",
        "PS5NativeABI.cmake", "NativeSharedAddon.cmake", "NativeStaticDepends.cmake",
        "tools/native_static_depends.py", "tools/build-native-shared-addon.py",
        "tools/native-clang.py", "tools/ps5-native-tool",
    ):
        require(relative in files, f"incomplete native SDK: {relative}; prepare a fresh SDK")
    descriptor_path = sdk_file(root, "manifest.json")
    require("manifest.json" in files, "native SDK descriptor is not covered by its receipt")
    for relative, expected in files.items():
        require(digest(sdk_file(root, relative)) == expected,
                f"native SDK input changed: {relative}; prepare a fresh SDK")
    descriptor = json.loads(descriptor_path.read_text())
    require(descriptor.get("schema") == 1 and
            descriptor.get("format") == "ps5-native-addon-sdk" and
            descriptor.get("process_lifetime") is True,
            "invalid native SDK descriptor")
    manifests = descriptor.get("provider_manifests")
    require(isinstance(manifests, list) and manifests, "native SDK lacks providers")
    providers = []
    for relative in manifests:
        require(relative in files, f"provider absent from SDK receipt: {relative}")
        path = sdk_file(root, relative)
        provider = load_provider(path)
        library = (path.parent / provider["import_library"]["path"]).resolve()
        require(library.is_relative_to(root) and
                library.relative_to(root).as_posix() in files,
                "native provider import library is not covered by the SDK receipt")
        providers.append(provider)
    contract = validate_providers(providers, descriptor.get("wrap_symbols"))
    require(receipt.get("link_contract") == next(
        p for p in providers if p["module"] == "libkodi-runtime")["link_contract"],
        "native SDK receipt/core contract mismatch")
    for module in ("libkodi-runtime", "libkodi-gl"):
        relative = f"runtimes/{module}.sprx"
        require(relative in files, f"native SDK is missing runtime: {module}")
        runtime = sdk_file(root, relative)
        proof = receipt.get("runtime_proofs", {}).get(module, {})
        provider = next(p for p in providers if p["module"] == module)
        require(runtime.read_bytes()[:4] == b"\x54\x14\xf5\xee" and
                proof.get("fself_sha256") == digest(runtime) and
                proof.get("native_elf_sha256") == provider["provider"]["native_elf_sha256"] and
                proof.get("sign_extract_equal") is True and
                proof.get("process_lifetime") is True,
                f"native SDK runtime receipt mismatch: {module}")
    contract["sdk_receipt_sha256"] = digest(root / "receipt.json")
    if archive_map is not None:
        mapping = load_verified_map(archive_map, sdk_file(root, "include/stdio.h"))
        contract["archive_map_sha256"] = digest(archive_map)
        contract["archive_replacements"] = len(mapping)
    return contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--archive-map", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(validate_sdk(args.sdk, args.archive_map), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"native runtime contract: {error}\n")


if __name__ == "__main__":
    main()
