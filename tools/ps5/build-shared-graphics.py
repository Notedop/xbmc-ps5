#!/usr/bin/env python3
"""Prepare the real GL/EGL driver group as PIC archives, never as a loadable SDK.

Run locally on Linux/WSL: python3 tools/ps5/build-shared-graphics.py
Requires the existing patched graphics checkout, configured Mesa baseline and
installed graphics SDK. No downloads, installation, packaging or deployment.
The output must be new and below build/ps5-shared-runtime. Reproduce a build
with --output build/ps5-shared-runtime/graphics-second.

Mesa's cached Meson options and cross files are replayed with b_staticpic=true.
PSBC's unmodified upstream Make recipes run in a private source copy because
their output and generated-header rules are source-relative. Runtime Make
overrides relocate outputs, preserve the baseline capability defines, and add
-fPIC through CC. Upstream import-stub targets are explicitly disabled.
Use --audit-existing to inspect TLS in a completed stage, cached/installed
graphics archives and SDK C++ runtime archives without rebuilding anything.
Native ELF TLS is rejected; emutls provider/ABI qualification remains external.
For native stdio ABI preparation use --sdk-include tools/ps5/sdk/include and
--output build/ps5-shared-runtime/graphics-ps5-native-abi. The native-clang
launcher preserves the actual PS5 source frontend and lowers bitcode
constructors to init arrays in a separate backend invocation.
C++ libc++ wrappers must
precede that overlay. Header hashes and all added flags fingerprint the stage;
an incomplete stage cannot be resumed after any fingerprint change.
The tracked PSBC recipe excludes external CLRX only under __PROSPERO__ in
the private source copy. --rebuild-psbc-from <completed-native-stage> reuses
the 18 unchanged native archives and rebuilds the patched PSBC member/group.
"""

import argparse
import ast
import configparser
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from native_shared_link import load_provider


ROOT = Path(__file__).resolve().parents[2]
PSBC_CLRX_GUARDS = (
    ("#ifndef _WIN32", "#if !defined(_WIN32) && !defined(__PROSPERO__)", 2),
    ("#ifdef _WIN32", "#if defined(_WIN32) || defined(__PROSPERO__)", 1),
)


def disable_ps5_clrx(text):
    """Keep LLVM/IR disassembly; exclude only external CLRX on the real PS5 platform."""
    for before, after, count in PSBC_CLRX_GUARDS:
        if text.count(before) != count:
            raise ValueError("upstream CLRX platform guards changed; review the PS5 recipe")
        text = text.replace(before, after)
    return text


def prepare_psbc_patch(source, psbc, output):
    relative = Path("src/amd/compiler/aco_print_asm.cpp")
    original = (source / "third_party/opengnm-psbc" / relative).read_text()
    patched = disable_ps5_clrx(original)
    target = psbc / relative
    if target.is_symlink() or target.read_text() not in (original, patched):
        raise ValueError("isolated PSBC disassembler differs from the reviewed patch inputs")
    if target.read_text() != patched:
        target.write_text(patched)
    patch = output / "psbc-clrx-ps5.patch"
    patch.write_text("".join(difflib.unified_diff(
        original.splitlines(keepends=True), patched.splitlines(keepends=True),
        fromfile="a/" + relative.as_posix(), tofile="b/" + relative.as_posix())))
    return {"source": str(source / "third_party/opengnm-psbc" / relative),
            "source_sha256": digest(source / "third_party/opengnm-psbc" / relative),
            "isolated_source": str(target), "isolated_sha256": digest(target),
            "patch": str(patch), "patch_sha256": digest(patch),
            "capability": "External CLRX disabled only for __PROSPERO__; existing LLVM/IR "
                          "paths and the existing unsupported-platform failure remain intact"}


def meson_compiler(compiler, arguments):
    """Split configure-only source/link probes; never parse source as FreeBSD."""
    launcher = ROOT / "tools/ps5/native-clang.py"
    if "-c" in arguments and any(Path(arg).suffix in (".S", ".s") for arg in arguments):
        return subprocess.call([compiler, *arguments])
    source = [arg for arg in arguments if Path(arg).suffix in (".c", ".cc", ".cpp", ".cxx", ".C")]
    if any(flag in arguments for flag in ("-c", "-E", "-S", "-fsyntax-only", "-emit-llvm")):
        return subprocess.call([sys.executable, str(launcher), compiler, *arguments])
    if not source:
        if any(arg.startswith(("-Wl,", "-fuse-ld=")) for arg in arguments):
            return subprocess.call([compiler, "--target=x86_64-unknown-freebsd9", *arguments])
        return subprocess.call([sys.executable, str(launcher), compiler, *arguments])
    if len(source) != 1 or "-o" not in arguments:
        raise ValueError("Meson native link probe requires one source and an explicit output")
    output = Path(arguments[arguments.index("-o") + 1])
    obj = output.with_name(output.name + ".ps5-probe.o")
    compile_args, link_args = [], []
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        if arg == "-o":
            index += 2
            continue
        if arg in ("-L", "-l", "-Xlinker"):
            link_args.extend(arguments[index:index + 2])
            index += 2
            continue
        if arg.startswith(("-Wl,", "-fuse-ld=", "-L", "-l")) or arg in (
                "-nostartfiles", "-nodefaultlibs", "-nostdlib", "-shared", "-pie",
                "-static") or Path(arg).suffix in (".so", ".a", ".o"):
            link_args.append(arg)
        else:
            compile_args.append(arg)
            if arg == "-pthread":
                link_args.append(arg)
        index += 1
    try:
        result = subprocess.call([sys.executable, str(launcher), compiler,
                                  *compile_args, "-c", "-o", str(obj)])
        if result:
            return result
        return subprocess.call([compiler, "--target=x86_64-unknown-freebsd9",
                                str(obj), *link_args, "-o", str(output)])
    finally:
        obj.unlink(missing_ok=True)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def snapshot(roots):
    """Hash sources and cached/installed artifacts, excluding Git bookkeeping."""
    result = {}
    for root in roots:
        for directory, dirs, files in os.walk(root):
            dirs[:] = sorted(name for name in dirs if name != ".git")
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink():
                    result[str(path)] = "symlink:" + os.readlink(path)
                elif path.is_file():
                    result[str(path)] = digest(path)
    return result


