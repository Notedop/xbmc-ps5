#!/usr/bin/env python3
"""Rebuild the eight audited stdio consumers, without installing anything.

Configured depends libraries replay their complete upstream library targets in
private source copies, retaining generated feature headers and compiler flags.
Gettext alone corrects header-only C11 thread detection and requires pthreads;
the original configuration and explicit native overrides are receipted.
The SDK's fmt12.1.0/libpng1.6.43 use their cached homebrew sources and recipes.
No archive member is patched or replaced in an existing archive.

python3 tools/ps5/build-native-stdio-depends.py --jobs 2
Outputs archive-map.json and receipts below build/ps5-native-stdio-depends.
Parent integration must replace exact paths, including BOTH bzip2 inputs.
"""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile

from native_c_link import Elf
from prepare_runtime_archive import archive_members

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "x86_64-unknown-freebsd-debug"
PACKAGES = ("bzip2", "curl", "openssl", "gettext", "libsmb2", "fmt", "libpng")
BAD_IMPORTS = {"__srget", "__swbuf", "__isthreaded"}
# Equivalent to ac_cv_header_threads_h=no gl_cv_have_weak=no with POSIX threads:
# native links require pthreads; even setlocale_null must not probe weak symbols.
GETTEXT_THREAD_CONFIG = {
    "HAVE_THREADS_H": 0, "HAVE_WEAK_SYMBOLS": 0, "USE_POSIX_THREADS_WEAK": 0,
}
GETTEXT_PTHREAD_IMPORTS = {
    "pthread_mutex_init", "pthread_mutex_lock", "pthread_mutex_unlock",
    "pthread_mutex_destroy", "pthread_cond_wait", "pthread_cond_signal",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run(command, cwd, log, env=None):
    with log.open("a") as stream:
        stream.write("\n$ " + shlex.join(map(str, command)) + "\n")
        stream.flush()
        result = subprocess.run(list(map(str, command)), cwd=cwd, env=env,
                                stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        tail = log.read_text(errors="replace").splitlines()[-35:]
        raise RuntimeError(f"{shlex.join(map(str, command))}\n" + "\n".join(tail))


def source_manifest(source):
    return {str(p.relative_to(source)): digest(p) for p in sorted(source.rglob("*"))
            if p.is_file() and p.suffix in (".c", ".h", ".cc", ".cpp", ".patch")}


def copy_configured(source, destination, sdk, wrapper_c, wrapper_cxx):
    """Preserve configuration, but discard every compiled library/object."""
    def ignore(directory, names):
        return [name for name in names if name in (".git", ".libs", ".deps") or
                Path(name).suffix in (".o", ".lo", ".a", ".la", ".so", ".bc")]

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)
    for path in destination.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if (path.name not in ("Makefile", "libtool", "config.status") and
                path.suffix not in (".make", ".cmake", ".txt")):
            continue
        original = path.read_text(errors="surrogateescape")
        text = original.replace(str(source), str(destination))
        for old, new in ((sdk / "bin/prospero-clang++", wrapper_cxx),
                         (sdk / "bin/prospero-clang", wrapper_c)):
            text = text.replace("/usr/bin/ccache " + str(old), str(new))
            text = text.replace(str(old), str(new))
        if text != original:
            path.write_text(text, errors="surrogateescape")


def unpack(archive, destination):
    destination.mkdir(parents=True)
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] in ("/", "..") or ".." in parts:
                raise ValueError("unsafe cached source archive path")
            member.name = str(Path(*parts[1:]))
            if member.name == ".":
                continue
            if member.issym() or member.islnk():
                raise ValueError("source archive links require explicit review")
            stream.extract(member, destination, filter="data")


def archive_symbols(nm, archive, defined=False):
    flags = ["--defined-only", "--extern-only"] if defined else ["--undefined-only"]
    output = subprocess.check_output([str(nm), *flags, str(archive)], text=True)
    return {line.split()[-1] for line in output.splitlines()
            if line.split() and not line.endswith(":")}


