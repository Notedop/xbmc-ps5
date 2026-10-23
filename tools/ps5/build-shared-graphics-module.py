#!/usr/bin/env python3
"""Strict link and optional native conversion of the complete staged GL/EGL group.

Consumes a full PIC/native-stdio archive stage and declared provider SDKs.
No static libc/C++/unwinder, duplicate emutls or substitute providers are linked.
Missing imports and unsupported startup block conversion. The common core
contract supplies shared heap/CWD/thread/exit behavior. Firmware-analysis files
are not inputs, and successful local conversion is not console/GPU acceptance.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET

from native_c_link import Elf, module_identity
from native_shared_link import convert, load_provider, select_providers
from native_runtime_sdk import generate as generate_runtime_sdk
from prepare_runtime_archive import prepare_archive


ROOT = Path(__file__).resolve().parents[2]
PS5_API = {
    "ps5_opengl_memory_image_create", "ps5_opengl_memory_image_destroy",
    "ps5_opengl_video_out_handle", "ps5_opengl_set_scanout_format",
}
ALLOCATOR_WRAPS = {
    "malloc", "calloc", "realloc", "free", "posix_memalign",
    "malloc_usable_size", "aligned_alloc", "memalign", "reallocf",
}


def optional_weak_nulls(raw, providers):
    declared = {symbol["name"] for provider in providers for symbol in provider["symbols"]}
    result = []
    for name, symbol in Elf(raw).symbols(".dynsym"):
        if not symbol[3] and symbol[1] >> 4 == 2 and name not in declared:
            if (symbol[1] & 15 not in (0, 1, 2) or symbol[4]
                    or name in ("__cxa_thread_atexit", "__cxa_thread_atexit_impl")):
                raise ValueError("unsupported optional weak import: " + name)
            result.append({"name": name, "type": symbol[1] & 15,
                           "resolution": "ELF undefined weak S=0; linker absolute zero, no body"})
    return result


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def wrapper_profile(core_names, link_contract=None):
    available = {name[len("__wrap_"):] for name in core_names if name.startswith("__wrap_")}
    if link_contract is None:
        requested = available | ALLOCATOR_WRAPS
        required = ALLOCATOR_WRAPS
    else:
        stdio = link_contract.get("stdio")
        if (link_contract.get("compiler_abi") != "ps5-sdk-native-frontend-v1"
                or link_contract.get("process_lifetime") is not True
                or not isinstance(stdio, dict)
                or stdio.get("opaque_file") is not True
                or stdio.get("fpos_size") != 24
                or stdio.get("fpos_alignment") != 8):
            raise ValueError("core link contract has incompatible source ABI/lifetime/stdio")
        wraps = link_contract.get("wrap_symbols")
        if (not isinstance(wraps, list)
                or any(not isinstance(name, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", name) for name in wraps)
                or len(set(wraps)) != len(wraps)
                or not (ALLOCATOR_WRAPS | {"chdir", "fopen"}) <= set(wraps)):
            raise ValueError("core link contract must declare all allocator and CWD wraps")
        requested = required = set(wraps)
    missing = sorted(required - available)
    return {"wraps": sorted(requested),
            "missing": ["__wrap_" + name for name in missing],
            "qualified": not missing}


def subprocess_evidence(source):
    psbc = source / "third_party/opengnm-psbc"
    paths = [psbc / "src/amd/compiler/aco_print_asm.cpp",
             psbc / "src/amd/compiler/aco_interface.cpp",
             psbc / "src/amd/vulkan/radv_shader.c"]
    evidence = []
    patterns = ("popen(", "pclose(", "clrxdisasm", "options->record_asm",
                "aco_info->record_asm =", "getenv(\"PSBC_DEBUG_DISASM\")")
    for path in paths:
        checksum = sha256(path)
        for line, text in enumerate(path.read_text().splitlines(), 1):
            if any(pattern in text for pattern in patterns):
                evidence.append({"path": str(path), "sha256": checksum,
                                 "line": line, "text": text.strip()})
    return {
        "archive": "libpsbc.ps5.a", "member": "aco_print_asm.ps5.cpp.o",
        "function": "aco::print_asm -> print_asm_clrx",
        "evidence": evidence,
        "semantics": "Optional shader disassembly invokes the external clrxdisasm process; "
                     "popen captures its output and pclose waits/reaps it.",
        "selection": "record_asm is keep_executable_info OR can_dump_shader. "
                     "Disabling runtime recording does not remove the compiled strong references.",
        "compile_guards": "CLRX code is excluded only by _WIN32 in these actual sources; "
                          "AMD_LLVM_AVAILABLE controls the alternate LLVM path, not CLRX removal.",
        "resolution": "Tracked __PROSPERO__ guards in the isolated PSBC source exclude only "
                      "external CLRX. Existing LLVM/IR paths remain. No Windows spoof, "
                      "popen/pclose replacement or subprocess emulation is provided."}


def core_surface(registry):
    names = set()
    for feature in ET.parse(registry).getroot().findall("feature"):
        if feature.get("api") != "gl" or tuple(map(int, feature.get("number").split("."))) > (4, 6):
            continue
        for entry in feature.findall("require"):
            if entry.get("profile") in (None, "core"):
                names.update(command.get("name") for command in entry.findall("command"))
        for entry in feature.findall("remove"):
            if entry.get("profile") == "core":
                names.difference_update(command.get("name") for command in entry.findall("command"))
    return names


def header_names(text):
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    return set(re.findall(r"\b((?:gl|egl)[A-Z][A-Za-z0-9_]*)\s*\(", text))


def nm_inventory(text):
    defined, functions, strong, weak = set(), set(), set(), set()
    for line in text.splitlines():
        words = line.split()
        if len(words) < 2 or len(words[-2]) != 1:
            continue
        kind, name = words[-2:]
        if kind == "U":
            strong.add(name)
        elif kind in ("w", "v"):
            weak.add(name)
        elif kind.isupper() or kind in ("u", "i"):
            defined.add(name)
            if kind in ("T", "W", "I"):
                functions.add(name)
    return defined, functions, strong - defined, weak - defined


def safe_archive_names(raw):
    """Flatten member names in-place without changing offsets or ELF bytes."""
    if raw[:8] != b"!<arch>\n":
        raise ValueError("expected materialized ordinary GNU archive")
    result = bytearray(raw)
    offset, strings, members, renames = 8, None, set(), {}
    while offset < len(raw):
        header = raw[offset:offset + 60]
        if len(header) != 60 or header[58:] != b"`\n":
            raise ValueError("invalid GNU archive header")
        size = int(header[48:58].decode("ascii").strip())
        start = offset + 60
        if size < 0 or start + size > len(raw):
            raise ValueError("truncated GNU archive")
        token = header[:16].decode("ascii").rstrip()
        if token == "//":
            strings = (start, raw[start:start + size])
        elif token not in ("/", "/SYM64/"):
            if token.startswith("/") and token[1:].isdigit():
                index = int(token[1:])
                if strings is None or index >= len(strings[1]):
                    raise ValueError("invalid long-name archive offset")
                end = strings[1].find(b"/\n", index)
                if end < index:
                    raise ValueError("unterminated long archive member name")
                original = strings[1][index:end].decode("ascii")
                name_start = strings[0] + index
            elif token.endswith("/"):
                original = token[:-1]
                name_start = offset
            else:
                raise ValueError("unsupported archive name encoding")
            safe = original.replace("/", "_")
            if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", safe)
                    or safe in members):
                raise ValueError("unsafe or colliding normalized archive name: " + original)
            members.add(safe)
            if safe != original:
                result[name_start:name_start + len(original)] = safe.encode("ascii")
                renames[original] = safe
        offset = start + size + size % 2
    if offset != len(raw):
        raise ValueError("invalid archive padding")
    return bytes(result), renames


def public_surface(source, defined, functions):
    include = source / "third_party/mesa-26.2.0/include"
    headers = sorted([*include.glob("GL/*.h"), *include.glob("EGL/*.h")])
    declared = set()
    for header in headers:
        declared.update(header_names(header.read_text()))
    core = core_surface(source / "third_party/mesa-26.2.0/src/mesa/glapi/glapi/registry/gl.xml")
    gl = {name for name in declared & functions if name.startswith("gl")}
    egl = {name for name in declared & functions if name.startswith("egl")}
    if len(core) != 657 or core - gl or len(egl) != 34:
        raise ValueError(f"incomplete public surface: core={len(core)}, "
                         f"missing={sorted(core - gl)}, EGL={len(egl)}")
    # The integration patch supplies source-level declarations, not an installed
    # header. Keep that ABI-documentation gap explicit rather than inventing one.
    integration_sources = [
        source / "src/egl/ps5_egl.c", source / "src/platform/ps5_agc_native_runtime.c"]
    integration_text = "\n".join(path.read_text() for path in integration_sources)
    if PS5_API - defined or any(
            not re.search(r"\b" + name + r"\s*\([^;{}]*\)\s*;", integration_text, re.S)
            for name in PS5_API):
        raise ValueError("missing actual PS5 integration API definitions/declarations")
    return {
        "gl46_core": sorted(core), "gl": sorted(gl), "egl": sorted(egl),
        "ps5": sorted(PS5_API), "exports": sorted(gl | egl | PS5_API),
        "header_inputs": [str(path) for path in headers],
        "ps5_contract_sources": [str(path) for path in integration_sources],
        "ps5_header_gap": "Upstream installed headers do not declare the four Kodi "
                          "integration APIs; retained from actual patched source declarations. "
                          "The weak ps5_opengl_register_gl_consumer profiling hook is private.",
    }


def require_native_stdio(stage_report, overlay):
    """Do not promote an old FILE-layout build merely by satisfying its imports."""
    overlay = overlay.resolve()
    config = stage_report.get("include_configuration", {})
    if (config.get("abi_profile") != "actual-ps5-frontend-opaque-stdio"
            or config.get("sdk_include") != str(overlay)
            or stage_report.get("sdk_include_unchanged") is not True):
        raise ValueError("graphics archives require a complete native stdio overlay rebuild")
    headers = {str(path): sha256(path) for path in sorted(overlay.rglob("*")) if path.is_file()}
    if not (overlay / "stdio.h").is_file() or config.get("header_sha256") != headers:
        raise ValueError("native stdio overlay fingerprint changed; rebuild all graphics archives")
    launcher = ROOT / "tools/ps5/native-clang.py"
    if config.get("compiler_launcher") != {
            "path": str(launcher), "sha256": sha256(launcher),
            "source_abi": "actual-ps5-frontend",
            "constructor_lowering": "bitcode-to-init-array-backend"}:
        raise ValueError("native compiler launcher fingerprint changed; rebuild graphics archives")
    probe_launcher = ROOT / "tools/ps5/build-shared-graphics.py"
    probe = config.get("meson_probe_launcher", {})
    if (probe.get("path") != str(probe_launcher)
            or probe.get("scope") != "native frontend objects; source-free configure links"
            or re.fullmatch(r"[0-9a-f]{64}", probe.get("sha256", "")) is None
            or stage_report["build_fingerprint"]["inputs"]["include_configuration"] != config):
        raise ValueError("Meson native probe launcher fingerprint changed; rebuild graphics archives")
    commands = [*stage_report["runtime_compile_commands"], *stage_report["psbc_compile_commands"]]
    mesa = json.loads(Path(stage_report["mesa_compile_commands"]).read_text())
    commands += [shlex.split(item["command"]) for item in mesa
                 if "prospero-clang" in item["command"] and ".a.p/" in item["output"]]
    for command in commands:
        if (not any(str(path) in command for path in (launcher, probe_launcher))
                or not all(flag in command for flag in (
                "-fPIC", "-fno-stack-protector",
                "-femulated-tls", "-I" + str(overlay)))):
            raise ValueError("graphics compilation lacks native ABI/PIC/TLS/init-array flags")
        if any(flag == "-fuse-init-array" or flag == "-fshort-wchar"
               or flag.startswith(("--target=", "-target", "-D__PROSPERO__", "-D__SCE__",
                                    "-D__STDC_NO_", "-U__FLOAT128__", "-U__SIZEOF_FLOAT128__",
                                    "-U__STDC_MB_MIGHT_NEQ_WC__"))
               for flag in command):
            raise ValueError("source ABI overrides cannot substitute for the actual PS5 frontend")
        if any(Path(flag).name == "prospero-clang++" for flag in command[:4]):
            cxx = config["cxx_flags"]
            wrapper = next(flag for flag in cxx if flag.endswith("/include/c++/v1"))
            if ("-nostdinc++" not in command or
                    command.index(wrapper) >= command.index("-I" + str(overlay))):
                raise ValueError("libc++ wrappers must precede the native stdio overlay")
            system_include = config["mesa_cpp_compiler"][-1]
            if not any(command[index:index + 2] == ["-isystem", system_include]
                       for index in range(len(command) - 1)):
                raise ValueError("SDK C headers disappeared from the C++ compiler invocation")
    return {"overlay": str(overlay), "header_sha256": headers,
            "compile_commands_verified": len(commands),
            "build_fingerprint": stage_report["build_fingerprint"]["sha256"]}


def verify_exports(raw, surface):
    image = Elf(raw)
    symbols = image.symbols(".dynsym")
    exports = {name for name, symbol in symbols if symbol[3] and name
               and symbol[1] >> 4 in (1, 2) and symbol[2] & 3 in (0, 3)}
    wanted = set(surface["exports"])
    if exports != wanted:
        raise ValueError(f"dynamic public API mismatch: missing={sorted(wanted - exports)}, "
                         f"unexpected={sorted(exports - wanted)}")
    if any(section[2] & 0x400 for section in image.section_headers):
        raise ValueError("native TLS remains unsupported")
    undefined = [{"name": name, "binding": "weak" if symbol[1] >> 4 == 2 else "global",
                  "type": symbol[1] & 15}
                 for name, symbol in symbols if name and not symbol[3]]
    return {"exports": len(exports), "gl46_core": len(set(surface["gl46_core"]) & exports),
            "egl": len(set(surface["egl"]) & exports), "ps5": sorted(set(surface["ps5"]) & exports),
            "undefined": undefined}


def build(args):
    module_identity(args.module)
    output = args.output.resolve()
    if output == ROOT or output in ROOT.parents or output.exists() or output.is_symlink():
        raise ValueError("choose a fresh isolated output directory")
    stage, source, sdk = args.graphics.resolve(), args.source.resolve(), args.sdk.resolve()
    stage_report = json.loads((stage / "build.json").read_text())
    if stage_report["stage"] != "relocatable-pic-archive-group":
        raise ValueError("a completed real PIC graphics archive stage is required")
    stdio_proof = require_native_stdio(stage_report, args.sdk_include)
    patch = stage_report.get("psbc_platform_patch")
    if (not isinstance(patch, dict) or sha256(patch["isolated_source"]) != patch["isolated_sha256"]
            or sha256(patch["patch"]) != patch["patch_sha256"]
            or sha256(patch["source"]) != patch["source_sha256"]):
        raise ValueError("graphics group requires the verified PS5-only CLRX exclusion rebuild")
    archives = [Path(item["path"]) for item in stage_report["archives"]]
    if len(archives) != 19 or any(sha256(item["path"]) != item["sha256"]
                                  for item in stage_report["archives"]):
        raise ValueError("full 19-archive stage missing or changed")
    output.mkdir(parents=True)
    report = {"schema": 1, "status": "preparing", "native_accepted": False,
              "runtime_execution_tested": False, "output": str(output),
              "commands": [], "providers": [], "provider_blockers": [],
              "startup_blockers": [], "source_archives": stage_report["archives"],
              "catalogue_provenance": [],
              "native_stdio_rebuild": stdio_proof,
              "psbc_platform_patch": patch,
              "platform_capabilities": {"external_clrx": False,
                                        "scope": "PS5-only isolated source guards; LLVM/IR retained"},
              "qualification_gaps": ["Diagnostic link only; no native loader or GPU execution",
                                     "Process lifetime only; arbitrary addon TLS unload rejected"]}
    protected = {str(path): sha256(path) for path in archives}
    prepared_dir = output / "archives"
    prepared_dir.mkdir()

    def run(phase, command):
        command = list(map(str, command))
        result = subprocess.run(command, capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        (output / (phase + ".stdout")).write_text(result.stdout)
        (output / (phase + ".stderr")).write_text(result.stderr)
        report["commands"].append({"phase": phase, "argv": command,
                                    "exit_code": result.returncode})
        return result

    try:
        inventory = run("archive-symbols", [sdk / "bin/llvm-nm", "-g", *archives])
        if inventory.returncode:
            raise ValueError("cannot inspect the full graphics archive group")
        defined, functions, strong, weak = nm_inventory(inventory.stdout)
        layout_helpers = sorted(strong & {"__srget", "__swbuf", "__srefill",
                                          "__swsetup", "__sflush", "__sF"})
        report["stdio_layout_helpers"] = layout_helpers
        if layout_helpers:
            raise ValueError("legacy FILE-layout helper imports remain: " + ", ".join(layout_helpers))
        surface = public_surface(source, defined, functions)
        for name in surface["header_inputs"] + surface["ps5_contract_sources"]:
            protected[name] = sha256(name)
        registry = source / "third_party/mesa-26.2.0/src/mesa/glapi/glapi/registry/gl.xml"
        protected[str(registry)] = sha256(registry)
        report["public_api"] = surface
        report["qualification_gaps"].append(surface["ps5_header_gap"])
        export_map = output / "exports.map"
        export_map.write_text("{\n  global:\n" + "".join(
            f"    {name};\n" for name in surface["exports"]) + "  local: *;\n};\n")
        (output / "archive-imports.json").write_text(json.dumps(
            {"strong": sorted(strong), "weak": sorted(weak),
             "scope": "whole-group union; final reachability determined by strict link"}, indent=2) + "\n")
        report["optional_subprocess_feature"] = subprocess_evidence(source)
        (output / "optional-subprocess-feature.json").write_text(
            json.dumps(report["optional_subprocess_feature"], indent=2) + "\n")
        prepared = []
        report["archive_preparation"] = []
        private_sources = output / "private-source-archives"
        private_sources.mkdir()
        for archive in archives:
            destination = prepared_dir / archive.name
            normalized, renames = safe_archive_names(archive.read_bytes())
            private_source = private_sources / archive.name
            private_source.write_bytes(normalized)
            try:
                item = prepare_archive(private_source, destination, args.ar, args.objcopy)
            except ValueError as error:
                # Diagnostic linking may retain complex legacy startup unchanged,
                # but it must never be converted or called a qualified module.
                if not any(reason in str(error) for reason in (
                        "multiple legacy constructor",
                        "expected standard numeric .ctors.N, got .ctors")):
                    raise
                shutil.copy2(private_source, destination)
                item = {"source": str(archive), "output": str(destination),
                        "mode": "unqualified-legacy-startup-retained", "error": str(error)}
                report["startup_blockers"].append(item)
            item["original_archive"] = str(archive)
            item["member_name_normalization"] = renames
            prepared.append(destination)
            report["archive_preparation"].append(item)
        manifests = [args.core.resolve(), *[path.resolve() for path in args.provider]]
        system = ROOT / "build/ps5-shared-runtime/system-sdk"
        for module in ("libSceLibcInternal", "libkernel"):
            manifest = system / module / "manifest.json"
            if module not in {load_provider(path)["module"] for path in manifests}:
                manifests.append(manifest)
        supplied_modules = [load_provider(manifest)["module"] for manifest in manifests]
        if len(set(supplied_modules)) != len(supplied_modules):
            raise ValueError("duplicate native provider module identities")
        missing_system = {"libSceAgc", "libSceAgcDriver", "libSceVideoOut",
                          "libSceSystemService"} - set(supplied_modules)
        if missing_system:
            raise ValueError("missing native system SDK contracts: " +
                             ", ".join(sorted(missing_system)) +
                             "; supply --provider manifests (firmware is not a build input)")
        providers, stubs = [], []
        for manifest in manifests:
            provider = load_provider(manifest)
            stub = manifest.parent / provider["import_library"]["path"]
            protected[str(manifest)] = sha256(manifest)
            protected[str(stub)] = sha256(stub)
            providers.append(provider)
            stubs.append(stub)
            report["providers"].append({"manifest": str(manifest), "module": provider["module"],
                                        "stub": str(stub), "stub_linker_only": True,
                                        "module_version": provider["provider"]["module_version"],
                                        "library_version": provider["provider"]["library_version"]})
        core_names = {symbol["name"] for symbol in providers[0]["symbols"]}
        contract = json.loads(args.core.resolve().read_text()).get("link_contract")
        if not isinstance(contract, dict):
            raise ValueError("core SDK must declare the portable native link_contract")
        heap = wrapper_profile(core_names, contract)
        wraps = heap["wraps"]
        report["core_link_contract"] = contract
        report["core_link_contract_qualified"] = heap["qualified"]
        report["core_wrappers"] = wraps
        report["missing_core_common_wrappers"] = heap["missing"]
        report["missing_core_allocator_wrappers"] = [
            name for name in heap["missing"] if name[len("__wrap_"):] in ALLOCATOR_WRAPS]
        report["shared_allocator_available"] = not report["missing_core_allocator_wrappers"]
        report["shared_heap_initialize_exported"] = "ps5_shared_heap_initialize" in core_names
        if not report["core_link_contract_qualified"]:
            report["qualification_gaps"].append(
                "Explicitly unqualified diagnostic: declared common core wrappers missing: "
                + ", ".join(heap["missing"])
                + ". All declared --wrap flags remain enabled to prevent core-state bypass; "
                  "parent must regenerate the core SDK and rerun before qualification.")
        crt = ROOT / "tools/ps5/native_module_crt.cpp"
        script = ROOT / "tools/ps5/native-shared.ld"
        protected[str(crt)], protected[str(script)] = sha256(crt), sha256(script)
        obj = output / "native-module-crt.o"
        report["crt_compile_flags"] = stage_report["include_configuration"]["cxx_flags"]
        compile_result = run("compile-crt", [
            sys.executable, ROOT / "tools/ps5/native-clang.py",
            sdk / "bin/prospero-clang++", *report["crt_compile_flags"], "-std=c++20",
            "-O2", "-fno-stack-protector",
            "-c", crt, "-o", obj])
        if compile_result.returncode:
            report["status"] = "crt-compile-blocked"
            return 1
        linked = output / "diagnostic-linked.so"
        report["section_gc"] = not report["startup_blockers"]
        command = [
            args.lld, "-shared", "--hash-style=sysv", "--eh-frame-hdr",
            "-z", "now", "-z", "max-page-size=0x4000", "--no-undefined", "--error-limit=0",
            "--soname=" + args.module + ".sprx", "-init", "module_start", "-fini", "module_stop",
            "--undefined=__ps5_native_lifecycle_v1",
            "-T", script, "--version-script=" + str(export_map), "-o", linked, obj,
            *(["--gc-sections"] if report["section_gc"] else []),
            *["--undefined=" + name for name in surface["exports"]],
            *["--wrap=" + name for name in wraps],
            "--start-group", *prepared, "--end-group", "--as-needed", *stubs]
        result = run("strict-link", command)
        report["strict_link"] = {
            "exit_code": result.returncode,
            "unresolved_diagnostics": sorted(set(re.findall(
                r"undefined symbol: ([^\n]+)", result.stderr))),
            "weak_candidates": sorted(weak),
            "static_c_cxx_unwind_emutls_added": False}
        if result.returncode:
            report["status"] = "strict-link-blocked"
            return 1
        nulls = optional_weak_nulls(linked.read_bytes(), providers)
        report["weak_null_resolution"] = nulls
        if nulls:
            # A relaxed GOT load of ABS zero can become an image-relative LEA.
            command.append("--no-relax")
            report["weak_null_linker_relaxation"] = False
            command.extend("--defsym=" + item["name"] + "=0" for item in nulls)
            result = run("strict-link-weak-null", command)
            if result.returncode:
                report["status"] = "weak-null-link-blocked"
                report["strict_link"]["exit_code"] = result.returncode
                return 1
            normalized = Elf(linked.read_bytes())
            local = dict(normalized.symbols(".symtab"))
            dynamic = dict(normalized.symbols(".dynsym"))
            for item in nulls:
                symbol = local.get(item["name"])
                if symbol is None or symbol[3] != 0xFFF1 or symbol[4] or item["name"] in dynamic:
                    raise ValueError("optional weak did not resolve to private absolute zero: " + item["name"])
        report["linked_elf"] = {"path": str(linked), "sha256": sha256(linked)}
        report["elf_validation"] = verify_exports(linked.read_bytes(), surface)
        report["status"] = "strict-link-diagnostic-only"
        if args.convert:
            if report["startup_blockers"] or not report["core_link_contract_qualified"]:
                report["conversion_blocker"] = "startup or common core contract qualification incomplete"
            else:
                try:
                    source_bytes = linked.read_bytes()
                    native = convert(source_bytes, args.module,
                                     select_providers(source_bytes, providers))
                    native_dir = output / "native"
                    native_dir.mkdir()
                    target = native_dir / (args.module + ".elf")
                    target.write_bytes(native)
                    report["converted_native"] = {"path": str(target), "sha256": sha256(target)}
                    manifest = generate_runtime_sdk(linked, target, args.module, output, args.lld)
                    manifest["link_contract"] = contract
                    manifest["qualification"] = {
                        "native_loader_accepted": False, "gpu_runtime_tested": False,
                        "process_lifetime_only": True, "external_clrx": False,
                        "weak_null_resolution": nulls}
                    (output / "manifest.json").write_text(json.dumps(
                        manifest, indent=2, sort_keys=True) + "\n")
                    report["runtime_sdk"] = {"manifest": str(output / "manifest.json"),
                                             "exports": len(manifest["symbols"])}
                    report["status"] = "native-sdk-metadata-verified-not-runtime-tested"
                except ValueError as error:
                    report["conversion_blocker"] = str(error)
        return 0
    except Exception as error:
        report["status"] = "preparation-blocked"
        report["error"] = str(error)
        raise
    finally:
        changed = [path for path, checksum in protected.items()
                   if not Path(path).exists() or sha256(path) != checksum]
        report["protected_inputs_unchanged"] = not changed
        report["changed_inputs"] = changed
        report["protected_input_sha256"] = protected
        (output / "build.json").write_text(json.dumps(report, indent=2) + "\n")
        if changed:
            raise ValueError("inputs changed during diagnostic preparation: " + ", ".join(changed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "build/ps5-shared-runtime/graphics-ps5-native-abi-module")
    parser.add_argument("--graphics", type=Path,
                        default=ROOT / "build/ps5-shared-runtime/graphics-ps5-native-clrx-off")
    parser.add_argument("--sdk-include", type=Path, default=ROOT / "tools/ps5/sdk/include")
    parser.add_argument("--source", type=Path, default=ROOT / "build/ps5-external/ps5-opengl")
    parser.add_argument("--sdk", type=Path, default=Path("/opt/ps5-payload-sdk"))
    parser.add_argument("--core", type=Path, default=ROOT / "build/ps5-shared-runtime/core-unified-tls/manifest.json")
    parser.add_argument("--provider", type=Path, action="append", default=[])
    parser.add_argument("--module", default="libkodi-gl")
    parser.add_argument("--lld", default=shutil.which("ld.lld"))
    parser.add_argument("--ar", default=shutil.which("llvm-ar-18"))
    parser.add_argument("--objcopy", default=shutil.which("llvm-objcopy-18"))
    parser.add_argument("--convert", action="store_true",
                        help="attempt qualified metadata conversion, never load/deploy")
    args = parser.parse_args()
    if not all((args.lld, args.ar, args.objcopy)):
        parser.error("ld.lld, llvm-ar-18 and llvm-objcopy-18 are required")
    try:
        result = build(args)
    except (OSError, ValueError, KeyError) as error:
        print(f"graphics module preparation failed: {error}", file=sys.stderr)
        return 1
    print(f"Graphics diagnostic report: {args.output / 'build.json'}")
    return result


if __name__ == "__main__":
    sys.exit(main())