def meson_arguments(config):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(config)
    arguments = [f"-D{key}={value}" for key, value in parser["options"].items()
                 if key != "b_staticpic"]
    arguments.append("-Db_staticpic=true")
    for kind in ("cross_file", "native_file"):
        for filename in ast.literal_eval(parser["properties"].get(kind, "[]")):
            arguments.extend(["--" + kind.replace("_", "-"), filename])
    return arguments


def include_configuration(sdk, overlay):
    launcher = ROOT / "tools/ps5/native-clang.py"
    meson_launcher = Path(__file__).resolve()
    common = ["-fPIC", "-femulated-tls", "-fno-stack-protector"]
    c_flags, cxx_flags = list(common), list(common)
    headers = {}
    if overlay is not None:
        overlay = overlay.resolve()
        if not (overlay / "stdio.h").is_file():
            raise ValueError("--sdk-include must contain the native opaque stdio.h overlay")
        headers = snapshot([overlay])
        if any(value.startswith("symlink:") for value in headers.values()):
            raise ValueError("SDK overlay headers must be regular files, not symlinks")
        c_flags += ["-I" + str(overlay)]
        cxx_flags += ["-nostdinc++", "-I" + str(sdk / "target/include/c++/v1"),
                      "-I" + str(overlay), "-isystem", str(sdk / "target/include")]
    stdlib = sdk / "target/include/stdlib.h"
    stdlib_text = stdlib.read_text()
    if overlay is not None and (overlay / "stdlib.h").exists():
        stdlib_text += (overlay / "stdlib.h").read_text()
    stdlib_text = re.sub(r"/\*.*?\*/|//[^\n]*", "", stdlib_text, flags=re.S)
    feature_overrides = {}
    if not re.search(r"\bqsort_s\s*\(", stdlib_text):
        feature_overrides["has_function_qsort_s"] = False
    return {"sdk_include": str(overlay) if overlay else None,
            "header_sha256": headers, "c_flags": c_flags, "cxx_flags": cxx_flags,
            "compiler_launcher": {"path": str(launcher), "sha256": digest(launcher),
                                  "source_abi": "actual-ps5-frontend",
                                  "constructor_lowering": "bitcode-to-init-array-backend"},
            "meson_probe_launcher": {"path": str(meson_launcher), "sha256": digest(meson_launcher),
                                    "scope": "native frontend objects; source-free configure links"},
            "c_compiler": [sys.executable, str(launcher), str(sdk / "bin/prospero-clang")],
            "cxx_compiler": [sys.executable, str(launcher), str(sdk / "bin/prospero-clang++")],
            "mesa_c_compiler": [sys.executable, str(meson_launcher), "--meson-compiler",
                               str(sdk / "bin/prospero-clang")],
            "mesa_cpp_compiler": [sys.executable, str(meson_launcher), "--meson-compiler",
                                 str(sdk / "bin/prospero-clang++"),
                                  "-isystem", str(sdk / "target/include")],
            "mesa_feature_overrides": feature_overrides,
            "mesa_feature_evidence": {
                "stdlib_header": str(stdlib), "sha256": digest(stdlib),
                "qsort_s_policy": "Do not select an undeclared C interface by NID alone; "
                                  "use Mesa's existing TLS qsort fallback when necessary"},
            "abi_profile": ("actual-ps5-frontend-opaque-stdio" if overlay
                            else "baseline-stdio-unqualified")}


def mesa_link_configuration():
    manifests = [
        ROOT / "build/ps5-shared-runtime/core-native-abi/manifest.json",
        ROOT / "build/ps5-shared-runtime/system-sdk/libSceLibcInternal/manifest.json",
        ROOT / "build/ps5-shared-runtime/system-sdk/libkernel/manifest.json"]
    providers = []
    for manifest in manifests:
        provider = load_provider(manifest)
        stub = manifest.parent / provider["import_library"]["path"]
        providers.append({"manifest": str(manifest), "manifest_sha256": digest(manifest),
                          "stub": str(stub), "stub_sha256": digest(stub)})
    # FreeBSD compile probes must not search for host crt/gcc or use a second
    # SDK static C/C++ runtime. These executables are link checks, never run.
    return {"providers": providers, "flags": [
        "-fuse-ld=lld",
        "-nostartfiles", "-nodefaultlibs", "-Wl,-e,main",
        "-Wl,--no-undefined", "-Wl,--as-needed", *[p["stub"] for p in providers]],
        "scope": "Meson configure link checks only; never execute or deploy stubs"}


