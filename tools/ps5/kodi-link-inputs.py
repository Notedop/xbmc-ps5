#!/usr/bin/env python3
"""Preserve Kodi's archive selection when relinking the native title."""

import argparse
import os
from pathlib import Path
import re
import shlex


def extract(build, hb, sdk, gl):
    ninja = (build / "build.ninja").read_text()
    match = re.search(r"^build kodi\.bin:.*?\n(?:  .*\n)*", ninja, re.M)
    if match is None:
        raise ValueError("build.ninja has no kodi.bin link rule")
    block = match.group(0)
    libraries = re.search(r"^  LINK_LIBRARIES = (.*)$", block, re.M)
    if libraries is None:
        raise ValueError("kodi.bin link rule has no LINK_LIBRARIES")

    search_dirs = []
    for field in ("LINK_FLAGS", "LINK_PATH", "LINK_LIBRARIES"):
        value = re.search(r"^  " + field + r" = (.*)$", block, re.M)
        tokens = iter(shlex.split(value.group(1)) if value else [])
        for token in tokens:
            if token == "-L":
                directory = next(tokens)
            elif token.startswith("-L"):
                directory = token[2:]
            else:
                continue
            path = Path(directory)
            search_dirs.append(path if path.is_absolute() else build / path)
    search_dirs.extend((hb / "lib", gl / "lib", sdk / "target/lib"))

    whole, deps, mode = [], [], None
    for token in shlex.split(libraries.group(1)):
        if token == "-Wl,--whole-archive":
            mode = "whole"
        elif token == "-Wl,--no-whole-archive":
            mode = "deps"
        elif mode == "whole":
            path = Path(token)
            whole.append(path if path.is_absolute() else build / path)
        elif mode == "deps":
            if token.endswith(".a"):
                path = Path(token)
                deps.append(path if path.is_absolute() else build / path)
            elif token.startswith("-l"):
                name = token[2:]
                archive = next((d / f"lib{name}.a" for d in search_dirs
                                if (d / f"lib{name}.a").is_file()), None)
                if archive is not None:
                    deps.append(archive)
                elif not any((d / f"lib{name}.so").is_file() for d in search_dirs):
                    raise FileNotFoundError(f"cannot resolve Kodi link input {token}")
                # Sony stubs are supplied by the native title's target/lib/*.so.

    # Payload libc/pthread cannot be linked into a native title.
    payload_lib = (sdk / "target/lib").resolve()
    deps = [path for path in deps if path.resolve().parent != payload_lib]
    return whole, list(dict.fromkeys(deps))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("build", "vendor", "hb", "sdk", "gl"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    whole, deps = extract(args.build, args.hb, args.sdk, args.gl)
    for filename, paths in (("kodi-whole.txt", whole), ("kodi-deps.txt", deps)):
        (args.vendor / filename).write_text("".join(os.fspath(p) + "\n" for p in paths))
    print(f"    {len(whole)} Kodi archives, {len(deps)} external libraries")


if __name__ == "__main__":
    main()
