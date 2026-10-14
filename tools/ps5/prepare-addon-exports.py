#!/usr/bin/env python3
"""Generate native-link policy from SDK runtime archives and trusted add-on imports."""
import argparse
from pathlib import Path
import re
import subprocess


def symbols(nm, path, *options):
    output = subprocess.check_output([nm, *options, str(path)], text=True)
    return {line.split()[-1] for line in output.splitlines()
            if line.split() and not line.endswith(":")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("addons", type=Path, nargs="*")
    args = parser.parse_args()
    nm = str(args.sdk / "bin/llvm-nm")
    runtime = set()
    for name in ("libc++.a", "libc++abi.a", "libunwind.a"):
        runtime |= symbols(nm, args.sdk / "target/lib" / name, "-g", "--defined-only")
    imports = set()
    for addon in args.addons:
        if not addon.is_file():
            parser.error(f"missing trusted add-on: {addon}")
        imports |= symbols(nm, addon, "-D", "--undefined-only")
    # The lifecycle entry points are intercepted by ElfLoader, not the host CRT.
    lifecycle = {"__cxa_atexit", "__cxa_finalize", "__cxa_thread_atexit",
                 "__cxa_thread_atexit_impl", "atexit"}
    wrappers = {"__wrap_" + name for name in
                ("malloc", "calloc", "realloc", "free", "posix_memalign",
                 "malloc_usable_size", "pthread_create", "pthread_setspecific", "pipe", "fcntl",
                 "chdir", "write", "fopen")}
    exports = (runtime | imports | wrappers) - lifecycle
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.$]*", name) for name in exports):
        parser.error("versioned or invalid runtime/add-on symbol name")
    args.output.mkdir(parents=True, exist_ok=True)
    names = "".join(f"    {name};\n" for name in sorted(exports))
    (args.output / "addon-symbols.map").write_text(
        "{\n  global:\n" + names + "  local:\n    *;\n};\n")
    (args.output / "addon-dynamic.list").write_text("{\n" + names + "};\n")
    # Exporting an archive symbol does not extract its member. Root only runtime
    # definitions requested by selected add-ons, never payload libc/crt1.
    (args.output / "addon-roots.rsp").write_text(
        "".join(f"--undefined={name}\n" for name in sorted((imports & runtime) - lifecycle)))
    (args.output / "addon-imports.txt").write_text("".join(name + "\n" for name in sorted(imports)))
    # Imports used only by an add-on need a real relocated slot in the title.
    # LLVM supplies the names/relocations; no custom resolver address table.
    anchors = sorted(imports - runtime - lifecycle)
    (args.output / "addon-imports.S").write_text(
        '.section .data.rel.ro.ps5_addon_imports,"aw",@progbits\n.p2align 3\n' +
        "".join(f".quad {name}\n" for name in anchors) +
        '.section .note.GNU-stack,"",@progbits\n')
    print(f"    add-on export policy: {len(exports)} names, "
          f"{len((imports & runtime) - lifecycle)} SDK archive roots")


if __name__ == "__main__":
    main()
