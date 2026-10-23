#!/usr/bin/env python3
"""Check system-only startup, initializer handoff and explicit failure paths."""
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from native_c_link import Elf, nid
import native_title_launcher as MODULE

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "shims/native-app/title_native_launcher.cpp"


def fixture():
    imports = {}
    for module, names in (
        ("libkernel", ("sceKernelLoadStartModule", "sceKernelDebugOutText", "_exit")),
        ("libSceLibcInternal", ("snprintf", "atexit", "_init_env", "exit")),
    ):
        imports.update({nid(name): {"module": module, "library": module} for name in names})
    symbols = [("_start", (0, 0x12, 0, 1, 0x100, 16)),
               ("main", (0, 0x12, 0, 1, 0x200, 16))]
    return SimpleNamespace(symbols=lambda _: symbols, section=lambda _: b""), imports


class LauncherProofTest(unittest.TestCase):
    def test_native_only_proof(self):
        linked, imports = fixture()
        proof = MODULE.verify(linked, imports)
        self.assertEqual(proof["native_load_order"], MODULE.LOAD_ORDER)
        self.assertEqual(proof["startup_abi"], "ps5-kodi-startup-v1")
        self.assertEqual(proof["entry_handoff"], "module-start-arguments")
        self.assertFalse(proof["executable_symbol_lookup"])
        self.assertTrue(proof["launcher_has_no_shared_imports"])
        self.assertFalse(proof["software_relocation"])

    def test_rejects_even_one_late_bound_runtime_import(self):
        linked, imports = fixture()
        imports[nid("__wrap_malloc")] = {"module": "libkodi-runtime", "library": "libkodi-runtime"}
        with self.assertRaisesRegex(ValueError, "only native"):
            MODULE.verify(linked, imports)

    def test_rejects_executable_originated_lookup(self):
        linked, imports = fixture()
        imports[nid("sceKernelDlsym")] = {"module": "libkernel", "library": "libkernel"}
        with self.assertRaisesRegex(ValueError, "executable-originated"):
            MODULE.verify(linked, imports)

    def test_requires_native_load_and_exit(self):
        for name in ("sceKernelLoadStartModule", "_exit", "atexit"):
            with self.subTest(name=name):
                linked, imports = fixture()
                del imports[nid(name)]
                with self.assertRaisesRegex(ValueError, name):
                    MODULE.verify(linked, imports)

    def test_rejects_old_title_preinitializer(self):
        linked, imports = fixture()
        linked.section = lambda _: bytes(8)
        with self.assertRaisesRegex(ValueError, "preinitializers"):
            MODULE.verify(linked, imports)

    def test_rejects_duplicate_runtime_and_obsolete_lookup_markers(self):
        for name in ("__wrap_malloc", "ps5_launcher_run_lookup", "ps5_launcher_finalize_lookup"):
            with self.subTest(name=name):
                linked, imports = fixture()
                symbols = linked.symbols(".symtab") + [(name, (0, 0x12, 0, 1, 0x300, 16))]
                linked.symbols = lambda _: symbols
                with self.assertRaisesRegex(ValueError, "shared-runtime dependency or obsolete"):
                    MODULE.verify(linked, imports)


