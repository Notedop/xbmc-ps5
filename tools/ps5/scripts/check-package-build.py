#!/usr/bin/env python3
"""Reject stale Kodi link outputs without running the build."""

import argparse
from pathlib import Path
import re
import shlex
import subprocess


def check(build, config=""):
    cache = {}
    for line in (build / "CMakeCache.txt").read_text().splitlines():
        if "=" in line and ":" in line.split("=", 1)[0]:
            key, value = line.split("=", 1)
            cache[key.split(":", 1)[0]] = value
    generator = cache.get("CMAKE_GENERATOR")
    tool = cache.get("CMAKE_MAKE_PROGRAM")
    if not tool:
        raise ValueError("CMAKE_MAKE_PROGRAM missing; reconfigure Kodi")
    if generator in ("Ninja", "Ninja Multi-Config"):
        command = [tool]
        if generator == "Ninja Multi-Config":
            if not config:
                raise ValueError("Ninja Multi-Config requires PS5_BUILD_CONFIG")
            command += ["-f", f"build-{config}.ninja"]
        command += ["-n", "kodi.bin"]
        pending = r"kodi\.bin$|-o\s+[\"']?kodi\.bin|Re-running CMake"
    elif generator == "Unix Makefiles":
        # A Make dry run cannot regenerate its CMake graph before checking links.
        graph = build / "CMakeFiles/Makefile.cmake"
        dependencies = re.search(r"set\(CMAKE_MAKEFILE_DEPENDS\s+(.*?)\n\s*\)",
                                 graph.read_text(), re.DOTALL)
        if not dependencies:
            raise ValueError("cannot read CMake graph dependencies; reconfigure Kodi")
        for name in shlex.split(dependencies.group(1)):
            path = build / name
            if not path.is_file() or path.stat().st_mtime_ns > graph.stat().st_mtime_ns:
                raise ValueError(f"CMake graph has pending work for {path}; reconfigure and build Kodi")
        command = [tool, "--no-print-directory", "-n", "kodi.bin"]
        pending = r"cmake_link_script\s+\S*/link\.txt|(?:^|\s)-o\s+[\"']?kodi\.bin"
    else:
        raise ValueError(f"unsupported package freshness generator: {generator}; use Ninja")
    result = subprocess.run(command, cwd=build, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"cannot verify kodi.bin freshness: {result.stderr.strip() or result.stdout.strip()}")
    links = [line for line in result.stdout.splitlines() if re.search(pending, line)]
    if links:
        raise ValueError(f"{build}/kodi.bin has pending link work; finish cmake --build {build}\n"
                         + "\n".join(links))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("--config", default="")
    args = parser.parse_args()
    try:
        check(args.build.resolve(), args.config)
    except (OSError, ValueError) as error:
        parser.exit(1, f"PS5 package: {error}\n")


if __name__ == "__main__":
    main()
