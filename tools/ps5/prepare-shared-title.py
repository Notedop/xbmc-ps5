#!/usr/bin/env python3
"""Prepare an isolated native title template to consume the shared SDK."""
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from native_shared_link import load_provider
from native_loader_companion import build as build_loader_companion

ROOT = Path(__file__).resolve().parents[2]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(text, old, new, label):
    require(text.count(old) == 1, f"unexpected native template {label}")
    return text.replace(old, new, 1)


def stage_startup(app):
    tools = app / "vendor/ps5-runtime-tools"
    tools.mkdir(exist_ok=True)
    for name in ("build-native-kodi-app.py", "build-native-title-launcher.py",
                 "native_c_link.py", "native_runtime_sdk.py", "native_shared_link.py",
                 "native-shared.ld", "native_title_launcher.py"):
        shutil.copyfile(ROOT / "tools/ps5" / name, tools / name)
    native = app / "tooling/native"
    startup_names = ("title_native_launcher.cpp", "kodi_app_entry.cpp", "title_native_startup.h")
    for name in startup_names:
        shutil.copyfile(ROOT / "shims/native-app" / name, native / name)
    shutil.copyfile(ROOT / "tools/ps5/native_module_crt.cpp", native / "native_module_crt.cpp")
    for name in ("title_shared_runtime.cpp", "title_shared_bindings.h"):
        path = app / "src" / name
        if path.is_file():
            old_receipt = app / "vendor/ps5-shared-runtime.json"
            require(old_receipt.is_file() and
                    digest(path) == json.loads(old_receipt.read_text())
                    .get("startup_sources", {}).get(name),
                    f"obsolete startup source changed: {name}")
            path.unlink()
    return {name: digest(native / name) for name in startup_names}


def patch_builder(text, wraps):
    start = text.find("objects=()\n")
    end = text.find('\napp="$dist/$title_id"\n', start)
    require(start >= 0 and end > start and text.count("objects=()\n") == 1,
            "unexpected native template compilation/link block")
    text = text[:start] + r'''shared_sdk=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sdk"])' \
    "$root/vendor/ps5-shared-runtime.json")
python3 "$root/vendor/ps5-runtime-tools/build-native-kodi-app.py" \
    --app "$root" --sdk "$shared_sdk" --native-tool "$tool"
python3 "$root/vendor/ps5-runtime-tools/build-native-title-launcher.py" \
    --app "$root" --sdk "$shared_sdk" --native-tool "$tool"
''' + text[end:]
    start = text.find('[[ -f $root/runtime/libc.prx ]]')
    end_marker = '"$tool" self --inspect --file "$app/eboot.bin"'
    end = text.find(end_marker, start)
    require(start >= 0 and end > start, "unexpected native template runtime packaging")
    text = text[:start] + r'''for name in libkodi-runtime.sprx libkodi-gl.sprx libc.prx; do
    input="$root/vendor/ps5-runtime/$name"
    [[ -f $input ]] || { echo "shared runtime module missing: $input" >&2; exit 2; }
    install -m 755 "$input" "$app/sce_module/${input##*/}"
    "$tool" self --inspect --file "$app/sce_module/${input##*/}"
done
install -m 755 "$build/kodi-app/libkodi-app.sprx" "$app/sce_module/libkodi-app.sprx"
cp "$build/kodi-app/proof.json" "$app/ps5-kodi-app-proof.json"
cp "$root/vendor/ps5-loader-companion/proof.json" "$app/ps5-loader-companion-proof.json"
''' + text[end:]
    return text


def patch_makefile(text):
    for target in ("app", "ffpkg", "ffpfsc", "packages"):
        text = replace_once(text, target + ": $(RUNTIME)\n", target + ":\n",
                            target + " bootstrap runtime prerequisite")
    return text


def provider_identities(providers):
    identities = []
    for provider in providers:
        manifest = json.loads(Path(provider["manifest"]).read_text())
        identity = {key: manifest[key] for key in
                    ("soname", "module", "module_version", "library", "library_version")}
        require(identity["module"] == provider["module"] and
                identity["soname"] == provider["soname"],
                "provider descriptor/manifest identity mismatch")
        require(all(isinstance(identity[key], int) and not isinstance(identity[key], bool) and
                    0 < identity[key] <= 0xFFFF
                    for key in ("module_version", "library_version")),
                "invalid provider module/library version")
        identities.append(identity)
    require(len({p["soname"] for p in identities}) == len(identities),
            "duplicate provider SONAME")
    return identities


