#!/usr/bin/env python3
"""Small host/native tests; all scratch artifacts stay below the repository."""
# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
import uuid
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("native_kodi_app", HERE / "build-native-kodi-app.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "build" / ("test-native-kodi-app-" + uuid.uuid4().hex)
        self.work.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.work)

    def write(self, name, text=""):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def command(self, args):
        subprocess.run(list(map(str, args)), check=True, capture_output=True, text=True)

    def test_retained_order_and_unproven_stub_filter(self):
        paths = [self.write(n) for n in ("second.a", "unproven.so", "first.a", "second.o")]
        group = self.write("group.a", 'SEARCH_DIR("ignored")\nGROUP (\n' +
                           "\n".join(json.dumps(str(p)) for p in paths) + "\n)")
        parsed = builder.group_paths(group, self.work)
        self.assertEqual(parsed, paths)
        self.assertEqual([p.name for p in parsed if p.suffix != ".so"],
                         ["second.a", "first.a", "second.o"])

    def test_response_preserves_duplicates_and_quotes(self):
        source = self.write("with space.a")
        rsp = self.write("whole.rsp", f'"{source}"\n"{source}"\n')
        self.assertEqual(builder.response_paths(rsp, self.work), [source, source])
        rsp.write_text("-Lbad")
        with self.assertRaisesRegex(ValueError, "paths-only"):
            builder.response_paths(rsp, self.work)

    def test_reject_arbitrary_group_directives(self):
        for text in ("INPUT(foo.a)", "GROUP(foo.a) INCLUDE evil", "GROUP(@nested.rsp)"):
            with self.subTest(text=text):
                path = self.write("group.a", text)
                with self.assertRaises(ValueError):
                    builder.group_paths(path, self.work)

    def test_keeps_main_source_and_excludes_obsolete_runtime(self):
        for name in ("main.cpp", "shim.c", "title_shared_runtime.cpp",
                     "app_crt.cpp", "app_cpp_runtime.cpp"):
            self.write("src/" + name)
        self.assertEqual([p.name for p in builder.source_paths(self.work)], ["main.cpp", "shim.c"])

    def test_compiler_contract(self):
        args = builder.compile_command(self.work, self.work / "sdk",
                                       Path("main.cpp"), self.work / "main.o")
        for flag in ("-fPIC", "-femulated-tls", "-fno-stack-protector", "-nostdinc++"):
            self.assertIn(flag, args)
        self.assertIn("-I" + str(self.work / "sdk/include"), args)
        self.assertTrue(any(p.endswith("/bin/prospero-clang++") for p in args))
        self.assertNotIn("-fno-exceptions", args)  # Native CRT catches initializer failures.
        cargs = builder.compile_command(self.work, self.work / "sdk",
                                        Path("shim.c"), self.work / "shim.o")
        self.assertIn("-std=c11", cargs)
        self.assertNotIn("-nostdinc++", cargs)

    def test_shared_network_replaces_only_matching_private_implementation(self):
        network = self.write("src/libc_net.c", "existing network implementation")
        self.write("src/main.cpp")
        self.assertEqual([p.name for p in builder.source_paths(self.work, {})],
                         ["libc_net.c", "main.cpp"])
        core = {
            "link_contract": {"network": {
                "implementation": "ps5-libc-net-v1",
                "symbols": sorted(builder.NETWORK_SYMBOLS),
                "source_sha256": builder.digest(network)}},
            "symbols": [{"name": name, "info": 0x12} for name in builder.NETWORK_SYMBOLS],
        }
        self.assertEqual([p.name for p in builder.source_paths(self.work, core)], ["main.cpp"])
        network.write_text("customized network implementation")
        with self.assertRaisesRegex(ValueError, "private network implementation differs"):
            builder.source_paths(self.work, core)
        network.unlink()
        core["symbols"].pop()
        with self.assertRaisesRegex(ValueError, "missing function exports"):
            builder.source_paths(self.work, core)

    def test_shared_network_contract_rejects_unproven_abi(self):
        for contract in (42, {}, {"implementation": "unknown"}, {
                "implementation": "ps5-libc-net-v1",
                "symbols": sorted(builder.NETWORK_SYMBOLS), "source_sha256": "bad"}):
            with self.subTest(contract=contract):
                with self.assertRaisesRegex(ValueError, "invalid shared network ABI"):
                    builder.shared_network_symbols({"link_contract": {"network": contract}})

    def test_private_lifecycle_requires_hidden_distinct_functions(self):
        symbols = {"module_start": (0, 0x12, 2, 1, 0x3000, 6),
                   "ps5_kodi_crt_initialize": (0, 0x12, 2, 1, 0x4000, 32),
                   "module_stop": (0, 0x12, 2, 1, 0x1000, 6),
                   "ps5_kodi_crt_finalize": (0, 0x12, 2, 1, 0x2000, 32)}
        image = SimpleNamespace(symbols=lambda name: list(symbols.items()))
        builder.validate_private_lifecycle(image)
        for invalid in (None, (0, 0x11, 2, 1, 0x2000, 32),
                        (0, 0x12, 0, 1, 0x2000, 32), (0, 0x12, 2, 0, 0x2000, 32),
                        (0, 0x12, 2, 1, 0x1000, 32)):
            with self.subTest(invalid=invalid):
                bad = dict(symbols)
                if invalid is None:
                    del bad["ps5_kodi_crt_finalize"]
                else:
                    bad["ps5_kodi_crt_finalize"] = invalid
                with self.assertRaises(ValueError):
                    builder.validate_private_lifecycle(
                        SimpleNamespace(symbols=lambda name: list(bad.items())))

    def test_staged_support_precedes_repository_fallbacks(self):
        entry = self.write("tooling/native/kodi_app_entry.cpp", "staged entry")
        crt = self.write("tooling/native/native_module_crt.cpp", "staged CRT")
        support = builder.support_paths(self.work, self.work / "sdk")
        self.assertEqual(support["entry"], entry)
        self.assertEqual(support["crt"], crt)
        crt.unlink()
        sdk_crt = self.write("sdk/tools/src/native_module_crt.cpp", "SDK CRT")
        self.assertEqual(builder.support_paths(self.work, self.work / "sdk")["crt"], sdk_crt)

    def test_existing_outputs_never_deleted_or_overwritten(self):
        marker = self.write("out/proof.json", "keep")
        with self.assertRaisesRegex(ValueError, "not empty"):
            builder.prepare_output(marker.parent)
        self.assertEqual(marker.read_text(), "keep")
        link = self.work / "link"
        link.symlink_to(marker.parent)
        with self.assertRaisesRegex(ValueError, "symlink"):
            builder.prepare_output(link)

    def test_command_failure_is_durable(self):
        log = self.work / "build.log"
        with log.open("w") as stream:
            with self.assertRaises(subprocess.CalledProcessError):
                builder.run([sys.executable, "-c", "print('diagnostic'); raise SystemExit(7)"],
                            self.work, stream)
        self.assertIn("diagnostic", log.read_text())
        self.assertIn("COMMAND FAILED: exit 7", log.read_text())

    def test_input_validation_failure_is_durable(self):
        with self.assertRaisesRegex(ValueError, "missing executable"):
            builder.build(self.work, self.work / "sdk", self.work / "absent", self.work / "out")
        self.assertIn("BUILD FAILED: ValueError", (self.work / "out/build.log").read_text())

    def test_sdk_and_additional_manifest_hash_binding(self):
        sdk = self.work / "sdk"
        manifest = self.write("sdk/manifest.json", json.dumps({
            "schema": 1, "format": "ps5-native-addon-sdk", "provider_manifests": [],
            "wrap_symbols": ["exit"]}))
        inputs = {"sdk_manifest_sha256": "bad", "wrap_symbols": ["exit"]}
        with self.assertRaisesRegex(ValueError, "SDK manifest hash"):
            builder.providers(self.work, sdk, inputs)
        inputs["sdk_manifest_sha256"] = builder.digest(manifest)
        provider = self.write("provider.json", "{}")
        inputs["additional_provider_manifests"] = [{"path": str(provider), "sha256": "bad"}]
        with self.assertRaisesRegex(ValueError, "additional provider manifest hash"):
            builder.providers(self.work, sdk, inputs)

    def legacy_fixture(self, values=None, relocations=None):
        values = values if values is not None else [0xFFFFFFFFFFFFFFFF, 0, 0, 0]
        relocations = relocations if relocations is not None else [(0x2008, 8, 0x1010),
                                                                  (0x2010, 8, 0x1020)]
        blobs = {".ps5_legacy_init": struct.pack("<" + "Q" * len(values), *values),
                 ".ps5_legacy_fini": b"", ".rela.dyn": b"".join(
                     struct.pack("<QQq", *r) for r in relocations)}
        headers = {
            ".ps5_legacy_init": (0, 1, 3, 0x2000, 0, len(blobs[".ps5_legacy_init"]), 0, 0, 8, 0),
            ".ps5_legacy_fini": (0, 1, 3, 0x2100, 0, 0, 0, 0, 8, 0),
            ".rela.dyn": (0, 4, 2, 0x3000, 0, len(blobs[".rela.dyn"]), 0, 0, 8, 24)}
        image = SimpleNamespace(
            sections=headers, section=blobs.__getitem__,
            symbols=lambda name: [("ctor_a", (0, 0x12, 0, 1, 0x1010, 1)),
                                  ("ctor_b", (0, 0x12, 0, 1, 0x1020, 1))],
            phdrs=[(1, 5, 0, 0x1000, 0, 0x100, 0x100, 0x1000)])
        link_map = self.write("legacy.map", f"2000 2000 {len(values)*8:x} 8 archive.a(member.o):(.ctors)\n")
        return image, link_map

    def test_legacy_callback_order_sentinels_and_origins(self):
        record = builder.legacy_lifecycle(*self.legacy_fixture())
        self.assertEqual([c["symbols"] for c in record["init"]["callbacks"]],
                         [["ctor_b"], ["ctor_a"]])
        self.assertEqual([s.get("sentinel") for s in record["init"]["slots"]],
                         ["header", None, None, "terminator"])
        self.assertTrue(all(c["source"] == "archive.a(member.o)"
                            for c in record["init"]["callbacks"]))

    def test_legacy_malformed_tables_fail_closed(self):
        cases = [
            ([0xFFFFFFFFFFFFFFFF, 0, 0, 0], [(0x2008, 1, 0x1010), (0x2010, 8, 0x1020)]),
            ([0xFFFFFFFFFFFFFFFF, 0, 0, 0], [(0x2008, (1 << 32) | 8, 0x1010),
                                           (0x2010, 8, 0x1020)]),
            ([0xFFFFFFFFFFFFFFFF, 0, 0, 0], [(0x2008, 8, 0x9999), (0x2010, 8, 0x1020)]),
            ([0xFFFFFFFFFFFFFFFF, 0, 0, 0], [(0x2008, 8, 0x1010)]),
            ([0xFFFFFFFFFFFFFFFF, 0x1010, 0, 0], []),
            ([0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF], []),
            ([2, 0], []),
        ]
        for values, relocs in cases:
            with self.subTest(values=values, relocs=relocs):
                with self.assertRaises(ValueError):
                    builder.legacy_lifecycle(*self.legacy_fixture(values, relocs))
        image, link_map = self.legacy_fixture()
        link_map.write_text("")
        with self.assertRaisesRegex(ValueError, "missing origin"):
            builder.legacy_lifecycle(image, link_map)

    def test_host_legacy_callbacks_execute_in_abi_order(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            self.skipTest("host C++ compiler unavailable")
        fixture = self.write("legacy-host.cpp", r"""
#include <cstdlib>
#include <unistd.h>
extern "C" void ctor_a() { const char c = 'a'; (void)!write(1, &c, 1); }
extern "C" void ctor_b() { const char c = 'b'; (void)!write(1, &c, 1); }
extern "C" void dtor_a() { const char c = 'A'; (void)!write(1, &c, 1); }
extern "C" void dtor_b() { const char c = 'B'; (void)!write(1, &c, 1); }
asm(".data\n"
    ".global __ps5_legacy_init_start, __ps5_legacy_init_end\n"
    "__ps5_legacy_init_start:\n.quad -1, ctor_a, ctor_b, 0\n"
    "__ps5_legacy_init_end:\n"
    ".global __ps5_legacy_fini_start, __ps5_legacy_fini_end\n"
    "__ps5_legacy_fini_start:\n.quad -1, dtor_a, dtor_b, 0\n"
    "__ps5_legacy_fini_end:\n.text\n");
extern "C" [[noreturn]] void __wrap_exit(int status) { std::_Exit(status); }
extern "C" int ps5_kodi_crt_finalize(size_t, const void*) noexcept { return 0; }
extern "C" int ps5_kodi_crt_initialize(size_t, const void*) noexcept { return 0; }
extern "C" void sceKernelDebugOutText(int, const char*) {}
int main(int, char**, char**) { return 0; }
""")
        binary = self.work / "legacy-host"
        self.command([compiler, "-std=c++17", "-DPS5_PRIVATE_LEGACY_LIFECYCLE=1",
                      ROOT / "shims/native-app/kodi_app_entry.cpp", fixture, "-o", binary])
        result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout, "baAB")

    def test_prioritized_legacy_sections_fail_link(self):
        compiler = shutil.which("clang") or shutil.which("gcc")
        linker = shutil.which("ld.lld")
        if not compiler or not linker:
            self.skipTest("host compiler/LLD unavailable")
        source = self.write("priority.c", """
static void callback(void) {}
__attribute__((section(".ctors.12345"), used))
void (*unsupported_priority)(void) = callback;
""")
        obj = self.work / "priority.o"
        self.command([compiler, "-fPIC", "-c", source, "-o", obj])
        script = self.write("native-app.ld", builder.legacy_linker_script(HERE / "native-shared.ld"))
        result = subprocess.run([linker, "-shared", "-T", str(script), str(obj),
                                 "-o", str(self.work / "rejected.so")],
                                check=False, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not support prioritized legacy", result.stderr)

    def test_host_run_and_finalize_abi(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            self.skipTest("host C++ compiler unavailable")
        entry = ROOT / "shims/native-app/kodi_app_entry.cpp"
        obj = self.work / "entry.o"
        self.command([compiler, "-std=c++17", "-c", entry, "-o", obj])
        fixture = self.write("fixture.cpp", r"""
#include <cstdlib>
#include <cstring>
#include <cstddef>
extern "C" [[noreturn]] void kodi_native_run(int, char**, char**);
extern "C" int kodi_native_finalize() noexcept;
extern "C" int module_stop(size_t, const void*) noexcept;
extern "C" int ps5_kodi_crt_initialize(size_t, const void*) noexcept { return 0; }
extern "C" void sceKernelDebugOutText(int, const char*) {}
static int stops = 0;
extern "C" int ps5_kodi_crt_finalize(size_t n, const void* p) noexcept
{
  if (n || p) return 99;
  ++stops;
  return 0;
}
int fake_main(int argc, char** argv, char** envp)
{
  return argc == 3 && !strcmp(argv[0], "kodi") && !strcmp(envp[0], "A=B") ? 37 : 99;
}
extern "C" [[noreturn]] void __wrap_exit(int status) { std::_Exit(status); }
int main()
{
  if (module_stop(0, nullptr) != -1 || stops) return 97;
  if (kodi_native_finalize() || stops != 1) return 98;
  char name[] = "kodi", env[] = "A=B";
  char* argv[] = {name, nullptr};
  char* envp[] = {env, nullptr};
  kodi_native_run(3, argv, envp);
}
""")
        # Rename only the entry object's undefined main, not the host harness.
        objcopy = shutil.which("objcopy")
        if not objcopy:
            self.skipTest("host objcopy unavailable")
        self.command([objcopy, "--redefine-sym", "main=_Z9fake_mainiPPcS0_", obj])
        binary = self.work / "host-entry"
        self.command([compiler, "-std=c++17", fixture, obj, "-o", binary])
        result = subprocess.run([str(binary)], check=False)
        self.assertEqual(result.returncode, 37)

    def test_host_private_crt_finalize_idempotent_and_unload_rejected(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            self.skipTest("host C++ compiler unavailable")
        crt = self.work / "private-crt.o"
        self.command([compiler, "-std=c++17", "-Dmodule_stop=ps5_kodi_crt_finalize",
                      "-Dmodule_start=ps5_kodi_crt_initialize",
                      "-D__cxa_finalize=fixture_cxa_finalize",
                      "-D__dso_handle=fixture_dso_handle", "-c",
                      HERE / "native_module_crt.cpp", "-o", crt])
        fixture = self.write("private-crt-host.cpp", r"""
#include <stddef.h>
#include <stdint.h>
#include "title_native_startup.h"
extern "C" int module_start(size_t, const void*) noexcept;
extern "C" int module_stop(size_t, const void*) noexcept;
extern "C" int kodi_native_finalize() noexcept;
static int init_count, fini_count, globals_count, added, removed;
static bool fail_initialization;
static KodiNativeApplication application{};
extern "C" void fixture_init() {
  if (application.run || application.finalize) __builtin_trap();
  ++init_count;
  if (fail_initialization) throw 7;
}
extern "C" void sceKernelDebugOutText(int, const char*) {}
extern "C" void fixture_fini() { ++fini_count; }
extern "C" void fixture_cxa_finalize(void*) { ++globals_count; }
extern "C" void __unw_add_dynamic_eh_frame_section(uintptr_t) { ++added; }
extern "C" void __unw_remove_dynamic_eh_frame_section(uintptr_t) { ++removed; }
extern "C" [[noreturn]] void __wrap_exit(int) { __builtin_trap(); }
asm(".data\n"
    ".global __ps5_init_array_start, __ps5_init_array_end\n"
    "__ps5_init_array_start:\n.quad fixture_init\n__ps5_init_array_end:\n"
    ".global __ps5_fini_array_start, __ps5_fini_array_end\n"
    "__ps5_fini_array_start:\n.quad __ps5_finalize_globals, fixture_fini\n"
    "__ps5_fini_array_end:\n"
    ".global __ps5_eh_frame_start\n__ps5_eh_frame_start:\n.long 0\n.text\n");
int main(int argc, char**, char**)
{
  const KodiNativeStartup startup{KODI_NATIVE_STARTUP_ABI, sizeof(KodiNativeStartup), &application};
  if (argc > 1) {
    fail_initialization = true;
    if (module_start(sizeof(startup), &startup) != -1 ||
        application.run || application.finalize || init_count != 1 ||
        added != 1 || removed != 1 || globals_count != 1) return 6;
    return 0;
  }
  if (module_start(sizeof(startup), &startup) || init_count != 1 || added != 1 ||
      !application.run || !application.finalize) return 1;
  KodiNativeApplication duplicate{};
  const KodiNativeStartup repeated{KODI_NATIVE_STARTUP_ABI, sizeof(KodiNativeStartup), &duplicate};
  if (module_start(sizeof(repeated), &repeated) != -1 ||
      duplicate.run || duplicate.finalize || init_count != 1 || added != 1) return 7;
  if (module_stop(0, nullptr) != -1 || fini_count || removed) return 2;
  if (kodi_native_finalize() || fini_count != 1 || globals_count != 1 || removed != 1) return 3;
  if (kodi_native_finalize() || fini_count != 1 || globals_count != 1 || removed != 1) return 4;
  if (module_stop(0, nullptr) != -1) return 5;
  return 0;
}
""")
        binary = self.work / "private-crt-host"
        self.command([compiler, "-std=c++17", "-I" + str(ROOT / "shims/native-app"), fixture, crt,
                      ROOT / "shims/native-app/kodi_app_entry.cpp", "-o", binary])
        self.command([binary])
        self.command([binary, "constructor-failure"])

    def test_startup_validation_precedes_crt_and_never_publishes_on_failure(self):
        compiler = shutil.which("clang++") or shutil.which("g++")
        if not compiler:
            self.skipTest("host C++ compiler unavailable")
        fixture = self.write("startup-host.cpp", r"""
#include <cassert>
#include <cstdlib>
#include "title_native_startup.h"
extern "C" int module_start(size_t, const void*) noexcept;
static int calls;
static bool fail;
extern "C" int ps5_kodi_crt_initialize(size_t size, const void* arguments) noexcept {
  assert(size == 0 && arguments == nullptr);
  ++calls;
  return fail ? -7 : 0;
}
extern "C" int ps5_kodi_crt_finalize(size_t, const void*) noexcept { return 0; }
extern "C" void sceKernelDebugOutText(int, const char*) {}
extern "C" [[noreturn]] void __wrap_exit(int value) { std::_Exit(value); }
int main(int, char**, char**) {
  KodiNativeApplication application{};
  KodiNativeStartup startup{KODI_NATIVE_STARTUP_ABI, sizeof(KodiNativeStartup), &application};
  assert(module_start(0, nullptr) == -1);
  assert(module_start(sizeof(startup), nullptr) == -1);
  assert(module_start(sizeof(startup) - 1, &startup) == -1);
  startup.version = KODI_NATIVE_STARTUP_ABI + 1;
  assert(module_start(sizeof(startup), &startup) == -1);
  startup.version = KODI_NATIVE_STARTUP_ABI;
  startup.size = 0;
  assert(module_start(sizeof(startup), &startup) == -1);
  startup.size = sizeof(startup);
  startup.application = nullptr;
  assert(module_start(sizeof(startup), &startup) == -1);
  assert(calls == 0 && !application.run && !application.finalize);
  startup.application = &application;
  fail = true;
  assert(module_start(sizeof(startup), &startup) == -7);
  assert(calls == 1 && !application.run && !application.finalize);
  fail = false;
  assert(module_start(sizeof(startup), &startup) == 0);
  assert(calls == 2 && application.run && application.finalize);
  return 0;
}
""")
        binary = self.work / "startup-host"
        self.command([compiler, "-std=c++17", "-I" + str(ROOT / "shims/native-app"),
                      fixture, ROOT / "shims/native-app/kodi_app_entry.cpp", "-o", binary])
        self.command([binary])

    def test_small_native_build_with_verified_sdk(self):
        # Uses installed SDK/provider fixtures, never retained Kodi archives.
        staged = ROOT / "build/ps5-shared-title-stage/app"
        sdk = ROOT / "build/ps5-shared-runtime/metadata-load-20261025T1145Z/addon-sdk"
        tool = staged / "build/host/ps5-native-tool"
        if not tool.is_file() or not (sdk / "manifest.json").is_file():
            self.skipTest("verified staged SDK/native signer unavailable")
        self.write("src/local.c", "int local_shim(void) { return 0; }\n")
        payload = self.work / ".deps/native/ps5-payload-sdk"
        payload.parent.mkdir(parents=True)
        payload.symlink_to(staged / ".deps/native/ps5-payload-sdk", target_is_directory=True)
        vendor = self.work / "vendor"
        vendor.mkdir()
        for name in ("ps5-shared-runtime.json", "ps5-runtime-imports.rsp"):
            (vendor / name).write_bytes((staged / "vendor" / name).read_bytes())
        (vendor / "ps5-runtime").symlink_to(staged / "vendor/ps5-runtime", target_is_directory=True)
        main = self.write("main.c", """
#include <stdint.h>
static volatile int state;
static void legacy_ctor_a(void) { state = 1; }
static void legacy_ctor_b(void) { state = 2; }
static void legacy_dtor_a(void) { state = 3; }
static void legacy_dtor_b(void) { state = 4; }
typedef void (*callback)(void);
__attribute__((section(".ctors"), used)) callback constructors[] = {
  (callback)UINTPTR_MAX, legacy_ctor_a, legacy_ctor_b, 0};
__attribute__((section(".dtors"), used)) callback destructors[] = {
  (callback)UINTPTR_MAX, legacy_dtor_a, legacy_dtor_b, 0};
int main(int argc, char** argv, char** envp)
{ (void)argv; (void)envp; return argc; }
""")
        obj = self.work / "main.o"
        with (self.work / "compile.log").open("w") as log:
            builder.run(builder.compile_command(self.work, sdk, main, obj), self.work, log)
        (vendor / "kodi-whole.rsp").write_text(json.dumps(str(obj)) + "\n")
        # An object is sufficient for a tiny nonwhole GROUP; no archive rebuild.
        group_obj = self.work / "group.o"
        # Do not define main twice: the group contributes an unrelated object.
        small = self.write("group.c", "int retained_group_symbol(void) { return 1; }\n")
        with (self.work / "compile.log").open("a") as log:
            builder.run(builder.compile_command(self.work, sdk, small, group_obj), self.work, log)
        unproven = self.write("unproven.so", "not a usable shared library\n")
        (vendor / "libkodi_group.a").write_text(
            "GROUP (" + json.dumps(str(group_obj)) + " " + json.dumps(str(unproven)) + ")\n")
        output = self.work / "result"
        record = builder.build(self.work, sdk, tool, output)
        self.assertEqual(record["format"], builder.FORMAT)
        self.assertFalse(record["console_accepted"])
        self.assertTrue(record["process_lifetime"])
        self.assertTrue(record["native_unload_rejected"])
        self.assertEqual(set(record["exports"]), set(builder.ENTRIES))
        self.assertTrue(record["sign_extract"]["exact_match"])
        self.assertEqual(record["native_sha256"], record["sign_extract"]["extracted_sha256"])
        self.assertEqual((output / "extracted.elf").read_bytes(), (output / "native.elf").read_bytes())
        ownership = record["import_ownership"]
        self.assertEqual(ownership["needed"],
                         ["libkodi-runtime.sprx", "libSceLibcInternal.sprx", "libkernel.sprx"])
        imports = {p["name"] for p in ownership["imports"]}
        self.assertIn("__wrap_exit", imports)
        self.assertIn("__cxa_finalize", imports)
        self.assertNotIn("main", imports)
        self.assertTrue((output / "proof.json").is_file())
        self.assertFalse((output / "manifest.json").exists())  # Not an add-on import SDK.
        image = builder.elf.Elf((output / "linked.so").read_bytes())
        symbols = dict(image.symbols(".symtab"))
        marker = symbols["__ps5_native_process_lifetime_v1"]
        self.assertEqual(marker[1] & 15, 1)
        self.assertEqual(marker[2], 2)
        self.assertEqual(marker[5], 17)
        self.assertEqual(image.memory(marker[4], marker[5]), b"PS5-NATIVE-LIFE\x01\0")
        self.assertEqual(symbols["ps5_kodi_crt_finalize"][2], 2)
        self.assertEqual(symbols["module_stop"][2], 2)
        legacy = record["legacy_lifecycle"]
        self.assertEqual([c["symbols"] for c in legacy["init"]["callbacks"]],
                         [["legacy_ctor_b"], ["legacy_ctor_a"]])
        self.assertEqual([c["symbols"] for c in legacy["fini"]["callbacks"]],
                         [["legacy_dtor_a"], ["legacy_dtor_b"]])
        staged_tools = vendor / "ps5-runtime-tools"
        staged_tools.mkdir()
        for name in ("build-native-kodi-app.py", "native-clang.py", "native-shared.ld",
                     "native_c_link.py", "native_shared_link.py", "native_runtime_sdk.py"):
            shutil.copy2(HERE / name, staged_tools / name)
        local_native = self.work / "tooling/native"
        local_native.mkdir(parents=True)
        shutil.copy2(ROOT / "shims/native-app/kodi_app_entry.cpp", local_native)
        shutil.copy2(ROOT / "shims/native-app/title_native_startup.h", local_native)
        shutil.copy2(HERE / "native_module_crt.cpp", local_native)
        staged_output = self.work / "staged-result"
        result = subprocess.run(
            [sys.executable, str(staged_tools / "build-native-kodi-app.py"),
             "--app", str(self.work), "--sdk", str(sdk), "--native-tool", str(tool),
             "--output", str(staged_output)], check=True, capture_output=True, text=True)
        staged_record = json.loads(result.stdout)
        self.assertTrue(staged_record["sign_extract"]["exact_match"])
        source_names = {item["path"] for item in staged_record["sources"]}
        self.assertIn(str(local_native / "kodi_app_entry.cpp"), source_names)
        self.assertIn(str(local_native / "native_module_crt.cpp"), source_names)


if __name__ == "__main__":
    unittest.main()
