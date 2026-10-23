#!/usr/bin/env python3
"""Check that init-array code generation can retain the actual PS5 frontend ABI."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from native_c_link import Elf

ROOT = Path(__file__).resolve().parents[2]
SDK = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
COMPILER = SDK / "bin/prospero-clang++"
SOURCE = r"""
#include <cstddef>
#include <stdio.h>
#if !defined(__PROSPERO__) || !defined(__SCE__) || __FreeBSD__ != 9
#error incorrect source platform
#endif
static_assert(sizeof(wchar_t) == 2);
static_assert(__BIGGEST_ALIGNMENT__ == 32);
static_assert(sizeof(fpos_t) == 24);
struct alignas(32) Wide { wchar_t text[5]; };
extern "C" {
Wide native_wide;
wchar_t native_character;
unsigned char native_max_align[alignof(std::max_align_t)];
void record_abi(unsigned);
}
struct Startup {
  Startup() { record_abi(sizeof(wchar_t)); native_wide.text[0] = L'A'; }
};
Startup startup __attribute__((init_priority(101)));
thread_local wchar_t character;
extern "C" wchar_t read_character() { return character; }
extern "C" wchar_t next_character(wchar_t value) { return value + 1; }
"""

HARNESS = r"""
#include <cassert>
struct alignas(32) Wide { unsigned short text[5]; };
extern "C" {
extern Wide native_wide;
extern unsigned short native_character;
unsigned short read_character();
unsigned short next_character(unsigned short);
}
static unsigned calls;
static unsigned short local_character = 0xabcd;
extern "C" void record_abi(unsigned width) { assert(width == 2); ++calls; }
extern "C" void* __emutls_get_address(void*) { return &local_character; }
int main()
{
  assert(calls == 1);
  assert(native_wide.text[0] == 'A');
  assert(native_character == 0);
  assert(read_character() == 0xabcd);
  assert(next_character(0xd7ff) == 0xd800);
}
"""


@unittest.skipUnless(COMPILER.is_file(), "PS5 SDK compiler required")
class NativeCompilerAbiTest(unittest.TestCase):
    def test_native_frontend_bitcode_to_init_array_preserves_source_abi(self):
        with tempfile.TemporaryDirectory(prefix="native-compiler-abi-") as temporary:
            directory = Path(temporary)
            native, bitcode, modern, launched = (directory / name for name in
                                                ("native.o", "native.bc", "modern.o", "launched.o"))
            flags = [
                str(COMPILER), "-std=c++17", "-O2", "-fPIC", "-femulated-tls",
                "-fno-stack-protector", "-nostdinc++",
                "-I" + str(SDK / "target/include/c++/v1"),
                "-I" + str(ROOT / "tools/ps5/sdk/include"),
                "-isystem", str(SDK / "target/include"), "-x", "c++", "-",
            ]
            commands = [
                ([*flags, "-c", "-o", str(native)], SOURCE),
                ([*flags, "-emit-llvm", "-c", "-o", str(bitcode)], SOURCE),
                ([str(COMPILER), "--target=x86_64-unknown-freebsd9", "-fPIC",
                  "-femulated-tls", "-fuse-init-array", "-Wno-override-module",
                  "-c", str(bitcode), "-o", str(modern)], None),
                ([sys.executable, str(ROOT / "tools/ps5/native-clang.py"),
                  *flags, "-c", "-o", str(launched)], SOURCE),
            ]
            for command, source in commands:
                result = subprocess.run(command, input=source, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            original, converted = Elf(native.read_bytes()), Elf(modern.read_bytes())
            symbols = dict(original.symbols(".symtab"))
            actual = dict(converted.symbols(".symtab"))
            for name in ("native_wide", "native_character", "native_max_align", "read_character"):
                self.assertEqual(actual[name][1:3], symbols[name][1:3], name)
                if symbols[name][1] & 15 == 1:
                    self.assertEqual(actual[name][5], symbols[name][5], name)
            self.assertEqual(actual["native_character"][5], 2)
            self.assertEqual(actual["native_wide"][5], 32)
            self.assertTrue(any(n.startswith(".ctors") for n in original.sections),
                            list(original.sections))
            self.assertFalse(any(n.startswith((".ctors", ".dtors")) for n in converted.sections))
            self.assertIn(".init_array.101", converted.sections)
            self.assertTrue(any(n.startswith("__emutls_v.") for n in actual))
            self.assertFalse(any(h[5] and h[2] & 0x400 for h in converted.sections.values()))
            launch_image = Elf(launched.read_bytes())
            launch_symbols = dict(launch_image.symbols(".symtab"))
            self.assertEqual(launch_symbols["native_character"][5], 2)
            self.assertEqual(launch_symbols["native_wide"][5], 32)
            self.assertIn(".init_array.101", launch_image.sections)
            self.assertFalse(any(n.startswith((".ctors", ".dtors")) for n in launch_image.sections))
            host = shutil.which("clang++-18")
            self.assertIsNotNone(host, "clang++-18 required for x86-64 ABI execution probe")
            harness = directory / "harness.cpp"
            harness.write_text(HARNESS)
            binary = directory / "abi-test"
            for command in ([host, "-std=c++17", str(harness), str(launched),
                             "-o", str(binary)], [str(binary)]):
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
