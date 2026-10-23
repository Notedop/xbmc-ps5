#!/usr/bin/env python3
"""Compile import-bearing metadata fixtures through normal build_addon and real FSELF signing.

These tests validate packaging/link metadata, not console execution or runtime ABI.
Scratch files stay in the repository build directory.
"""
# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import unittest
import uuid
import xml.etree.ElementTree as ET
import zipfile

import addon_repository as repository
import native_runtime_sdk as sdk
from addon_build import _autoconf_site
import native_shared_link as link
from native_c_link import Elf, SYM, cstring, nid

ROOT = Path(__file__).resolve().parents[2]
LLD = shutil.which("ld.lld")
CMAKE = shutil.which("cmake")
PAYLOAD = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
CC = str(PAYLOAD / "bin/prospero-clang") if (PAYLOAD / "bin/prospero-clang").is_file() else None
CXX = str(PAYLOAD / "bin/prospero-clang++") if (PAYLOAD / "bin/prospero-clang++").is_file() else None
SIGNER = ROOT / "build/ps5-external/ps5-opengl/build/native-app/PPSA99005/build/host/ps5-native-tool"
ABI_FLAGS = ("-fPIC", "-femulated-tls", "-fno-stack-protector")
CORE_WRAP_SYMBOLS = ("malloc", "calloc", "realloc", "free", "posix_memalign",
                     "malloc_usable_size", "aligned_alloc", "memalign", "reallocf",
                     "chdir", "fopen", "pthread_create", "exit")
CXX_ABI_SOURCE = """
#include <cstddef>
struct alignas(32) NativeWide { wchar_t text[5]; };
extern "C" {
NativeWide native_wide;
wchar_t native_character;
unsigned char native_max_align[alignof(std::max_align_t)];
}
"""
SPEC = importlib.util.spec_from_file_location("addon_builder",
                                             Path(__file__).with_name("build-native-shared-addon.py"))
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@unittest.skipUnless(CC and CXX and LLD and CMAKE and SIGNER.is_file() and
                     (PAYLOAD / "target/include/c++/v1/__config").is_file(),
                     "PS5 SDK compilers, LLD, CMake and native signer required")
class NativeAddonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = ROOT / "build" / ("test-native-shared-addon-" + uuid.uuid4().hex)
        cls.root.mkdir(parents=True)
        cls.sdk = cls.root / "sdk"
        tools = cls.sdk / "tools"
        tools.mkdir(parents=True)
        for name in ("native_shared_link.py", "native_runtime_sdk.py", "native_c_link.py",
                     "native_system_provider.py", "native_module_crt.cpp", "native-shared.ld",
                     "build-native-shared-addon.py", "native-clang.py"):
            shutil.copyfile(ROOT / "tools/ps5" / name, tools / name)
        shutil.copy2(SIGNER, tools / "ps5-native-tool")
        for name in ("PS5NativeRuntimeConfig.cmake", "NativeSharedAddon.cmake", "PS5NativeABI.cmake"):
            shutil.copyfile(ROOT / "cmake/scripts/ps5" / name, cls.sdk / name)
        (cls.sdk / "include").mkdir()
        (cls.sdk / "include/stdio.h").write_text("#include_next <stdio.h>\n")
        shutil.copyfile(ROOT / "tools/ps5/native_abi_check.h", cls.sdk / "include/ps5_native_abi.h")
        baseline = cls.root / "native-abi-baseline.cpp"
        baseline.write_text(CXX_ABI_SOURCE)
        baseline_object = baseline.with_suffix(".o")
        cls.run_command(CXX, *ABI_FLAGS, "-std=c++17", "-nostdinc++",
                        "-I" + str(PAYLOAD / "target/include/c++/v1"),
                        "-include", ROOT / "tools/ps5/native_abi_check.h",
                        "-c", baseline, "-o", baseline_object)
        cls.baseline_symbols = dict(Elf(baseline_object.read_bytes()).symbols(".symtab"))
        cls.provider("libkodi-runtime", """
          void __cxa_finalize(void *p) {}
          int __cxa_atexit(void *f, void *p, void *d) { return 0; }
          int __cxa_thread_atexit(void *f, void *p, void *d) { return 0; }
          void *__emutls_get_address(void *p) { return p; }
          void __unw_add_dynamic_eh_frame_section(unsigned long p) {}
          void __unw_remove_dynamic_eh_frame_section(unsigned long p) {}
          void *__cxa_begin_catch(void *p) { return p; }
          void __cxa_end_catch(void) {}
          int __gxx_personality_v0(void) { return 0; }
          void _ZSt9terminatev(void) {}
          void *__wrap_malloc(unsigned long n) { return (void*)n; }
          void __wrap_free(void *p) {}
          void *__wrap_calloc(unsigned long n, unsigned long s) { return (void*)(n*s); }
          void *__wrap_realloc(void *p, unsigned long n) { return p; }
          int __wrap_posix_memalign(void **p, unsigned long a, unsigned long n) { return 0; }
          unsigned long __wrap_malloc_usable_size(void *p) { return 0; }
          void *__wrap_aligned_alloc(unsigned long a, unsigned long n) { return (void*)n; }
          void *__wrap_memalign(unsigned long a, unsigned long n) { return (void*)n; }
          void *__wrap_reallocf(void *p, unsigned long n) { return p; }
          int __wrap_chdir(const char *p) { return 0; }
          void *__wrap_fopen(const char *p, const char *m) { return (void*)p; }
          int __wrap_pthread_create(void) { return 0; }
          void __wrap_exit(int code) {}
          int getaddrinfo(void) { return 0; }
          void freeaddrinfo(void) {}
          int getnameinfo(void) { return 0; }
          const char *gai_strerror(int code) { return ""; }
          int gethostname(void) { return 0; }
          void *gethostbyname(void) { return 0; }
          int *__h_errno(void) { return 0; }
          int __wrap_fixture_extra(void) { return 0; }
          int runtime_compute(int x) { return x + 42; }
        """)
        core_manifest = cls.sdk / "libkodi-runtime/manifest.json"
        core = json.loads(core_manifest.read_text())
        core["link_contract"] = {"wrap_symbols": list(CORE_WRAP_SYMBOLS),
                                 "compiler_abi": "ps5-sdk-native-frontend-v1",
                                 "process_lifetime": True,
                                 "stdio": {"opaque_file": True, "fpos_size": 24,
                                           "fpos_alignment": 8},
                                 "network": {"implementation": "ps5-libc-net-v1",
                                             "symbols": sorted((
                                                 "getaddrinfo", "freeaddrinfo", "getnameinfo",
                                                 "gai_strerror", "gethostname",
                                                 "gethostbyname", "__h_errno")),
                                             "source_sha256": "0" * 64}}
        core_manifest.write_text(json.dumps(core))
        cls.provider("libkodi-gl", "void glFixture(void) {}")
        cls.provider("libkernel", "int unused_kernel(void) { return 0; }")
        cls.descriptor = {"schema": 1, "format": "ps5-native-addon-sdk",
                          "provider_manifests": [name + "/manifest.json" for name in
                                                 ("libkodi-runtime", "libkodi-gl", "libkernel")],
                          "wrap_symbols": list(CORE_WRAP_SYMBOLS)}
        (cls.sdk / "manifest.json").write_text(json.dumps(cls.descriptor))
        cls.depends = cls.root / "depends"
        cls.run_command(CMAKE, "-DCORE_SOURCE_DIR=" + str(ROOT),
                        "-DADDON_DEPENDS_PATH=" + str(cls.depends),
                        "-DCORE_SYSTEM_NAME=freebsd", "-DCORE_PLATFORM_NAME=ps5",
                        "-DCMAKE_CXX_STANDARD=17", "-DCMAKE_CXX_STANDARD_REQUIRED=ON",
                        "-DPS5_NATIVE_RUNTIME_SDK=" + str(cls.sdk),
                        "-P", ROOT / "cmake/scripts/common/PrepareEnv.cmake")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    @staticmethod
    def run_command(*args):
        result = subprocess.run(list(map(str, args)), capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(f"{args}\n{result.stdout}\n{result.stderr}")
        return result.stdout

    @classmethod
    def shared_source(cls, module, body, flags=(), libraries=()):
        source = cls.root / (module + ".c")
        source.write_text("""
          __attribute__((visibility("hidden"))) int module_start(void) { return 0; }
          __attribute__((visibility("hidden"))) int module_stop(void) { return 0; }
        """ + body)
        obj, elf, native = (source.with_suffix(s) for s in (".o", ".so", ".elf"))
        cls.run_command(CC, *ABI_FLAGS, "-include", ROOT / "tools/ps5/native_abi_check.h",
                        *flags, "-c", source, "-o", obj)
        cls.run_command(LLD, "-shared", "--no-undefined", "--hash-style=sysv",
                        "--eh-frame-hdr", "-z", "now", "-z", "max-page-size=0x4000",
                        "-init", "module_start", "-fini", "module_stop",
                        "-T", ROOT / "tools/ps5/native-shared.ld", "-o", elf, obj, *libraries)
        return elf

    @classmethod
    def provider(cls, module, body):
        elf = cls.shared_source(module, body)
        native = elf.with_suffix(".elf")
        native.write_bytes(link.convert(elf.read_bytes(), module))
        sdk.generate(elf, native, module, cls.sdk / module, LLD)

    def project(self, language, package=False, thread_destructor=False, graphics=False):
        root = self.root / (language + ("-package-" if package else "-install-") + uuid.uuid4().hex)
        root.mkdir()
        addon = "test." + language.lower()
        (root / addon).mkdir()
        platform = "@PLATFORM@" if language == "C" else "@PLATFORM_TAG@"
        (root / addon / "addon.xml.in").write_text(
            '<addon id="' + addon + '" version="1.0.0"><requires>@ADDON_DEPENDS@</requires>'
            '<extension point="test" library_freebsd="@LIBRARY_FILENAME@"/>'
            '<extension point="xbmc.addon.metadata"><platform>' + platform +
            '</platform></extension></addon>')
        suffix = ".cpp" if language == "CXX" else ".c"
        source = root / ("main" + suffix)
        source.write_text("""
          #include <stdlib.h>
          #include <stdio.h>
          #include <unistd.h>
          #ifdef __cplusplus
          #include <new>
          extern "C" {
          #endif
          extern int runtime_compute(int);
          #ifdef USE_GL
          extern void glFixture(void);
          #endif
          void *volatile allocated;
          int ADDON_Create(void) {
            void *p = malloc(8); allocated = p; free(p);
            chdir(".");
            allocated = fopen("fixture", "r");
            #ifdef USE_GL
            glFixture();
            #endif
            return runtime_compute(1);
          }
          void ADDON_Destroy(void) {}
          #ifdef __cplusplus
          }
          struct Global { Global() { runtime_compute(2); } ~Global() { runtime_compute(3); } };
          Global global __attribute__((init_priority(101)));
          thread_local wchar_t native_tls_character;
          extern "C" wchar_t read_native_character() { return native_tls_character; }
          #endif
        """)
        if language == "CXX":
            with source.open("a") as stream:
                stream.write(CXX_ABI_SOURCE)
        if thread_destructor:
            with source.open("a") as stream:
                stream.write("\nthread_local Global tls_global;\n"
                             "Global *get_tls_global() { return &tls_global; }\n")
        (root / "CMakeLists.txt").write_text(f"""
cmake_minimum_required(VERSION 3.22)
project(native_addon LANGUAGES C CXX)
set(KODI_BUILD_DIR "${{CMAKE_BINARY_DIR}}/kodi-tree")
set(PS5_PAYLOAD_SDK "{PAYLOAD}")
set(Python3_EXECUTABLE "/target/python-must-not-run" CACHE FILEPATH "" FORCE)
set(Python3_VERSION "3.14.0" CACHE STRING "" FORCE)
find_package(Kodi REQUIRED CONFIG PATHS "{self.depends}/lib/kodi" NO_DEFAULT_PATH)
if(NOT Python3_EXECUTABLE STREQUAL "/target/python-must-not-run" OR
   NOT Python3_VERSION STREQUAL "3.14.0")
  message(FATAL_ERROR "Native SDK discovery changed target Python hints")
endif()
set(OVERRIDE_PATHS ON)
set(CMAKE_INSTALL_LIBDIR lib/kodi)
set(CMAKE_INSTALL_DATADIR share/kodi)
set(FIXTURE_SOURCES "{source}")
set(FIXTURE_LIBRARIES {"kodi::OpenGl" if graphics else ""})
{"add_compile_definitions(USE_GL)" if graphics else ""}
build_addon({addon} FIXTURE FIXTURE_LIBRARIES)
include(CPack)
""")
        binary = root / "out"
        self.run_command(CMAKE, "-S", root, "-B", binary,
                         "-DCMAKE_TOOLCHAIN_FILE=" + str(ROOT / "toolchain/ps5-native-addon.cmake"),
                         "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CXX_STANDARD=17",
                         "-DPACKAGE_ZIP=" + ("ON" if package else "OFF"))
        self.run_command(CMAKE, "--build", binary, "-j2")
        return root, binary, addon

    def inspect(self, binary, addon, graphics=False):
        sprx = binary / (addon + ".sprx")
        self.assertEqual(sprx.read_bytes()[:4], b"\x54\x14\xf5\xee")
        receipt = json.loads(sprx.with_suffix(".sprx.json").read_text())
        expected = {"libkodi-runtime.sprx"}
        if graphics:
            expected.add("libkodi-gl.sprx")
        self.assertEqual(set(receipt["needed"]), expected)
        extracted = binary / "extracted.elf"
        self.run_command(SIGNER, "self", "--extract", "--file", sprx, "--out", extracted)
        native = sdk.NativeImage(extracted.read_bytes())
        self.assertEqual(native.tag(12), dict(Elf(next(binary.glob(addon + ".so.*")).read_bytes())
                                             .symbols(".symtab"))["module_start"][4])
        strings = native.memory(native.tag(5), native.tag(10))
        self.assertEqual(cstring(strings, native.tag(sdk.MODULE_INFO) & 0xFFFFFFFF), addon)
        self.assertEqual(native.tag(sdk.MODULE_INFO) >> 32, 0x101)
        self.assertEqual(cstring(strings, native.tag(0x61000041)), addon + ".sprx")
        symbols = SYM.iter_unpack(native.memory(native.tag(6), native.tag(sdk.SYMTABSZ)))
        imports = {cstring(strings, s[0]) for s in symbols if s[0] and not s[3]}
        core_id = link.encode_id(receipt["needed"].index("libkodi-runtime.sprx") + 1)
        self.assertIn(nid("runtime_compute") + "#" + core_id + "#" + core_id, imports)
        self.assertIn(nid("__wrap_malloc") + "#" + core_id + "#" + core_id, imports)
        self.assertIn(nid("__wrap_chdir") + "#" + core_id + "#" + core_id, imports)
        self.assertIn(nid("__wrap_fopen") + "#" + core_id + "#" + core_id, imports)
        self.assertFalse(any(nid("malloc") + "#" in name for name in imports))
        manifest = ET.parse(binary / addon / "addon.xml")
        self.assertEqual(manifest.find("extension").get("library_freebsd"), addon + ".sprx")
        self.assertEqual(manifest.findtext(
            "extension[@point='xbmc.addon.metadata']/platform"), "ps5")
        self.assertEqual((binary / "kodi-tree/addons" / addon / sprx.name).read_bytes(),
                         sprx.read_bytes())
        if addon == "test.cxx":
            linked = Elf(next(binary.glob(addon + ".so.*")).read_bytes())
            actual = dict(linked.symbols(".symtab"))
            for name in ("native_wide", "native_character", "native_max_align"):
                self.assertEqual(actual[name][5], self.baseline_symbols[name][5], name)
            obj = Elf((binary / "CMakeFiles" / (addon + ".dir") / "main.cpp.o").read_bytes())
            self.assertIn(".init_array.101", obj.sections)
            self.assertFalse(any(n.startswith((".ctors", ".dtors")) for n in obj.sections))
            self.assertTrue(any(n.startswith("__emutls_v.") for n, _ in obj.symbols(".symtab")))
            self.assertFalse(any(h[5] and h[2] & 0x400 for h in obj.sections.values()))

    def test_autoconf_compilers_use_native_abi_and_opaque_stdio(self):
        root = self.root / "autoconf"
        root.mkdir()
        runtime = root / "sdk"
        shutil.copytree(self.sdk, runtime)
        shutil.copyfile(ROOT / "tools/ps5/sdk/include/stdio.h", runtime / "include/stdio.h")
        site = root / "config.site"
        site.write_text(_autoconf_site(PAYLOAD, runtime))
        for compiler, suffix, headers, assertion in (
            ("CC", "c", "#include <stdio.h>\n", "_Static_assert"),
            ("CXX", "cpp", "#include <cstdio>\n#include <fstream>\n", "static_assert"),
        ):
            source = root / ("probe." + suffix)
            source.write_text(headers + assertion + '(sizeof(fpos_t) == 24, "fpos ABI");\n'
                              + 'int probe(void) { return 0; }\n')
            if compiler == "CXX":
                source.write_text(source.read_text() + "struct Init { Init(); }; Init init;\n")
            command = ('. "$1"; eval "$' + compiler + ' -c \\"$2\\" -o \\"$3\\""')
            self.run_command("sh", "-c", command, "probe", site, source, source.with_suffix(".o"))
            source.with_suffix(".o").unlink()
            implicit = '. "$1"; eval "$' + compiler + ' -c \\"$2\\""'
            result = subprocess.run(["sh", "-c", implicit, "probe", str(site), str(source)],
                                    cwd=root, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sections = Elf(source.with_suffix(".o").read_bytes()).sections
            self.assertIn(".text", sections)
            if compiler == "CXX":
                self.assertIn(".init_array", sections)
                self.assertFalse(any(name.startswith((".ctors", ".dtors")) for name in sections))
            source.write_text(headers + "int bad(void) { return sizeof(FILE); }\n")
            result = subprocess.run(["sh", "-c", command, "probe", str(site), str(source),
                                     str(source.with_suffix(".o"))],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("incomplete", result.stderr)

    @unittest.skipUnless(shutil.which("autoconf"), "Autoconf required")
    def test_autoconf_configure_with_native_compilers(self):
        root = self.root / "configure"
        root.mkdir()
        runtime = root / "sdk"
        shutil.copytree(self.sdk, runtime)
        shutil.copyfile(ROOT / "tools/ps5/sdk/include/stdio.h", runtime / "include/stdio.h")
        site = root / "config.site"
        site.write_text(_autoconf_site(PAYLOAD, runtime))
        (root / "configure.ac").write_text("""
AC_INIT([native-compiler-probe], [1])
AC_PROG_CC
AC_PROG_CXX
AC_COMPILE_IFELSE([AC_LANG_PROGRAM([[#include <stdio.h>]],
  [[_Static_assert(sizeof(fpos_t) == 24, "fpos ABI");]])], [],
  [AC_MSG_ERROR([incorrect C ABI])])
AC_LANG_PUSH([C++])
AC_COMPILE_IFELSE([AC_LANG_PROGRAM([[#include <cstdio>]],
  [[static_assert(sizeof(fpos_t) == 24, "fpos ABI");]])], [],
  [AC_MSG_ERROR([incorrect C++ ABI])])
AC_LANG_POP([C++])
AC_OUTPUT
""")
        for command in ([shutil.which("autoconf")], ["sh", "configure"]):
            result = subprocess.run(command, cwd=root,
                                    env=dict(os.environ, CONFIG_SITE=str(site)),
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((root / "config.status").is_file())

    def test_c_and_cpp_normal_install(self):
        for language in ("C", "CXX"):
            with self.subTest(language=language):
                root, binary, addon = self.project(language)
                self.inspect(binary, addon)
                prefix = root / "installed"
                self.run_command(CMAKE, "--install", binary, "--prefix", prefix)
                self.assertEqual(len(list(prefix.rglob("*.sprx"))), 1)
                module = next(prefix.rglob("*.sprx"))
                self.assertEqual(stat.S_IMODE(module.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(module.with_suffix(".sprx.json").stat().st_mode),
                                 0o644)
                with zipfile.ZipFile(io.BytesIO(repository.zip_directory(module.parent))) as package:
                    self.assertEqual(stat.S_IMODE(package.getinfo(
                        addon + "/" + addon + ".sprx").external_attr >> 16), 0o755)
                    self.assertEqual(stat.S_IMODE(package.getinfo(
                        addon + "/" + addon + ".sprx.json").external_attr >> 16), 0o644)
                self.assertFalse(list(prefix.rglob("*.so*")))
                self.assertTrue(list(prefix.rglob("addon.xml")))
                self.assertEqual(ET.parse(next(prefix.rglob("addon.xml"))).findtext(
                    "extension[@point='xbmc.addon.metadata']/platform"), "ps5")
                self.run_command(CMAKE, "--build", binary, "--clean-first", "-j2")
                self.inspect(binary, addon)

    def test_release_cpack_preserves_fself(self):
        _, binary, addon = self.project("CXX", package=True)
        self.inspect(binary, addon)
        self.run_command(CMAKE, "--build", binary, "--target", "package")
        archive = next(binary.glob("addon-*.zip"))
        self.assertTrue(archive.name.endswith("-ps5.zip"), archive.name)
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(stat.S_IMODE(package.getinfo(
                addon + "/" + addon + ".sprx").external_attr >> 16), 0o755)
            self.assertEqual(stat.S_IMODE(package.getinfo(
                addon + "/" + addon + ".sprx.json").external_attr >> 16), 0o644)
            self.assertEqual(package.read(addon + "/" + addon + ".sprx"),
                             (binary / (addon + ".sprx")).read_bytes())
            self.assertFalse(any(".so" in name for name in package.namelist()))
            self.assertEqual(ET.fromstring(package.read(addon + "/addon.xml")).findtext(
                "extension[@point='xbmc.addon.metadata']/platform"), "ps5")

    def test_non_ps5_freebsd_metadata_unchanged(self):
        depends = self.root / "freebsd-depends"
        self.run_command(CMAKE, "-DCORE_SOURCE_DIR=" + str(ROOT),
                         "-DADDON_DEPENDS_PATH=" + str(depends),
                         "-DCORE_SYSTEM_NAME=freebsd", "-DCORE_PLATFORM_NAME=freebsd",
                         "-P", ROOT / "cmake/scripts/common/PrepareEnv.cmake")
        config = (depends / "lib/kodi/KodiConfig.cmake").read_text()
        self.assertIn("set(PLATFORM_TAG freebsd)", config)
        self.assertIn("set(PLATFORM freebsd)", config)

    def test_descriptor_rejects_escape_and_stub_tampering(self):
        path = self.sdk / "manifest.json"
        original = path.read_bytes()
        try:
            descriptor = dict(self.descriptor, provider_manifests=["../libkernel.c"])
            path.write_text(json.dumps(descriptor))
            with self.assertRaisesRegex(ValueError, "escapes SDK"):
                builder.describe(self.sdk)
        finally:
            path.write_bytes(original)
        library = self.sdk / "libkernel/lib/libkernel.so"
        original = library.read_bytes()
        try:
            library.write_bytes(original + b"tampered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                builder.describe(self.sdk)
        finally:
            library.write_bytes(original)

    def test_descriptor_requires_declared_core_wrapper_exports(self):
        path = self.sdk / "manifest.json"
        original = path.read_bytes()
        try:
            descriptor = dict(self.descriptor,
                              wrap_symbols=self.descriptor["wrap_symbols"] + ["missing_allocator"])
            path.write_text(json.dumps(descriptor))
            with self.assertRaisesRegex(ValueError, "__wrap_missing_allocator"):
                builder.describe(self.sdk)
        finally:
            path.write_bytes(original)

    def test_descriptor_cannot_omit_core_common_wraps(self):
        path = self.sdk / "manifest.json"
        original = path.read_bytes()
        try:
            descriptor = dict(self.descriptor,
                              wrap_symbols=[name for name in CORE_WRAP_SYMBOLS if name != "fopen"])
            path.write_text(json.dumps(descriptor))
            with self.assertRaisesRegex(ValueError, "exactly match core link_contract"):
                builder.describe(self.sdk)
        finally:
            path.write_bytes(original)

    def test_failed_signing_and_unowned_outputs(self):
        root, binary, addon = self.project("C")
        output = root / "failure.sprx"
        args = type("Arguments", (), {"sdk": self.sdk,
                    "source": next(binary.glob(addon + ".so.*")), "module": addon,
                    "output": output, "native_tool": Path("/bin/false")})()
        with self.assertRaises(subprocess.CalledProcessError):
            builder.build(args)
        self.assertFalse(output.exists())
        self.assertFalse(list(root.glob(".native-addon-*")))
        output.write_bytes(b"not ours")
        with self.assertRaisesRegex(ValueError, "unowned"):
            builder.build(args)
        self.assertEqual(output.read_bytes(), b"not ours")
        output.unlink()
        output.symlink_to(binary / (addon + ".sprx"))
        with self.assertRaisesRegex(ValueError, "symlink"):
            builder.build(args)

    def test_rejects_undeclared_needed_and_native_tls(self):
        stub = self.sdk / "libkodi-runtime/lib/libkodi-runtime.so"
        source = self.shared_source("native-tls", """
          _Thread_local int native_tls;
          void *__tls_get_addr(void *p) { return p; }
          extern int runtime_compute(int);
          int run(void) { return runtime_compute(native_tls); }
        """, flags=["-fno-emulated-tls"], libraries=[stub])
        args = type("Arguments", (), {"sdk": self.sdk, "source": source,
                    "module": "test.tls", "output": self.root / "test.tls.sprx",
                    "native_tool": None})()
        with self.assertRaisesRegex(ValueError, "native TLS"):
            builder.build(args)
        raw = source.read_bytes().replace(b"libkodi-runtime.sprx", b"libevil-runtime.sprx")
        source.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "undeclared source DT_NEEDED"):
            builder.build(args)
        self.assertFalse(args.output.exists())

    def test_failed_rebuild_preserves_published_pair(self):
        _, binary, addon = self.project("C")
        output = binary / (addon + ".sprx")
        receipt = output.with_suffix(".sprx.json")
        original = output.read_bytes(), receipt.read_bytes()
        tool = binary / "bad-signer.py"
        tool.write_text("""#!/usr/bin/env python3
import pathlib, sys
args = sys.argv
pathlib.Path(args[args.index("--out") + 1]).write_bytes(b"wrong")
""")
        tool.chmod(0o700)
        args = type("Arguments", (), {"sdk": self.sdk,
                    "source": next(binary.glob(addon + ".so.*")), "module": addon,
                    "output": output, "native_tool": tool})()
        with self.assertRaisesRegex(ValueError, "extraction differs"):
            builder.build(args)
        self.assertEqual(original, (output.read_bytes(), receipt.read_bytes()))
        self.assertFalse(list(binary.glob(".native-addon-*")))

    def test_cpp_thread_local_destructor_rejected(self):
        with self.assertRaisesRegex(AssertionError, "thread-local destructors require"):
            self.project("CXX", thread_destructor=True)

    def test_shared_graphics_provider_import(self):
        _, binary, addon = self.project("C", graphics=True)
        self.inspect(binary, addon, graphics=True)

    def test_compiler_abi_guards_reject_incompatible_targets(self):
        source = self.root / "abi-check.c"
        source.write_text("typedef __WCHAR_TYPE__ native_wchar;\n"
                          "_Static_assert(sizeof(native_wchar) == 2, \"wchar ABI\");\n")
        for flags, diagnostic in (
                (["--target=x86_64-unknown-freebsd"], "FreeBSD 9 compiler ABI"),
                (["-fno-short-wchar"], "16-bit wchar_t"),
                (["-U__SCE__"], "Sony SDK platform guards"),
                (["-U__STDC_NO_THREADS__"], "no-threads/no-complex guards"),
                (["-march=x86-64"], "znver2 target"),
                (["--target=x86_64-unknown-freebsd9", "-march=znver2", "-fshort-wchar",
                  "-D__PROSPERO__=1", "-D__SCE__=1", "-D__STDC_NO_THREADS__=1",
                  "-D__STDC_NO_COMPLEX__=1", "-U__FLOAT128__", "-U__SIZEOF_FLOAT128__",
                  "-U__STDC_MB_MIGHT_NEQ_WC__"], "32-byte biggest alignment")):
            with self.subTest(flags=flags):
                result = subprocess.run([CC, *ABI_FLAGS, *flags, "-include",
                                         str(ROOT / "tools/ps5/native_abi_check.h"),
                                         "-c", str(source), "-o", str(source.with_suffix(".o"))],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)

    def test_launcher_refresh_recompiles_addon_and_crt(self):
        _, binary, addon = self.project("CXX")
        objects = [binary / "CMakeFiles" / (addon + ".dir") / "main.cpp.o"]
        objects.extend((binary / "CMakeFiles").rglob("native_module_crt.cpp.o"))
        self.assertEqual(len(objects), 2)
        before = {path: path.stat().st_mtime_ns for path in objects}
        launcher = self.depends / "lib/kodi/ps5-runtime/tools/native-clang.py"
        original = launcher.read_bytes()
        try:
            launcher.write_bytes(original + b"\n# SDK refresh recompilation test.\n")
            output = self.run_command(CMAKE, "--build", binary, "-j2")
            self.assertIn("main.cpp.o", output)
            self.assertIn("native_module_crt.cpp.o", output)
            self.assertTrue(all(path.stat().st_mtime_ns > before[path] for path in objects))
            self.inspect(binary, addon)
        finally:
            launcher.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
