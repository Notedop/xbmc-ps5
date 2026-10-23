#!/usr/bin/env python3
"""Discover and stage native SPRX installs; reject legacy ELF and auxiliary lists."""

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import xml.etree.ElementTree as ET

from native_addon import installed as native_installed

BASENAME = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]{0,127}")
ADDON_ID = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]*")


def contained(path, root):
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes installed add-on: {path}")


def discover(prefixes, exclusions, audio_ids=None, include=(), catalogue=None, sdk=None):
    """Return validated candidates; catalogue selection never overrides exclusions."""
    if catalogue is not None:
        unknown = set(include) - catalogue["addons"].keys()
        if audio_ids is not None:
            unknown |= set(audio_ids) - catalogue["addons"].keys()
        if unknown:
            raise ValueError(f"explicitly selected add-ons not in catalogue: {', '.join(sorted(unknown))}")
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
            if (not extensions and not lib.exists()
                    and (catalogue is None or addon not in catalogue["addons"])):
                report.append(dict(id=addon, prefix=str(prefix), status="resource-only"))
                continue
            if addon in seen:
                raise ValueError(f"duplicate add-on ID {addon}: {seen[addon]} and {manifest}")
            seen[addon] = str(manifest)
            found.add(addon)
            record = dict(id=addon, prefix=str(prefix), manifest=str(manifest))
            if (catalogue is None and audio_ids is not None
                    and addon.startswith("audiodecoder.") and addon not in audio_ids):
                report.append(dict(record, status="excluded", reason="explicit PS5_AUDIO_DECODERS selection"))
                continue
            if addon in exclusions and (catalogue is not None or addon not in include):
                report.append(dict(record, status="excluded", reason=exclusions[addon]))
                continue
            if catalogue is not None and addon not in catalogue["addons"]:
                report.append(dict(record, status="excluded", reason="not in PS5 add-on catalogue"))
                continue
            if len(extensions) != 1:
                raise ValueError(f"expected one binary extension: {manifest}")
            entry = native_installed(data, prefix, sdk)
            selected.append(entry)
            report.append(dict(record, status="selected", binaries=[str(entry[3])]))
    missing = set(include) - found
    if audio_ids is not None:
        missing |= set(audio_ids) - found
    if missing:
        raise ValueError(f"explicitly selected add-ons not installed: {', '.join(sorted(missing))}")
    return selected, report


def stage(selected, destination, catalogue=None, bundled_only=False):
    if bundled_only and catalogue is None:
        raise ValueError("bundled-only staging requires a catalogue")
    if catalogue is not None:
        from addon_catalogue import prepare_manifest
        manifests = {}
        for addon, data, *_ in selected:
            root = ET.parse(data / "addon.xml").getroot()
            prepare_manifest(root, catalogue, addon)
            manifests[addon] = root
    if bundled_only:
        selected = [entry for entry in selected if catalogue["addons"][entry[0]]["bundled"]]
    for addon, data, library, binary, companions in selected:
        output = destination / "addons" / addon
        if output.exists():
            raise ValueError(f"destination add-on already exists: {output}")
    for addon, data, library, binary, companions in selected:
        output = destination / "addons" / addon
        shutil.copytree(data, output)
        if catalogue is not None:
            ET.indent(manifests[addon], space="  ")
            ET.ElementTree(manifests[addon]).write(output / "addon.xml",
                                                 encoding="utf-8", xml_declaration=True)
        for path in (binary, binary.with_suffix(".sprx.json")):
            if path.parent != data:
                shutil.copy2(path, output / path.name)