def validate_gettext_threads(nm, archive):
    output = subprocess.check_output(
        [str(nm), "--undefined-only", str(archive)], text=True)
    imports = {tuple(line.split()) for line in output.splitlines()
               if len(line.split()) == 2}
    if any(symbol == "thrd_exit" for _, symbol in imports):
        raise ValueError("gettext retains unsupported C11 thrd_exit import")
    if any(kind.lower() == "w" and symbol.startswith("pthread_")
           for kind, symbol in imports):
        raise ValueError("gettext pthread locking must not be optional")
    strong = {symbol for kind, symbol in imports if kind == "U"}
    if not GETTEXT_PTHREAD_IMPORTS <= strong:
        raise ValueError("gettext lost required pthread locking imports")
    return {"thrd_exit_imports": [], "optional_pthread_imports": [],
            "required_pthread_imports": sorted(GETTEXT_PTHREAD_IMPORTS)}


def configure_gettext_threads(source, upstream, compiler, component, log):
    """Correct only native threading facts, not the retained library features."""
    probe = component / "pthread-probe.c"
    probe.write_text(
        "#if !defined(__PROSPERO__) || !__STDC_NO_THREADS__\n"
        '#error "expected native SDK without C11 threads"\n'
        "#endif\n#include <pthread.h>\n"
        "int probe(pthread_mutex_t *lock) { return pthread_mutex_lock(lock); }\n")
    run([compiler, "-c", probe, "-o", component / "pthread-probe.o"], component, log)
    overrides = {}
    for filename in ("gettext-runtime/config.h", "gettext-runtime/intl/config.h",
                     "gettext-runtime/config.status", "gettext-runtime/intl/config.status"):
        path = source / filename
        text = path.read_text()
        before = digest(path)
        header = filename.endswith(".h")
        posix = "#define USE_POSIX_THREADS 1" if header else 'D["USE_POSIX_THREADS"]=" 1"'
        if posix not in text:
            raise ValueError("gettext requires its configured POSIX thread backend")
        for name, value in GETTEXT_THREAD_CONFIG.items():
            old = f"#define {name} 1" if header else f'D["{name}"]=" 1"'
            new = f"#define {name} {value}" if header else f'D["{name}"]=" {value}"'
            if old not in text or (header and text.count(old) != 1):
                raise ValueError(f"unexpected original gettext configuration: {name}")
            text = text.replace(old, new)
        path.write_text(text)
        overrides[filename] = {"original_sha256": digest(upstream / filename),
                               "retained_configuration_sha256": before,
                               "native_sha256": digest(path),
                               "defines": GETTEXT_THREAD_CONFIG}
    return overrides


def validate_objects(archive, records):
    members, indexed = archive_members(archive.read_bytes())
    compiled = {Path(record["object"]).name for record in records}
    missing = set(members) - compiled
    if missing or not indexed:
        raise ValueError(f"archive contains objects without fresh compile evidence: {missing}")
    for member, raw in members.items():
        image = Elf(raw)
        if image.kind != 1:
            raise ValueError(f"{member}: expected native ET_REL")
        for name, section in image.sections.items():
            if section[2] & 0x400 or name.startswith((".ctors", ".dtors")):
                raise ValueError(f"{member}: native TLS/legacy constructor section {name}")
    return {"member_count": len(members), "all_members_have_compile_records": True,
            "native_tls_sections": [], "legacy_ctor_sections": []}


def compiler_probe(compiler, output, log):
    probe = output / "opaque-probe.c"
    probe.write_text("#include <stdio.h>\n#include <stddef.h>\n"
                     "_Static_assert(sizeof(fpos_t)==24, \"size\");\n"
                     "_Static_assert(_Alignof(fpos_t)==8, \"alignment\");\n"
                     "_Static_assert(offsetof(fpos_t,__state)==8, \"state\");\n")
    run([compiler, "-c", probe, "-o", output / "opaque-probe.o"], output, log)
    probe.write_text("#include <stdio.h>\nint bad(void) { return sizeof(FILE); }\n")
    result = subprocess.run([str(compiler), "-c", str(probe), "-o",
                             str(output / "must-not-exist.o")], capture_output=True, text=True)
    if result.returncode == 0 or "incomplete" not in result.stderr:
        raise ValueError("compiler failed to enforce opaque native FILE")
    (output / "opaque-rejection.txt").write_text(result.stderr)
    probe.unlink()
    (output / "opaque-probe.o").unlink()


