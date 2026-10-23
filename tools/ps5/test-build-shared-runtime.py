#!/usr/bin/env python3
"""Check shared-core publication, required exports and SDK-only system contracts."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "shared_core_builder", Path(__file__).with_name("build-shared-runtime.py"))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class SharedCoreBuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shared-core-build-")
        self.root = Path(self.temp.name)
        tool = self.root / "native-tool"
        tool.write_text("fixture")
        tool.chmod(0o755)
        self.args = SimpleNamespace(
            output=self.root / "result", native_tool=tool,
            provider=[self.root / "provider.json"], sdk=self.root / "sdk",
            lld="ld.lld", clang="clang-18", ar="llvm-ar-18", objcopy="llvm-objcopy-18")
        self.profile_blockers = []
        self.names = {"__wrap_" + n for n in builder.qualifier.RUNTIME_WRAPS}
        self.names.update(("ps5_shared_heap_initialize", "__emutls_get_address",
                           "__cxa_thread_atexit_impl"))
        self.names.update(builder.qualifier.NETWORK_SYMBOLS)
        self.nonfunction = set()
        self.network_source_hash = builder.digest(builder.qualifier.NETWORK_SOURCE)
        self.corrupt_roundtrip = False

    def tearDown(self):
        self.temp.cleanup()

    def qualify(self, sdk, output, *args):
        output.mkdir()
        (output / "qualification-report.json").write_text(json.dumps({
            "status": "strict-link-passed", "system_import_mode": "verified-native",
            "native_profile_blockers": self.profile_blockers,
            "sources": {str(builder.qualifier.NETWORK_SOURCE): self.network_source_hash},
        }))
        (output / "diagnostic-linked.so").write_bytes(b"linked")
        return 0

    def generate(self, linked, native, module, output, lld):
        manifest = {
            "symbols": [{"name": n, "info": 0x11 if n in self.nonfunction else 0x12}
                        for n in sorted(self.names)],
            "import_library": {"path": "lib/libkodi-runtime.so"},
        }
        (output / "lib").mkdir()
        (output / "lib/libkodi-runtime.so").write_bytes(b"linker-only")
        (output / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def run_command(self, command, **kwargs):
        destination = Path(command[command.index("--out") + 1])
        source = Path(command[command.index("--file") + 1])
        destination.write_bytes(b"corrupt" if self.corrupt_roundtrip and
                                "--extract" in command else source.read_bytes())

    def exercise(self):
        with patch.object(builder, "verified_system_providers", return_value=[{}]), \
                patch.object(builder, "select_providers", side_effect=lambda source, providers: providers), \
                patch.object(builder.qualifier, "qualify", side_effect=self.qualify), \
                patch.object(builder, "convert", return_value=b"native"), \
                patch.object(builder, "generate", side_effect=self.generate), \
                patch.object(builder.subprocess, "run", side_effect=self.run_command), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return builder.build(self.args)

    def test_publishes_only_complete_signed_core_with_all_wraps(self):
        report = self.exercise()
        self.assertEqual(report["status"], "native-built-console-unqualified")
        self.assertFalse(report["console_accepted"])
        self.assertTrue(report["process_lifetime"])
        self.assertEqual(report["wrap_symbols"], list(builder.qualifier.RUNTIME_WRAPS))
        manifest = json.loads((self.args.output / "manifest.json").read_text())
        self.assertEqual(manifest["link_contract"]["wrap_symbols"], report["wrap_symbols"])
        self.assertEqual(manifest["link_contract"]["compiler_abi"], report["compiler_abi"])
        self.assertEqual(manifest["link_contract"]["network"], {
            "implementation": "ps5-libc-net-v1",
            "symbols": ["__h_errno", "freeaddrinfo", "gai_strerror", "getaddrinfo",
                        "gethostbyname", "gethostname", "getnameinfo"],
            "source_sha256": builder.digest(builder.qualifier.NETWORK_SOURCE),
        })
        self.assertEqual((self.args.output / report["runtime_filename"]).read_bytes(), b"native")
        for relative, expected in report["files"].items():
            self.assertEqual(builder.digest(self.args.output / relative), expected)
        self.assertEqual(len(list(self.root.glob(".result-*"))), 0)

    def test_missing_allocator_export_blocks_publication(self):
        self.names.remove("__wrap_malloc")
        with self.assertRaisesRegex(ValueError, "missing required exports"):
            self.exercise()
        self.assertFalse(self.args.output.exists())
        failed = next(self.root.glob(".result-*"))
        self.assertEqual(json.loads((failed / "build.json").read_text())["status"], "blocked")

    def test_native_profile_blocker_prevents_publication(self):
        self.profile_blockers = [{"native_tls_sections": [".tdata"]}]
        with self.assertRaisesRegex(ValueError, "profile blockers"):
            self.exercise()
        self.assertFalse(self.args.output.exists())

    def test_each_missing_network_export_prevents_publication(self):
        for name in builder.qualifier.NETWORK_SYMBOLS:
            with self.subTest(name=name):
                self.names.remove(name)
                with self.assertRaisesRegex(ValueError, "missing required exports"):
                    self.exercise()
                self.assertFalse(self.args.output.exists())
                self.names.add(name)

    def test_network_data_export_prevents_publication(self):
        self.nonfunction.add("getaddrinfo")
        with self.assertRaisesRegex(ValueError, "must be function exports"):
            self.exercise()
        self.assertFalse(self.args.output.exists())

    def test_network_source_change_prevents_publication(self):
        self.network_source_hash = "0" * 64
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.exercise()
        self.assertFalse(self.args.output.exists())

    def test_native_roundtrip_mismatch_prevents_publication(self):
        self.corrupt_roundtrip = True
        with self.assertRaisesRegex(ValueError, "extraction differs"):
            self.exercise()
        self.assertFalse(self.args.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.args.output.mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.exercise()

if __name__ == "__main__":
    unittest.main()
