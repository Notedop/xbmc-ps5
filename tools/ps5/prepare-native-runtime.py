#!/usr/bin/env python3
"""Prepare fingerprinted production core, graphics, native dependencies and add-on SDK."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import ast
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from addon_build import _InputHasher, _sdk_inputs
from addon_repository import workflow_lock
from native_contract import NETWORK_SYMBOLS, digest, require, validate_sdk
from native_graphics import qualify_graphics
from native_shared_link import load_provider
from native_c_link import Elf, nid
from native_runtime_sdk import eligible_symbols, import_object, validate_stub
from native_static_depends import load_verified_map

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SYSTEM_MODULES = (
    "libkernel", "libSceLibcInternal", "libSceNet", "libSceAgc", "libSceAgcDriver",
    "libSceVideoOut", "libSceSystemService", "libSceAudioOut", "libSceImeDialog",
    "libScePad", "libSceSysmodule", "libSceUserService", "libSceVideodec2",
)


def system_contract(source, module, output, lld):
    symbols = eligible_symbols(Elf(source.read_bytes()).symbols(".dynsym"), str(source))
    symbols = {name: row for name, row in symbols.items() if name not in NETWORK_SYMBOLS}
    output.mkdir(parents=True)
    (output / "lib").mkdir()
    obj = output / "import.o"
    obj.write_bytes(import_object(symbols))
    stub = output / "lib" / (module + ".so")
    subprocess.run([str(lld), "-shared", "--hash-style=sysv", "--soname=" + module + ".sprx",
                    "-o", str(stub), str(obj)], check=True, capture_output=True, text=True)
    validate_stub(stub.read_bytes(), symbols, module)
    obj.unlink()
    manifest = output / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": 1, "format": "ps5-native-runtime-sdk", "module": module, "library": module,
        "module_version": 257, "library_version": 1, "soname": module + ".sprx",
        "linker_only": True, "runtime_loadable": False,
        "runtime_implementation_qualified": False,
        "proof_scope": "SDK-declared imports; not installed system implementation validation",
        "provider": {"module": module, "export_library": module,
                     "module_version": 257, "library_version": 1},
        "source_import_library_sha256": digest(source),
        "import_library": {"path": "lib/" + module + ".so", "sha256": digest(stub)},
        "symbols": [dict(name=name, nid=nid(name), info=row[1], other=row[2],
                         value=row[4], size=row[5]) for name, row in sorted(symbols.items())],
    }, indent=2, sort_keys=True) + "\n")
    load_provider(manifest)
    return manifest


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def recipe_inputs(script, seen=None):
    """Include local imports and builder scripts invoked by a recipe."""
    seen = set() if seen is None else seen
    script = Path(script)
    if script in seen:
        return seen
    seen.add(script)
    for node in ast.walk(ast.parse(script.read_text())):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module + ".py")
        elif isinstance(node, ast.Import):
            names.extend(alias.name + ".py" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.endswith(".py"):
                names.append(node.value)
        for name in names:
            candidate = HERE / name
            if candidate.is_file():
                recipe_inputs(candidate, seen)
    return seen


def provider_inputs(manifests):
    return [item for path in manifests for item in
            (path, path.parent / load_provider(path)["import_library"]["path"])]


def provider_abi(manifests):
    result = []
    for path in manifests:
        provider = load_provider(path)
        contract = dict(provider.get("link_contract", {}))
        if "network" in contract:
            contract["network"] = {key: value for key, value in contract["network"].items()
                                   if key != "source_sha256"}
        result.append({
            **{key: provider[key] for key in
               ("module", "module_version", "library", "library_version", "soname")},
            "link_contract": contract,
            "symbols": sorted(
                (symbol["name"], symbol["nid"], symbol["info"], symbol["other"],
                 symbol["size"] if symbol["info"] & 15 != 2 else 0)
                for symbol in provider["symbols"]),
        })
    return result


class StageCache:
    def __init__(self, output):
        self.output = output
        self.hasher = _InputHasher(output)
        self.inputs = []

    def run(self, name, inputs, build, products, identity=None):
        inputs = sorted(set(map(Path, inputs)))
        before = self.hasher.tree_digest(inputs)
        fingerprint = hashlib.sha256(json.dumps(
            {"schema": 1, "inputs": before, "identity": identity},
            sort_keys=True).encode()).hexdigest()
        work = self.output / "stages" / name / fingerprint
        receipt = work / "stage.json"
        if receipt.is_file():
            saved = json.loads(receipt.read_text())
            require(saved.get("fingerprint") == fingerprint and saved.get("files"),
                    f"invalid completed stage: {receipt}")
            require(set(saved["files"]) ==
                    {path.relative_to(work).as_posix() for path in products(work)},
                    f"incomplete completed stage: {receipt}")
            for relative, expected in saved["files"].items():
                path = work / relative
                require(path.resolve().is_relative_to(work.resolve()) and
                        digest(path) == expected, f"cached {name} changed: {relative}")
            print(f"Reusing verified {name}: {work}", flush=True)
        else:
            require(not work.exists(), f"incomplete {name} at {work}; diagnostics retained; "
                    "remove only this incomplete stage after investigating, then rerun")
            work.mkdir(parents=True)
            build(work)
            files = products(work)
            require(files, f"{name} produced no qualified outputs")
            checksums = {}
            for path in files:
                require(path.resolve().is_relative_to(work.resolve()),
                        f"{name} output escapes its stage: {path}")
                checksums[path.relative_to(work).as_posix()] = digest(path)
            require(self.hasher.tree_digest(inputs) == before,
                    f"{name} inputs changed while building; stage not published")
            write_json(receipt, {"schema": 1, "fingerprint": fingerprint, "files": checksums})
        self.inputs.append((inputs, before))
        self.hasher.save()
        return work

    def check_inputs(self):
        for paths, expected in self.inputs:
            require(self.hasher.tree_digest(paths) == expected,
                    "native preparation inputs changed; current SDK not replaced")
        self.hasher.save()


def run_builder(work, name, *arguments):
    log = work / (name + ".log")
    print(f"Preparing {name}; log: {log}", flush=True)
    with log.open("w") as stream:
        result = subprocess.run([sys.executable, str(HERE / (name + ".py")),
                                 *map(str, arguments)],
                                stdout=stream, stderr=subprocess.STDOUT)
    require(result.returncode == 0,
            f"native preparation failed ({result.returncode}): {name}; see {log}")


def provider_products(root):
    manifest = root / "manifest.json"
    provider = load_provider(manifest)
    files = [manifest, root / provider["import_library"]["path"], root / "build.json"]
    files.extend(path for path in root.rglob("*")
                 if path.is_file() and path.suffix in (".sprx", ".elf"))
    return files


def graphics_products(root):
    report = json.loads((root / "build.json").read_text())
    require(report.get("stage") == "relocatable-pic-archive-group" and
            report["baseline_preservation"]["unchanged"] and report["sdk_include_unchanged"],
            "graphics archives are not qualified")
    files = [root / "build.json", root / "baseline-before.json"]
    files.extend(Path(item["path"]) for item in report["archives"])
    patch = report["psbc_platform_patch"]
    files.extend(Path(patch[key]) for key in ("isolated_source", "patch"))
    return files


def publish_generation(output, work):
    # Both compatibility paths traverse one atomically replaced generation pointer.
    for name in ("sdk", "depends"):
        pointer = output / name
        if pointer.exists() or pointer.is_symlink():
            require(pointer.is_symlink() and
                    pointer.resolve().is_relative_to(output / "artifacts"),
                    f"refusing to replace unowned preparation path: {pointer}")
        temporary = output / ("." + name + "-next")
        require(not temporary.exists() and not temporary.is_symlink(),
                f"unfinished preparation pointer: {temporary}")
    current = output / "current"
    require(not (current.exists() or current.is_symlink()) or (current.is_symlink() and
            current.resolve().is_relative_to(output / "artifacts")),
            f"refusing to replace unowned preparation path: {current}")
    temporary = output / ".current-next"
    require(not temporary.exists() and not temporary.is_symlink(),
            f"unfinished preparation pointer: {temporary}")
    if not current.is_symlink():
        old = output / "sdk"
        # Preserve the old generation while converting the two legacy aliases.
        initial = old.resolve().parent if old.is_symlink() else work
        current.symlink_to(initial.relative_to(output), target_is_directory=True)
    for name in ("sdk", "depends"):
        temporary = output / ("." + name + "-next")
        temporary.symlink_to("current/" + name, target_is_directory=True)
        os.replace(temporary, output / name)
    temporary = output / ".current-next"
    temporary.symlink_to(work.relative_to(output), target_is_directory=True)
    os.replace(temporary, current)


def prepare(args):
    subprocess.run([sys.executable, str(HERE / "check-sdk.py"), "--sdk", str(args.sdk)],
                   check=True)
    output = args.output.resolve()
    source = args.source.resolve()
    provenance = qualify_graphics(source, args.opengl)
    require(args.native_tool.is_file() and os.access(args.native_tool, os.X_OK),
            f"missing native tool: {args.native_tool}; prepare the app template or use --native-tool")
    output.mkdir(parents=True, exist_ok=True)
    with workflow_lock(output):
        cache = StageCache(output)
        homebrew = args.sdk / "target/user/homebrew"
        sdk_inputs = [path for path in _sdk_inputs(args.sdk) if path != homebrew]
        host_tools = []
        for name in ("clang-18", "ld.lld", "llvm-ar-18", "llvm-objcopy-18", "make", "cmake",
                     "ninja", "meson"):
            tool = shutil.which(name)
            require(tool, f"missing native preparation tool: {name}")
            host_tools.append(Path(tool).resolve())
        common = [*sdk_inputs, args.sdk / "toolchain",
                  HERE / "sdk/include", HERE / "native-clang.py"]
        compiler_resource = subprocess.run(
            [str(host_tools[0]), "--print-resource-dir"],
            check=True, capture_output=True, text=True).stdout.strip()
        common.append(Path(compiler_resource))

        providers = []
        for directory in args.system_sdk:
            manifests = sorted(directory.resolve().glob("*/manifest.json"))
            require(manifests, f"no native provider SDKs in {directory}")
            providers.extend(manifests)
        if not providers:
            libraries = [(args.graphics_imports if module in ("libSceAgc", "libSceAgcDriver")
                          else args.sdk / "target/lib") / (module + ".so")
                         for module in SYSTEM_MODULES]

            def build_system(work):
                for module, library in zip(SYSTEM_MODULES, libraries):
                    system_contract(library, module, work / module, args.sdk / "bin/ld.lld")

            systems = cache.run("system", [*libraries, args.sdk / "bin/ld.lld",
                                HERE / "native_runtime_sdk.py", HERE / "native_c_link.py",
                                HERE / "native_contract.py"], build_system,
                                lambda work: sorted(work.glob("*/manifest.json")) +
                                sorted(work.glob("*/lib/*.so")))
            providers = sorted(systems.glob("*/manifest.json"))
        identities = [load_provider(path)["module"] for path in providers]
        require(len(set(identities)) == len(identities) and
                {"libSceLibcInternal", "libkernel", "libSceNet", "libSceAgc", "libSceAgcDriver",
                 "libSceVideoOut", "libSceSystemService"} <= set(identities),
                "missing or duplicate native libc/kernel/network/graphics provider contracts")
        provider_args = [item for path in providers for item in ("--provider", path)]
        provider_files = provider_inputs(providers)
        core_inputs = [
            *common, *host_tools[:4], *provider_files, args.native_tool,
            *recipe_inputs(HERE / "build-shared-runtime.py"),
            ROOT / "shims/native-app", ROOT / "shims/libkodishim",
            HERE / "native_module_crt.cpp", HERE / "native-shared.ld"]
        graphics_inputs = [
            *common, homebrew, *host_tools, *recipe_inputs(HERE / "build-shared-graphics.py"),
            source / "src", source / "toolchain", source / "tests/ps5/native-app.mk",
            source / "third_party/mesa-26.2.0/src",
            source / "third_party/mesa-26.2.0/include",
            source / "third_party/opengnm-psbc/src", args.opengl,
            source / "build/core33-native-runtime/runtime-config.txt",
            source / "build/mesa-ps5-probe/meson-private/cmd_line.txt",
            source / "build/mesa-ps5-probe/meson-info/intro-buildoptions.json"]
        for relative in (
                "third_party/mesa-26.2.0/meson.build", "third_party/mesa-26.2.0/meson.options",
                "third_party/mesa-26.2.0/VERSION", "third_party/mesa-26.2.0/bin",
                "third_party/opengnm-psbc/psbc_stubs.c",
                "third_party/opengnm/include", "third_party/Vulkan-Headers/include",
                "third_party/SPIRV-Headers/include"):
            graphics_inputs.append(source / relative)
        # Meson's saved configuration can refer to additional generated cross files.
        config = configparser.ConfigParser()
        config.read(source / "build/mesa-ps5-probe/meson-private/cmd_line.txt")
        for path in ast.literal_eval(config.get("properties", "cross_file")):
            graphics_inputs.append(Path(path))
        spec = importlib.util.spec_from_file_location(
            "stdio_depends", HERE / "build-native-stdio-depends.py")
        depends_builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(depends_builder)
        dependency_inputs = {}
        packages = ("bzip2", "curl", "openssl", "gettext", "libsmb2", "fmt", "libpng")
        for package in packages:
            inputs = [*common, homebrew, args.depends / "include", *host_tools,
                      *recipe_inputs(HERE / "build-native-stdio-depends.py"),
                      *depends_builder.originals(args, package)]
            if package in ("fmt", "libpng"):
                filename = "12.1.0.tar.gz" if package == "fmt" else "libpng-1.6.43.tar.xz"
                inputs.extend(args.homebrew_sources / package / name
                              for name in (filename, "PKGBUILD"))
            else:
                recipe = ROOT / "tools/depends/target" / package
                inputs.extend((recipe / "Makefile", recipe / depends_builder.PLATFORM))
            dependency_inputs[package] = inputs
        assembly_inputs = [
            *recipe_inputs(HERE / "prepare-native-addon-sdk.py"),
            ROOT / "cmake/scripts/ps5", ROOT / "toolchain/ps5-native-addon.cmake",
            HERE / "native_abi_check.h", HERE / "native_module_crt.cpp",
            HERE / "native-shared.ld", HERE / "sdk/include", args.native_tool]
        for paths in (core_inputs, graphics_inputs, assembly_inputs, *dependency_inputs.values()):
            cache.hasher.tree_digest(paths)
        cache.hasher.save()
        if args.check_inputs:
            print("Native preparation inputs verified; no core, graphics or dependencies built.")
            return
        core = cache.run("core", core_inputs,
            lambda work: run_builder(work, "build-shared-runtime", "--sdk", args.sdk,
                "--native-tool", args.native_tool, "--output", work / "core", *provider_args),
            lambda work: provider_products(work / "core"),
            identity={"native_tool": digest(args.native_tool)})
        core = core / "core"
        probe_providers = [core / "manifest.json", *[path for path in providers
                           if load_provider(path)["module"] in ("libSceLibcInternal", "libkernel")]]
        probes = [item for path in probe_providers for item in ("--provider", path)]
        archives = cache.run("graphics-archives", graphics_inputs,
            lambda work: run_builder(work, "build-shared-graphics", "--source", source,
                "--sdk", args.sdk, "--installed", args.opengl, "--output", work / "archives",
                "--sdk-include", HERE / "sdk/include", "--jobs", args.jobs, *probes),
            lambda work: graphics_products(work / "archives"),
            identity={"graphics": provenance, "probe_abi": provider_abi(probe_providers)})
        archives = archives / "archives"
        graphics = cache.run("graphics-module", [
            *common, *host_tools[:4], *recipe_inputs(HERE / "build-shared-graphics-module.py"),
            *provider_files, *provider_inputs([core / "manifest.json"]), archives / "build.json",
            source / "src", source / "third_party/mesa-26.2.0/src/mesa/glapi/glapi/registry/gl.xml",
            HERE / "native_module_crt.cpp", HERE / "native-shared.ld"],
            lambda work: run_builder(work, "build-shared-graphics-module", "--source", source,
                "--sdk", args.sdk, "--graphics", archives, "--output", work / "graphics",
                "--core", core / "manifest.json", "--convert", *provider_args),
            lambda work: provider_products(work / "graphics"))
        graphics = graphics / "graphics"
        mapping = {}
        for package in packages:
            dependency = cache.run("depends-" + package, dependency_inputs[package],
                lambda work, package=package: run_builder(work, "build-native-stdio-depends",
                    "--sdk", args.sdk, "--depends", args.depends,
                    "--homebrew-sources", args.homebrew_sources, "--output", work / "depends",
                    "--jobs", args.jobs, "--only", package),
                lambda work, package=package: [work / "depends/archive-map.json",
                    work / "depends" / package / "receipt.json",
                    *[Path(path) for path in json.loads(
                        (work / "depends/archive-map.json").read_text()).values()]])
            mapping.update(json.loads((dependency / "depends/archive-map.json").read_text()))
        assembly_digest = cache.hasher.tree_digest(assembly_inputs)
        cache.inputs.append((assembly_inputs, assembly_digest))
        generation_identity = {"core": str(core), "graphics": str(graphics),
                               "depends": mapping, "provenance": provenance,
                               "assembly": assembly_digest}
        fingerprint = hashlib.sha256(json.dumps(generation_identity, sort_keys=True).encode()).hexdigest()
        work = output / "artifacts" / fingerprint
        sdk, archive_map = work / "sdk", work / "depends/archive-map.json"
        receipt_path = work / "preparation.json"
        if not receipt_path.exists():
            require(not work.exists(), f"incomplete SDK assembly: {work}; diagnostics retained")
            work.mkdir(parents=True)
            archive_map.parent.mkdir()
            write_json(archive_map, mapping)
            system_args = [item for path in providers for item in ("--system-sdk", path.parent)]
            run_builder(work, "prepare-native-addon-sdk", "--core", core, "--graphics", graphics,
                        "--native-tool", args.native_tool, "--output", sdk, *system_args)
            write_json(sdk / "graphics-provenance.json", {
                **provenance, "archives_report_sha256": digest(archives / "build.json"),
                "runtime_sha256": digest(sdk / "runtimes/libkodi-gl.sprx")})
            receipt = json.loads((sdk / "receipt.json").read_text())
            receipt["files"]["graphics-provenance.json"] = digest(sdk / "graphics-provenance.json")
            receipt["native_depends_map_sha256"] = digest(archive_map)
            write_json(sdk / "receipt.json", receipt)
            (sdk / "checksums.sha256").write_text("".join(
                f"{digest(path)}  {path.relative_to(sdk).as_posix()}\n"
                for path in sorted(sdk.rglob("*"))
                if path.is_file() and path.name != "checksums.sha256"))
            validate_sdk(sdk, archive_map)
            cache.check_inputs()
            require(qualify_graphics(source, args.opengl) == provenance,
                    "graphics provenance changed during preparation; SDK not published")
            write_json(receipt_path, {"schema": 2, "fingerprint": fingerprint,
                       "sdk_receipt_sha256": digest(sdk / "receipt.json"),
                       "archive_map_sha256": digest(archive_map)})
        receipt = json.loads(receipt_path.read_text())
        require(receipt.get("fingerprint") == fingerprint and
                receipt.get("sdk_receipt_sha256") == digest(sdk / "receipt.json") and
                receipt.get("archive_map_sha256") == digest(archive_map),
                "prepared generation changed")
        validate_sdk(sdk, archive_map)
        load_verified_map(archive_map, HERE / "sdk/include/stdio.h")
        cache.check_inputs()
        require(qualify_graphics(source, args.opengl) == provenance,
                "graphics provenance changed during preparation; SDK not published")
        publish_generation(output, work)
    print(f"PS5_NATIVE_RUNTIME_SDK={output / 'sdk'}\n"
          f"PS5_NATIVE_DEPENDS_MAP={output / 'depends/archive-map.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-sdk", type=Path, action="append", default=[],
                        help="optional provider contract roots; otherwise use payload SDK imports")
    parser.add_argument("--output", type=Path, default=ROOT / "build/ps5-native")
    parser.add_argument("--sdk", type=Path, default=Path(os.environ.get(
        "PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk")))
    parser.add_argument("--depends", type=Path, default=Path(os.environ.get(
        "PS5_DEPENDS_PREFIX", ROOT / "tools/depends/xbmc-depends/x86_64-unknown-freebsd-debug")))
    work = Path(os.environ.get("WORK", ROOT / "build/ps5-external"))
    parser.add_argument("--source", type=Path, default=Path(os.environ.get(
        "PS5_OPENGL_SOURCE", work / "ps5-opengl")))
    parser.add_argument("--opengl", type=Path, default=Path(os.environ.get(
        "PS5_OPENGL_PREFIX", "/opt/ps5-opengl-gl46")))
    parser.add_argument("--graphics-imports", type=Path,
                        help="graphics SDK directory containing the AGC import libraries")
    parser.add_argument("--homebrew-sources", type=Path,
                        default=Path(os.environ.get(
                            "PS5_HOMEBREW_SOURCES", work / "pacbrew-repo")))
    parser.add_argument("--native-tool", type=Path)
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8))
    parser.add_argument("--check-inputs", action="store_true",
                        help="verify prerequisites without compiling core, graphics or dependencies")
    args = parser.parse_args()
    for name in ("source", "sdk", "depends", "opengl", "homebrew_sources"):
        setattr(args, name, getattr(args, name).resolve())
    if args.native_tool is None:
        template = Path(os.environ.get("APP_TEMPLATE", args.source / "build/native-app/PPSA99005"))
        args.native_tool = template / "build/host/ps5-native-tool"
    if args.graphics_imports is None:
        args.graphics_imports = args.opengl / "lib"
    if not 1 <= args.jobs <= 32:
        parser.error("--jobs must be between 1 and 32")
    try:
        prepare(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"native preparation: {error}\n")


if __name__ == "__main__":
    main()
