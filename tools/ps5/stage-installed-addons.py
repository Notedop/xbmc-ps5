#!/usr/bin/env python3
"""Discover installed Kodi binary add-ons, preflight, stage and list trusted ELFs.

stdout is exclusively the selected primary/helper paths for export generation.
Diagnostics go to stderr; the JSON report records every discovered manifest,
missing optional prefix and exclusion. Unknown invalid binaries fail closed.
"""

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

from addon_elf import elf_problems

MANIFEST = "ps5-auxiliaries.list"
BASENAME = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]{0,127}")
ADDON_ID = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]*")


def contained(path, root):
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes installed add-on: {path}")


def discover(prefixes, exclusions, audio_ids=None, include=()):
    selected, report, seen, found = [], [], {}, set()
    for prefix in dict.fromkeys(p.resolve() for p in prefixes):
        if not prefix.is_dir():
            report.append(dict(prefix=str(prefix), status="missing-prefix"))
            continue
        data_root = prefix / "share/kodi/addons"
        lib_root = prefix / "lib/kodi/addons"
        manifests = sorted(data_root.glob("*/addon.xml"))
        for directory in sorted(lib_root.glob("*")):
            if not (data_root / directory.name / "addon.xml").is_file():
                raise ValueError(f"binary directory without manifest: {directory}")
        for manifest in manifests:
            data = manifest.parent
            contained(data, data_root)
            contained(manifest, data)
            root = ET.parse(manifest).getroot()
            addon = root.get("id", "")
            if root.tag != "addon" or not ADDON_ID.fullmatch(addon):
                raise ValueError(f"invalid add-on ID/manifest: {manifest}")
            extensions = [ext for ext in root.findall("extension")
                          if any(key.startswith("library") for key in ext.attrib)]
            lib = lib_root / data.name
            if not extensions and not lib.exists():
                report.append(dict(id=addon, prefix=str(prefix), status="resource-only"))
                continue
            if addon in seen:
                raise ValueError(f"duplicate add-on ID {addon}: {seen[addon]} and {manifest}")
            seen[addon] = str(manifest)
            found.add(addon)
            record = dict(id=addon, prefix=str(prefix), manifest=str(manifest))
            if audio_ids is not None and addon.startswith("audiodecoder.") and addon not in audio_ids:
                report.append(dict(record, status="excluded", reason="explicit PS5_AUDIO_DECODERS selection"))
                continue
            if addon in exclusions and addon not in include:
                report.append(dict(record, status="excluded", reason=exclusions[addon]))
                continue
            if len(extensions) != 1:
                raise ValueError(f"expected one binary extension: {manifest}")
            library = extensions[0].get("library_freebsd", "")
            if not BASENAME.fullmatch(library) or library in (".", ".."):
                raise ValueError(f"invalid/missing library_freebsd basename: {manifest}")
            binary = lib / library
            if not binary.is_file():
                raise ValueError(f"missing binary: {binary}")
            contained(lib, lib_root)
            contained(binary, lib)
            companions = sorted(p for p in lib.glob("lib*.so")
                                if not p.is_symlink() and p != binary)
            if len(companions) > 64:
                raise ValueError(f"too many helpers (maximum 64): {lib}")
            for elf in [binary, *companions]:
                contained(elf, lib)
                if not BASENAME.fullmatch(elf.name):
                    raise ValueError(f"invalid helper basename: {elf}")
                problems = elf_problems(elf)
                if problems:
                    raise ValueError(f"{elf} is not loadable on PS5: {'; '.join(problems)}")
            authorized = {p.resolve() for p in [binary, *companions]}
            for installed in lib.iterdir():
                if installed.is_file() and installed.resolve() not in authorized:
                    raise ValueError(f"unrecognized binary/helper in installed add-on: {installed}")
            if (data / MANIFEST).exists():
                raise ValueError(f"installed data must not provide {MANIFEST}: {data}")
            names = {library, *(p.name for p in companions)}
            for resource in data.rglob("*"):
                contained(resource, data)
                if resource.relative_to(data).parts[0] in names:
                    raise ValueError(f"resource collides with binary/helper: {resource}")
                if resource.is_file():
                    with resource.open("rb") as stream:
                        if stream.read(4) == b"\x7fELF":
                            raise ValueError(f"ELF must be installed under lib/kodi/addons: {resource}")
            selected.append((addon, data, library, binary, companions))
            report.append(dict(record, status="selected", elfs=[str(p) for p in [binary, *companions]]))
    missing = set(include) - found
    if audio_ids is not None:
        missing |= set(audio_ids) - found
    if missing:
        raise ValueError(f"explicitly selected add-ons not installed: {', '.join(sorted(missing))}")
    return selected, report


def stage(selected, destination):
    for addon, data, library, binary, companions in selected:
        output = destination / "addons" / addon
        if output.exists():
            raise ValueError(f"destination add-on already exists: {output}")
    for addon, data, library, binary, companions in selected:
        output = destination / "addons" / addon
        shutil.copytree(data, output)
        for elf in [binary, *companions]:
            shutil.copy2(elf, output / elf.name)
        if companions:
            lines = ["# Generated by tools/ps5/stage-installed-addons.py", "format 1",
                     f"primary {library}", *(f"helper {p.name}" for p in companions)]
            (output / MANIFEST).write_text("\n".join(lines) + "\n", encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, action="append", default=[])
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path,
                        default=Path(__file__).with_name("addon-exclusions.json"))
    parser.add_argument("--audio-selection", help="legacy override; empty excludes all audio decoders")
    parser.add_argument("--include", action="append", default=[],
                        help="explicit override of selection exclusions (not ELF validation)")
    args = parser.parse_args()
    try:
        exclusions = json.loads(args.exclusions.read_text())
        if not isinstance(exclusions, dict) or any(
                not isinstance(reason, str) or not reason.strip() for reason in exclusions.values()):
            raise ValueError("exclusions must map add-on IDs to nonempty reasons")
        selected, report = discover(args.prefix, exclusions,
                                    None if args.audio_selection is None else args.audio_selection.split(),
                                    args.include)
        if args.destination:
            stage(selected, args.destination)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        for record in report:
            print(f"add-on {record.get('id', record['prefix'])}: {record['status']}"
                  f"{': ' + record['reason'] if 'reason' in record else ''}", file=sys.stderr)
        for _, _, _, binary, companions in selected:
            for elf in [binary, *companions]:
                print(elf)
    except (OSError, ValueError, ET.ParseError) as error:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(dict(status="failed", error=str(error)), indent=2) + "\n")
        parser.exit(1, f"installed add-on preflight: {error}\n")


if __name__ == "__main__":
    main()
