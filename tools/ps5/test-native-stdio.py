#!/usr/bin/env python3
"""Bounded stdio overlay probes; no installation, linking, or console ABI claim.

Run with python3 tools/ps5/test-native-stdio.py. PS5_PAYLOAD_SDK selects the SDK;
PS5_NATIVE_SYSTEM_SDK selects optional provider manifests for SDK export checks.
Host ASan/UBSan tests mock native calls, not the console's implementation.
Standard declarations do not supply missing native exports (including rename,
tmpfile/tmpnam, pipe and memory streams, getline/getdelim, and dprintf).
"""
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from native_c_link import nid
from native_shared_link import load_provider


ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "tools/ps5/sdk/include"
SDK = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
PROVIDER = Path(os.environ.get(
    "PS5_NATIVE_SYSTEM_SDK", ROOT / "build/ps5-native/sdk/system")) / \
    "libSceLibcInternal/manifest.json"
NATIVE_UNLOCKED = (
    "clearerr_unlocked", "feof_unlocked", "ferror_unlocked", "fileno_unlocked",
    "getc_unlocked", "getchar_unlocked", "putc_unlocked", "putchar_unlocked",
)
FORWARDED_UNLOCKED = {
    "fflush_unlocked": "fflush", "fputc_unlocked": "fputc",
    "fputs_unlocked": "fputs", "fread_unlocked": "fread",
    "fwrite_unlocked": "fwrite",
}
NORMAL_CALLS = ("clearerr", "feof", "ferror", "fileno", "getc", "getchar",
                "putc", "putchar", "fflush", "fputc", "fputs", "fread", "fwrite")
CALLS = r"""
void probe(FILE *s, char *buffer)
{
  clearerr(s);
  (void)feof(s); (void)ferror(s); (void)fileno(s);
  (void)getc(s); (void)getchar(); (void)putc(65, s); (void)putchar(65);
  (void)fflush(s); (void)fputc(65, s); (void)fputs(buffer, s);
  (void)fread(buffer, 1, 1, s); (void)fwrite(buffer, 1, 1, s);
  clearerr_unlocked(s);
  (void)feof_unlocked(s); (void)ferror_unlocked(s); (void)fileno_unlocked(s);
  (void)getc_unlocked(s); (void)getchar_unlocked();
  (void)putc_unlocked(65, s); (void)putchar_unlocked(65);
  (void)fflush_unlocked(s); (void)fputc_unlocked(65, s);
  (void)fputs_unlocked(buffer, s);
  (void)fread_unlocked(buffer, 1, 1, s);
  (void)fwrite_unlocked(buffer, 1, 1, s);
}
"""
LAYOUT = r"""
#include <stddef.h>
CHECK(sizeof(fpos_t) == 24);
CHECK(ALIGN(fpos_t) == 8);
CHECK(offsetof(fpos_t, __offset) == 0);
CHECK(offsetof(fpos_t, __state) == 8);
CHECK(sizeof(((fpos_t *)0)->__state) == 16);
CHECK(_IOFBF == 0 && _IOLBF == 1 && _IONBF == 2);
CHECK(EOF == -1 && SEEK_SET == 0 && SEEK_CUR == 1 && SEEK_END == 2);
"""