def compiler_mode(config, cpp, arguments):
    configuration = json.loads(Path(config).read_text())
    if "-c" in arguments and "-o" not in arguments:
        sources = [arg for arg in arguments if Path(arg).suffix in (".c", ".cc", ".cpp")]
        if len(sources) != 1:
            raise ValueError("implicit object output requires one translation unit")
        arguments = [*arguments, "-o", Path(sources[0]).with_suffix(".o").name]
    sdk, overlay = Path(configuration["sdk"]), Path(configuration["overlay"])
    prefix = ["-fPIC", "-femulated-tls", "-fno-stack-protector"]
    if cpp:
        prefix += ["-nostdinc++", "-I" + str(sdk / "target/include/c++/v1")]
    prefix += ["-I" + str(overlay), "-isystem", str(sdk / "target/include"),
               "-include", str(overlay / "stdio.h")]
    if configuration["package"] == "fmt":
        prefix += ["-DFMT_USE_FALLBACK_FILE=1"]
    compiler = sdk / ("bin/prospero-clang++" if cpp else "bin/prospero-clang")
    command = [sys.executable, str(ROOT / "tools/ps5/native-clang.py"),
               str(compiler), *prefix, *arguments]
    result = subprocess.call(command)
    if result == 0 and "-c" in arguments and "-o" in arguments:
        obj = Path(arguments[arguments.index("-o") + 1]).resolve()
        source = [Path(arg).resolve() for arg in arguments
                  if Path(arg).suffix in (".c", ".cc", ".cpp", ".cxx")]
        record = {"object": str(obj), "source": [
            {"path": str(p), "sha256": digest(p)} for p in source if p.is_file()],
                  "command": command, "overlay_sha256": digest(overlay / "stdio.h")}
        destination = Path(configuration["records"]) / (hashlib.sha256(
            str(obj).encode()).hexdigest() + ".json")
        write_json(destination, record)
    return result


def make_wrappers(component, args, package):
    records = component / "compile-records"
    records.mkdir()
    config = component / "compiler.json"
    write_json(config, {"sdk": str(args.sdk), "overlay": str(args.overlay),
                        "package": package, "records": str(records)})
    wrappers = []
    for cpp in (False, True):
        wrapper = component / ("cxx" if cpp else "cc")
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join([
            sys.executable, str(Path(__file__).resolve()), "--compiler", str(config),
            "--language", "c++" if cpp else "c", "--"]) + ' "$@"\n')
        wrapper.chmod(0o755)
        wrappers.append(wrapper)
    return wrappers


def originals(args, name):
    depends, hb = args.depends / "lib", args.sdk / "target/user/homebrew/lib"
    return {
        "bzip2": [depends / "libbz2.a", hb / "libbz2.a"],
        "curl": [depends / "libcurl.a"],
        "openssl": [depends / "libcrypto.a"],
        "gettext": [depends / "libintl.a"],
        "libsmb2": [depends / "libsmb2.a"],
        "fmt": [hb / "libfmt.a"],
        "libpng": [hb / "libpng16.a"],
    }[name]


def feature_headers(source, package):
    names = {
        "curl": ["build/lib/curl_config.h"],
        "openssl": ["include/openssl/configuration.h", "include/openssl/opensslv.h"],
        "gettext": ["gettext-runtime/intl/config.h", "gettext-runtime/config.h"],
        "libsmb2": ["build/config.h"],
        "bzip2": ["bzlib.h"],
    }.get(package, [])
    result = {name: digest(source / name) for name in names if (source / name).is_file()}
    if package in ("curl", "gettext", "libsmb2") and not result:
        raise ValueError(f"{package}: expected retained feature configuration")
    return result


