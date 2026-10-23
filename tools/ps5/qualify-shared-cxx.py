#!/usr/bin/env python3
"""Strict, local-only qualification of SDK C++ archives for shared linking.

This does not sign, package, deploy or declare the native runtime ready.
Missing imports remain link errors; no stub implementations are generated.
"""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from native_c_link import Elf, cstring, require
from native_shared_link import dynamic_entries, load_provider
from prepare_runtime_archive import prepare_archive

ROOT = Path(__file__).resolve().parents[2]
HEAP_WRAPS = (
    "malloc", "calloc", "realloc", "free", "posix_memalign",
    "malloc_usable_size", "aligned_alloc", "memalign", "reallocf",
)
RUNTIME_WRAPS = (*HEAP_WRAPS, "chdir", "fopen", "pthread_create", "exit")
NETWORK_SOURCE = ROOT / "shims/native-app/libc_net.c"
NETWORK_SYMBOLS = tuple(sorted((
    "getaddrinfo", "freeaddrinfo", "getnameinfo", "gai_strerror", "gethostname",
    "gethostbyname", "__h_errno",
)))


@contextmanager
def record_report(build, report):
    try:
        yield
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        report["status"] = "blocked"
        report["error"] = str(error)
        raise
    finally:
        (build / "qualification-report.json").write_text(json.dumps(report, indent=2) + "\n")


