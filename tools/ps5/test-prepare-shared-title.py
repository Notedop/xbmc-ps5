#!/usr/bin/env python3
"""Check isolated shared-title preparation and title lifecycle ordering."""
# SPDX-License-Identifier: GPL-3.0-or-later

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "prepare_shared_title", ROOT / "tools/ps5/prepare-shared-title.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
WRAPS = [
    "malloc", "calloc", "realloc", "free", "posix_memalign", "malloc_usable_size",
    "aligned_alloc", "memalign", "reallocf", "chdir", "fopen", "pthread_create", "exit",
]
COMMAND = 'PS5_PAYLOAD_SDK="$sdk_root" sh "$root/tooling/prospero-clang18"'
BUILDER = f'''#!/usr/bin/env bash
set -euo pipefail
objects=()
{COMMAND} -c "$source" -o "$object"
{COMMAND} -c "$native/app_crt.cpp" -o "$build/obj/app_crt.o"
{COMMAND} \\
    -std=c++20 -O2 -Wall -Wextra -fno-exceptions -fno-rtti \\
    -ffunction-sections -fdata-sections \\
    -c "$native/app_cpp_runtime.cpp" -o "$build/obj/app_cpp_runtime.o"

link_inputs=("$build/obj/app_crt.o" "$build/obj/app_cpp_runtime.o" "$object")
"$sdk_root/bin/prospero-lld" -T "$native/ps5-pie.ld" --wrap=malloc --wrap=pthread_setspecific --wrap=write --wrap=sceKernelLoadStartModule \\
    --as-needed "$sdk_root"/target/lib/*.so
app="$dist/$title_id"
"$tool" self --sign --in "$build/eboot.elf" --out "$app/eboot.bin"
[[ -f $root/runtime/libc.prx ]] || bash "$root/tools/rebuild-libc.sh"
runtime_modules=("$root/runtime/libc.prx")
"$tool" self --inspect --file "$app/eboot.bin"
'''
SONAME = '''    constexpr std::string_view suffix = ".sprx";
    if (name.ends_with(suffix))
        name.replace(name.size() - suffix.size(), suffix.size(), ".prx");
'''
CONVERTER = {
    "elf_object.hpp": "    std::string library_name;\n",
    "elf_object.cpp": '#include "elf_object.hpp"\n' + SONAME +
                      "    stub.library_name = stub.module_name;\n",
    "sce_module_writer.cpp": '''    std::uint32_t module_name{};
    std::uint32_t name{};
module.module_name | (std::uint64_t{1} << 32)
library.name | (std::uint64_t{1} << 32)
        const Stub *provider = module_order[i];
strings.add(provider->module_name)});
strings.add(provider->library_name)});
''',
}


class SharedTitleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shared-title-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.sdk, self.app = self.directory / "sdk", self.directory / "app"
        for path in ("tools", "include", "runtimes", "lib"):
            (self.sdk / path).mkdir(parents=True)
        for path in ("vendor", ".deps/native/ps5-payload-sdk/target/lib", "src",
                     "tooling/native", "tools"):
            (self.app / path).mkdir(parents=True)
        (self.sdk / "tools/native-clang.py").write_text("# compiler fixture\n")
        (self.sdk / "include/stdio.h").write_text("/* opaque FILE fixture */\n")
        (self.sdk / "manifest.json").write_text('{"schema": 1}\n')
        (self.app / "vendor/libkodi_group.a").write_text('GROUP (\n "libtest.a"\n)\n')
        (self.app / "tools/build.sh").write_text(BUILDER)
        (self.app / "Makefile").write_text(
            "".join(target + ": $(RUNTIME)\n" for target in
                    ("app", "ffpkg", "ffpfsc", "packages")))
        for name, source in CONVERTER.items():
            (self.app / "tooling/native" / name).write_text(source)
        self.providers, proofs = [], {}
        for module in ("libkodi-runtime", "libkodi-gl"):
            library = self.sdk / "lib" / (module + ".so")
            library.write_bytes(b"linker-only fixture")
            runtime = self.sdk / "runtimes" / (module + ".sprx")
            runtime.write_bytes(b"\x54\x14\xf5\xee" + module.encode())
            manifest = self.sdk / "lib" / (module + ".json")
            manifest.write_text(json.dumps({
                "module": module, "soname": runtime.name, "library": module,
                "module_version": 0x101, "library_version": 1,
            }))
            self.providers.append({"module": module, "soname": runtime.name,
                                   "library": str(library), "manifest": str(manifest)})
            proofs[module] = {"process_lifetime": True, "sign_extract_equal": True,
                              "fself_sha256": MODULE.digest(runtime)}
        self.receipt = {
            "status": "metadata-verified-console-unqualified",
            "files": {str(p.relative_to(self.sdk)): MODULE.digest(p)
                      for p in self.sdk.rglob("*") if p.is_file()},
            "runtime_proofs": proofs,
        }
        self.write_receipt()
        self.describe = patch.object(
            MODULE.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"providers": self.providers, "wrap_symbols": WRAPS})))
        self.describe.start()
        self.addCleanup(self.describe.stop)
        def companion_fixture(sdk, output, tool):
            output.mkdir()
            binary = output / "libc.prx"
            binary.write_bytes(b"\x54\x14\xf5\xee" + b"loader companion fixture")
            return {"signed_sha256": MODULE.digest(binary)}
        self.companion = patch.object(MODULE, "build_loader_companion",
                                      side_effect=companion_fixture)
        self.companion.start()
        self.addCleanup(self.companion.stop)

    def write_receipt(self):
        (self.sdk / "receipt.json").write_text(json.dumps(self.receipt))

    def test_prepares_only_shared_owners_and_preserves_native_sonames(self):
        record = MODULE.prepare(self.sdk, self.app)
        script = (self.app / "tools/build.sh").read_text()
        self.assertNotIn("--wrap=", script)
        self.assertNotIn("app_cpp_runtime", script)
        self.assertNotIn("rebuild-libc.sh", script)
        self.assertIn("libkodi-runtime.sprx libkodi-gl.sprx libc.prx", script)
        self.assertIn("ps5-loader-companion-proof.json", script)
        self.assertNotIn("$(RUNTIME)", (self.app / "Makefile").read_text())
        self.assertLess(script.index("build-native-kodi-app.py"),
                        script.index("build-native-title-launcher.py"))
        self.assertLess(script.index("build-native-title-launcher.py"),
                        script.index("self --sign"))
        self.assertIn('install -m 755 "$build/kodi-app/libkodi-app.sprx"', script)
        self.assertIn("ps5-kodi-app-proof.json", script)
        for name in ("build-native-kodi-app.py", "build-native-title-launcher.py",
                     "native_c_link.py", "native_runtime_sdk.py"):
            self.assertEqual((self.app / "vendor/ps5-runtime-tools" / name).read_bytes(),
                             (ROOT / "tools/ps5" / name).read_bytes())
        self.assertEqual((self.app / "tooling/native/title_native_launcher.cpp").read_bytes(),
                         (ROOT / "shims/native-app/title_native_launcher.cpp").read_bytes())
        self.assertEqual(set(record["startup_sources"]),
                         {"title_native_launcher.cpp", "kodi_app_entry.cpp", "title_native_startup.h"})
        for name in record["startup_sources"]:
            self.assertEqual((self.app / "tooling/native" / name).read_bytes(),
                             (ROOT / "shims/native-app" / name).read_bytes())
        self.assertEqual(record["sdk"], str(self.sdk.resolve()))
        converter = (self.app / "tooling/native/elf_object.cpp").read_text()
        self.assertNotIn('constexpr std::string_view suffix', converter)
        self.assertIn("apply_shared_provider_identity(stub);", converter)
        self.assertEqual([p["module_version"] for p in record["providers"]], [0x101, 0x101])
        header = (self.app / "tooling/native/shared_providers.hpp").read_text()
        self.assertIn('"libkodi-runtime", "libkodi-runtime", 257, 1', header)
        self.assertEqual({p.name for p in (self.app / "vendor/ps5-runtime").iterdir()},
                         {"libkodi-runtime.sprx", "libkodi-gl.sprx", "libc.prx"})
        self.assertEqual(record["loader_companion_sha256"],
                         MODULE.digest(self.app / "vendor/ps5-runtime/libc.prx"))
        self.assertEqual(record["wrap_symbols"], WRAPS)
        self.assertFalse(record["console_accepted"])
        response = (self.app / "vendor/ps5-runtime-imports.rsp").read_text()
        self.assertLess(response.index("libkodi-runtime"), response.index("libkodi-gl"))

    def test_rejects_sdk_tampering(self):
        (self.sdk / "include/stdio.h").write_text("changed")
        with self.assertRaisesRegex(ValueError, "shared SDK file changed"):
            MODULE.prepare(self.sdk, self.app)

    def test_rejects_unproven_runtime(self):
        self.receipt["runtime_proofs"]["libkodi-gl"]["sign_extract_equal"] = False
        self.write_receipt()
        with self.assertRaisesRegex(ValueError, "runtime does not match"):
            MODULE.prepare(self.sdk, self.app)

    def test_rejects_duplicate_tls_owner(self):
        (self.app / "src/shared_tls.c").touch()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            MODULE.prepare(self.sdk, self.app)

    def test_rejects_static_cpp_runtime(self):
        (self.app / "vendor/libkodi_group.a").write_text("GROUP (\n libc++.a\n)\n")
        with self.assertRaisesRegex(ValueError, "statically link"):
            MODULE.prepare(self.sdk, self.app)

    def test_rejects_changed_builder_template(self):
        with self.assertRaisesRegex(ValueError, "template"):
            MODULE.patch_builder(BUILDER.replace("objects=()", "objects=(changed)"), WRAPS)

    def test_preserves_exact_provider_versions_and_published_names(self):
        manifest = Path(self.providers[0]["manifest"])
        data = json.loads(manifest.read_text())
        data.update(module_version=0x203, library_version=7, library="runtime_exports")
        manifest.write_text(json.dumps(data))
        identities = MODULE.provider_identities(self.providers)
        sources = MODULE.patch_converter(CONVERTER, identities)
        header = sources["shared_providers.hpp"]
        self.assertIn('"libkodi-runtime", "runtime_exports", 515, 7', header)
        writer = sources["sce_module_writer.cpp"]
        self.assertIn("static_cast<std::uint64_t>(module.version)", writer)
        self.assertIn("static_cast<std::uint64_t>(library.version)", writer)
        self.assertNotIn("std::uint64_t{1} << 32", writer)
        self.assertIn("missing shared provider identity", writer)

    def test_rejects_changed_converter_template(self):
        sources = dict(CONVERTER)
        sources["sce_module_writer.cpp"] = ""
        with self.assertRaisesRegex(ValueError, "template"):
            MODULE.patch_converter(sources, MODULE.provider_identities(self.providers))

    def additional_provider(self, module="firmware"):
        manifest = self.sdk / "lib" / (module + ".json")
        data = {"module": module, "soname": module + ".sprx", "library": module,
                "module_version": 257, "library_version": 1,
                "import_library": {"path": module + ".so"}}
        manifest.write_text(json.dumps(data))
        (manifest.parent / (module + ".so")).write_bytes(b"verified linker fixture")
        return manifest, data

    def test_prepares_additional_title_provider(self):
        manifest, data = self.additional_provider()
        self.write_receipt()
        with patch.object(MODULE, "load_provider", return_value=data) as load:
            record = MODULE.prepare(self.sdk, self.app, [manifest])
        load.assert_called_once_with(manifest.resolve())
        self.assertEqual(record["additional_provider_manifests"],
                         [{"path": str(manifest.resolve()), "sha256": MODULE.digest(manifest)}])
        self.assertEqual(record["providers"][-1]["module"], "firmware")
        self.assertIn('"firmware.sprx", "firmware", "firmware", 257, 1',
                      (self.app / "tooling/native/shared_providers.hpp").read_text())
        self.assertEqual((self.app / ".deps/native/ps5-payload-sdk/target/lib/firmware.so")
                         .read_bytes(), b"verified linker fixture")

    def test_rejects_additional_shared_runtime_owner(self):
        manifest, data = self.additional_provider("libkodi-runtime")
        with patch.object(MODULE, "load_provider", return_value=data):
            with self.assertRaisesRegex(ValueError, "must not replace"):
                MODULE.title_providers(self.providers, [manifest])

    def test_rejects_duplicate_additional_provider(self):
        manifest, data = self.additional_provider()
        with patch.object(MODULE, "load_provider", return_value=data):
            with self.assertRaisesRegex(ValueError, "duplicate provider SONAME"):
                MODULE.title_providers(self.providers, [manifest, manifest])

    def test_shared_sdk_system_provider_is_not_added_twice(self):
        manifest, data = self.additional_provider()
        existing = {"module": data["module"], "soname": data["soname"],
                    "manifest": str(manifest), "library": str(manifest.parent / "firmware.so")}
        providers = [*self.providers, existing]
        with patch.object(MODULE, "load_provider", return_value=data):
            result, receipts = MODULE.title_providers(providers, [manifest])
            self.assertEqual(result, providers)
            self.assertEqual(receipts, [])
            changed = manifest.with_name("changed.json")
            changed.write_text(manifest.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "conflicts with shared SDK"):
                MODULE.title_providers(providers, [changed])

    def test_current_native_template(self):
        template = ROOT / "build/ps5-external/ps5-opengl/build/native-app/PPSA99005/tools/build.sh"
        if not template.is_file():
            self.skipTest("prepared native title template unavailable")
        self.describe.stop()
        script = self.directory / "build.sh"
        script.write_text(MODULE.patch_builder(template.read_text(), WRAPS))
        makefile = MODULE.patch_makefile((template.parent.parent / "Makefile").read_text())
        self.assertNotIn("app: $(RUNTIME)", makefile)
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        native = template.parent.parent / "tooling/native"
        sources = {name: (native / name).read_text() for name in CONVERTER}
        patched = MODULE.patch_converter(sources, MODULE.provider_identities(self.providers))
        self.assertIn("provider->module_version", patched["sce_module_writer.cpp"])


class PackageBuildStatusTest(unittest.TestCase):
    def test_build_failure_is_not_hidden_by_an_existing_executable(self):
        script = (ROOT / "tools/ps5/package.sh").read_text()
        section = script[script.index("build_result=0\n"):script.index("\nKODI_IMAGE=")]
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                app = directory / "app"
                (app / "vendor").mkdir(parents=True)
                (app / "vendor/kodi-whole.rsp").write_text("")
                (app / "Makefile").write_text(
                    "app:\n\tmkdir -p dist/TEST\n\tprintf executable > dist/TEST/eboot.bin\n" +
                    ("\tfalse\n" if fail else ""))
                result = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c",
                     'APP="$1"; STAGE="$2"; TITLE_ID=TEST\n' + section,
                     "package-status-test", str(app), str(directory)],
                    capture_output=True, text=True)
                self.assertTrue((app / "dist/TEST/eboot.bin").is_file())
                self.assertEqual(result.returncode, 1 if fail else 0, result.stderr)
                self.assertEqual("native build failed" in result.stdout, fail)


if __name__ == "__main__":
    unittest.main()
