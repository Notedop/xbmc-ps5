#!/usr/bin/env python3
"""Build the system-only executable that loads Kodi after its native providers."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from native_c_link import Elf, nid, require
from native_shared_link import load_provider
from native_title_launcher import STARTUP_ABI, STARTUP_SOURCES, verify

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(app, sdk, native_tool):
    app, sdk, native_tool = app.resolve(), sdk.resolve(), native_tool.resolve()
    record = json.loads((app / "vendor/ps5-shared-runtime.json").read_text())
    require(record["sdk_manifest_sha256"] == digest(sdk / "manifest.json"),
            "launcher SDK manifest changed")
    descriptor = json.loads((sdk / "manifest.json").read_text())
    selected = {}
    for relative in descriptor["provider_manifests"]:
        manifest = sdk / relative
        if json.loads(manifest.read_text())["module"] not in ("libkernel", "libSceLibcInternal"):
            continue
        provider = load_provider(manifest)
        if provider["module"] in ("libkernel", "libSceLibcInternal"):
            require(provider["module"] not in selected, "duplicate launcher provider")
            selected[provider["module"]] = (provider, manifest)
    require(set(selected) == {"libkernel", "libSceLibcInternal"},
            "launcher requires verified kernel and native C providers")
    native = app / "tooling/native"
    source = native / "title_native_launcher.cpp"
    require(set(record["startup_sources"]) == STARTUP_SOURCES and
            all(record["startup_sources"][name] == digest(native / name)
                for name in STARTUP_SOURCES), "startup source receipt changed")
    target = app / ".deps/native/ps5-payload-sdk"
    compiler = target / "bin/prospero-clang++"
    output = app / "build"
    objects = output / "obj"
    objects.mkdir(parents=True, exist_ok=True)
    commands = []
    with (output / "launcher-build.log").open("w") as log:
        def run(command):
            commands.append([str(part) for part in command])
            log.write("\n" + " ".join(commands[-1]) + "\n")
            log.flush()
            subprocess.run(commands[-1], cwd=app, check=True, stdout=log,
                           stderr=subprocess.STDOUT)

        flags = ["-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror",
                 "-fPIC", "-femulated-tls", "-fno-stack-protector",
                 "-fno-exceptions", "-fno-rtti", "-ffunction-sections", "-fdata-sections",
                 "-nostdinc++", "-I" + str(target / "target/include/c++/v1"),
                 "-I" + str(sdk / "include"), "-isystem", str(target / "target/include")]
        compiled = []
        for name, path in (("title_launcher", source), ("launcher_crt", native / "app_crt.cpp")):
            object_path = objects / (name + ".o")
            run([sys.executable, app / "vendor/ps5-runtime-tools/native-clang.py",
                 compiler, *flags, "-c", path, "-o", object_path])
            compiled.append(object_path)
        libraries = [selected[module][1].parent / selected[module][0]["import_library"]["path"]
                     for module in ("libkernel", "libSceLibcInternal")]
        run([target / "bin/prospero-lld", "-T", native / "ps5-pie.ld", "--eh-frame-hdr",
             "--no-undefined", "--version-script", native / "app-symbols.map",
             "-e", "_start", "-o", output / "llvm-pie.elf", *compiled,
             "--as-needed", *libraries])
        run([native_tool, "link", "--in", output / "llvm-pie.elf",
             "--out", output / "eboot.elf", "--stub-dir", target / "target/lib",
             "--module-sdk", "0x02000009", "--companion-sdk", "0x08050001",
             "--file-name", "eboot.elf"])
    image = Elf((output / "llvm-pie.elf").read_bytes())
    forbidden = {"__wrap_malloc", "__emutls_get_address", "__cxa_throw",
                 "__cxa_finalize", "__unw_add_dynamic_eh_frame_section", "glClear"}
    require(not forbidden.intersection(name for name, _ in image.symbols(".dynsym")),
            "launcher must not import or define a shared runtime")
    application = output / "kodi-app"
    imports = {name for name, symbol in image.symbols(".dynsym") if name and not symbol[3]}
    ownership = {}
    for name in imports:
        owners = [provider for provider, _ in selected.values()
                  if any(symbol["name"] == name for symbol in provider["symbols"])]
        require(len(owners) == 1, f"launcher import requires one system owner: {name}")
        ownership[nid(name)] = {key: owners[0][key] for key in ("module", "library")}
    startup = verify(image, ownership)
    proof = {"schema": 1, "format": "ps5-system-only-native-launcher",
             "console_accepted": False, "source_sha256": digest(source),
             "linked_sha256": digest(output / "llvm-pie.elf"),
             "native_sha256": digest(output / "eboot.elf"), "commands": commands,
             "startup_abi": STARTUP_ABI, "startup": startup,
             "application_native_sha256": digest(application / "native.elf")}
    (output / "launcher-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--native-tool", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.app, args.sdk, args.native_tool)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"build-native-title-launcher: {error}; see build/launcher-build.log\n")


if __name__ == "__main__":
    main()