def qualify(sdk, build, lld, clang, ar="llvm-ar-18", objcopy="llvm-objcopy-18",
            providers=()):
    sdk, build = Path(sdk).resolve(), Path(build).resolve()
    compiler = sdk / "bin/prospero-clang++"
    c_compiler = sdk / "bin/prospero-clang"
    resource = subprocess.run([str(clang), "--print-resource-dir"], check=True,
                              capture_output=True, text=True).stdout.strip()
    archives = [sdk / "target/lib" / name for name in ("libc++.a", "libc++abi.a", "libunwind.a")]
    builtins = Path(resource) / "lib/linux/libclang_rt.builtins-x86_64.a"
    platform_archive = sdk / "target/lib/libc.a"
    sources = [ROOT / "shims" / name for name in (
        "native-app/libc_locale.c", "native-app/libc_posix.c", "libkodishim/kodishim.c",
        "native-app/shared_tls.c", "native-app/shared_heap.c", "native-app/thread_stack.c",
        "native-app/shared_exit.c")] + [NETWORK_SOURCE]
    crt = ROOT / "tools/ps5/native_module_crt.cpp"
    overlay = ROOT / "tools/ps5/sdk/include"
    headers = [overlay / "stdio.h", ROOT / "shims/native-app/shared_heap.h",
               ROOT / "shims/native-app/shared_tls.h"]
    for path in [compiler, c_compiler, *archives, platform_archive, builtins, *sources,
                 crt, *headers]:
        require(path.is_file(), f"missing qualification input: {path}")
    provider_paths = [Path(p).resolve() for p in providers]
    manifests = [load_provider(p) for p in provider_paths]
    require(len({p["soname"] for p in manifests}) == len(manifests),
            "duplicate system provider SONAME")
    stubs = ([path.parent / manifest["import_library"]["path"]
              for path, manifest in zip(provider_paths, manifests)] if manifests else
             sorted(p for p in (sdk / "target/lib").glob("*.so")
                    if p.name not in ("libScePosixForWebKit.so", "libkernel_sys.so")))
    require(stubs, "no SDK system import libraries found")
    build.mkdir(parents=True, exist_ok=True)
    report = {
        "stage": "shared-cxx-link-qualification",
        "native_loadable": False,
        "commands": [],
        "archives": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in [*archives, platform_archive, builtins]},
        "sources": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [crt, *sources]},
        "headers": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in headers},
        "wrap_symbols": list(RUNTIME_WRAPS),
        "system_import_mode": "verified-native" if manifests else "sdk-stub-diagnostic",
        "system_providers": [
            {"manifest": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
             "module": manifest["module"], "library": manifest["library"],
             "import_library_sha256": manifest["import_library"]["sha256"]}
            for path, manifest in zip(provider_paths, manifests)],
        "limitations": [
            "SDK system stubs are link inputs, not proof of installed native provider coverage.",
            "Strict shared linking is not native conversion or console acceptance.",
            "Existing title compatibility implementations are not a qualified shared C ABI.",
            "Clients must apply the shared wrap contract; native internal calls are not interposed.",
            "Shared allocator, TLS and native stdio still require console acceptance.",
            "The core runtime and its heap/TLS state must remain loaded for process lifetime.",
        ],
    }
    with record_report(build, report), tempfile.TemporaryDirectory(prefix="link-", dir=build) as temporary:
        obj, linked = Path(temporary) / "crt.o", Path(temporary) / "linked.so"
        prepared = Path(temporary) / "libc++.a"
        report["archive_preparation"] = prepare_archive(archives[0], prepared, ar, objcopy)
        link_archives = [prepared, *archives[1:]]
        platform_objects = [Path(temporary) / (p.stem + ".o") for p in sources]
        sdk_objects = [Path(temporary) / "assert.o"]
        commands = [
            ("compile", [str(compiler), "-std=c++20", "-O2",
             "-fPIC", "-fno-stack-protector", "-femulated-tls",
             "-DPS5_SHARED_RUNTIME_CORE=1", "-nostdinc++",
             "-I" + str(sdk / "target/include/c++/v1"), "-I" + str(overlay),
             "-isystem", str(sdk / "target/include"),
             "-c", str(crt), "-o", str(obj)]),
            *((f"compile-{source.stem}",
               [str(c_compiler), "-std=c11", "-O2",
                "-fPIC", "-fno-stack-protector",
                *(["-femulated-tls"] if source == NETWORK_SOURCE else []),
                "-I" + str(overlay),
                "-c", str(source), "-o", str(output)])
              for source, output in zip(sources, platform_objects)),
            ("link", [str(lld), "-shared", "--hash-style=sysv", "--eh-frame-hdr", "-z", "now",
             "-z", "max-page-size=0x4000", "--no-undefined", "--error-limit=0",
             "-L" + str(sdk / "target/lib"), "-init", "module_start", "-fini", "module_stop",
             "-T", str(ROOT / "tools/ps5/native-shared.ld"), "-o", str(linked), str(obj),
             *map(str, platform_objects), *map(str, sdk_objects),
             *("--wrap=" + name for name in RUNTIME_WRAPS),
             "--whole-archive", *map(str, link_archives), "--no-whole-archive", str(builtins),
             "--as-needed", *map(str, stubs)]),
        ]
        # Do not pull the SDK's entire C library or its separate emutls registry.
        report["sdk_objects"] = {}
        for output in sdk_objects:
            command = [str(ar), "p", str(platform_archive), output.name]
            result = subprocess.run(command, capture_output=True)
            phase = f"extract-{output.stem}"
            report["commands"].append({"phase": phase, "argv": command,
                                       "exit_code": result.returncode})
            (build / (phase + ".log")).write_bytes(result.stderr)
            require(result.returncode == 0 and result.stdout,
                    f"cannot extract SDK {output.name}; see {build / (phase + '.log')}")
            image = Elf(result.stdout)
            require(image.kind == 1, f"SDK {output.name} is not an ET_REL object")
            expected = "__assert"
            require(any(n == expected and s[3] and s[1] & 15 == 2
                        for n, s in image.symbols(".symtab")),
                    f"SDK {output.name} does not define function {expected}")
            require(not any(h[5] and h[2] & 0x400 for h in image.sections.values()),
                    f"SDK {output.name} contains unsupported native TLS")
            output.write_bytes(result.stdout)
            report["sdk_objects"][output.name] = hashlib.sha256(result.stdout).hexdigest()
        for phase, command in commands:
            result = subprocess.run(command, capture_output=True, text=True)
            diagnostic = result.stdout + result.stderr
            (build / (phase + ".log")).write_text(diagnostic)
            report["commands"].append({"phase": phase, "argv": command,
                                       "exit_code": result.returncode})
            if result.returncode:
                report["status"] = "blocked"
                report["failed_phase"] = phase
                report["undefined_symbols"] = sorted(set(
                    re.findall(r"undefined symbol: (.+)", diagnostic)))
                break
        else:
            image = Elf(linked.read_bytes())
            report["status"] = "strict-link-passed"
            report["undefined_symbols"] = [n for n, s in image.symbols(".dynsym") if n and not s[3]]
            report["legacy_constructor_sections"] = [
                n for n, h in image.sections.items() if h[5] and n.startswith((".ctors", ".dtors"))]
            report["native_tls_sections"] = [n for n, h in image.sections.items() if h[5] and h[2] & 0x400]
            weak_imports = [
                n for n, s in image.symbols(".dynsym") if n and not s[3] and s[1] >> 4 == 2]
            provided = set()
            if manifests:
                needed = {cstring(image.section(".dynstr"), offset)
                          for offset in dynamic_entries(image).get(1, [])}
                provided = {s["name"] for p in manifests if p["soname"] in needed
                            for s in p["symbols"]}
            report["bound_weak_imports"] = [n for n in weak_imports if n in provided]
            report["unbound_weak_imports"] = [n for n in weak_imports if n not in provided]
            report["native_profile_blockers"] = []
            for key in ("legacy_constructor_sections", "native_tls_sections", "unbound_weak_imports"):
                if report[key]:
                    report["native_profile_blockers"].append({key: report[key]})
            report["export_count"] = sum(bool(n and s[3]) for n, s in image.symbols(".dynsym"))
            destination = build / "diagnostic-linked.so"
            shutil.copyfile(linked, destination)
            report["diagnostic_elf"] = str(destination)
            report["diagnostic_elf_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"C++ qualification: {report['status']}; report: {build / 'qualification-report.json'}")
    if report["status"] == "blocked":
        print(f"Unresolved symbols: {len(report['undefined_symbols'])}; see {build / 'link.log'}")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default="/opt/ps5-payload-sdk")
    parser.add_argument("--build", type=Path, default=ROOT / "build/ps5-shared-runtime/cxx-qualification")
    parser.add_argument("--lld", default=shutil.which("ld.lld"))
    parser.add_argument("--clang", default=shutil.which("clang-18"))
    parser.add_argument("--ar", default=shutil.which("llvm-ar-18"))
    parser.add_argument("--objcopy", default=shutil.which("llvm-objcopy-18"))
    parser.add_argument("--provider", type=Path, action="append", default=[],
                        help="verified native-system SDK manifest; repeat for each module")
    args = parser.parse_args()
    if not all((args.lld, args.clang, args.ar, args.objcopy)):
        parser.error("ld.lld, clang-18, llvm-ar-18 and llvm-objcopy-18 are required")
    try:
        return qualify(args.sdk, args.build, args.lld, args.clang, args.ar, args.objcopy,
                       args.provider)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"qualify-shared-cxx: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
