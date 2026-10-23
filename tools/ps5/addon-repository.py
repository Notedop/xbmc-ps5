#!/usr/bin/env python3
"""Build, package and explicitly publish the whitelisted PS5 binary add-ons."""

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from addon_catalogue import DEFAULT_CATALOGUE, load_catalogue
from addon_repository import package, publish, workflow_lock

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", type=Path, default=DEFAULT_CATALOGUE)
    parser.add_argument("--output", type=Path, default=ROOT / "build/ps5-addon-repository/output")
    parser.add_argument("--work", type=Path, default=ROOT / "build/ps5-addon-repository/work")
    parser.add_argument("--sdk", type=Path, default=Path(
        os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk")))
    parser.add_argument("--depends", type=Path, default=Path(os.environ.get(
        "PS5_DEPENDS_PREFIX", str(ROOT / "build/ps5-depends-test/x86_64-unknown-freebsd-debug"))))
    parser.add_argument("--runtime-sdk", type=Path, default=Path(os.environ.get(
        "PS5_NATIVE_RUNTIME_SDK", str(ROOT / "build/ps5-native/sdk"))))
    parser.add_argument("--archive-map", type=Path, default=Path(os.environ.get(
        "PS5_NATIVE_DEPENDS_MAP", str(ROOT / "build/ps5-native/depends/archive-map.json"))))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show the whitelist without compiling or contacting upstream")
    build_parser = commands.add_parser("build", help="incrementally build whitelisted targets")
    build_parser.add_argument("--only", action="append", default=[], metavar="TARGET")
    build_parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 4))
    build_parser.add_argument("--refresh-sources", action="store_true",
                              help="explicitly resolve upstream refs again and update source locks")
    build_parser.add_argument("--package", action="store_true",
                              help="also verify and package the complete catalogue after building")
    package_parser = commands.add_parser("package", help="verify the title and create repository ZIPs")
    for command_parser in (build_parser, package_parser):
        command_parser.add_argument("--title-app", type=Path,
                                    required=command_parser is package_parser,
                                    help="app directory from the matching fresh title stage")
    publish_parser = commands.add_parser("publish", help="copy packages to a clean local repo checkout")
    publish_parser.add_argument("--checkout", required=True, type=Path)
    publish_parser.add_argument("--commit", action="store_true")
    publish_parser.add_argument("--push", action="store_true")
    publish_parser.add_argument("--dry-run", action="store_true")
    publish_parser.add_argument("--apply-native-migration", action="store_true")
    args = parser.parse_args()
    if args.command == "build" and args.package and args.title_app is None:
        parser.error("--title-app is required with build --package")
    try:
        with ExitStack() as locks:
            if args.command in ("build", "package"):
                locks.enter_context(workflow_lock(args.work.resolve()))
            if args.command in ("package", "publish") or (
                    args.command == "build" and args.package):
                locks.enter_context(workflow_lock(args.output.resolve()))
            execute(args)
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"PS5 add-on repository: {error}\n")


def execute(args):
    catalogue = load_catalogue(args.catalogue)
    if args.command == "list":
        for addon, policy in sorted(catalogue["addons"].items()):
            print(f"{addon}: {'bundled + repository' if policy['bundled'] else 'repository only'}"
                  f"; {policy['channel']}; target {policy['target']}")
        print(f"{len(catalogue['addons'])} add-ons; {len(catalogue['targets'])} build targets; "
              f"runtime {catalogue['runtime']}")
    elif args.command == "build":
        from addon_build import build

        if args.jobs < 1:
            raise ValueError("--jobs must be positive")
        prefixes = build(ROOT, catalogue, args.work.resolve(), args.only, args.jobs,
                         args.sdk.resolve(), args.depends.resolve(), args.refresh_sources,
                         args.runtime_sdk.resolve(), args.archive_map.resolve())
        print("Installed prefixes:\n" + "\n".join(map(str, prefixes)))
    if args.command == "package" or (args.command == "build" and args.package):
        from addon_build import validate_build

        record = validate_build(ROOT, catalogue, args.work.resolve(),
                                args.sdk.resolve(), args.depends.resolve(),
                                args.runtime_sdk.resolve(), args.archive_map.resolve())
        result = package(ROOT, catalogue, [Path(p) for p in record["prefixes"]],
                         args.output.resolve(), args.title_app.resolve(), record,
                         args.runtime_sdk.resolve(), args.archive_map.resolve())
        print(f"Packaged {len(result['addons'])} add-ons into {args.output}")
    elif args.command == "publish":
        changed = publish(args.output, args.checkout, args.commit, args.push, catalogue,
                          args.apply_native_migration, args.dry_run)
        print(("Publication changes planned; checkout unchanged." if args.dry_run else
               "Publication updated.") if changed else "Publication already matches; no changes.")
        if changed and not args.commit and not args.dry_run:
            print("Changes are local and uncommitted; review the publication checkout.")


if __name__ == "__main__":
    main()
