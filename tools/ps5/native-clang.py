#!/usr/bin/env python3
"""Compiler launcher retaining PS5 source ABI with modern ELF ctor arrays."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


def default_object_output(compiler, arguments):
    result = subprocess.run([compiler, *arguments, "-###"], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("cannot determine native object output: " + result.stderr.strip())
    outputs = []
    for line in result.stderr.splitlines():
        if not line.lstrip().startswith('"'):
            continue
        command = shlex.split(line)
        if "-cc1" in command and "-emit-obj" in command and "-o" in command:
            outputs.append(Path(command[command.index("-o") + 1]))
    if len(outputs) != 1:
        raise ValueError("native object compilation requires one compiler object output")
    return outputs[0]


def launch(compiler, arguments):
    prefix = []
    if Path(compiler).name in ("ccache", "ccache.exe", "sccache", "sccache.exe"):
        if not arguments:
            raise ValueError("compiler cache must be followed by the native compiler")
        prefix, compiler, arguments = [compiler], arguments[0], arguments[1:]
    if "-c" not in arguments or any(
            flag in arguments for flag in ("-E", "-S", "-fsyntax-only", "-emit-llvm")):
        return subprocess.call([*prefix, compiler, *arguments])
    output = None
    frontend = []
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg == "-o":
            if output is not None or index + 1 == len(arguments):
                raise ValueError("expected one compiler object output")
            output = Path(arguments[index + 1])
            index += 2
            continue
        if arg.startswith("-o") and len(arg) > 2:
            if output is not None:
                raise ValueError("expected one compiler object output")
            output = Path(arg[2:])
            index += 1
            continue
        if arg.startswith(("--target=", "-target=")) and "ps5" not in arg:
            raise ValueError("native source compilation must retain the PS5 target")
        if arg == "-target":
            if index + 1 == len(arguments) or "ps5" not in arguments[index + 1]:
                raise ValueError("native source compilation must retain the PS5 target")
        if arg == "-fuse-init-array":
            raise ValueError("init arrays are selected by the backend, not the PS5 frontend")
        frontend.append(arg)
        index += 1
    if output is None:
        # Ask the driver so include options and explicit source languages retain their semantics.
        output = default_object_output(compiler, frontend)
    output = output.resolve()
    with tempfile.TemporaryDirectory(prefix=".native-clang-", dir=output.parent) as temporary:
        directory = Path(temporary)
        bitcode, obj = directory / "source.bc", directory / "object.o"
        # Keep default dependency targets tied to the requested object, not source.bc.
        if any(flag in frontend for flag in ("-MD", "-MMD")):
            if not any(flag.startswith(("-MT", "-MQ")) for flag in frontend):
                frontend.extend(("-MT", str(output)))
            if not any(flag.startswith("-MF") for flag in frontend):
                frontend.extend(("-MF", str(output.with_suffix(".d"))))
        result = subprocess.call([*prefix, compiler, *frontend, "-emit-llvm", "-o", str(bitcode)])
        if result:
            return result
        optimization = [flag for flag in arguments if flag.startswith("-O")]
        result = subprocess.call([
            *prefix, compiler, "--target=x86_64-unknown-freebsd9", "-march=znver2",
            "-fPIC", "-femulated-tls", "-fuse-init-array", "-Wno-override-module",
            *optimization, "-c", str(bitcode), "-o", str(obj),
        ])
        if result:
            return result
        os.replace(obj, output)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return launch(args.compiler, args.arguments)
    except (OSError, ValueError) as error:
        parser.exit(1, f"native-clang: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