def stage_system_addons(source, destination, *, bunny_diagnostic=False):
    """Bundle repositories/runtime and an optional diagnostic; testing stays opt-in."""
    manifest = destination / "system/addon-manifest.xml"
    tree = ET.parse(manifest)
    root = tree.getroot()
    if root.tag != "addons":
        raise ValueError(f"invalid system add-on manifest: {manifest}")
    addons = ["resource.ps5.addon-runtime", "repository.ps5", "repository.ps5.testing"]
    if bunny_diagnostic:
        adaptive = destination / "addons/inputstream.adaptive"
        if not (adaptive / "addon.xml").is_file():
            raise ValueError("Bunny diagnostic requires staged native inputstream.adaptive")
        native_installed(adaptive, destination.parents[1])
        addons.append("plugin.video.ps5.adaptive.test")
    for addon in addons:
        data = source / addon
        metadata = ET.parse(data / "addon.xml").getroot()
        if metadata.tag != "addon" or metadata.get("id") != addon:
            raise ValueError(f"invalid PS5 system add-on: {data}")
        output = destination / "addons" / addon
        if output.exists():
            if (output / "addon.xml").read_bytes() != (data / "addon.xml").read_bytes():
                raise ValueError(f"destination add-on already exists: {output}")
        else:
            shutil.copytree(data, output)
        entries = [entry for entry in root.findall("addon") if (entry.text or "").strip() == addon]
        # Even optional manifest entries are auto-enabled on first discovery.
        if addon == "repository.ps5.testing":
            for entry in entries:
                root.remove(entry)
            continue
        if not entries:
            ET.SubElement(root, "addon").text = addon
        else:
            entries[0].attrib.pop("optional", None)
            for duplicate in entries[1:]:
                root.remove(duplicate)
    ET.indent(tree, space="  ")
    tree.write(manifest, encoding="utf-8", xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, action="append", default=[])
    parser.add_argument("--runtime-sdk", type=Path,
                        help="validate extracted native imports against this prepared SDK")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--exclusions", type=Path,
                        default=Path(__file__).with_name("addon-exclusions.json"))
    parser.add_argument("--audio-selection", help="legacy override; empty excludes all audio decoders")
    parser.add_argument("--include", action="append", default=[],
                        help="explicit override of selection exclusions (not native validation)")
    parser.add_argument("--catalogue", type=Path,
                        help="limit candidates to this PS5 catalogue; exclusions remain authoritative")
    parser.add_argument("--bundled-only", action="store_true",
                        help="copy only bundled catalogue entries; still inventory all candidates")
    parser.add_argument("--system-addons-source", type=Path,
                        help="source addons directory for PS5 repository/runtime registration")
    parser.add_argument("--bunny-diagnostic", action="store_true",
                        help="include the Python Bunny diagnostic; requires staged native Adaptive")
    args = parser.parse_args()
    try:
        if args.bundled_only and args.catalogue is None:
            raise ValueError("--bundled-only requires --catalogue")
        if args.system_addons_source and not args.destination:
            raise ValueError("--system-addons-source requires --destination")
        if args.bunny_diagnostic and not args.system_addons_source:
            raise ValueError("--bunny-diagnostic requires --system-addons-source")
        catalogue = None
        if args.catalogue is not None:
            from addon_catalogue import load_catalogue
            catalogue = load_catalogue(args.catalogue)
        exclusions = json.loads(args.exclusions.read_text())
        if not isinstance(exclusions, dict) or any(
                not isinstance(reason, str) or not reason.strip() for reason in exclusions.values()):
            raise ValueError("exclusions must map add-on IDs to nonempty reasons")
        if args.runtime_sdk:
            from native_contract import validate_sdk
            validate_sdk(args.runtime_sdk)
        selected, report = discover(args.prefix, exclusions,
                                    None if args.audio_selection is None else args.audio_selection.split(),
                                    args.include, catalogue=catalogue, sdk=args.runtime_sdk)
        if catalogue is not None:
            from addon_catalogue import prepare_manifest
            for addon, data, *_ in selected:
                prepare_manifest(ET.parse(data / "addon.xml").getroot(), catalogue, addon)
            missing = set(catalogue["addons"]) - {entry[0] for entry in selected} - exclusions.keys()
            if missing:
                print("WARNING: incomplete native catalogue inventory: "
                      f"{', '.join(sorted(missing))}. Build/install the full candidate inventory and "
                      "validate every native package before publication.", file=sys.stderr)
        if args.destination:
            stage(selected, args.destination, catalogue=catalogue, bundled_only=args.bundled_only)
            if args.system_addons_source:
                stage_system_addons(args.system_addons_source, args.destination,
                                    bunny_diagnostic=args.bunny_diagnostic)
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