class NativeStdioTest(unittest.TestCase):
    def setUp(self):
        # Keep every probe in the repository, never in a system temporary path.
        self.directory = tempfile.TemporaryDirectory(prefix=".stdio-probe-", dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)

    def compile(self, source, *, cpp=False, flags=(), success=True, host=False):
        if host:
            compiler = shutil.which("clang-18") or shutil.which("clang")
        else:
            compiler = SDK / ("bin/prospero-clang++" if cpp else "bin/prospero-clang")
        if not compiler or not Path(compiler).is_file():
            self.skipTest("required compiler unavailable")
        path = self.work / ("probe.cpp" if cpp else "probe.c")
        path.write_text(source, encoding="utf-8")
        output = self.work / "probe.out"
        command = [str(compiler), "-std=c++17" if cpp else "-std=c11",
                   "-Werror", "-fPIC"]
        # libc++'s C wrappers must precede both the overlay and SDK C headers.
        if cpp:
            command += ["-nostdinc++", "-I" + str(SDK / "target/include/c++/v1")]
        command += ["-I" + str(OVERLAY),
                   "-isystem", str(SDK / "target/include")]
        if not host:
            command += ["--target=x86_64-unknown-freebsd"]
        command += [*flags, str(path), "-o", str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, "incomplete FILE was accepted")
        return output, result

    def test_target_c_layout_and_wchar_include_orders(self):
        for includes in ("#include <stdio.h>\n#include <wchar.h>\n",
                         "#include <wchar.h>\n#include <stdio.h>\n"):
            with self.subTest(includes=includes):
                self.compile(includes + "#define CHECK(x) _Static_assert(x, #x)\n"
                             "#define ALIGN(x) _Alignof(x)\n" + LAYOUT + r"""
int (*save_position)(FILE *restrict, fpos_t *restrict) = fgetpos;
int (*restore_position)(FILE *, const fpos_t *) = fsetpos;
FILE **streams[] = {&stdin, &stdout, &stderr};
""", flags=("-c",))

    def test_target_cpp_standard_headers_and_file_streams(self):
        for standard in ("c++11", "c++17", "c++20"):
            for wchar_first in (False, True):
                with self.subTest(standard=standard, wchar_first=wchar_first):
                    source = ("#include <cwchar>\n" if wchar_first else "")
                    source += r"""
#include <cstdio>
#include <fstream>
#include <iostream>
#include <cwchar>
#include <type_traits>
#define CHECK(x) static_assert(x, #x)
#define ALIGN(x) alignof(x)
""" + LAYOUT + r"""
CHECK((std::is_same<std::fpos_t, fpos_t>::value));
int (*save_position)(FILE *, fpos_t *) = std::fgetpos;
int (*restore_position)(FILE *, const fpos_t *) = std::fsetpos;
void probe(const char *path)
{
  std::fstream stream(path, std::ios::in | std::ios::out);
  stream.seekg(0);
  auto position = stream.tellg();
  stream.seekp(position);
  std::cout << stream.rdbuf();
  std::wcout << L"stdio";
}
"""
                    self.compile(source, cpp=True, flags=("-std=" + standard, "-c"))

    def test_target_file_is_incomplete(self):
        for cpp in (False, True):
            for operation in ("sizeof(FILE)", "stream->_flags", "stream->_file"):
                with self.subTest(cpp=cpp, operation=operation):
                    _, result = self.compile(
                        "#include <stdio.h>\n"
                        "int probe(FILE *stream) { return " + operation + "; }\n",
                        cpp=cpp, flags=("-c",), success=False)
                    self.assertRegex(result.stderr, "incomplete|undefined")

    def test_target_normal_and_unlocked_emit_public_calls(self):
        output, _ = self.compile("#include <stdio.h>\n" + CALLS + r"""
FILE *input_stream(void) { return stdin; }
FILE *output_stream(void) { return stdout; }
FILE *error_stream(void) { return stderr; }
""",
                                 flags=("-O1", "-fno-builtin", "-S", "-emit-llvm"))
        ir = output.read_text()
        called = set(re.findall(r"\bcall\b[^\n]*@([A-Za-z_][A-Za-z_0-9]*)\(", ir))
        self.assertTrue(set(NORMAL_CALLS + NATIVE_UNLOCKED) <= called, called)
        self.assertFalse(set(FORWARDED_UNLOCKED) & called, called)
        self.assertNotIn("getelementptr", ir)
        self.assertNotIn("__isthreaded", ir)
        self.assertNotIn("__srget", ir)
        self.assertNotIn("__swbuf", ir)
        for name in ("__stdinp", "__stdoutp", "__stderrp"):
            self.assertRegex(ir, r"load ptr, ptr @" + name + r"\b")
        for name in ("_Stdin", "_Stdout", "_Stderr"):
            self.assertNotIn("@" + name, ir)

    def test_target_posix_public_prototypes(self):
        self.compile(r"""
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
FILE *(*open_fd)(int, const char *) = fdopen;
int (*descriptor)(FILE *) = fileno;
int (*seek_offset)(FILE *, off_t, int) = fseeko;
off_t (*tell_offset)(FILE *) = ftello;
ssize_t (*read_line)(char **restrict, size_t *restrict, FILE *restrict) = getline;
ssize_t (*read_delimited)(char **restrict, size_t *restrict, int, FILE *restrict) = getdelim;
FILE *(*memory_file)(void *restrict, size_t, const char *restrict) = fmemopen;
FILE *(*memory_stream)(char **, size_t *) = open_memstream;
int (*rename_at)(int, const char *, int, const char *) = renameat;
int (*print_fd)(int, const char *restrict, ...) = dprintf;
int (*vprint_fd)(int, const char *restrict, va_list) = vdprintf;
int (*try_lock)(FILE *) = ftrylockfile;
void (*lock_stream)(FILE *) = flockfile;
void (*unlock_stream)(FILE *) = funlockfile;
FILE *(*open_pipe)(const char *, const char *) = popen;
int (*close_pipe)(FILE *) = pclose;
char *(*terminal_name)(char *) = ctermid;
""", flags=("-c",))

    @unittest.skipUnless(PROVIDER.is_file(), "native libc SDK contract unavailable")
    def test_sdk_export_metadata(self):
        provider = load_provider(PROVIDER)
        exports = provider["symbols"]
        for name in NORMAL_CALLS + NATIVE_UNLOCKED + (
                "fgetpos", "fsetpos", "fseeko", "ftello", "flockfile", "funlockfile"):
            with self.subTest(symbol=name):
                matches = [row for row in exports if row["nid"] == nid(name)]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["info"] & 15, 2)
        for name in FORWARDED_UNLOCKED:
            self.assertFalse(any(row["nid"] == nid(name) for row in exports), name)
        for name, size in (("__stdinp", 8), ("__stdoutp", 8), ("__stderrp", 8),
                           ("_Stdin", 456), ("_Stdout", 456), ("_Stderr", 456)):
            matches = [row for row in exports if row["nid"] == nid(name)]
            self.assertEqual(len(matches), 1, name)
            self.assertEqual(matches[0]["info"] & 15, 1, name)
            sizes = (0, size) if provider.get("runtime_implementation_qualified") is False else (size,)
            self.assertIn(matches[0]["size"], sizes, name)

    def test_host_mock_24_byte_positions_and_opaque_calls_with_sanitizers(self):
        if not (SDK / "target/include/sys/_types.h").is_file():
            self.skipTest("SDK system type headers unavailable")
        # Rename mocks so sanitizer/runtime stdio retains the host's own ABI.
        names = NORMAL_CALLS + NATIVE_UNLOCKED + ("fgetpos", "fsetpos",
                                                "__stdinp", "__stdoutp", "__stderrp")
        source = "".join(f"#define {name} mock_{name}\n" for name in names)
        source += "#include <stdio.h>\n" + r"""
#define CHECK(x) do { if (!(x)) __builtin_trap(); } while (0)
static unsigned char token;
FILE *__stdinp = (void *)&token;
FILE *__stdoutp = (void *)&token;
FILE *__stderrp = (void *)&token;
static unsigned calls;
static unsigned char restored[24];
int fgetpos(FILE *restrict s, fpos_t *restrict position)
{
  CHECK(s == stdin);
  unsigned char bytes[24];
  for (unsigned i = 0; i < 24; ++i) bytes[i] = (unsigned char)(i * 7 + 3);
  __builtin_memcpy(position, bytes, 24);
  return 0;
}
int fsetpos(FILE *s, const fpos_t *position)
{
  CHECK(s == stdin);
  __builtin_memcpy(restored, position, 24);
  return 0;
}
static int touch(FILE *s) { CHECK(s == stdin); ++calls; return 7; }
void clearerr(FILE *s) { (void)touch(s); }
int feof(FILE *s) { return touch(s); }
int ferror(FILE *s) { return touch(s); }
int fileno(FILE *s) { return touch(s); }
int getc(FILE *s) { return touch(s); }
int getchar(void) { return touch(stdin); }
int putc(int c, FILE *s) { CHECK(c == 65); return touch(s); }
int putchar(int c) { CHECK(c == 65); return touch(stdout); }
int fflush(FILE *s) { return touch(s); }
int fputc(int c, FILE *s) { CHECK(c == 65); return touch(s); }
int fputs(const char *p, FILE *s) { CHECK(p[0] == 'x'); return touch(s); }
size_t fread(void *p, size_t size, size_t count, FILE *s)
{
  CHECK(size == 1 && count == 1); CHECK(((char *)p)[0] == 'x');
  (void)touch(s); return count;
}
size_t fwrite(const void *p, size_t size, size_t count, FILE *s)
{
  CHECK(size == 1 && count == 1); CHECK(((const char *)p)[0] == 'x');
  (void)touch(s); return count;
}
void clearerr_unlocked(FILE *s) { clearerr(s); }
int feof_unlocked(FILE *s) { return feof(s); }
int ferror_unlocked(FILE *s) { return ferror(s); }
int fileno_unlocked(FILE *s) { return fileno(s); }
int getc_unlocked(FILE *s) { return getc(s); }
int getchar_unlocked(void) { return getchar(); }
int putc_unlocked(int c, FILE *s) { return putc(c, s); }
int putchar_unlocked(int c) { return putchar(c); }
""" + CALLS + r"""
int main(void)
{
  struct {
    unsigned char before[8];
    fpos_t position;
    unsigned char after[8];
  } guarded;
  __builtin_memset(&guarded, 0xa5, sizeof(guarded));
  CHECK(sizeof(fpos_t) == 24 && _Alignof(fpos_t) == 8);
  CHECK(fgetpos(stdin, &guarded.position) == 0);
  CHECK(fsetpos(stdin, &guarded.position) == 0);
  for (unsigned i = 0; i < 24; ++i)
    CHECK(restored[i] == (unsigned char)(i * 7 + 3));
  for (unsigned i = 0; i < 8; ++i)
    CHECK(guarded.before[i] == 0xa5 && guarded.after[i] == 0xa5);
  char buffer[] = "x";
  probe(stdin, buffer);
  CHECK(calls == 26);
  CHECK(fflush_unlocked(stdin) == 7);
  CHECK(fputc_unlocked(65, stdout) == 7);
  CHECK(fputs_unlocked(buffer, stderr) == 7);
  CHECK(fread_unlocked(buffer, 1, 1, stdin) == 1);
  CHECK(fwrite_unlocked(buffer, 1, 1, stdout) == 1);
  return 0;
}
"""
        output, _ = self.compile(
            source, host=True, flags=("-O1", "-fsanitize=address,undefined",
                                     "-fno-omit-frame-pointer"))
        result = subprocess.run([str(output)], capture_output=True, text=True,
                                env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
