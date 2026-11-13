#!/usr/bin/env python3
"""Verify pinned graphics inputs and their prepared SDK provenance."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from native_contract import digest, require, validate_sdk

HERE = Path(__file__).resolve().parent


def patch_identity():
    return {path.name: digest(path) for path in
            sorted((HERE.parent / "patches/ps5-opengl").glob("*.patch"))}


def pinned_revision():
    revision = (HERE.parent / "patches/ps5-opengl/PS5-OPENGL-COMMIT").read_text().strip()
    require(len(revision) == 40 and all(c in "0123456789abcdef" for c in revision),
            "invalid PS5 OpenGL revision pin")
    manifest = json.loads((HERE.parent / "dependency-manifest.json").read_text())
    entry = next(row for row in manifest["dependencies"] if row["name"] == "ps5-opengl")
    require(entry["pin"] == revision, "OpenGL manifest and revision pin differ")
    return revision


def qualify_graphics(source, installed):
    source, installed = source.resolve(), installed.resolve()
    revision = pinned_revision()
    actual = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
    require(actual == revision, f"graphics source {source} is {actual}; expected {revision}; "
            "select the pinned checkout with PS5_OPENGL_SOURCE/WORK, preserving old sources")
    patches = sorted((HERE.parent / "patches/ps5-opengl").glob("*.patch"))
    require(patches, "missing Kodi graphics integration patches")
    subprocess.run(["git", "-C", str(source), "apply", "--reverse", "--check",
                    *map(str, patches)], check=True, capture_output=True, text=True)
    changes = subprocess.run(["git", "-C", str(source), "diff", "--binary", "HEAD"],
                             check=True, capture_output=True).stdout
    with tempfile.TemporaryDirectory(prefix="ps5-graphics-index-") as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))
        subprocess.run(["git", "-C", str(source), "read-tree", "HEAD"],
                       env=env, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "apply", "--cached", *map(str, patches)],
                       env=env, check=True, capture_output=True)
        expected = subprocess.run(
            ["git", "-C", str(source), "diff", "--cached", "--binary", "HEAD"],
            env=env, check=True, capture_output=True).stdout
    require(changes == expected, "graphics checkout differs from the pin plus Kodi patches; "
            "preserve local edits and select a qualified checkout")
    stamp = (source / "build/.kodi-sdk-build").read_text().splitlines()
    require(stamp and stamp[0] == revision, "graphics SDK was not built from the pinned revision")
    recorded = {Path(line.split(maxsplit=1)[1]).name: line.split()[0]
                for line in stamp[1:]}
    require(recorded == patch_identity(), "graphics SDK integration patches changed; "
            "rerun bootstrap STEP=opengl for this WORK")
    profile = (source / "build/.kodi-display-profile").read_text().strip()
    require(profile in {f"{height}p{fps}" for height in (1080, 1440, 2160) for fps in (60, 120)},
            "invalid graphics display profile")
    height, fps = profile.split("p")
    header = (installed / "include/ps5_opengl_display.h").read_text()
    require(f"#define PS5_OPENGL_NATIVE_HEIGHT {height}\n" in header and
            f"#define PS5_OPENGL_NATIVE_FPS {fps}\n" in header,
            "installed graphics display profile differs from the built profile")
    cached = source / "build/sdk/ps5-opengl-gl46"
    files = {path.relative_to(cached).as_posix(): digest(path)
             for path in sorted(cached.rglob("*")) if path.is_file()}
    require(files and "lib/libps5_opengl_core33.a" in files, "missing built graphics SDK")
    for relative, expected in files.items():
        require(digest(installed / relative) == expected,
                f"installed graphics differ from the pinned build: {relative}")
    require(digest(source / "build/core33-native-runtime/libps5_opengl_core33.a") ==
            files["lib/libps5_opengl_core33.a"],
            "graphics runtime configuration and installed baseline differ")
    return {"schema": 1, "revision": revision, "patches": patch_identity(),
            "profile": profile, "source": str(source), "installed": str(installed),
            "source_diff_sha256": hashlib.sha256(changes).hexdigest(),
            "installed_files": files}


def validate_prepared_graphics(sdk):
    sdk = Path(sdk).resolve()
    validate_sdk(sdk)
    receipt = json.loads((sdk / "receipt.json").read_text())
    name = "graphics-provenance.json"
    require(name in receipt["files"], "prepared graphics lack pinned source provenance; "
            "rerun native preparation with the pinned graphics source and installation")
    report = json.loads((sdk / name).read_text())
    require(report.get("revision") == pinned_revision() and
            report.get("patches") == patch_identity(),
            "prepared graphics revision/integration patches differ from Kodi's pin")
    require(report.get("runtime_sha256") == digest(sdk / "runtimes/libkodi-gl.sprx"),
            "prepared graphics provenance does not match the delivered runtime")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-sdk", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--installed", type=Path)
    args = parser.parse_args()
    if bool(args.prepared_sdk) == bool(args.source or args.installed) or (
            not args.prepared_sdk and not (args.source and args.installed)):
        parser.error("select --prepared-sdk, or both --source and --installed")
    try:
        report = (validate_prepared_graphics(args.prepared_sdk) if args.prepared_sdk else
                  qualify_graphics(args.source, args.installed))
        print(f"Verified graphics {report['revision']} ({report['profile']})")
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"native graphics: {error}\n")


if __name__ == "__main__":
    main()
