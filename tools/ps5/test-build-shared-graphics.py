#!/usr/bin/env python3
"""Local-only regression tests; scratch data stays inside the project."""

import importlib.util
from pathlib import Path
import shutil
import unittest
import uuid
import io
import subprocess


SPEC = importlib.util.spec_from_file_location(
    "shared_graphics", Path(__file__).with_name("build-shared-graphics.py"))
GRAPHICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GRAPHICS)


class SharedGraphicsTests(unittest.TestCase):
    def test_meson_replays_configuration_with_pic(self):
        args = GRAPHICS.meson_arguments(
            "[options]\nbuildtype=release\nplatforms=\nb_staticpic=false\n"
            "c_args=-Wno-thread-safety-analysis\n[properties]\n"
            "cross_file=['/sdk/prospero.ini']\n")
        self.assertIn("-Dplatforms=", args)
        self.assertIn("-Dc_args=-Wno-thread-safety-analysis", args)
        self.assertIn("-Db_staticpic=true", args)
        self.assertNotIn("-Db_staticpic=false", args)
        self.assertEqual(args[-2:], ["--cross-file", "/sdk/prospero.ini"])

    def test_native_stdio_overlay_order_and_fingerprint(self):
        overlay = GRAPHICS.ROOT / "tools/ps5/sdk/include"
        if not (overlay / "stdio.h").exists():
            self.skipTest("requires the actual native stdio overlay")
        sdk = Path("/opt/ps5-payload-sdk")
        config = GRAPHICS.include_configuration(sdk, overlay)
        self.assertEqual(config["abi_profile"], "actual-ps5-frontend-opaque-stdio")
        self.assertIn(str(overlay / "stdio.h"), config["header_sha256"])
        self.assertIn("-I" + str(overlay), config["c_flags"])
        cxx = config["cxx_flags"]
        self.assertLess(cxx.index("-I" + str(sdk / "target/include/c++/v1")),
                        cxx.index("-I" + str(overlay)))
        self.assertIn("-nostdinc++", cxx)
        self.assertNotIn("-fuse-init-array", cxx)
        self.assertIn("-femulated-tls", cxx)
        self.assertIn("-fno-stack-protector", cxx)
        self.assertFalse(any(flag.startswith(("--target", "-D__PROSPERO__", "-D__SCE__"))
                             for flag in cxx))
        self.assertNotIn("-fshort-wchar", cxx)
        self.assertEqual(config["compiler_launcher"]["source_abi"], "actual-ps5-frontend")
        self.assertIn(str(GRAPHICS.ROOT / "tools/ps5/native-clang.py"),
                      config["c_compiler"])
        self.assertEqual(config["mesa_cpp_compiler"][-2:],
                         ["-isystem", str(sdk / "target/include")])
        self.assertFalse(config["mesa_feature_overrides"]["has_function_qsort_s"])
        source = GRAPHICS.ROOT / "build/ps5-external/ps5-opengl"
        if (source / "build/mesa-ps5-probe/meson-info/intro-buildoptions.json").exists():
            flags = GRAPHICS.mesa_include_arguments(source, config)
            self.assertIn("-nostdinc++", flags[1])
            self.assertNotEqual(
                GRAPHICS.stage_fingerprint(source, sdk, config)["sha256"],
                GRAPHICS.stage_fingerprint(source, sdk, {
                    **config, "header_sha256": {"changed-stdio.h": "changed"}})["sha256"])

    def test_actual_native_overlay_compiler_flags(self):
        sdk = Path("/opt/ps5-payload-sdk")
        overlay = GRAPHICS.ROOT / "tools/ps5/sdk/include"
        if not (sdk / "bin/prospero-clang++").exists() or not (overlay / "stdio.h").exists():
            self.skipTest("requires actual SDK compilers and native stdio overlay")
        flags = GRAPHICS.include_configuration(sdk, overlay)
        probes = [
            ("prospero-clang", "c", "c11", flags["c_flags"],
             '#include <stdio.h>\n_Static_assert(sizeof(fpos_t)==24, "native fpos");\n'
             '_Static_assert(_Alignof(fpos_t)==8, "native fpos alignment");\n'
             '_Static_assert(__BIGGEST_ALIGNMENT__==32, "native alignment");\n'
             '_Static_assert(__STDC_NO_THREADS__ && __STDC_NO_COMPLEX__, "native features");\n'
             '_Static_assert(sizeof(__WCHAR_TYPE__)==2, "PS5 wchar");\n'
             '_Static_assert(__FreeBSD__==9 && __PROSPERO__==1, "PS5 platform");\n'
             'int probe(FILE *stream) { return ferror(stream); }\n'),
            ("prospero-clang++", "c++", "c++17", flags["cxx_flags"],
             '#include <cstdio>\nstatic_assert(sizeof(fpos_t)==24, "native fpos");\n'
             'static_assert(alignof(fpos_t)==8, "native fpos alignment");\n'
             'static_assert(__BIGGEST_ALIGNMENT__==32, "native alignment");\n'
             'static_assert(sizeof(wchar_t)==2, "PS5 wchar");\n'
             'int probe(FILE *stream) { return std::ferror(stream); }\n')]
        for compiler, language, standard, options, source in probes:
            with self.subTest(compiler=compiler):
                result = subprocess.run(
                    [*flags["mesa_c_compiler" if language == "c" else "mesa_cpp_compiler"],
                     *options, "-Werror", "-std=" + standard,
                     "-fsyntax-only", "-x", language, "-"], input=source,
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        if (GRAPHICS.ROOT / "build/ps5-shared-runtime/core-native-abi/manifest.json").exists():
            link_config = GRAPHICS.mesa_link_configuration()
            self.assertEqual(link_config["providers"][0]["manifest"],
                             str(GRAPHICS.ROOT / "build/ps5-shared-runtime/core-native-abi/manifest.json"))
            result = subprocess.run(
                [*flags["mesa_c_compiler"], "-Wl,--version", *link_config["flags"]],
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("LLD", result.stdout)

    def test_full_group_parser(self):
        self.assertEqual(GRAPHICS.group_names(
            "/* driver */ GROUP ( runtime.a mesa.a psbc.a )"),
            ["runtime.a", "mesa.a", "psbc.a"])
        with self.assertRaises(ValueError):
            GRAPHICS.group_names("GROUP ( runtime.a runtime.a )")

    def test_tracked_ps5_clrx_recipe_only_changes_reviewed_platform_guards(self):
        source = GRAPHICS.ROOT / "build/ps5-external/ps5-opengl"
        original = source / "third_party/opengnm-psbc/src/amd/compiler/aco_print_asm.cpp"
        if not original.exists():
            self.skipTest("requires actual PSBC source")
        before = GRAPHICS.digest(original)
        scratch = GRAPHICS.ROOT / "build/ps5-shared-runtime" / ("test-" + uuid.uuid4().hex)
        target = scratch / "psbc/src/amd/compiler/aco_print_asm.cpp"
        target.parent.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, scratch)
        shutil.copy2(original, target)
        report = GRAPHICS.prepare_psbc_patch(source, scratch / "psbc", scratch)
        patched = target.read_text()
        self.assertEqual(before, GRAPHICS.digest(original))
        self.assertEqual(patched.count("#if !defined(_WIN32) && !defined(__PROSPERO__)"), 2)
        self.assertEqual(patched.count("#if defined(_WIN32) || defined(__PROSPERO__)"), 1)
        for old, new, count in GRAPHICS.PSBC_CLRX_GUARDS:
            patched = patched.replace(new, old)
        self.assertEqual(patched, original.read_text())
        self.assertEqual(report, GRAPHICS.prepare_psbc_patch(source, scratch / "psbc", scratch))
        self.assertIn("__PROSPERO__", Path(report["patch"]).read_text())
        with self.assertRaisesRegex(ValueError, "guards changed"):
            GRAPHICS.disable_ps5_clrx(original.read_text().replace("#ifdef _WIN32", "#if 0"))

    def test_actual_meson_link_probe_keeps_native_source_alignment(self):
        sdk = Path("/opt/ps5-payload-sdk")
        if not (GRAPHICS.ROOT / "build/ps5-shared-runtime/core-native-abi/manifest.json").exists():
            self.skipTest("requires actual native-ABI core import SDK")
        scratch = GRAPHICS.ROOT / "build/ps5-shared-runtime" / ("test-" + uuid.uuid4().hex)
        scratch.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, scratch)
        config = GRAPHICS.include_configuration(sdk, GRAPHICS.ROOT / "tools/ps5/sdk/include")
        link = GRAPHICS.mesa_link_configuration()
        for kind, extension in (("c", "c"), ("cpp", "cpp")):
            with self.subTest(language=kind):
                source, output = scratch / ("probe." + extension), scratch / (kind + ".elf")
                assertion = "_Static_assert" if kind == "c" else "static_assert"
                source.write_text(
                    assertion + '(__BIGGEST_ALIGNMENT__==32, "actual native frontend");\n'
                    + assertion + '(sizeof(__WCHAR_TYPE__)==2, "native wchar");\n'
                    'int main(void) { return 0; }\n')
                result = subprocess.run([
                    *config["mesa_" + kind + "_compiler"],
                    *config["c_flags" if kind == "c" else "cxx_flags"],
                    str(source), *link["flags"], "-o", str(output)],
                    text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_bytes()[:4], b"\x7fELF")
                self.assertFalse(output.with_name(output.name + ".ps5-probe.o").exists())
        assembly, obj = scratch / "probe.S", scratch / "assembly.o"
        assembly.write_text(".text\n.globl native_assembly_probe\nnative_assembly_probe:\nret\n")
        result = subprocess.run([
            *config["mesa_c_compiler"], *config["c_flags"],
            "-c", str(assembly), "-o", str(obj)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(obj.read_bytes()[:4], b"\x7fELF")

    def test_pic_commands_fail_closed(self):
        commands = GRAPHICS.compile_commands(
            "make: entering directory\n/sdk/prospero-clang -fPIC \\\n"
            " -c x.c -o x.o\n")
        GRAPHICS.require_pic(commands)
        launched = GRAPHICS.compile_commands(
            "/usr/bin/python3 /project/native-clang.py /sdk/prospero-clang++ "
            "-fPIC -femulated-tls -c x.cpp -o x.o\n")
        self.assertEqual(len(launched), 1)
        GRAPHICS.require_pic(launched)
        for command in ([], [["clang", "-c", "x.c"]],
                        [["clang", "-fPIC", "-fno-pic", "-c", "x.c"]]):
            with self.assertRaises(ValueError):
                GRAPHICS.require_pic(command)

    def test_group_imports_do_not_hide_strong_or_weak_imports(self):
        defined, required, weak = GRAPHICS.nm_symbols(
            "runtime.a(x.o):\n0000 T eglInitialize\n"
            " U sceAgcSubmit\n U mesa_internal\n w optional_hook\n"
            "mesa.a(y.o):\n0000 T mesa_internal\n")
        self.assertEqual(required - defined, {"sceAgcSubmit"})
        self.assertEqual(weak - defined, {"optional_hook"})

    def test_registry_core_removals_and_version_limit(self):
        registry = io.StringIO(
            '<registry><feature api="gl" number="1.0"><require>'
            '<command name="glKeep"/><command name="glOld"/></require></feature>'
            '<feature api="gl" number="3.2"><remove profile="core">'
            '<command name="glOld"/></remove></feature>'
            '<feature api="gl" number="4.7"><require>'
            '<command name="glFuture"/></require></feature></registry>')
        self.assertEqual(GRAPHICS.core_commands(registry), {"glKeep"})

    def test_native_tls_sections_symbols_and_relocations_are_rejected(self):
        records = GRAPHICS.tls_records([
            "File: libnative.a(native.o)\n",
            " [ 4] .tbss NOBITS 0000000000000000 000040 000008 00 WAT 0 0 8\n",
            " 7: 0000000000000000 8 TLS GLOBAL DEFAULT 4 native_tls\n",
            "0000000000000000 0000000700000016 R_X86_64_GOTTPOFF 0 native_tls - 4\n",
            "0000000000000000 0000000700000013 R_X86_64_TLSGD 0 native_tls - 4\n",
            " 8: 0000000000000000 0 NOTYPE GLOBAL DEFAULT UND __tls_get_addr\n"])
        self.assertEqual(records["native_tls_members"]["libnative.a(native.o)"], [
            "relocation:R_X86_64_GOTTPOFF", "relocation:R_X86_64_TLSGD",
            "section:.tbss", "symbol:__tls_get_addr", "symbol:native_tls"])

    def test_emutls_is_not_native_tls(self):
        records = GRAPHICS.tls_records([
            "File: libemulated.a(emulated.o)\n",
            " [ 4] .data.__emutls_v.value PROGBITS 0000 000040 000020 00 WA 0 0 8\n",
            " 7: 0000000000000000 32 OBJECT GLOBAL DEFAULT 4 __emutls_v.value\n",
            "00000000 00000000 R_X86_64_GOTPCRELX 0 __emutls_get_address - 4\n",
            " L (link order), O (extra OS processing required), G (group), T (TLS),\n"])
        self.assertFalse(records["native_tls_members"])
        self.assertEqual(records["emutls_members"]["libemulated.a(emulated.o)"], [
            "relocation:R_X86_64_GOTPCRELX", "symbol:__emutls_v.value"])

    @unittest.skipUnless(shutil.which("cc") and shutil.which("ar") and shutil.which("readelf")
                         and Path("/opt/ps5-payload-sdk/bin/prospero-clang").exists(),
                         "requires local ELF tools and the existing PS5 SDK")
    def test_real_native_and_emulated_tls_archive_inspection(self):
        scratch = GRAPHICS.ROOT / "build/ps5-shared-runtime" / ("test-" + uuid.uuid4().hex)
        scratch.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, scratch)
        source = scratch / "tls.c"
        source.write_text("__thread int value; int *get_value(void) { return &value; }\n")
        compilers = {
            "native": ["cc", "-fPIC"],
            "emulated": ["/opt/ps5-payload-sdk/bin/prospero-clang",
                         "-fPIC", "-femulated-tls"]}
        for model, compiler in compilers.items():
            with self.subTest(model=model):
                obj, archive = scratch / (model + ".o"), scratch / (model + ".a")
                subprocess.run([*compiler, "-c", str(source), "-o", str(obj)], check=True)
                subprocess.run(["ar", "rcs", str(archive), str(obj)], check=True)
                result = GRAPHICS.inspect_tls(archive, shutil.which("readelf"))
                self.assertEqual(bool(result["native_tls_members"]), model == "native")
                self.assertEqual(bool(result["emutls_members"]), model == "emulated")

    def test_snapshot_detects_cached_archive_and_source_changes(self):
        scratch = GRAPHICS.ROOT / "build/ps5-shared-runtime" / ("test-" + uuid.uuid4().hex)
        scratch.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, scratch)
        (scratch / "source.c").write_text("source")
        (scratch / "cached.a").write_bytes(b"archive")
        before = GRAPHICS.snapshot([scratch])
        (scratch / "cached.a").write_bytes(b"modified")
        after = GRAPHICS.snapshot([scratch])
        self.assertNotEqual(before, after)
        self.assertEqual(before[str(scratch / "source.c")],
                         after[str(scratch / "source.c")])


if __name__ == "__main__":
    unittest.main()
