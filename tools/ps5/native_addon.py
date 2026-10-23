"""Installed native add-on validation shared by builds, staging and publication."""
# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from native_c_link import SYM, nid
from native_contract import COMPILER_ABI, require
from native_runtime_sdk import (
    NativeImage, IMPORT_LIB, NEEDED_MODULE, SYMTABSZ, decode_id,
    import_identities, string_at,
)
from native_shared_link import load_provider

FSELF = b"\x54\x14\xf5\xee"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def contained(path, root):
    require(not path.is_symlink() and path.resolve().is_relative_to(root.resolve()),
            f"path escapes installed native add-on or is a symlink: {path}")


def validate_binary(binary, sdk=None):
    require(binary.is_file(), f"missing native SPRX: {binary}")
    raw = binary.read_bytes()
    require(binary.suffix == ".sprx" and raw[:4] == FSELF,
            f"native add-on must be signed SPRX, not ordinary ELF/import stub: {binary}")
    receipt_path = binary.with_suffix(binary.suffix + ".json")
    contained(receipt_path, binary.parent)
    require(receipt_path.is_file(), f"missing native add-on receipt: {receipt_path}")
    receipt = json.loads(receipt_path.read_text())
    require(isinstance(receipt.get("module"), str) and
            receipt.get("schema") == 1 and receipt.get("format") == "ps5-native-addon" and
            receipt.get("module") + ".sprx" == binary.name and
            receipt.get("output_sha256") == digest(raw) and
            receipt.get("compiler_abi") == COMPILER_ABI and
            receipt.get("sign_extract_equal") is True,
            f"missing or mismatched native add-on receipt: {binary}")
    requirements = receipt.get("provider_requirements")
    dependencies = receipt.get("native_dependencies")
    require(isinstance(requirements, list) and requirements and isinstance(dependencies, dict),
            f"missing native provider requirements: {binary}")
    require([p["soname"] for p in requirements] == receipt.get("needed") ==
            dependencies.get("needed"), f"native dependency receipt mismatch: {binary}")
    require(all("libkodi-app" not in p["module"] and
                "libkodi-app" not in p["soname"] for p in requirements),
            f"forbidden private Kodi application import: {binary}")
    if sdk is None:
        return receipt
    descriptor = json.loads((sdk / "manifest.json").read_text())
    providers = {p["soname"]: p for p in
                 (load_provider(sdk / name) for name in descriptor["provider_manifests"])}
    for requirement in requirements:
        provider = providers.get(requirement["soname"])
        require(provider is not None and
                all(provider.get(key) == value for key, value in requirement.items()),
                f"unsupported native provider identity/version: {requirement['soname']}")
    with tempfile.TemporaryDirectory(prefix="ps5-addon-verify-") as directory:
        extracted = Path(directory) / "native.elf"
        subprocess.run([str(sdk / "tools/ps5-native-tool"), "self", "--extract",
                        "--file", str(binary), "--out", str(extracted)],
                       check=True, capture_output=True, text=True)
        native = extracted.read_bytes()
    require(digest(native) == receipt.get("native_elf_sha256"),
            f"native add-on extraction differs from receipt: {binary}")
    image = NativeImage(native)
    image.validate_metadata_layout()
    strings = image.memory(image.tag(5), image.tag(10))
    for tag, key in ((NEEDED_MODULE, "modules"), (IMPORT_LIB, "libraries")):
        actual = import_identities(image, strings, tag, receipt["module"])
        expected = {p["id"]: {k: v for k, v in p.items() if k != "soname"}
                    for p in dependencies[key]}
        require(actual == expected, f"native dependency metadata differs: {binary}")
    rows = list(SYM.iter_unpack(image.memory(image.tag(6), image.tag(SYMTABSZ))))
    imports = {p["dynsym_index"]: p for p in dependencies["imports"]}
    undefined = {i: row for i, row in enumerate(rows) if row[0] and not row[3]}
    require(len(imports) == len(dependencies["imports"]) and imports.keys() == undefined.keys(),
            f"native import inventory differs: {binary}")
    for index, row in undefined.items():
        symbol = imports[index]
        parts = string_at(strings, row[0]).split("#")
        require(len(parts) == 3 and parts[0] == symbol["nid"] == nid(symbol["name"]) and
                decode_id(parts[1]) == symbol["library_id"] == symbol["module_id"] ==
                decode_id(parts[2]) and
                row[1:] == (symbol["info"], symbol["other"], row[3],
                            symbol["value"], symbol["size"]),
                f"native import metadata differs at index {index}: {binary}")
        identity = symbol["module_id"]
        require(0 < identity <= len(requirements), "undeclared native provider identity")
        provider = providers[requirements[identity - 1]["soname"]]
        require(any(s["name"] == symbol["name"] and s["nid"] == symbol["nid"] and
                    s["info"] & 15 == symbol["info"] & 15 for s in provider["symbols"]),
                f"unsupported native provider symbol: {symbol['name']}")
    return receipt


def installed(directory, prefix, sdk=None):
    metadata = directory / "addon.xml"
    contained(directory, prefix)
    contained(metadata, directory)
    require(metadata.is_file(), f"missing installed native metadata: {metadata}")
    root = ET.parse(metadata).getroot()
    addon = root.get("id")
    extensions = [ext for ext in root.findall("extension")
                  if any(key.startswith("library") for key in ext.attrib)]
    require(root.tag == "addon" and addon == directory.name and len(extensions) == 1,
            f"invalid installed native add-on metadata: {metadata}")
    require(root.findtext("./extension[@point='xbmc.addon.metadata']/platform") == "ps5",
            f"native add-on platform must be ps5: {metadata}")
    name = extensions[0].get("library_freebsd", "")
    require(name and Path(name).name == name and "\\" not in name and name.endswith(".sprx"),
            f"invalid native library_freebsd: {metadata}")
    locations = [directory / name, prefix / "lib/kodi/addons" / addon / name]
    binaries = [p for p in locations if p.is_file()]
    require(len(binaries) == 1, f"missing or ambiguous native SPRX install: {metadata}")
    binary = binaries[0]
    contained(binary, prefix)
    validate_binary(binary, sdk)
    allowed = {binary, binary.with_suffix(".sprx.json")}
    lib = prefix / "lib/kodi/addons" / addon
    for folder in dict.fromkeys((directory, lib)):
        if not folder.exists():
            continue
        contained(folder, prefix)
        for path in folder.rglob("*"):
            contained(path, folder)
            if not path.is_file() or path in allowed:
                continue
            raw = path.read_bytes()
            require(path.name != "ps5-auxiliaries.list" and
                    raw[:4] not in (b"\x7fELF", FSELF) and
                    not (folder == lib or path.name.endswith((".so", ".sprx"))),
                    f"unmodelled native dependency, legacy auxiliary or ordinary ELF: {path}")
    return addon, directory, name, binary, []
