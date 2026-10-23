#!/usr/bin/env python3
"""Compile and package one import-free Kodi C add-on; never deploy or launch."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

from native_c_link import convert, module_identity, require, validate

ROOT = Path(__file__).resolve().parents[2]


def manifest_identity(path):
    manifest = ET.parse(path).getroot()
    require(manifest.tag == "addon", "expected an add-on manifest")
    module = module_identity(manifest.get("id", ""))
    libraries = [extension.get("library_freebsd") or extension.get("library_linux")
                 for extension in manifest.findall("extension")
                 if extension.get("library_freebsd") or extension.get("library_linux")]
    require(libraries == [module + ".sprx"],
            "manifest must select exactly one native library named <addon-id>.sprx")
    return module


def build(args):
    module = manifest_identity(args.manifest)
    require(args.source.suffix == ".c", "only a single C source is supported")
    require(args.source.is_file(), "C source does not exist")
    sdk = Path(args.sdk).resolve()
    native_tool = Path(args.native_tool).resolve()
    require(native_tool.is_file() and os.access(native_tool, os.X_OK),
            f"missing executable native title tool: {native_tool}")
    compiler = sdk / "bin/prospero-clang"
    require(compiler.is_file(), f"missing SDK compiler: {compiler}")
    output = args.build.resolve()
    addon = output / module
    addon.mkdir(parents=True, exist_ok=True)
    obj, elf, roundtrip = (output / name for name in ("main.o", "native.elf", "roundtrip.elf"))
    sprx = addon / (module + ".sprx")
    subprocess.run([
        str(compiler), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        "-fPIC", "-fvisibility=hidden", "-ffreestanding", "-fno-builtin",
        "-fno-stack-protector", "-fno-unwind-tables", "-fno-asynchronous-unwind-tables",
        "-I" + str(ROOT / "xbmc/addons/kodi-dev-kit/include"),
        *("-I" + str(path.resolve()) for path in args.include),
        *("-D" + define for define in args.define),
        "-c", str(args.source.resolve()), "-o", str(obj),
    ], check=True)
    elf.write_bytes(convert(obj.read_bytes(), module))
    subprocess.run([str(native_tool), "self", "--sign", "--file", str(elf),
                    "--out", str(sprx)], check=True)
    subprocess.run([str(native_tool), "self", "--extract", "--file", str(sprx),
                    "--out", str(roundtrip)], check=True)
    extracted = roundtrip.read_bytes()
    require(extracted == elf.read_bytes(), "signed native module extraction differs from ELF")
    validate(extracted, module)
    shutil.copyfile(args.manifest, addon / "addon.xml")
    files = [obj, elf, sprx, addon / "addon.xml"]
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n"
                        for path in files)
    (output / "artifacts.sha256").write_text(checksums)
    print(checksums, end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--sdk", default=os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
    parser.add_argument("--native-tool", required=True)
    parser.add_argument("--include", type=Path, action="append", default=[])
    parser.add_argument("--define", action="append", default=[])
    build(parser.parse_args())
