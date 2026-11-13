#!/usr/bin/env python3
"""Verify the payload SDK against Kodi's package recipe and installed-file receipt."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RECEIPT = ".kodi-sdk.json"
REQUIRED = (
    "bin/prospero-clang", "bin/prospero-clang++", "bin/ps5-wrapper-args.py",
    "toolchain/prospero.cmake", "target/lib/libSceVideoOut.so",
    "target/lib/libSceVideodec2.so", "target/lib/libSceMbus.so",
    "target/lib/libSceMediaFrameworkInterface.so", "target/lib/libSceSaveData.so",
    "target/lib/libSceUsbStorage.so",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs(directory=None):
    if directory is not None:
        paths = [directory / "PKGBUILD", directory / "check-sdk.py",
                 *directory.glob("*.patch"), *directory.glob("*.c")]
    else:
        paths = [HERE.parent / "sdk/PKGBUILD", Path(__file__).resolve(),
                 *HERE.parent.glob("patches/ps5-payload-sdk/*.patch"),
                 *ROOT.glob("shims/sce_stubs/*.c")]
    files = {path.name: digest(path) for path in paths}
    recipe = next(path for path in paths if path.name == "PKGBUILD").read_text()
    revision = re.search(r"^_sdk_commit=([0-9a-f]{40})$", recipe, re.MULTILINE)
    if not revision:
        raise ValueError("SDK recipe must pin a full upstream commit")
    return {"revision": revision.group(1), "files": files}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def file_record(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"SDK file escapes its installation: {relative}")
    if path.is_symlink():
        if not path.exists():
            raise ValueError(f"broken SDK symlink: {relative}")
        return {"symlink": os.readlink(path)}
    return {"sha256": digest(path), "mode": path.stat().st_mode & 0o777}


def record(sdk, directory):
    for relative in REQUIRED:
        if not (sdk / relative).is_file():
            raise ValueError(f"SDK package is incomplete: {relative}")
    files = {
        path.relative_to(sdk).as_posix(): file_record(sdk, path.relative_to(sdk))
        for path in sorted(sdk.rglob("*"))
        if (path.is_file() or path.is_symlink()) and path.name != RECEIPT
    }
    value = {"schema": 1, "inputs": inputs(directory), "files": files}
    (sdk / RECEIPT).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def verify(sdk, match_inputs=True):
    receipt = sdk / RECEIPT
    if not receipt.is_file():
        raise ValueError(f"unqualified SDK preserved at {sdk}; no Kodi SDK receipt. "
                         "Use a new empty PS5_PAYLOAD_SDK prefix or explicitly resolve "
                         "the existing installation before running STEP=sdk")
    saved = json.loads(receipt.read_text())
    if saved.get("schema") != 1 or not isinstance(saved.get("inputs"), dict):
        raise ValueError(f"invalid SDK receipt at {receipt}")
    if match_inputs and saved["inputs"] != inputs():
        raise ValueError(f"SDK recipe/customizations differ at {sdk}; existing SDK preserved. "
                         "Run prepare-depends STEP=sdk to update a qualified managed SDK, "
                         "or select a new empty PS5_PAYLOAD_SDK prefix")
    files = saved.get("files", {})
    if not all(name in files for name in REQUIRED):
        raise ValueError("SDK receipt omits required tools or custom stubs")
    for relative, expected in files.items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError(f"invalid SDK receipt path: {relative}")
        if file_record(sdk, relative) != expected:
            raise ValueError(f"SDK file changed: {relative}; repair/reinstall the qualified SDK")
    for relative in ("bin/prospero-clang", "bin/prospero-clang++"):
        if not os.access(sdk / relative, os.X_OK):
            raise ValueError(f"SDK compiler wrapper is not executable: {relative}")
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--inputs", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--fingerprint", action="store_true")
    action.add_argument("--write-receipt", action="store_true")
    args = parser.parse_args()
    try:
        if args.fingerprint:
            value = verify(args.sdk, match_inputs=False)["inputs"] if args.sdk else inputs(args.inputs)
            print(fingerprint(value))
        elif args.sdk is None:
            parser.error("--sdk is required")
        elif args.write_receipt:
            if args.inputs is None:
                parser.error("--write-receipt requires --inputs")
            record(args.sdk, args.inputs)
        else:
            saved = verify(args.sdk)
            print(f"Verified payload SDK: {saved['inputs']['revision']}")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"PS5 SDK: {error}\n")


if __name__ == "__main__":
    main()