def build_component(args, name):
    component = args.output / name
    receipt = component / "receipt.json"
    old_archives = originals(args, name)
    identity = {"overlay": digest(args.overlay / "stdio.h"),
                "launcher": digest(ROOT / "tools/ps5/native-clang.py"),
                "original_archives": {str(p): digest(p) for p in old_archives}}
    if name == "gettext":
        identity["native_thread_configuration"] = GETTEXT_THREAD_CONFIG
    if receipt.exists() and name not in args.rebuild:
        saved = json.loads(receipt.read_text())
        if saved["identity"] != identity:
            raise ValueError(f"stale completed component: {component}; use a new output")
        provenance = saved["provenance"]
        if "source_manifest" in provenance:
            if source_manifest(Path(provenance["configured_source"])) != provenance["source_manifest"]:
                raise ValueError("configured upstream sources changed since migration")
        elif digest(provenance["source_archive"]) != provenance["source_sha256"]:
            raise ValueError("cached upstream source archive changed since migration")
        if digest(provenance["recipe"]) != provenance["recipe_sha256"]:
            raise ValueError("upstream recipe changed since migration")
        for output in saved["outputs"]:
            if digest(output["rebuilt"]) != output["sha256"]:
                raise ValueError("rebuilt archive changed since receipt")
        records = [json.loads(p.read_text()) for p in
                   (component / "compile-records").glob("*.json")]
        saved["object_validation"] = validate_objects(Path(saved["outputs"][0]["rebuilt"]), records)
        if name == "gettext":
            saved["validation"]["threading"] = validate_gettext_threads(
                args.sdk / "bin/llvm-nm", Path(saved["outputs"][0]["rebuilt"]))
        write_json(receipt, saved)
        print(name + ": verified completed replacement", flush=True)
        return saved
    if component.exists():
        if name not in args.rebuild and not args.retry_incomplete:
            raise ValueError(f"incomplete {component}; use --retry-incomplete to replace")
        shutil.rmtree(component)
    component.mkdir(parents=True)
    log = component / "build.log"
    cc, cxx = make_wrappers(component, args, name)
    compiler_probe(cc, component, log)
    source = component / "source"
    upstream = ROOT / "tools/depends/target" / name / PLATFORM
    provenance = {}
    features = {}
    if name not in ("fmt", "libpng"):
        features = feature_headers(upstream, name)
        provenance = {"configured_source": str(upstream),
                      "source_manifest": source_manifest(upstream),
                      "configuration_headers": features,
                      "recipe": str(upstream.parent / "Makefile"),
                      "recipe_sha256": digest(upstream.parent / "Makefile")}
        provenance["retained_build_configuration"] = {
            str(p.relative_to(upstream)): digest(p)
            for p in (upstream / "Makefile", upstream / "configdata.pm",
                      upstream / "build/CMakeCache.txt", upstream / "config.status")
            if p.is_file()}
        copy_configured(upstream, source, args.sdk, cc, cxx)
        if name == "gettext":
            provenance["native_thread_configuration"] = configure_gettext_threads(
                source, upstream, cc, component, log)
            features = feature_headers(source, name)
    else:
        version, filename = (("12.1.0", "12.1.0.tar.gz") if name == "fmt" else
                             ("1.6.43", "libpng-1.6.43.tar.xz"))
        cache = args.homebrew_sources / name
        archive = cache / filename
        if not archive.is_file():
            raise FileNotFoundError(f"exact cached {name}{version} source required: {archive}")
        unpack(archive, source)
        provenance = {"version": version, "source_archive": str(archive),
                      "source_sha256": digest(archive), "recipe": str(cache / "PKGBUILD"),
                      "recipe_sha256": digest(cache / "PKGBUILD")}
        headers = (["fmt/base.h", "fmt/format.h", "fmt/format-inl.h", "fmt/os.h"]
                   if name == "fmt" else ["png.h", "pngconf.h"])
        provenance["installed_header_equivalence"] = {}
        for header in headers:
            local = source / ("include/" + header if name == "fmt" else header)
            installed = args.sdk / "target/user/homebrew/include" / header
            if digest(local) != digest(installed):
                raise ValueError(f"cached {name}{version} source differs from installed {header}")
            provenance["installed_header_equivalence"][header] = digest(installed)
    print(name + ": rebuilding complete upstream library target", flush=True)
    env = {**os.environ, "TMPDIR": str(component), "CC": str(cc), "CXX": str(cxx),
           "AR": str(args.sdk / "bin/prospero-ar"),
           "RANLIB": str(args.sdk / "bin/prospero-ranlib")}
    make = ["make", "-j" + str(args.jobs)]
    if name in ("curl", "libsmb2"):
        target = "libcurl_static" if name == "curl" else "smb2"
        library = "libcurl.a" if name == "curl" else "libsmb2.a"
        run([*make, "-f", f"lib/CMakeFiles/{target}.dir/build.make",
             "lib/" + library], source / "build", log, env)
        built = source / "build/lib" / library
    elif name == "bzip2":
        run([*make, "libbz2.a", "CC=" + str(cc),
             "AR=" + str(args.sdk / "bin/prospero-ar"),
             "CFLAGS=-fPIC -DPIC -O2 -D_FILE_OFFSET_BITS=64"], source, log, env)
        built = source / "libbz2.a"
    elif name == "openssl":
        run([*make, "libcrypto.a", "libssl.a", "CC=" + str(cc)], source, log, env)
        built = source / "libcrypto.a"
    elif name == "gettext":
        intl = source / "gettext-runtime/intl"
        run([*make, "CC=" + str(cc)], intl / "gnulib-lib", log, env)
        run([*make, "libintl.la", "CC=" + str(cc)], intl, log, env)
        built = intl / ".libs/libintl.a"
    elif name == "fmt":
        run(["cmake", "-S", source, "-B", source / "build",
             "-DCMAKE_SYSTEM_NAME=FreeBSD", "-DCMAKE_CXX_COMPILER=" + str(cxx),
             "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
             "-DCMAKE_BUILD_TYPE=Release", "-DFMT_DOC=OFF", "-DFMT_TEST=OFF",
             "-DFMT_INSTALL=ON", "-DBUILD_SHARED_LIBS=OFF"], component, log, env)
        run(["cmake", "--build", source / "build", "--target", "fmt",
             "--parallel", str(args.jobs)], component, log, env)
        built = source / "build/libfmt.a"
    else:
        run([source / "configure", "--host=x86_64-pc-freebsd",
             "--prefix=/user/homebrew", "--enable-static", "--disable-shared",
             "--disable-tests", "CFLAGS=-O2 -fPIC",
             "CPPFLAGS=-I" + str(args.sdk / "target/user/homebrew/include"),
             "LDFLAGS=-L" + str(args.sdk / "target/user/homebrew/lib")],
            source, log, env)
        run([*make, "libpng16.la"], source, log, env)
        built = source / ".libs/libpng16.a"
        old_header = args.sdk / "target/user/homebrew/include/libpng16/pnglibconf.h"
        if (source / "pnglibconf.h").read_bytes() != old_header.read_bytes():
            raise ValueError("libpng feature configuration differs from installed1.6.43")
        features = {"pnglibconf.h": digest(old_header)}
    for filename, expected in features.items():
        if digest(source / filename) != expected:
            raise ValueError(f"feature configuration changed: {name}/{filename}")
    archive = component / built.name
    shutil.copy2(built, archive)
    nm = args.sdk / "bin/llvm-nm"
    undefined = archive_symbols(nm, archive)
    if undefined & BAD_IMPORTS:
        raise ValueError(f"old stdio helper imports remain: {undefined & BAD_IMPORTS}")
    records = [json.loads(p.read_text()) for p in (component / "compile-records").glob("*.json")]
    if not records:
        raise ValueError("no fresh source compilations recorded")
    for record in records:
        if record["overlay_sha256"] != identity["overlay"]:
            raise ValueError("mixed overlay compilation")
    exports = archive_symbols(nm, archive, defined=True)
    threading = validate_gettext_threads(nm, archive) if name == "gettext" else None
    baseline = set().union(*(archive_symbols(nm, old, defined=True) for old in old_archives))
    missing = sorted(baseline - exports)
    if name == "fmt":
        missing = [symbol for symbol in missing if "file_print_buffer" not in symbol]
    if missing:
        raise ValueError(f"public definitions removed from {name}: {missing[:15]}")
    objects = validate_objects(archive, records)
    outputs = [{"original": str(old), "rebuilt": str(archive), "sha256": digest(archive)}
               for old in old_archives]
    saved = {"identity": identity, "outputs": outputs, "provenance": provenance,
             "feature_headers": features, "compile_count": len(records),
             "object_validation": objects,
             "undefined_symbols": sorted(undefined), "builder_sha256": digest(__file__),
             "validation": {"opaque_FILE_rejected": True, "fpos_size": 24, "fpos_align": 8,
                            "all_objects_fresh_with_forced_opaque_overlay": True,
                            "old_helper_imports": [], "missing_public_definitions": missing},
             "host_execution_is_not_console_proof": True}
    if threading is not None:
        saved["validation"]["threading"] = threading
    write_json(receipt, saved)
    print(name + f": ready ({len(records)} fresh translation units)", flush=True)
    return saved