def stage_fingerprint(source, sdk, includes, link_config=None):
    paths = [
        source / "tests/ps5/native-app.mk", source / "toolchain/ps5-opengl-core33.mk",
        source / "toolchain/Makefile.opengnm-psbc-ps5",
        source / "toolchain/opengnm-psbc-ps5.mak",
        source / "build/mesa-ps5-probe/meson-private/cmd_line.txt",
        source / "build/core33-native-runtime/runtime-config.txt",
        sdk / "bin/prospero-clang", sdk / "bin/prospero-clang++",
        ROOT / "tools/ps5/native-clang.py"]
    inputs = {"include_configuration": includes,
              "mesa_link_configuration": link_config,
              "psbc_clrx_platform_recipe": {
                  "source_sha256": digest(source / "third_party/opengnm-psbc/src/amd/compiler/aco_print_asm.cpp"),
                  "replacements": PSBC_CLRX_GUARDS},
              "recipes_and_wrappers": {str(path): digest(path) for path in paths}}
    return {"inputs": inputs, "sha256": hashlib.sha256(
        json.dumps(inputs, sort_keys=True).encode()).hexdigest()}


def mesa_include_arguments(source, includes, link_config=None):
    options = json.loads(
        (source / "build/mesa-ps5-probe/meson-info/intro-buildoptions.json").read_text())
    values = {option["name"]: option["value"] for option in options}
    arguments = [f"-D{kind}={values[kind] + includes[flags]!r}"
                 for kind, flags in (("c_args", "c_flags"), ("cpp_args", "cxx_flags"))]
    if link_config:
        arguments += [f"-D{kind}={values[kind] + link_config['flags']!r}"
                      for kind in ("c_link_args", "cpp_link_args")]
    return arguments


def group_names(script):
    match = re.fullmatch(r"\s*GROUP\s*\((.*?)\)\s*",
                        re.sub(r"/\*.*?\*/", "", script, flags=re.S), re.S)
    if not match:
        raise ValueError("expected the complete upstream GROUP linker script")
    names = match.group(1).split()
    if len(names) != len(set(names)) or any(not n.endswith(".a") for n in names):
        raise ValueError("invalid upstream archive group")
    return names


def compile_commands(text):
    commands = []
    for line in text.replace("\\\n", " ").splitlines():
        words = shlex.split(line)
        if words and "-c" in words and any(
                Path(word).name.startswith("prospero-clang") for word in words[:4]):
            commands.append(words)
    return commands


def require_pic(commands):
    if not commands:
        raise ValueError("no compilation commands found")
    for command in commands:
        if "-fPIC" not in command or any(
                flag in command for flag in ("-fno-pic", "-fno-PIC", "-fno-pie")):
            raise ValueError("non-PIC compilation: " + shlex.join(command))


def nm_symbols(text):
    defined, required, weak = set(), set(), set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and len(fields[-2]) == 1:
            kind, name = fields[-2:]
            if kind == "U":
                required.add(name)
            elif kind in ("w", "v"):
                weak.add(name)
            elif kind.isupper() or kind in ("u", "i"):
                defined.add(name)
    return defined, required, weak


def core_commands(registry):
    commands = set()
    for feature in ET.parse(registry).getroot().findall("feature"):
        if feature.get("api") != "gl" or tuple(
                map(int, feature.get("number").split("."))) > (4, 6):
            continue
        for requirement in feature.findall("require"):
            if requirement.get("profile") in (None, "core"):
                commands.update(item.get("name") for item in requirement.findall("command"))
        for removal in feature.findall("remove"):
            if removal.get("profile") == "core":
                commands.difference_update(item.get("name") for item in removal.findall("command"))
    return commands


def tls_records(lines):
    """Read GNU readelf -SWsr output without retaining debug relocation dumps."""
    native, emulated = {}, {}
    member = None
    for line in lines:
        if line.startswith("File: "):
            member = line.strip()[6:]
            continue
        if member is None:
            continue
        section = re.match(
            r"\s*\[\s*\d+\]\s+(\S+)\s+\S+\s+[0-9a-fA-F]+\s+"
            r"[0-9a-fA-F]+\s+[0-9a-fA-F]+\s+\S+\s+(\S+)", line)
        symbol = re.match(
            r"\s*\d+:\s+[0-9a-fA-F]+\s+\S+\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(\S+)",
            line)
        relocation = re.search(r"\bR_X86_64_(\S+)", line)
        if section and "T" in section[2]:
            native.setdefault(member, set()).add("section:" + section[1])
        if symbol and symbol[1] == "TLS":
            native.setdefault(member, set()).add("symbol:" + symbol[2])
        if relocation and ("TLS" in relocation[1] or relocation[1].startswith(
                ("DTP", "TPOFF", "GOTTPOFF"))):
            native.setdefault(member, set()).add("relocation:R_X86_64_" + relocation[1])
        if symbol and symbol[2] == "__tls_get_addr":
            native.setdefault(member, set()).add("symbol:__tls_get_addr")
        if symbol and symbol[2].startswith("__emutls_"):
            emulated.setdefault(member, set()).add("symbol:" + symbol[2])
        if relocation and "__emutls_" in line:
            emulated.setdefault(member, set()).add("relocation:R_X86_64_" + relocation[1])
    return {"native_tls_members": {key: sorted(value) for key, value in native.items()},
            "emutls_members": {key: sorted(value) for key, value in emulated.items()}}