MOCK = r'''
#include <cassert>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "title_native_startup.h"
#ifndef FAILURE
#define FAILURE 0
#endif
int ps5_launcher_main(int, char**, char**);
static int loads;
static void (*finalizer)();
static bool finalized;
static int finish() {
  assert(loads == 3 && !finalized);
  finalized = true;
  return FAILURE == 11 ? -7 : 0;
}
static void run(int argc, char** argv, char** envp) {
  assert(argc == 2 && std::strcmp(argv[1], "test") == 0 && envp == nullptr);
  assert(loads == 3 && finalizer != nullptr);
  if (FAILURE == 10)
    return;
  finalizer();
  assert(finalized);
  std::puts("native order and finalization complete");
  std::_Exit(0);
}
extern "C" {
void sceKernelDebugOutText(int channel, const char* message) {
  assert(channel == 0);
  std::fputs(message, stderr);
}
[[noreturn]] void ps5_test_exit(int code) { std::_Exit(code); }
int sceKernelLoadStartModule(const char* path, size_t size, const void* args,
                            unsigned int flags, const void* options, int* result) {
  const char* paths[] = {"/app0/sce_module/libkodi-runtime.sprx",
                        "/app0/sce_module/libkodi-gl.sprx",
                        "/app0/sce_module/libkodi-app.sprx"};
  assert(loads < 3 && std::strcmp(path, paths[loads]) == 0);
  assert(flags == 0 && options == nullptr);
  ++loads;
  *result = FAILURE == loads * 2 ? 7 : 0;
  if (loads != 3)
    assert(size == 0 && args == nullptr);
  else {
    assert(size == sizeof(KodiNativeStartup) && args != nullptr);
    const auto* startup = static_cast<const KodiNativeStartup*>(args);
    assert(startup->version == KODI_NATIVE_STARTUP_ABI && startup->size == size);
    assert(startup->application != nullptr && startup->application->run == nullptr &&
           startup->application->finalize == nullptr);
    if (FAILURE != 7)
      startup->application->run = run;
    if (FAILURE != 8)
      startup->application->finalize = finish;
  }
  return FAILURE == loads * 2 - 1 ? -42 : loads + 100;
}
int ps5_test_atexit(void (*callback)()) {
  assert(loads == 3 && finalizer == nullptr);
  finalizer = callback;
  return FAILURE == 9 ? -1 : 0;
}
}
int main() {
  std::setvbuf(stdout, nullptr, _IONBF, 0);
  char first[] = "launcher";
  char second[] = "test";
  char* args[] = {first, second, nullptr};
  return ps5_launcher_main(2, args, nullptr);
}
'''


@unittest.skipUnless(shutil.which("clang++-18"), "clang++-18 required")
class LauncherLifecycleTest(unittest.TestCase):
    def run_launcher(self, failure=0):
        with tempfile.TemporaryDirectory(prefix="native-launcher-test-") as temporary:
            directory = Path(temporary)
            object_path, mock, binary = directory / "launcher.o", directory / "mock.cpp", directory / "test"
            mock.write_text(MOCK)
            common = ["clang++-18", "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror",
                      "-I" + str(SOURCE.parent)]
            result = subprocess.run([
                *common, "-Dmain=ps5_launcher_main", "-Datexit=ps5_test_atexit",
                "-D_exit=ps5_test_exit", "-c", str(SOURCE), "-o", str(object_path),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([*common, "-DFAILURE=" + str(failure), str(object_path),
                                     str(mock), "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return subprocess.run([str(binary)], capture_output=True, text=True)

    def test_loads_providers_before_application_and_finalizes_without_unloading(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "native order and finalization complete\n")
        self.assertIn("entering native Kodi application", result.stderr)

    def test_every_load_and_initializer_failure_stops_before_entry(self):
        for failure in range(1, 7):
            with self.subTest(failure=failure):
                result = self.run_launcher(failure)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("entering native Kodi", result.stderr)
                self.assertEqual(result.stderr.count("load-start"), (failure + 1) // 2)

    def test_handoff_registration_return_and_finalization_failures_are_explicit(self):
        for failure in range(7, 12):
            with self.subTest(failure=failure):
                result = self.run_launcher(failure)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                expected = ("startup interface missing" if failure <= 8 else
                            "cannot register" if failure == 9 else
                            "unexpectedly returned" if failure == 10 else "finalization failed")
                self.assertIn(expected, result.stderr)

    def test_actual_native_launcher_has_no_runtime_or_lookup_undefined_symbols(self):
        sdk = Path("/opt/ps5-payload-sdk")
        if not (sdk / "bin/prospero-clang++").is_file():
            self.skipTest("native compiler required")
        with tempfile.TemporaryDirectory(prefix="native-launcher-object-") as temporary:
            output = Path(temporary) / "launcher.o"
            result = subprocess.run([
                "python3", str(ROOT / "tools/ps5/native-clang.py"),
                str(sdk / "bin/prospero-clang++"), "-std=c++20", "-O2", "-Wall", "-Wextra",
                "-Werror", "-fPIC", "-femulated-tls", "-fno-stack-protector", "-fno-exceptions",
                "-fno-rtti", "-nostdinc++", "-I" + str(sdk / "target/include/c++/v1"),
                "-isystem", str(sdk / "target/include"), "-c", str(SOURCE), "-o", str(output),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            symbols = Elf(output.read_bytes()).symbols(".symtab")
            undefined = {name for name, symbol in symbols if name and not symbol[3]}
            self.assertEqual(undefined, {"sceKernelLoadStartModule", "sceKernelDebugOutText",
                                         "snprintf", "atexit", "_exit"})


if __name__ == "__main__":
    unittest.main()
