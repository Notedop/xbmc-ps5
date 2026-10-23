#!/usr/bin/env python3
"""Check that failed shared-runtime qualification cannot report link success."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from native_runtime_sdk import import_object

spec = importlib.util.spec_from_file_location("qualify_cxx", Path(__file__).with_name("qualify-shared-cxx.py"))
qualifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualifier)


class CxxQualificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qualify-cxx-")
        self.root = Path(self.temp.name)
        self.sdk = self.root / "sdk"
        self.resource = self.root / "resource"
        for path in [self.sdk / "bin/prospero-clang++",
                     self.sdk / "bin/prospero-clang",
                     *(self.sdk / "target/lib" / name for name in (
                         "libc++.a", "libc++abi.a", "libunwind.a", "libc.a", "libkernel.so",
                         "libScePosixForWebKit.so", "libkernel_sys.so")),
                     self.resource / "lib/linux/libclang_rt.builtins-x86_64.a"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        self.build = self.root / "qualification"
        self.sdk_object = import_object({
            "__assert": (0, 0x12, 0, 1, 0, 1),
            "__emutls_get_address": (0, 0x22, 0, 1, 0, 1),
        })

    def tearDown(self):
        self.temp.cleanup()

    def exercise(self, compile_code, link_code, providers=()):
        results = [subprocess.CompletedProcess([], 0, str(self.resource), ""),
                   subprocess.CompletedProcess([], 0, self.sdk_object, b""),
                   subprocess.CompletedProcess([], compile_code, "", "compile diagnostic"),
                   *(subprocess.CompletedProcess([], 0, "", "") for _ in range(8)),
                   subprocess.CompletedProcess([], link_code, "",
                                               "ld.lld: error: undefined symbol: absent_function\n")]
        with patch.object(qualifier, "prepare_archive", return_value={"fixture": True}), \
                patch.object(qualifier.subprocess, "run", side_effect=results) as run:
            with contextlib.redirect_stdout(io.StringIO()):
                code = qualifier.qualify(self.sdk, self.build, "ld.lld", "clang-18",
                                         providers=providers)
        return code, run, json.loads((self.build / "qualification-report.json").read_text())

    def test_link_failure_is_reported_and_no_native_candidate_published(self):
        code, run, report = self.exercise(0, 1)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["failed_phase"], "link")
        self.assertFalse(report["native_loadable"])
        self.assertEqual(report["undefined_symbols"], ["absent_function"])
        self.assertEqual(run.call_count, 12)
        command = report["commands"][-1]["argv"]
        self.assertIn("--no-undefined", command)
        self.assertIn("--whole-archive", command)
        self.assertIn("--wrap=chdir", command)
        self.assertIn("--wrap=fopen", command)
        for name in ("libc_locale.o", "libc_posix.o", "kodishim.o", "shared_tls.o",
                     "shared_heap.o", "thread_stack.o", "shared_exit.o", "libc_net.o", "assert.o"):
            self.assertTrue(any(p.endswith("/" + name) for p in command))
        for name in qualifier.HEAP_WRAPS:
            self.assertIn("--wrap=" + name, command)
        self.assertIn("--wrap=pthread_create", command)
        self.assertIn("--wrap=exit", command)
        compile_command = next(c["argv"] for c in report["commands"] if c["phase"] == "compile")
        self.assertIn("-DPS5_SHARED_RUNTIME_CORE=1", compile_command)
        network_compile = next(c["argv"] for c in report["commands"]
                               if c["phase"] == "compile-libc_net")
        self.assertIn("-femulated-tls", network_compile)
        self.assertIn(str(qualifier.NETWORK_SOURCE), report["sources"])
        self.assertFalse(any(p.startswith("--target=") for p in compile_command))
        self.assertNotIn("-fuse-init-array", compile_command)
        cpp = "-I" + str(self.sdk / "target/include/c++/v1")
        overlay = "-I" + str(qualifier.ROOT / "tools/ps5/sdk/include")
        self.assertLess(compile_command.index(cpp), compile_command.index(overlay))
        self.assertIn(str(qualifier.ROOT / "tools/ps5/sdk/include/stdio.h"), report["headers"])
        self.assertFalse(any(p.endswith("/emutls.o") for p in command))
        self.assertNotIn(str(self.sdk / "target/lib/libc.a"), command)
        self.assertNotIn(str(self.sdk / "target/lib/libkernel_sys.so"), command)
        self.assertNotIn(str(self.sdk / "target/lib/libScePosixForWebKit.so"), command)
        self.assertFalse((self.build / "diagnostic-linked.so").exists())

    def test_compile_failure_stops_before_link(self):
        code, run, report = self.exercise(1, 0)
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(report["failed_phase"], "compile")
        self.assertEqual((self.build / "compile.log").read_text(), "compile diagnostic")
        self.assertFalse((self.build / "link.log").exists())

    def test_verified_providers_replace_sdk_wildcard_stubs(self):
        provider = self.root / "native-sdk" / "manifest.json"
        provider.parent.mkdir()
        provider.write_text("{}")
        stub = provider.parent / "lib" / "libSceLibcInternal.so"
        stub.parent.mkdir()
        stub.write_bytes(b"fixture")
        manifest = {
            "soname": "libSceLibcInternal.so",
            "module": "libSceLibcInternal",
            "library": "libSceLibcInternal",
            "import_library": {"path": "lib/libSceLibcInternal.so", "sha256": "a" * 64},
        }
        with patch.object(qualifier, "load_provider", return_value=manifest):
            code, _, report = self.exercise(0, 1, [provider])
        self.assertEqual(code, 1)
        command = report["commands"][-1]["argv"]
        self.assertIn(str(stub), command)
        self.assertNotIn(str(self.sdk / "target/lib/libkernel.so"), command)
        self.assertEqual(report["system_import_mode"], "verified-native")
        self.assertEqual(report["system_providers"][0]["import_library_sha256"], "a" * 64)

    def test_preparation_failure_replaces_stale_success_report(self):
        self.build.mkdir()
        (self.build / "qualification-report.json").write_text('{"status": "strict-link-passed"}')
        resource = subprocess.CompletedProcess([], 0, str(self.resource), "")
        with patch.object(qualifier.subprocess, "run", return_value=resource), \
                patch.object(qualifier, "prepare_archive", side_effect=ValueError("unsupported ctors")):
            with self.assertRaisesRegex(ValueError, "unsupported ctors"):
                qualifier.qualify(self.sdk, self.build, "ld.lld", "clang-18")
        report = json.loads((self.build / "qualification-report.json").read_text())
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["native_loadable"])
        self.assertEqual(report["error"], "unsupported ctors")

    def test_sdk_extraction_failure_stops_before_compile(self):
        resource = subprocess.CompletedProcess([], 0, str(self.resource), "")
        failed = subprocess.CompletedProcess([], 1, b"", b"missing member")
        with patch.object(qualifier, "prepare_archive", return_value={}), \
                patch.object(qualifier.subprocess, "run", side_effect=[resource, failed]) as run:
            with self.assertRaisesRegex(ValueError, "cannot extract SDK assert.o"):
                qualifier.qualify(self.sdk, self.build, "ld.lld", "clang-18")
        self.assertEqual(run.call_count, 2)
        self.assertEqual((self.build / "extract-assert.log").read_text(), "missing member")
        report = json.loads((self.build / "qualification-report.json").read_text())
        self.assertEqual(report["status"], "blocked")

    def test_strict_link_does_not_qualify_unbound_weak_import_or_native_runtime(self):
        report = self.exercise_weak_import("__cxa_thread_atexit_impl", provided=False)
        self.assertEqual(report["unbound_weak_imports"], ["__cxa_thread_atexit_impl"])
        self.assertEqual(report["native_profile_blockers"],
                         [{"unbound_weak_imports": ["__cxa_thread_atexit_impl"]}])

    def test_actual_needed_provider_can_bind_a_weak_native_function(self):
        report = self.exercise_weak_import("pthread_create", provided=True)
        self.assertEqual(report["bound_weak_imports"], ["pthread_create"])
        self.assertEqual(report["unbound_weak_imports"], [])
        self.assertEqual(report["native_profile_blockers"], [])

    def exercise_weak_import(self, name, provided):
        symbols = [
            ("module_start", (0, 0x12, 0, 1, 0x10, 1)),
            (name, (0, 0x22, 0, 0, 0, 0)),
        ]
        linked_image = SimpleNamespace(sections={}, symbols=lambda _: symbols,
                                       section=lambda _: b"libkernel.sprx\0")
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text("{}")
        manifest = {
            "soname": "libkernel.sprx", "module": "libkernel", "library": "libkernel",
            "import_library": {"path": str(self.sdk / "target/lib/libkernel.so"),
                               "sha256": "a" * 64},
            "symbols": [{"name": name, "info": 0x22, "other": 0, "size": 1}],
        }
        original_elf = qualifier.Elf

        def read_elf(raw):
            return linked_image if raw == b"linked fixture" else original_elf(raw)

        def run_command(command, **kwargs):
            if "--print-resource-dir" in command:
                return subprocess.CompletedProcess(command, 0, str(self.resource), "")
            if command[1] == "p":
                return subprocess.CompletedProcess(command, 0, self.sdk_object, b"")
            if "-shared" in command:
                Path(command[command.index("-o") + 1]).write_bytes(b"linked fixture")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(qualifier, "prepare_archive", return_value={"fixture": True}), \
                patch.object(qualifier, "Elf", side_effect=read_elf), \
                patch.object(qualifier, "load_provider", return_value=manifest), \
                patch.object(qualifier, "dynamic_entries", return_value={1: [0]}), \
                patch.object(qualifier.subprocess, "run", side_effect=run_command):
            with contextlib.redirect_stdout(io.StringIO()):
                code = qualifier.qualify(self.sdk, self.build, "ld.lld", "clang-18",
                                         providers=[manifest_path] if provided else [])
        self.assertEqual(code, 0)
        report = json.loads((self.build / "qualification-report.json").read_text())
        self.assertEqual(report["status"], "strict-link-passed")
        self.assertFalse(report["native_loadable"])
        self.assertEqual((self.build / "diagnostic-linked.so").read_bytes(), b"linked fixture")
        return report


if __name__ == "__main__":
    unittest.main()