def inspect_tls(archive, readelf):
    command = [readelf, "-SWsr", str(archive)]
    with subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=dict(os.environ, LC_ALL="C")) as process:
        records = tls_records(process.stdout)
        if process.wait():
            raise RuntimeError("TLS inspection failed: " + shlex.join(command))
    return {"path": str(archive), "sha256": digest(archive), "command": command, **records}


def audit_tls(args, report):
    """Inventory actual objects, not inferred TLS settings from build flags."""
    readelf = shutil.which("readelf")
    if not readelf:
        raise ValueError("GNU readelf is required for the archive TLS audit")
    source, sdk, output = args.source.resolve(), args.sdk.resolve(), args.output.resolve()
    names = report["archive_group"]
    cached = {path.name: path for path in
              (source / "build/mesa-ps5-probe").rglob("*.a") if path.name in names}
    cached["libps5_opengl_core33.a"] = (
        source / "build/core33-native-runtime/libps5_opengl_core33.a")
    cached["libpsbc.ps5.a"] = source / "third_party/opengnm-psbc/libpsbc.ps5.a"
    if set(cached) != set(names):
        raise ValueError("cached baseline archive group is incomplete")
    groups = {
        "prepared_graphics": [Path(item["path"]) for item in report["archives"]],
        "cached_graphics": [cached[name] for name in names],
        "installed_graphics": [args.installed.resolve() / "lib" / name for name in names],
        "sdk_cxx": [path for name in ("libc++.a", "libc++abi.a", "libc++experimental.a",
                                     "libunwind.a")
                    if (path := sdk / "target/lib" / name).exists()]}
    for archive in report["archives"]:
        if digest(Path(archive["path"])) != archive["sha256"]:
            raise ValueError("prepared archive changed since build: " + archive["path"])
    inventory = {name: [inspect_tls(path, readelf) for path in paths]
                 for name, paths in groups.items()}
    probes = {}
    for compiler in ("prospero-clang", "prospero-clang++"):
        command = [str(sdk / "bin" / compiler), "-###", "-fPIC", "-c", "-x",
                   "c++" if compiler.endswith("++") else "c", "-",
                   "-o", str(output / "tls-compiler-probe.o")]
        result = subprocess.run(command, input="", text=True, capture_output=True, check=True)
        probes[compiler] = {
            "command": command, "driver_trace": result.stderr,
            "wrapper_sha256": digest(sdk / "bin" / compiler),
            "effective_emutls": '"-femulated-tls"' in result.stderr}
    native = [item["path"] for items in inventory.values() for item in items
              if item["native_tls_members"]]
    prepared_native = [item["path"] for item in inventory["prepared_graphics"]
                       if item["native_tls_members"]]
    audit = {"method": "GNU readelf sections, symbol types and x86_64 TLS relocations",
             "readelf_version": subprocess.check_output(
                 [readelf, "--version"], text=True).splitlines()[0],
             "archives": inventory, "compiler_probes": probes,
             "native_tls_archives": native, "native_loadable": False,
             "qualification": "Inspection only; emutls provider, lifetime and ABI remain external"}
    path = output / "tls-model.json"
    path.write_text(json.dumps(audit, indent=2) + "\n")
    report["tls_audit"] = str(path)
    report["native_tls_absent"] = not prepared_native
    if prepared_native:
        raise ValueError("unsupported native TLS detected; isolated emutls rebuild required: "
                         + ", ".join(prepared_native))
    if not all(probe["effective_emutls"] for probe in probes.values()):
        raise ValueError("compiler default no longer supplies emulated TLS; explicit rebuild required")


def audit_existing(args):
    output = args.output.resolve()
    allowed = (ROOT / "build/ps5-shared-runtime").resolve()
    if output == allowed or not output.is_relative_to(allowed):
        raise ValueError("output must be below build/ps5-shared-runtime")
    path = output / "build.json"
    report = json.loads(path.read_text())
    if report["stage"] != "relocatable-pic-archive-group":
        raise ValueError("--audit-existing requires a completed archive stage")
    if (report["source"] != str(args.source.resolve()) or
            report["installed_baseline"] != str(args.installed.resolve())):
        raise ValueError("audit baseline paths differ from the completed stage")
    before = json.loads((output / "baseline-before.json").read_text())
    if snapshot([args.source.resolve(), args.installed.resolve()]) != before:
        raise ValueError("protected baseline differs from the original stage")
    try:
        audit_tls(args, report)
    finally:
        unchanged = snapshot([args.source.resolve(), args.installed.resolve()]) == before
        report["tls_audit_baseline_unchanged"] = unchanged
        path.write_text(json.dumps(report, indent=2) + "\n")
        if not unchanged:
            raise RuntimeError("protected baseline changed during TLS inspection")
    print(f"Actual TLS audit (NOT ABI qualification): {output / 'tls-model.json'}")


