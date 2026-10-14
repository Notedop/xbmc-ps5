#!/usr/bin/env python3
"""Fail packaging if selected add-ons lack resolver-visible title symbols."""
import argparse
import re
import subprocess


def table(path):
    output = subprocess.check_output(["readelf", "--dyn-syms", "-W", path], text=True)
    result = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0].endswith(":") and fields[0][:-1].isdigit():
            result[fields[7]] = fields[1:7]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title")
    parser.add_argument("addons", nargs="*")
    args = parser.parse_args()
    title = table(args.title)
    relocs = subprocess.check_output(["readelf", "-rW", args.title], text=True)
    got = set(re.findall(r"R_X86_64_(?:GLOB_DAT|JUMP_SLOT|64)\s+\S+\s+(\S+)\s+\+\s+0(?:\s|$)", relocs))
    definitions = set()
    for name, (value, size, kind, binding, visibility, section) in title.items():
        if (section.isdigit() and binding in {"GLOBAL", "WEAK"} and
                visibility in {"DEFAULT", "PROTECTED"} and kind in {"FUNC", "OBJECT", "NOTYPE"}):
            definitions.add(name)
    scoped = {"__cxa_atexit", "__cxa_finalize"}
    unsafe = {"atexit", "__cxa_thread_atexit", "__cxa_thread_atexit_impl"}
    missing = set()
    required = 0
    for addon in args.addons:
        for name, (_, _, _, binding, _, section) in table(addon).items():
            if section != "UND" or binding == "WEAK":
                continue
            required += 1
            if name == "pthread_setspecific" and "__wrap_" + name not in definitions:
                missing.add("__wrap_" + name)
            if name in unsafe or not (name in scoped or name in definitions or
                                      "__wrap_" + name in definitions or
                                      (name in got and name in title and title[name][-1] == "UND")):
                missing.add(name)
    if missing:
        parser.exit(1, "!! add-on imports not safely exposed by title:\n  " +
                    "\n  ".join(sorted(missing)) + "\n")
    print(f"    host export preflight PASS: {len(definitions)} linked definitions; "
          f"{required} strong imports in {len(args.addons)} selected add-on(s) "
          "(not a console runtime PASS)")


if __name__ == "__main__":
    main()