def main():
    if "--compiler" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--compiler")
        parser.add_argument("--language")
        parser.add_argument("arguments", nargs=argparse.REMAINDER)
        args = parser.parse_args()
        return compiler_mode(args.compiler, args.language == "c++",
                             args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/ps5-native-stdio-depends")
    parser.add_argument("--sdk", type=Path, default=Path("/opt/ps5-payload-sdk"))
    parser.add_argument("--depends", type=Path, default=ROOT / "build/ps5-depends-test" / PLATFORM)
    parser.add_argument("--overlay", type=Path, default=ROOT / "tools/ps5/sdk/include")
    parser.add_argument("--homebrew-sources", type=Path,
                        default=Path(os.environ.get(
                            "PS5_HOMEBREW_SOURCES", ROOT / "build/ps5-external/pacbrew-repo")))
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--only", nargs="+", choices=PACKAGES, default=PACKAGES)
    parser.add_argument("--retry-incomplete", action="store_true")
    parser.add_argument("--rebuild", nargs="+", choices=PACKAGES, default=[],
                        help="explicitly replace selected completed isolated components")
    args = parser.parse_args()
    for name in ("output", "sdk", "depends", "overlay", "homebrew_sources"):
        setattr(args, name, getattr(args, name).resolve())
    if args.jobs < 1 or any(
            args.output.is_relative_to(path) or path.is_relative_to(args.output)
            for path in (args.sdk, args.depends, args.homebrew_sources, ROOT / "tools")):
        parser.error("output must be isolated from inputs, jobs must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    saved = [build_component(args, name) for name in args.only]
    mapping = {item["original"]: item["rebuilt"] for receipt in saved for item in receipt["outputs"]}
    prior = args.output / "archive-map.json"
    if prior.exists():
        mapping = {**json.loads(prior.read_text()), **mapping}
    write_json(prior, mapping)
    write_json(args.output / "integration.json", {
        "archive_map": str(prior), "complete": len(mapping) == 8,
        "contract": [
            "Replace resolved absolute archive paths using archive-map.json, preserving link order.",
            "Replace both original libbz2.a paths; they deliberately share one rebuilt1.0.8 archive.",
            "Do not install into /opt or existing depends prefixes and do not add stdio helper shims.",
            "Keep the opaque overlay on every future C compilation; place libc++ wrappers first for C++.",
            "Receipts preserve original hashes, feature headers, upstream library build commands, "
            "and per-object source/forced-overlay compiler evidence.",
            "Gettext records scoped native thread feature corrections: no C11 threads, "
            "mandatory POSIX locking (no weak optional pthread detection).",
            "Run test-native-stdio-depends.py before consuming the complete mapping. "
            "Host tests do not establish console execution qualification.",
        ]})
    for receipt in saved:
        for original, expected in receipt["identity"]["original_archives"].items():
            if digest(original) != expected:
                raise ValueError(f"original installation changed during build: {original}")
    print(f"{len(mapping)} exact archive replacements: {prior}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit("native-stdio-depends: " + str(error))