def materialize(source, destination, ar):
    with source.open("rb") as stream:
        thin = stream.read(8) == b"!<thin>\n"
    if thin:
        subprocess.run([str(ar), "-M"],
                       input=f"CREATE {destination}\nADDLIB {source}\nSAVE\nEND\n",
                       text=True, check=True, cwd=source.parent)
    else:
        shutil.copy2(source, destination)


def run_stage(args):
    source = args.source.resolve()
    installed = args.installed.resolve()
    output = args.output.resolve()
    sdk = args.sdk.resolve()
    includes = include_configuration(sdk, args.sdk_include)
    link_config = mesa_link_configuration()
    fingerprint = stage_fingerprint(source, sdk, includes, link_config)
    if output == ROOT or output in ROOT.parents:
        raise ValueError("output must be an isolated build directory")
    previous = None
    if output.exists() and args.resume:
        previous = json.loads((output / "build.json").read_text())
        if previous["stage"] != "incomplete":
            raise ValueError("only incomplete stages can be resumed")
        if previous["source"] != str(source) or previous["installed_baseline"] != str(installed):
            raise ValueError("resume baseline paths differ")
        if previous.get("build_fingerprint") != fingerprint:
            raise ValueError("resume fingerprint changed (headers, compile flags, recipes or wrappers)")
    elif output.exists():
        raise ValueError("output already exists; choose a fresh --output")
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("source and output must not overlap")
    runtime_config = source / "build/core33-native-runtime/runtime-config.txt"
    config = runtime_config.read_text().splitlines()
    if len(config) != 5:
        raise ValueError("expected five-line upstream runtime configuration")
    names = group_names((source / "toolchain/libPS5OpenGL.ld").read_text())
    baseline_runtime = source / "build/core33-native-runtime/libps5_opengl_core33.a"
    if digest(baseline_runtime) != digest(installed / "lib/libps5_opengl_core33.a"):
        raise ValueError("installed runtime differs from cached runtime configuration")
    output.mkdir(parents=True, exist_ok=bool(previous))
    before = snapshot([source, installed])
    if previous:
        if before != json.loads((output / "baseline-before.json").read_text()):
            raise ValueError("protected baseline changed since the incomplete stage")
    else:
        (output / "baseline-before.json").write_text(json.dumps(before, indent=2) + "\n")
    report = {"stage": "incomplete", "native_loadable": False,
              "source": str(source), "installed_baseline": str(installed),
              "runtime_baseline_config": config, "commands": [],
              "include_configuration": includes, "build_fingerprint": fingerprint,
              "mesa_configure_link_configuration": link_config,
              "archive_group": names,
              "limitations": ["PIC archives only; no shared-module link or startup validation",
                              "Unresolved strong and weak imports are reported, not satisfied",
                              "No AGC placeholder shared libraries are built or copied"]}
    if previous:
        report["commands"] = previous["commands"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    compiler_scratch = output / "compiler-scratch"
    compiler_scratch.mkdir(exist_ok=True)
    env["TMPDIR"] = str(compiler_scratch)
    env.pop("MAKEFLAGS", None)
    env.pop("MFLAGS", None)

    def run(command, cwd=ROOT, capture=False):
        command = list(map(str, command))
        report["commands"].append({"argv": command, "cwd": str(cwd)})
        with (output / "build.log").open("a") as log:
            log.write("\n$ " + shlex.join(command) + "\n")
            log.flush()
            result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                    stdout=subprocess.PIPE if capture else log,
                                    stderr=log, check=True)
            if capture:
                log.write(result.stdout)
                return result.stdout
        return ""

    try:
        mesa = output / "mesa"
        runtime = output / "runtime"
        psbc = output / "psbc/third_party/opengnm-psbc"
        psbc.parent.mkdir(parents=True, exist_ok=True)
        if not psbc.exists():
            shutil.copytree(source / "third_party/opengnm-psbc", psbc,
                            ignore=shutil.ignore_patterns(".git", "*.o", "*.a",
                                                         "*.identity", "__pycache__"))
        for name in ("opengnm", "Vulkan-Headers", "SPIRV-Headers"):
            if not (psbc.parent / name).exists():
                (psbc.parent / name).symlink_to(source / "third_party" / name,
                                              target_is_directory=True)
        platform = output / "psbc/src/platform"
        platform.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / "src/platform/ps5_mesa_shims.c", platform)
        report["psbc_platform_patch"] = prepare_psbc_patch(source, psbc, output)
        mesa_config = source / "build/mesa-ps5-probe/meson-private/cmd_line.txt"
        cpp_cross = output / "native-cpp.ini"
        cpp_cross.write_text(
            "[binaries]\nc = " + repr(includes["mesa_c_compiler"]) + "\n"
            "cpp = " + repr(includes["mesa_cpp_compiler"]) + "\n"
            "[properties]\n" + "".join(
                key + " = " + str(value).lower() + "\n"
                for key, value in includes["mesa_feature_overrides"].items()))
        report["mesa_cpp_cross_file"] = {"path": str(cpp_cross), "sha256": digest(cpp_cross)}
        run(["meson", "setup", *(["--reconfigure"] if (mesa / "build.ninja").exists() else []),
             mesa, source / "third_party/mesa-26.2.0",
             *[arg for arg in meson_arguments(mesa_config.read_text())
               if not arg.startswith(("-Dc_args=", "-Dcpp_args=",
                                      "-Dc_link_args=", "-Dcpp_link_args="))],
             "--cross-file", cpp_cross, *mesa_include_arguments(source, includes, link_config)])
        mesa_commands = json.loads((mesa / "compile_commands.json").read_text())
        target_commands = [shlex.split(item["command"]) for item in mesa_commands
                           if "prospero-clang" in item["command"]
                           and ".a.p/" in item["output"]]
        require_pic(target_commands)
        report["mesa_compile_commands"] = str(mesa / "compile_commands.json")
        report["mesa_compile_commands_sha256"] = digest(mesa / "compile_commands.json")
        report["mesa_build_options"] = json.loads(
            (mesa / "meson-info/intro-buildoptions.json").read_text())
        psbc_make = ["make", "--no-print-directory", "-C", psbc, "-f",
                     source / "toolchain/Makefile.opengnm-psbc-ps5",
                     f"CONFIG={source}/toolchain/opengnm-psbc-ps5.mak",
                     f"PS5_PAYLOAD_SDK={sdk}", f"-j{args.jobs}",
                     "CC=" + shlex.join([*includes["c_compiler"], *includes["c_flags"]]),
                     "CXX=" + shlex.join([*includes["cxx_compiler"], *includes["cxx_flags"]])]
        psbc_commands = compile_commands(run([*psbc_make, "-B", "-n", "libpsbc"], capture=True))
        require_pic(psbc_commands)
        report["psbc_compile_commands"] = psbc_commands
        run([*psbc_make, "libpsbc"])
        make = ["make", "--no-print-directory", "-C", source / "tests/ps5",
                "-f", "native-app.mk", f"-j{args.jobs}",
                f"PS5_PAYLOAD_SDK={sdk}",
                "CC=" + shlex.join([*includes["c_compiler"], *includes["c_flags"]]),
                f"PS5_OPENGL_BUILD={runtime}", f"PS5_OPENGL_MESA_BUILD={mesa}",
                f"PS5_OPENGL_PSBC={psbc}", "PS5_OPENGL_IMPORT_STUBS=",
                "PS5_ASYNC_NATIVE_PREP=" + (
                    "1" if "-DPS5_ASYNC_NATIVE_PREP=1" in config[4].split() else "0"),
                f"PS5_OPENGL_RUNTIME_DEFINES={config[4]}"]
        # Query Make instead of maintaining a second archive/dependency list.
        libraries = [Path(line) for line in run(
            [*make, "print-static-libs"], capture=True).splitlines() if line.startswith("/")]
        if [path.name for path in libraries] != names:
            raise ValueError("Make archive group differs from canonical full API GROUP")
        mesa_libraries = [path for path in libraries if path.is_relative_to(mesa)]
        run(["ninja", f"-j{args.jobs}", "-C", mesa,
             *[path.relative_to(mesa) for path in mesa_libraries]])
        runtime_commands = compile_commands(run([*make, "-B", "-n", "runtime"], capture=True))
        require_pic(runtime_commands)
        if len(runtime_commands) != 6:
            raise ValueError("expected all six real runtime translation units")
        report["runtime_compile_commands"] = runtime_commands
        run([*make, "runtime"])
        actual_config = (runtime / "runtime-config.txt").read_text().splitlines()
        if actual_config[3:] != config[3:]:
            raise ValueError("runtime capability/behavior defines drifted from baseline")
        report["runtime_config"] = actual_config
        libdir = output / "lib"
        libdir.mkdir(exist_ok=True)
        report["archives"] = []
        for library in libraries:
            destination = libdir / library.name
            materialize(library, destination, sdk / "bin/prospero-ar")
            report["archives"].append({"path": str(destination),
                                       "sha256": digest(destination),
                                       "built_archive": str(library)})
        shutil.copy2(source / "toolchain/libPS5OpenGL.ld", libdir)
        symbols = run([sdk / "bin/llvm-nm", "-g", *[libdir / n for n in names]],
                      capture=True)
        defined, required, weak = nm_symbols(symbols)
        commands = core_commands(
            source / "third_party/mesa-26.2.0/src/mesa/glapi/glapi/registry/gl.xml")
        missing = sorted(commands - defined)
        if len(commands) != 657 or missing:
            raise ValueError(f"incomplete GL4.6 surface: count={len(commands)}, missing={missing}")
        report["gl46_core_surface"] = {"required": len(commands), "defined": len(commands),
                                       "missing": missing}
        imports = {"strong": sorted(required - defined),
                   "weak": sorted(weak - defined),
                   "method": "whole archive group symbol union; not a final link"}
        (output / "imports.json").write_text(json.dumps(imports, indent=2) + "\n")
        report["imports"] = str(output / "imports.json")
        report["defined_gl_symbols"] = len([s for s in defined if re.match(r"gl[A-Z]", s)])
        report["defined_egl_symbols"] = sorted(s for s in defined if s.startswith("egl"))
        generated = (runtime / "gl46_entrypoints.c").read_text()
        report["entrypoint_generator_output"] = str(runtime / "gl46_entrypoints.c")
        report["entrypoint_generator_sha256"] = hashlib.sha256(generated.encode()).hexdigest()
        report["upstream_link_dependencies"] = run(
            [*make, "--eval=shared-graphics-deps:;@echo $(PS5_OPENGL_LDLIBS)",
             "shared-graphics-deps"], capture=True).strip()
        audit_tls(args, report)
        report["stage"] = "relocatable-pic-archive-group"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        after = snapshot([source, installed])
        changed = sorted(key for key in before.keys() | after.keys()
                         if before.get(key) != after.get(key))
        report["baseline_preservation"] = {
            "unchanged": not changed, "hashed_files": len(before),
            "changed_paths": changed, "before": str(output / "baseline-before.json")}
        report["sdk_include_unchanged"] = (
            includes == include_configuration(sdk, args.sdk_include))
        if not report["sdk_include_unchanged"]:
            report["stage"] = "incomplete"
        (output / "build.json").write_text(json.dumps(report, indent=2) + "\n")
        if changed:
            raise RuntimeError("protected baseline changed: " + ", ".join(changed))
        if not report["sdk_include_unchanged"]:
            raise RuntimeError("native stdio overlay changed during the build")
    print(f"PIC archive stage (NOT native-loadable): {output / 'build.json'}")