def patch_converter(sources, identities):
    sources = dict(sources)
    sources["elf_object.hpp"] = replace_once(
        sources["elf_object.hpp"], "    std::string library_name;\n",
        "    std::string library_name;\n"
        "    std::uint16_t module_version{};\n"
        "    std::uint16_t library_version{};\n", "stub identity versions")
    source = replace_once(sources["elf_object.cpp"], '#include "elf_object.hpp"\n',
                          '#include "elf_object.hpp"\n#include "shared_providers.hpp"\n',
                          "provider identity header")
    source = replace_once(source, '''    constexpr std::string_view suffix = ".sprx";
    if (name.ends_with(suffix))
        name.replace(name.size() - suffix.size(), suffix.size(), ".prx");
''', "", "SONAME preservation")
    source = replace_once(source, "    stub.library_name = stub.module_name;\n",
                          "    stub.library_name = stub.module_name;\n"
                          "    apply_shared_provider_identity(stub);\n", "stub identity lookup")
    sources["elf_object.cpp"] = source
    source = sources["sce_module_writer.cpp"]
    for field in ("module_name", "name"):
        source = replace_once(source, f"    std::uint32_t {field}{{}};\n",
                              f"    std::uint32_t {field}{{}};\n"
                              "    std::uint16_t version{};\n", field + " record version")
    for record, name in (("module", "module_name"), ("library", "name")):
        source = replace_once(
            source, f"{record}.{name} | (std::uint64_t{{1}} << 32)",
            f"{record}.{name} | (static_cast<std::uint64_t>({record}.version) << 32)",
            record + " dynamic version")
    source = replace_once(
        source, "        const Stub *provider = module_order[i];\n",
        "        const Stub *provider = module_order[i];\n"
        "        require(provider->module_version != 0 && provider->library_version != 0,\n"
        '                "missing shared provider identity: " + provider->soname);\n',
        "required provider identity")
    source = replace_once(
        source, "strings.add(provider->module_name)});",
        "strings.add(provider->module_name), provider->module_version});",
        "module provider version")
    source = replace_once(
        source, "strings.add(provider->library_name)});",
        "strings.add(provider->library_name), provider->library_version});",
        "library provider version")
    sources["sce_module_writer.cpp"] = source
    rows = [
        "        {" + ", ".join(json.dumps(p[key], ensure_ascii=True) for key in
                               ("soname", "module", "library")) +
        f', {p["module_version"]}, {p["library_version"]}' + "},"
        for p in identities
    ]
    sources["shared_providers.hpp"] = '''// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

namespace ps5::elf
{
inline void apply_shared_provider_identity(Stub& stub)
{
    struct Provider
    {
        const char* soname;
        const char* module;
        const char* library;
        std::uint16_t module_version;
        std::uint16_t library_version;
    };
    static constexpr Provider providers[] = {
''' + "\n".join(rows) + '''
    };
    for (const auto& provider : providers)
    {
        if (stub.soname != provider.soname)
            continue;
        stub.module_name = provider.module;
        stub.library_name = provider.library;
        stub.module_version = provider.module_version;
        stub.library_version = provider.library_version;
        return;
    }
}
} // namespace ps5::elf
'''
    return sources


def title_providers(providers, manifests=()):
    providers = list(providers)
    sdk_providers = {provider["soname"]: provider for provider in providers}
    receipts = []
    for manifest in manifests:
        manifest = manifest.resolve()
        provider = load_provider(manifest)
        require(provider["module"] not in {"libkodi-runtime", "libkodi-gl"},
                "additional title providers must not replace shared runtime owners")
        if provider["soname"] in sdk_providers:
            require(digest(manifest) ==
                    digest(Path(sdk_providers[provider["soname"]]["manifest"])),
                    "additional provider conflicts with shared SDK: " + provider["soname"])
            continue
        library = (manifest.parent / provider["import_library"]["path"]).resolve()
        providers.append({
            "module": provider["module"], "soname": provider["soname"],
            "manifest": str(manifest), "library": str(library),
        })
        receipts.append({"path": str(manifest), "sha256": digest(manifest)})
    provider_identities(providers)
    return providers, receipts


