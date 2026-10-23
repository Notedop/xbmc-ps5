#!/usr/bin/env python3
"""Prepare fingerprinted production core, graphics, native dependencies and add-on SDK."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from addon_build import _InputHasher, _sdk_inputs
from addon_repository import workflow_lock
from native_contract import NETWORK_SYMBOLS, digest, require, validate_sdk
from native_shared_link import load_provider
from native_c_link import Elf, nid
from native_runtime_sdk import eligible_symbols, import_object, validate_stub

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SYSTEM_MODULES = (
    "libkernel", "libSceLibcInternal", "libSceNet", "libSceAgc", "libSceAgcDriver",
    "libSceVideoOut", "libSceSystemService", "libSceAudioOut", "libSceImeDialog",
    "libScePad", "libSceSysmodule", "libSceUserService", "libSceVideodec2",
)


def system_contract(source, module, output, lld):
    symbols = eligible_symbols(Elf(source.read_bytes()).symbols(".dynsym"), str(source))
    symbols = {name: row for name, row in symbols.items() if name not in NETWORK_SYMBOLS}
    output.mkdir(parents=True)
    (output / "lib").mkdir()
    obj = output / "import.o"
    obj.write_bytes(import_object(symbols))
    stub = output / "lib" / (module + ".so")
    subprocess.run([str(lld), "-shared", "--hash-style=sysv", "--soname=" + module + ".sprx",
                    "-o", str(stub), str(obj)], check=True, capture_output=True, text=True)
    validate_stub(stub.read_bytes(), symbols, module)
    obj.unlink()
    manifest = output / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": 1, "format": "ps5-native-runtime-sdk", "module": module, "library": module,
        "module_version": 257, "library_version": 1, "soname": module + ".sprx",
        "linker_only": True, "runtime_loadable": False,
        "runtime_implementation_qualified": False,
        "proof_scope": "SDK-declared imports; not installed system implementation validation",
        "provider": {"module": module, "export_library": module,
                     "module_version": 257, "library_version": 1},
        "source_import_library_sha256": digest(source),
        "import_library": {"path": "lib/" + module + ".so", "sha256": digest(stub)},
        "symbols": [dict(name=name, nid=nid(name), info=row[1], other=row[2],
                         value=row[4], size=row[5]) for name, row in sorted(symbols.items())],
    }, indent=2, sort_keys=True) + "\n")
    load_provider(manifest)
    return manifest


def prepare(args):
    output = args.output.resolve()
    def system_library(module):
        root = args.graphics_imports if module in ("libSceAgc", "libSceAgcDriver") else \
            args.sdk / "target/lib"
        return root / (module + ".so")

    providers = []
    for directory in args.system_sdk:
        manifests = sorted(directory.resolve().glob("*/manifest.json"))
        require(manifests, f"no native provider SDKs in {directory}")
        providers.extend(manifests)
    identities = [load_provider(path)["module"] for path in providers]
    require(len(set(identities)) == len(identities), "duplicate native system providers")
    require(not providers or {"libSceLibcInternal", "libkernel", "libSceNet",
             "libSceAgc", "libSceAgcDriver", "libSceVideoOut",
             "libSceSystemService"} <= set(identities),
            "supply native libc/kernel/network/graphics SDK contracts and linker-only "
            "import libraries via --system-sdk; firmware analysis files are not build inputs")
    source = args.source.resolve()
    inputs = [
        *[p for p in HERE.glob("*.py") if not p.name.startswith("test")],
        ROOT / "shims/native-app", ROOT / "shims/libkodishim",
        ROOT / "cmake/scripts/ps5", ROOT / "toolchain/ps5-native-addon.cmake",
        *_sdk_inputs(args.sdk), args.depends / "lib", args.opengl,
        args.homebrew_sources / "fmt/12.1.0.tar.gz",
        args.homebrew_sources / "fmt/PKGBUILD",
        args.homebrew_sources / "libpng/libpng-1.6.43.tar.xz",
        args.homebrew_sources / "libpng/PKGBUILD",
        source / "src", source / "toolchain",
        source / "third_party/mesa-26.2.0/src",
        source / "third_party/mesa-26.2.0/include",
        source / "third_party/opengnm-psbc/src",
        source / "build/core33-native-runtime/runtime-config.txt",
        source / "build/mesa-ps5-probe/meson-private/cmd_line.txt",
        source / "build/mesa-ps5-probe/meson-info/intro-buildoptions.json",
        args.native_tool,
    ]
    for path in providers:
        provider = load_provider(path)
        inputs.extend((path, path.parent / provider["import_library"]["path"]))
    inputs.extend(ROOT / "tools/depends/target" / package for package in
                  ("bzip2", "curl", "openssl", "gettext", "libsmb2"))
    if not providers:
        inputs.extend(system_library(module) for module in SYSTEM_MODULES)
    for path in inputs:
        require(path.exists(), f"missing native preparation input: {path}")
    output.mkdir(parents=True, exist_ok=True)
    with workflow_lock(output):
        hasher = _InputHasher(output)
        fingerprint = hasher.tree_digest(inputs)
        hasher.save()
        work = output / "artifacts" / fingerprint
        sdk, archive_map = work / "sdk", work / "depends/archive-map.json"
        receipt_path = work / "preparation.json"
        if receipt_path.is_file():
            saved = json.loads(receipt_path.read_text())
            require(saved.get("fingerprint") == fingerprint, "preparation fingerprint differs")
            validate_sdk(sdk, archive_map)
            for name, expected in saved["files"].items():
                require(digest(work / name) == expected, f"prepared runtime changed: {name}")
            print(f"Reusing verified native runtime: {work}", flush=True)
        else:
            require(not work.exists(), f"incomplete native preparation at {work}; "
                    "diagnostics retained; choose a fresh --output after correcting the failure")
            work.mkdir(parents=True)
            if not providers:
                providers = [system_contract(system_library(module),
                                             module, work / "system-sdk" / module,
                                             args.sdk / "bin/ld.lld")
                             for module in SYSTEM_MODULES]

            def run(name, *arguments):
                log = work / (name + ".log")
                print(f"Preparing {name}; log: {log}", flush=True)
                command = [sys.executable, str(HERE / (name + ".py")), *map(str, arguments)]
                with log.open("w") as stream:
                    result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
                require(result.returncode == 0,
                        f"native preparation failed ({result.returncode}): {name}; see {log}")

            provider_args = [item for path in providers for item in ("--provider", path)]
            run("build-shared-runtime", "--sdk", args.sdk, "--output", work / "core",
                "--native-tool", args.native_tool, *provider_args)
            run("build-shared-graphics", "--source", source, "--sdk", args.sdk,
                "--installed", args.opengl, "--output", work / "graphics-archives",
                "--sdk-include", HERE / "sdk/include", "--jobs", args.jobs)
            run("build-shared-graphics-module", "--source", source, "--sdk", args.sdk,
                "--graphics", work / "graphics-archives", "--output", work / "graphics",
                "--core", work / "core/manifest.json", "--convert", *provider_args)
            run("build-native-stdio-depends", "--sdk", args.sdk, "--depends", args.depends,
                "--homebrew-sources", args.homebrew_sources, "--output", work / "depends",
                "--jobs", args.jobs)
            system_args = [item for path in providers for item in ("--system-sdk", path.parent)]
            run("prepare-native-addon-sdk", "--core", work / "core",
                "--graphics", work / "graphics", "--native-tool", args.native_tool,
                "--output", sdk, *system_args)
            validate_sdk(sdk, archive_map)
            require(hasher.tree_digest(inputs) == fingerprint,
                    "native preparation inputs changed during the build; retained outputs "
                    "are not published as the current SDK")
            hasher.save()
            receipt_path.write_text(json.dumps({
                "schema": 1, "fingerprint": fingerprint,
                "files": {p.relative_to(work).as_posix(): digest(p) for p in
                          (work / "core/build.json", work / "graphics/build.json",
                           work / "graphics-archives/build.json", sdk / "receipt.json",
                           archive_map)},
            }, indent=2, sort_keys=True) + "\n")
        for name in ("sdk", "depends"):
            pointer = output / name
            if pointer.exists() or pointer.is_symlink():
                require(pointer.is_symlink() and
                        pointer.resolve().is_relative_to(output / "artifacts"),
                        f"refusing to replace unowned preparation path: {pointer}")
            temporary = output / ("." + name + "-" + fingerprint)
            require(not temporary.exists() and not temporary.is_symlink(),
                    f"unfinished native preparation pointer: {temporary}")
            temporary.symlink_to((work / name).relative_to(output), target_is_directory=True)
            os.replace(temporary, pointer)
    print(f"PS5_NATIVE_RUNTIME_SDK={output / 'sdk'}\n"
          f"PS5_NATIVE_DEPENDS_MAP={output / 'depends/archive-map.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-sdk", type=Path, action="append", default=[],
                        help="optional provider contract roots; otherwise use payload SDK imports")
    parser.add_argument("--output", type=Path, default=ROOT / "build/ps5-native")
    parser.add_argument("--sdk", type=Path, default=Path(os.environ.get(
        "PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk")))
    parser.add_argument("--depends", type=Path, default=Path(os.environ.get(
        "PS5_DEPENDS_PREFIX", ROOT / "build/ps5-depends-test/x86_64-unknown-freebsd-debug")))
    parser.add_argument("--source", type=Path, default=ROOT / "build/ps5-external/ps5-opengl")
    parser.add_argument("--opengl", type=Path, default=Path(os.environ.get(
        "PS5_OPENGL_PREFIX", "/opt/ps5-opengl-gl46")))
    parser.add_argument("--graphics-imports", type=Path,
                        help="graphics SDK directory containing the AGC import libraries")
    parser.add_argument("--homebrew-sources", type=Path,
                        default=Path(os.environ.get(
                            "PS5_HOMEBREW_SOURCES", ROOT / "build/ps5-external/pacbrew-repo")))
    parser.add_argument("--native-tool", type=Path, default=ROOT /
                        "build/ps5-external/ps5-opengl/build/native-app/PPSA99005/build/host/ps5-native-tool")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    args = parser.parse_args()
    if args.graphics_imports is None:
        args.graphics_imports = args.opengl / "lib"
    if not 1 <= args.jobs <= 32:
        parser.error("--jobs must be between 1 and 32")
    try:
        prepare(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"native preparation: {error}\n")


if __name__ == "__main__":
    main()