def rebuild_psbc_stage(args):
    """Rebuild the changed PSBC member; retain byte-proven native Mesa/runtime archives."""
    previous = args.rebuild_psbc_from.resolve()
    old_path = previous / "build.json"
    old = json.loads(old_path.read_text())
    source, installed, sdk, output = (
        args.source.resolve(), args.installed.resolve(), args.sdk.resolve(), args.output.resolve())
    allowed = ROOT / "build/ps5-shared-runtime"
    if output.exists() or output == allowed or not output.is_relative_to(allowed):
        raise ValueError("choose a fresh output below build/ps5-shared-runtime")
    if (old.get("stage") != "relocatable-pic-archive-group"
            or old["source"] != str(source) or old["installed_baseline"] != str(installed)
            or old["include_configuration"].get("abi_profile") != "actual-ps5-frontend-opaque-stdio"
            or not old["baseline_preservation"]["unchanged"] or not old["sdk_include_unchanged"]):
        raise ValueError("PSBC-only rebuild requires a proven complete actual native frontend stage")
    includes = include_configuration(sdk, args.sdk_include)
    comparable = lambda config: {k: v for k, v in config.items() if k != "meson_probe_launcher"}
    if comparable(includes) != comparable(old["include_configuration"]):
        raise ValueError("source ABI/compiler/header profile changed; rebuild the entire group")
    before = snapshot([source, installed])
    if before != json.loads(Path(old["baseline_preservation"]["before"]).read_text()):
        raise ValueError("upstream/cached/installed baseline changed since the reused stage")
    protected = {str(old_path): digest(old_path)}
    names = group_names((source / "toolchain/libPS5OpenGL.ld").read_text())
    if [Path(item["path"]).name for item in old["archives"]] != names:
        raise ValueError("reused stage does not contain the exact complete driver group")
    for item in old["archives"]:
        path = Path(item["path"])
        if digest(path) != item["sha256"]:
            raise ValueError("reused native archive changed: " + str(path))
        protected[str(path)] = item["sha256"]
    output.mkdir(parents=True)
    (output / "baseline-before.json").write_text(json.dumps(before, indent=2) + "\n")
    report = copy.deepcopy(old)
    report.update(stage="incomplete", commands=[], archives=[],
                  reused_graphics_stage={"path": str(previous), "sha256": digest(old_path),
                                         "scope": "18 unchanged archives and native PSBC object cache"})
    report["build_fingerprint"] = stage_fingerprint(
        source, sdk, old["include_configuration"], old["mesa_configure_link_configuration"])
    report["build_fingerprint"]["inputs"]["reused_graphics_stage"] = report["reused_graphics_stage"]
    report["build_fingerprint"]["inputs"]["psbc_recipe_builder_sha256"] = digest(Path(__file__))
    report["build_fingerprint"]["sha256"] = hashlib.sha256(json.dumps(
        report["build_fingerprint"]["inputs"], sort_keys=True).encode()).hexdigest()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(output / "compiler-scratch"))
    Path(env["TMPDIR"]).mkdir()
    env.pop("MAKEFLAGS", None)
    env.pop("MFLAGS", None)

    def run(command, capture=False):
        command = list(map(str, command))
        with (output / "build.log").open("a") as log:
            log.write("\n$ " + shlex.join(command) + "\n")
            log.flush()
            result = subprocess.run(command, env=env, text=True, check=True,
                                    stdout=subprocess.PIPE if capture else log, stderr=log)
            if capture:
                log.write(result.stdout)
        report["commands"].append({"argv": command, "cwd": str(ROOT), "exit_code": 0})
        return result.stdout if capture else ""

    try:
        shutil.copytree(previous / "psbc", output / "psbc", symlinks=True)
        psbc = output / "psbc/third_party/opengnm-psbc"
        report["psbc_platform_patch"] = prepare_psbc_patch(source, psbc, output)
        command = ["make", "--no-print-directory", "-C", psbc, "-f",
                   source / "toolchain/Makefile.opengnm-psbc-ps5",
                   f"CONFIG={source}/toolchain/opengnm-psbc-ps5.mak",
                   f"PS5_PAYLOAD_SDK={sdk}", f"-j{args.jobs}",
                   "CC=" + shlex.join([*includes["c_compiler"], *includes["c_flags"]]),
                   "CXX=" + shlex.join([*includes["cxx_compiler"], *includes["cxx_flags"]])]
        commands = compile_commands(run([*command, "-B", "-n", "libpsbc"], capture=True))
        require_pic(commands)
        report["psbc_compile_commands"] = commands
        run([*command, "libpsbc"])
        libdir = output / "lib"
        libdir.mkdir()
        for item in old["archives"]:
            name = Path(item["path"]).name
            destination = libdir / name
            if name == "libpsbc.ps5.a":
                materialize(psbc / name, destination, sdk / "bin/prospero-ar")
            else:
                shutil.copy2(item["path"], destination)
            report["archives"].append({"path": str(destination), "sha256": digest(destination),
                                       "reused_from": item["path"] if name != "libpsbc.ps5.a" else None})
        shutil.copy2(source / "toolchain/libPS5OpenGL.ld", libdir)
        symbols = run([sdk / "bin/llvm-nm", "-g",
                       *[item["path"] for item in report["archives"]]], capture=True)
        defined, required, weak = nm_symbols(symbols)
        if len(core_commands(source / "third_party/mesa-26.2.0/src/mesa/glapi/glapi/registry/gl.xml")
               & defined) != 657:
            raise ValueError("PSBC rebuild changed the complete GL4.6 surface")
        imports = {"strong": sorted(required - defined), "weak": sorted(weak - defined),
                   "method": "whole archive group union; not final link"}
        psbc_imports = run([sdk / "bin/llvm-nm", "-A", "--undefined-only",
                            libdir / "libpsbc.ps5.a"], capture=True)
        external = [line for line in psbc_imports.splitlines()
                    if line.split() and line.split()[-1] in ("popen", "pclose", "system")]
        if any(":aco_print_asm.ps5.cpp.o:" in line for line in external):
            raise ValueError("external CLRX subprocess imports remain in the patched member")
        report["psbc_platform_patch"]["other_archive_external_references"] = external
        (output / "imports.json").write_text(json.dumps(imports, indent=2) + "\n")
        report["imports"] = str(output / "imports.json")
        audit_tls(args, report)
        report["stage"] = "relocatable-pic-archive-group"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        after = snapshot([source, installed])
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        changed += [k for k, value in protected.items() if digest(Path(k)) != value]
        report["baseline_preservation"] = {
            "unchanged": not changed, "hashed_files": len(before),
            "changed_paths": changed, "before": str(output / "baseline-before.json")}
        report["sdk_include_unchanged"] = includes == include_configuration(sdk, args.sdk_include)
        if changed or not report["sdk_include_unchanged"]:
            report["stage"] = "incomplete"
        (output / "build.json").write_text(json.dumps(report, indent=2) + "\n")
        if changed or not report["sdk_include_unchanged"]:
            raise RuntimeError("protected baseline/producer/header inputs changed during PSBC rebuild")
    print(f"Rebuilt PS5 CLRX-disabled PIC group: {output / 'build.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=ROOT / "build/ps5-external/ps5-opengl")
    parser.add_argument("--installed", type=Path, default=Path("/opt/ps5-opengl-gl46"))
    parser.add_argument("--sdk", type=Path, default=Path("/opt/ps5-payload-sdk"))
    parser.add_argument("--output", type=Path,
                        default=ROOT / "build/ps5-shared-runtime/graphics")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--sdk-include", type=Path,
                        help="native ABI header overlay; C++ wrappers precede this include path")
    parser.add_argument("--resume", action="store_true",
                        help="resume an incomplete stage only if the protected baseline is identical")
    parser.add_argument("--rebuild-psbc-from", type=Path,
                        help="reuse a proven native group and rebuild its isolated PSBC platform feature")
    parser.add_argument("--audit-existing", action="store_true",
                        help="audit actual TLS in an existing completed stage and baselines")
    args = parser.parse_args()
    if not 1 <= args.jobs <= 32:
        parser.error("--jobs must be between 1 and 32")
    try:
        if args.audit_existing:
            audit_existing(args)
        elif args.rebuild_psbc_from:
            rebuild_psbc_stage(args)
        else:
            run_stage(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"graphics preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--meson-compiler":
        sys.exit(meson_compiler(sys.argv[2], sys.argv[3:]))
    sys.exit(main())
