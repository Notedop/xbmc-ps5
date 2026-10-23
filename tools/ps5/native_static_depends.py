#!/usr/bin/env python3
"""Validate isolated native dependency archives before either title link."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_verified_map(path, stdio_header):
    path = Path(path).resolve()
    mapping = json.loads(path.read_text())
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("native archive map must be a nonempty object")
    expected_overlay = digest(Path(stdio_header))
    result = {}
    link_names = {}
    for original, replacement in mapping.items():
        if not isinstance(original, str) or not isinstance(replacement, str):
            raise ValueError("native archive paths must be strings")
        old, new = Path(original), Path(replacement)
        if (not old.is_absolute() or not new.is_absolute() or
                any(character in original + replacement for character in (';', '\n', '\r'))):
            raise ValueError("native archive paths must be absolute single CMake items")
        old, new = old.resolve(strict=True), new.resolve(strict=True)
        if old.name in link_names and link_names[old.name] != new:
            raise ValueError(f"ambiguous native archive link name: {old.name}")
        link_names[old.name] = new
        receipt = json.loads((new.parent / "receipt.json").read_text())
        identity = receipt["identity"]
        if identity["overlay"] != expected_overlay:
            raise ValueError(f"native archive stdio ABI differs from the SDK: {new}")
        if identity["original_archives"].get(str(old)) != digest(old):
            raise ValueError(f"original dependency changed since native rebuild: {old}")
        output = next((item for item in receipt["outputs"]
                       if item["original"] == str(old) and item["rebuilt"] == str(new)), None)
        if output is None or output["sha256"] != digest(new):
            raise ValueError(f"native replacement changed or lacks provenance: {new}")
        validation = receipt["validation"]
        if (validation["opaque_FILE_rejected"] is not True or
                validation["fpos_size"] != 24 or validation["fpos_align"] != 8 or
                validation["all_objects_fresh_with_forced_opaque_overlay"] is not True):
            raise ValueError(f"incomplete native archive ABI evidence: {new}")
        result[str(old)] = str(new)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--stdio-header", type=Path, required=True)
    args = parser.parse_args()
    try:
        mapping = load_verified_map(args.map, args.stdio_header)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"native-static-depends: {error}\n")
    print(json.dumps(mapping, sort_keys=True))


if __name__ == "__main__":
    main()