def prepare(sdk, app, manifests=(), native_tool=None):
    sdk, app = sdk.resolve(), app.resolve()
    result = subprocess.run([
        sys.executable, str(sdk / "tools/build-native-shared-addon.py"),
        "--sdk", str(sdk), "--describe",
    ], check=True, capture_output=True, text=True)
    described = json.loads(result.stdout)
    receipt = json.loads((sdk / "receipt.json").read_text())
    require(receipt.get("status") == "metadata-verified-console-unqualified",
            "missing completed shared SDK receipt")
    for relative, expected in receipt["files"].items():
        path = (sdk / relative).resolve()
        require(path.is_relative_to(sdk) and path.is_file() and digest(path) == expected,
                f"shared SDK file changed: {relative}")
    providers, manifest_receipts = title_providers(described["providers"], manifests)
    identities = provider_identities(providers)
    own = {"libkodi-runtime", "libkodi-gl"}
    require(own <= {p["module"] for p in providers}, "shared core/graphics providers required")
    vendor = app / "vendor"
    stubdir = app / ".deps/native/ps5-payload-sdk/target/lib"
    require(vendor.is_dir() and stubdir.is_dir(), "expected an isolated staged native title")
    forbidden = {
        "app_heap.c", "heap_dmem.c", "runtime_shims.c", "thread_stack.c", "shared_tls.c",
        "shared_exit.c", "pthread_tls.c", "libc_locale.c", "libc_posix.c",
    }
    require(not any((app / "src" / name).exists() for name in forbidden),
            "shared title must not compile a duplicate platform/graphics/heap/TLS runtime")
    group_path = vendor / "libkodi_group.a"
    group = group_path.read_text()
    require(not any(name in group for name in ("libc++.a", "libc++abi.a", "libunwind.a")),
            "shared title must not statically link the C++ runtime")
    libraries = []
    for provider in providers:
        library = Path(provider["library"])
        copied = stubdir / library.name
        shutil.copyfile(library, copied)
        libraries.append(copied)
    providers_by_module = {p["module"]: p for p in providers}
    for module in sorted(own):
        runtime = sdk / "runtimes" / (module + ".sprx")
        proof = receipt["runtime_proofs"][module]
        require(proof["process_lifetime"] is True and proof["sign_extract_equal"] is True and
                digest(runtime) == proof["fself_sha256"],
                f"shared runtime does not match verified SDK: {module}")
        require(runtime.read_bytes()[:4] == b"\x54\x14\xf5\xee", "expected signed PS5 runtime")
        (vendor / "ps5-runtime").mkdir(exist_ok=True)
        shutil.copyfile(runtime, vendor / "ps5-runtime" / runtime.name)
        require(module in providers_by_module, f"missing shared provider: {module}")
    native_tool = native_tool or (
        ROOT / "build/ps5-external/ps5-opengl/build/native-app/PPSA99005/build/host/ps5-native-tool")
    companion = vendor / "ps5-loader-companion"
    companion_proof = build_loader_companion(sdk, companion, native_tool)
    shutil.copy2(companion / "libc.prx", vendor / "ps5-runtime/libc.prx")
    (vendor / "ps5-runtime-tools").mkdir()
    shutil.copyfile(sdk / "tools/native-clang.py",
                    vendor / "ps5-runtime-tools/native-clang.py")
    shutil.copytree(sdk / "include", vendor / "ps5-runtime-include")
    startup_sources = stage_startup(app)
    ordered = ['"' + str(p) + '"' for p in libraries]
    ordered.extend('"' + str(p) + '"' for p in sorted(stubdir.glob("*.so"))
                   if p not in libraries)
    (vendor / "ps5-runtime-imports.rsp").write_text("\n".join(ordered) + "\n")
    group = replace_once(group, "GROUP (\n", "GROUP (\n" + "".join(
        '  "' + str(p) + '"\n' for p in libraries), "provider group")
    group_path.write_text(group)
    native = app / "tooling/native"
    sources = {name: (native / name).read_text() for name in
               ("elf_object.hpp", "elf_object.cpp", "sce_module_writer.cpp")}
    for name, source in patch_converter(sources, identities).items():
        (native / name).write_text(source)
    script = app / "tools/build.sh"
    script.write_text(patch_builder(script.read_text(), described["wrap_symbols"]))
    makefile = app / "Makefile"
    makefile.write_text(patch_makefile(makefile.read_text()))
    record = {
        "schema": 1, "format": "ps5-shared-title-inputs",
        "console_accepted": False, "sdk_manifest_sha256": digest(sdk / "manifest.json"),
        "sdk": str(sdk),
        "wrap_symbols": described["wrap_symbols"],
        "providers": identities,
        "additional_provider_manifests": manifest_receipts,
        "loader_companion_sha256": companion_proof["signed_sha256"],
        "startup_sources": startup_sources,
        "runtime_sha256": {module + ".sprx": digest(
            vendor / "ps5-runtime" / (module + ".sprx")) for module in sorted(own)},
    }
    (vendor / "ps5-shared-runtime.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--provider", type=Path, action="append", default=[],
                        help="additional verified title-only provider manifest (repeatable)")
    parser.add_argument("--native-tool", type=Path,
                        help="native SELF signer used to build the mandatory loader companion")
    args = parser.parse_args()
    try:
        prepare(args.sdk, args.app, args.provider, args.native_tool)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        parser.exit(1, f"prepare-shared-title: {detail}\n")


if __name__ == "__main__":
    main()
