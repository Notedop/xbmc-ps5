#!/usr/bin/env python3
"""Build the native shared C++/platform core and linker SDK; never deploy."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from native_c_link import require
from native_runtime_sdk import generate
from native_shared_link import convert, load_provider, select_providers

ROOT = Path(__file__).resolve().parents[2]
MODULE = "libkodi-runtime"
spec = importlib.util.spec_from_file_location(
    "shared_cxx_qualifier", Path(__file__).with_name("qualify-shared-cxx.py"))
qualifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualifier)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_system_providers(paths):
    providers = [load_provider(p) for p in paths]
    require(providers, "the shared core requires verified native-system provider SDKs")
    require(len({p["module"] for p in providers}) == len(providers),
            "duplicate native-system provider SDKs")
    require({"libkernel", "libSceLibcInternal"} <= {p["module"] for p in providers},
            "the shared core requires native kernel and libc SDK contracts")
    return providers


def build(args):
    output = args.output.resolve()
    require(not output.exists() and not output.is_symlink(),
            "runtime output already exists; use a fresh output directory")
    native_tool = args.native_tool.resolve()
    require(native_tool.is_file() and os.access(native_tool, os.X_OK),
            f"missing executable native tool: {native_tool}")
    provider_paths = [p.resolve() for p in args.provider]
    providers = verified_system_providers(provider_paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="." + output.name + "-", dir=output.parent))
    report = {
        "schema": 1,
        "format": "ps5-native-core-build",
        "console_accepted": False,
        "process_lifetime": True,
        "wrap_symbols": list(qualifier.RUNTIME_WRAPS),
        "compiler_abi": "ps5-sdk-native-frontend-v1",
        "native_tool": {"path": str(native_tool), "sha256": digest(native_tool)},
        "commands": [],
    }
    try:
        code = qualifier.qualify(args.sdk, work / "qualification", args.lld, args.clang,
                                 args.ar, args.objcopy, provider_paths)
        require(code == 0, "shared core strict link failed; see qualification logs")
        qualification = json.loads(
            (work / "qualification/qualification-report.json").read_text())
        require(qualification["status"] == "strict-link-passed" and
                qualification["system_import_mode"] == "verified-native" and
                not qualification["native_profile_blockers"],
                "shared core still has native profile blockers")
        linked = work / "qualification/diagnostic-linked.so"
        native = work / "native" / (MODULE + ".elf")
        native.parent.mkdir()
        source = linked.read_bytes()
        native.write_bytes(convert(source, MODULE, select_providers(source, providers)))
        manifest = generate(linked, native, MODULE, work, args.lld)
        names = {s["name"] for s in manifest["symbols"]}
        required = {"__wrap_" + name for name in qualifier.RUNTIME_WRAPS}
        required.update(("ps5_shared_heap_initialize", "__emutls_get_address",
                         "__cxa_thread_atexit_impl"))
        required.update(qualifier.NETWORK_SYMBOLS)
        require(required <= names,
                "shared core is missing required exports: " + ", ".join(sorted(required - names)))
        functions = {s["name"] for s in manifest["symbols"] if s["info"] & 15 == 2}
        require(set(qualifier.NETWORK_SYMBOLS) <= functions,
                "shared core network services must be function exports")
        source_hash = digest(qualifier.NETWORK_SOURCE)
        require(qualification["sources"].get(str(qualifier.NETWORK_SOURCE)) == source_hash,
                "shared core network source changed during qualification")
        require(not {"malloc", "calloc", "realloc", "free"} & names,
                "shared core must export allocator wrappers, not shadow native allocation")
        manifest["link_contract"] = {
            "wrap_symbols": list(qualifier.RUNTIME_WRAPS),
            "process_lifetime": True,
            "compiler_abi": report["compiler_abi"],
            "stdio": {"opaque_file": True, "fpos_size": 24, "fpos_alignment": 8},
            "network": {
                "implementation": "ps5-libc-net-v1",
                "symbols": list(qualifier.NETWORK_SYMBOLS),
                "source_sha256": source_hash,
            },
        }
        (work / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        runtime = work / "runtimes" / (MODULE + ".sprx")
        runtime.parent.mkdir()
        roundtrip = work / "native" / "roundtrip.elf"
        commands = [
            [str(native_tool), "self", "--sign", "--file", str(native), "--out", str(runtime)],
            [str(native_tool), "self", "--extract", "--file", str(runtime),
             "--out", str(roundtrip)],
        ]
        for command in commands:
            subprocess.run(command, check=True)
            report["commands"].append(command)
        require(roundtrip.read_bytes() == native.read_bytes(),
                "signed shared core extraction differs from its native ELF")
        roundtrip.unlink()
        report["status"] = "native-built-console-unqualified"
        report["export_count"] = len(names)
        report["runtime_filename"] = str(runtime.relative_to(work))
        report["files"] = {
            str(p.relative_to(work)): digest(p) for p in (
                native, runtime, work / "manifest.json",
                work / manifest["import_library"]["path"],
                work / "qualification/qualification-report.json")
        }
        (work / "build.json").write_text(json.dumps(report, indent=2) + "\n")
        work.rename(output)
    except BaseException as error:
        report["status"] = "blocked"
        report["error"] = str(error)
        (work / "build.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"Shared core failure evidence retained at {work}", file=sys.stderr)
        raise
    print(f"Shared core built: {output}; console acceptance is still required")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=Path("/opt/ps5-payload-sdk"))
    parser.add_argument("--output", type=Path,
                        default=ROOT / "build/ps5-shared-runtime/core")
    parser.add_argument("--provider", type=Path, action="append", required=True)
    parser.add_argument("--native-tool", type=Path, required=True)
    parser.add_argument("--lld", default=shutil.which("ld.lld"))
    parser.add_argument("--clang", default=shutil.which("clang-18"))
    parser.add_argument("--ar", default=shutil.which("llvm-ar-18"))
    parser.add_argument("--objcopy", default=shutil.which("llvm-objcopy-18"))
    args = parser.parse_args()
    if not all((args.lld, args.clang, args.ar, args.objcopy)):
        parser.error("ld.lld, clang-18, llvm-ar-18 and llvm-objcopy-18 are required")
    try:
        build(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"build-shared-runtime: {error}\n")


if __name__ == "__main__":
    main()
