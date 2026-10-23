#!/usr/bin/env python3
"""Tests use the actual staged API and sources; no functional fake providers."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid
from prepare_runtime_archive import archive_members
from native_runtime_sdk import prove_provider, validate_stub


SPEC = importlib.util.spec_from_file_location(
    "graphics_module", Path(__file__).with_name("build-shared-graphics-module.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PREPARATION = Path(os.environ.get(
    "PS5_NATIVE_RUNTIME_SDK", MODULE.ROOT / "build/ps5-native/sdk")).resolve().parent


class GraphicsModuleTests(unittest.TestCase):
    def test_header_parser_ignores_comment_only_symbols(self):
        self.assertEqual(MODULE.header_names(
            "/* glNotADeclaration(); */\nGLAPI void APIENTRY glClear(int mask);\n"
            "// eglNotADeclaration();\nEGLAPI int EGLAPIENTRY eglInitialize(void);\n"),
            {"glClear", "eglInitialize"})

    def test_archive_inventory_preserves_weak_undefined(self):
        defined, functions, strong, weak = MODULE.nm_inventory(
            "a.o:\n000000 T glClear\n U internal\n w optional\n"
            "b.o:\n000000 T internal\n U external\n")
        self.assertEqual(strong, {"external"})
        self.assertEqual(weak, {"optional"})
        self.assertIn("glClear", functions)
        self.assertIn("internal", defined)

    def test_all_nine_allocator_wraps_are_mandatory_even_for_diagnostics(self):
        profile = MODULE.wrapper_profile({"__wrap_chdir", "__wrap_fopen"})
        self.assertFalse(profile["qualified"])
        self.assertEqual(set(profile["missing"]), {
            "__wrap_" + name for name in MODULE.ALLOCATOR_WRAPS})
        self.assertTrue(MODULE.ALLOCATOR_WRAPS <= set(profile["wraps"]))
        self.assertNotIn("reallocarray", profile["wraps"])
        partial = MODULE.wrapper_profile({
            "__wrap_malloc", "__wrap_calloc", "__wrap_realloc", "__wrap_free"})
        self.assertFalse(partial["qualified"])
        self.assertEqual(len(partial["missing"]), 5)
        complete = MODULE.wrapper_profile({"__wrap_" + name for name in MODULE.ALLOCATOR_WRAPS})
        self.assertTrue(complete["qualified"])
        self.assertFalse(complete["missing"])

    def test_actual_final_core_satisfies_all_nine_allocator_wrappers(self):
        manifest = MODULE.ROOT / "build/ps5-shared-runtime/core-unified-tls/manifest.json"
        if not manifest.exists():
            self.skipTest("requires built final heap-aware core SDK")
        provider = MODULE.load_provider(manifest)
        names = {symbol["name"] for symbol in provider["symbols"]}
        contract = json.loads(manifest.read_text())["link_contract"]
        profile = MODULE.wrapper_profile(names, contract)
        self.assertTrue(profile["qualified"])
        self.assertEqual(set(profile["wraps"]), set(contract["wrap_symbols"]))
        self.assertTrue({"chdir", "fopen"} <= set(profile["wraps"]))
        self.assertIn("pthread_create", profile["wraps"])
        self.assertIn("exit", profile["wraps"])
        self.assertIn("ps5_shared_heap_initialize", names)

    def test_declared_common_wraps_not_inferred_from_every_available_export(self):
        contract = {
            "compiler_abi": "ps5-sdk-native-frontend-v1", "process_lifetime": True,
            "stdio": {"opaque_file": True, "fpos_size": 24, "fpos_alignment": 8},
            "wrap_symbols": sorted(MODULE.ALLOCATOR_WRAPS | {"chdir", "fopen"})}
        names = {"__wrap_" + name for name in contract["wrap_symbols"]}
        profile = MODULE.wrapper_profile(names | {"__wrap_undeclared"}, contract)
        self.assertNotIn("undeclared", profile["wraps"])
        partial = MODULE.wrapper_profile(names - {"__wrap_chdir"}, contract)
        self.assertFalse(partial["qualified"])
        self.assertEqual(partial["missing"], ["__wrap_chdir"])
        self.assertIn("chdir", partial["wraps"])
        thread_contract = {**contract, "wrap_symbols": contract["wrap_symbols"] + ["pthread_create"]}
        thread_missing = MODULE.wrapper_profile(names, thread_contract)
        self.assertFalse(thread_missing["qualified"])
        self.assertEqual(thread_missing["missing"], ["__wrap_pthread_create"])
        self.assertIn("pthread_create", thread_missing["wraps"])
        self.assertTrue(MODULE.wrapper_profile(
            names | {"__wrap_pthread_create"}, thread_contract)["qualified"])
        exit_contract = {**thread_contract, "wrap_symbols": thread_contract["wrap_symbols"] + ["exit"]}
        exit_missing = MODULE.wrapper_profile(names | {"__wrap_pthread_create"}, exit_contract)
        self.assertFalse(exit_missing["qualified"])
        self.assertEqual(exit_missing["missing"], ["__wrap_exit"])
        self.assertIn("exit", exit_missing["wraps"])
        for invalid in (
                {**contract, "compiler_abi": "generic-freebsd"},
                {**contract, "process_lifetime": False},
                {**contract, "stdio": {**contract["stdio"], "fpos_alignment": 4}},
                {**contract, "wrap_symbols": sorted(MODULE.ALLOCATOR_WRAPS)},
                {**contract, "wrap_symbols": contract["wrap_symbols"] + ["fopen"]}):
            with self.subTest(contract=invalid), self.assertRaises(ValueError):
                MODULE.wrapper_profile(names, invalid)

    def test_actual_subprocess_feature_report_identifies_aco_without_fake_replacement(self):
        source = MODULE.ROOT / "build/ps5-external/ps5-opengl"
        if not (source / "third_party/opengnm-psbc/src/amd/compiler/aco_print_asm.cpp").exists():
            self.skipTest("requires actual ACO sources")
        report = MODULE.subprocess_evidence(source)
        self.assertEqual(report["archive"], "libpsbc.ps5.a")
        self.assertTrue(any("popen(" in item["text"] for item in report["evidence"]))
        self.assertTrue(any("pclose(" in item["text"] for item in report["evidence"]))
        self.assertIn("__PROSPERO__", report["resolution"])
        self.assertIn("LLVM/IR", report["resolution"])

    @unittest.skipUnless(shutil.which("cc") and shutil.which("ld.lld"), "requires host C compiler/LLD")
    def test_undefined_weak_null_is_linker_absolute_zero_without_function_body(self):
        scratch = MODULE.ROOT / "build/ps5-shared-runtime" / ("test-" + uuid.uuid4().hex)
        scratch.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, scratch)
        source, library, exports = scratch / "weak.c", scratch / "weak.so", scratch / "exports.map"
        source.write_text(
            "extern void optional(void) __attribute__((weak));\n"
            "int probe(void) { if (optional) optional(); return optional != 0; }\n")
        exports.write_text("{ global: probe; local: *; };\n")
        command = ["cc", "-fuse-ld=lld", "-std=c11", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                   "-nostdlib", "-nodefaultlibs", str(source),
                   "-Wl,--no-undefined", "-Wl,--version-script=" + str(exports), "-o", str(library)]
        subprocess.run(command, check=True)
        nulls = MODULE.optional_weak_nulls(library.read_bytes(), [])
        self.assertEqual([row["name"] for row in nulls], ["optional"])
        self.assertFalse(MODULE.optional_weak_nulls(
            library.read_bytes(), [{"symbols": [{"name": "optional", "info": 34}]}]))
        subprocess.run([*command, "-Wl,--no-relax", "-Wl,--defsym=optional=0"], check=True)
        self.assertFalse(MODULE.optional_weak_nulls(library.read_bytes(), []))
        symbol = dict(MODULE.Elf(library.read_bytes()).symbols(".symtab"))["optional"]
        self.assertEqual(symbol[3], 0xFFF1)
        self.assertEqual(symbol[4], 0)
        subprocess.run([MODULE.sys.executable, "-c",
                        "import ctypes,sys; assert ctypes.CDLL(sys.argv[1]).probe()==0",
                        str(library)], check=True)

    def test_old_file_layout_stage_is_never_promoted(self):
        with self.assertRaisesRegex(ValueError, "complete native stdio overlay rebuild"):
            MODULE.require_native_stdio({"stage": "relocatable-pic-archive-group"},
                                        MODULE.ROOT / "tools/ps5/sdk/include")

    def test_actual_native_abi_stage_fingerprint_and_no_legacy_stdio_helpers(self):
        stage = MODULE.ROOT / "build/ps5-shared-runtime/graphics-ps5-native-abi"
        if not (stage / "build.json").exists():
            self.skipTest("requires completed actual native-ABI rebuild")
        report = json.loads((stage / "build.json").read_text())
        if report["stage"] != "relocatable-pic-archive-group":
            self.skipTest("native-ABI rebuild not yet complete")
        proof = MODULE.require_native_stdio(report, MODULE.ROOT / "tools/ps5/sdk/include")
        self.assertGreater(proof["compile_commands_verified"], 478)
        imports = json.loads((stage / "imports.json").read_text())
        self.assertFalse(set(imports["strong"]) & {"__srget", "__swbuf", "__srefill",
                                                  "__swsetup", "__sflush", "__sF"})

    def test_macro_approximated_stage_is_not_actual_ps5_source_abi(self):
        stage = MODULE.ROOT / "build/ps5-shared-runtime/graphics-native-abi/build.json"
        if not stage.exists():
            self.skipTest("requires historical macro-approximated diagnostic stage")
        with self.assertRaisesRegex(ValueError, "complete native stdio overlay rebuild"):
            MODULE.require_native_stdio(json.loads(stage.read_text()),
                                        MODULE.ROOT / "tools/ps5/sdk/include")

    def test_actual_strict_full_graphics_link_retains_crt_without_subprocess_replacements(self):
        output = PREPARATION / "graphics"
        if not (output / "diagnostic-linked.so").exists():
            self.skipTest("requires actual strict full native graphics link")
        report = json.loads((output / "build.json").read_text())
        self.assertEqual(report["strict_link"]["exit_code"], 0)
        self.assertFalse(report["strict_link"]["unresolved_diagnostics"])
        validation = MODULE.verify_exports(
            (output / "diagnostic-linked.so").read_bytes(), report["public_api"])
        self.assertEqual(validation["exports"], 1458)
        self.assertEqual(validation["gl46_core"], 657)
        self.assertEqual(validation["egl"], 34)
        image = MODULE.Elf((output / "diagnostic-linked.so").read_bytes())
        symbols = dict(image.symbols(".symtab"))
        marker = symbols["__ps5_native_lifecycle_v1"]
        self.assertEqual(marker[1] & 15, 1)
        self.assertEqual(marker[2], 2)
        self.assertTrue(marker[3])
        self.assertFalse(set(report["public_api"]["exports"]) & {
            "__wrap_popen", "__wrap_pclose", "__wrap_system"})
        self.assertFalse(set(symbols) & {"__wrap_popen", "__wrap_pclose", "__wrap_system"})
        self.assertFalse({"popen", "pclose", "system"} & {
            symbol["name"] for symbol in validation["undefined"]})

    def test_actual_native_graphics_sdk_proves_every_full_public_export(self):
        output = PREPARATION / "graphics"
        if not (output / "manifest.json").exists():
            self.skipTest("requires actual proven native graphics SDK")
        manifest = MODULE.load_provider(output / "manifest.json")
        report = json.loads((output / "build.json").read_text())
        native = output / "native/libkodi-gl.elf"
        symbols = prove_provider((output / "diagnostic-linked.so").read_bytes(),
                                 native.read_bytes(), "libkodi-gl")
        self.assertEqual(set(symbols), set(report["public_api"]["exports"]))
        self.assertEqual(len(symbols), 1458)
        validate_stub((output / "lib/libkodi-gl.so").read_bytes(), symbols, "libkodi-gl")
        self.assertEqual(manifest["provider"]["native_elf_sha256"], MODULE.sha256(native))
        self.assertFalse(manifest["runtime_loadable"])
        self.assertFalse(manifest["qualification"]["native_loader_accepted"])
        command = next(item["argv"] for item in report["commands"]
                       if item["phase"] == "strict-link-weak-null")
        self.assertIn("--no-relax", command)
        self.assertEqual({arg.removeprefix("--wrap=") for arg in command
                          if arg.startswith("--wrap=")},
                         set(manifest["link_contract"]["wrap_symbols"]))
        self.assertIn("pthread_create", manifest["link_contract"]["wrap_symbols"])
        self.assertIn("exit", manifest["link_contract"]["wrap_symbols"])
        imports = {symbol["name"] for symbol in manifest["dependencies"]["imports"]}
        self.assertIn("__wrap_pthread_create", imports)
        self.assertNotIn("pthread_create", imports)

    @unittest.skipUnless((MODULE.ROOT / "build/ps5-shared-runtime/graphics/lib").exists(),
                         "requires the existing real PIC graphics stage")
    def test_actual_complete_public_graphics_surface(self):
        source = MODULE.ROOT / "build/ps5-external/ps5-opengl"
        archives = sorted((MODULE.ROOT / "build/ps5-shared-runtime/graphics/lib").glob("*.a"))
        self.assertEqual(len(archives), 19)
        output = subprocess.check_output(
            ["/opt/ps5-payload-sdk/bin/llvm-nm", "-g", *map(str, archives)], text=True)
        defined, functions, _, _ = MODULE.nm_inventory(output)
        api = MODULE.public_surface(source, defined, functions)
        self.assertEqual(len(api["gl46_core"]), 657)
        self.assertEqual(len(api["egl"]), 34)
        self.assertTrue(set(api["gl46_core"]) <= set(api["gl"]))
        self.assertEqual(set(api["ps5"]), MODULE.PS5_API)
        self.assertNotIn("ps5_opengl_register_gl_consumer", api["exports"])
        self.assertNotIn("ps5_screen_create", api["exports"])
        self.assertNotIn("__emutls_get_address", api["exports"])
        self.assertEqual(len(api["exports"]), len(set(api["exports"])))

    @unittest.skipUnless((MODULE.ROOT / "build/ps5-shared-runtime/graphics/lib").exists(),
                         "requires the real materialized Mesa archive")
    def test_actual_mesa_member_names_can_be_prepared_without_code_changes(self):
        path = MODULE.ROOT / "build/ps5-shared-runtime/graphics/lib/libglapi_bridge.a"
        original = path.read_bytes()
        normalized, renames = MODULE.safe_archive_names(original)
        members, _ = archive_members(normalized)
        self.assertTrue(renames)
        self.assertEqual(len(normalized), len(original))
        self.assertTrue(all("/" not in name for name in members))
        for payload in members.values():
            self.assertIn(payload, original)

    @unittest.skipUnless((MODULE.ROOT / "build/ps5-shared-runtime/graphics-module/build.json").exists(),
                         "requires the actual diagnostic link execution")
    def test_actual_unqualified_legacy_startup_is_preserved(self):
        report = json.loads((MODULE.ROOT /
                            "build/ps5-shared-runtime/graphics-module/build.json").read_text())
        for item in report.get("startup_blockers", []):
            original = Path(item["original_archive"]).read_bytes()
            normalized, _ = MODULE.safe_archive_names(original)
            self.assertEqual(Path(item["output"]).read_bytes(), normalized)
            members, _ = archive_members(normalized)
            self.assertTrue(any(".ctors" in MODULE.Elf(data).sections for data in members.values()))
        for command in report["commands"]:
            if command["phase"] == "strict-link":
                self.assertIn("--no-undefined", command["argv"])
                self.assertIn("--start-group", command["argv"])
                self.assertIn("--end-group", command["argv"])
                self.assertNotIn("--allow-shlib-undefined", command["argv"])
                self.assertFalse(any(Path(arg).name in
                                     ("libc.a", "libc++.a", "libc++abi.a", "libunwind.a")
                                     for arg in command["argv"]))


if __name__ == "__main__":
    unittest.main()
