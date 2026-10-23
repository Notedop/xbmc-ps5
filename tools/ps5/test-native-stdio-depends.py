#!/usr/bin/env python3
"""Validate isolated migration receipts and exercise actual affected sources.

Run after build-native-stdio-depends.py. Host ASan/UBSan probes use opaque FILE
and public host stdio calls; they do NOT execute PS5 archives or prove console
ABI behavior. Curl's dynbuf and gettext's locking are bounded test fixtures.
OpenSSL/SMB tests execute unchanged function excerpts with BIO/context fixtures.
"""
# SPDX-License-Identifier: GPL-3.0-or-later

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
STAGE = Path(os.environ.get("PS5_STDIO_DEPENDS", ROOT / "build/ps5-native-stdio-depends"))
SDK = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))
OVERLAY = ROOT / "tools/ps5/sdk/include"
spec = importlib.util.spec_from_file_location("migration", ROOT / "tools/ps5/build-native-stdio-depends.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix=".stdio-depends-test-", dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)

    def source(self, name):
        source = STAGE / name / "source"
        if not source.is_dir():
            self.skipTest(f"build {name} first")
        return source

    def source_function(self, path, name):
        text = path.read_text()
        match = re.search(r"(?:static\s+)?(?:int|void)\s+" + re.escape(name) +
                          r"\s*\([^;{}]*\)\s*\{", text)
        self.assertIsNotNone(match)
        depth = 1
        index = match.end()
        while depth:
            self.assertLess(index, len(text))
            depth += (text[index] == "{") - (text[index] == "}")
            index += 1
        return text[match.start():index] + "\n"

    def host_bridge(self):
        bridge = self.work / "bridge"
        (bridge / "sys").mkdir(parents=True)
        (bridge / "sys/cdefs.h").write_text(r"""
#include_next <sys/cdefs.h>
#define __POSIX_VISIBLE 200809
#define __ISO_C_VISIBLE 2011
#define __BSD_VISIBLE 1
#define __XSI_VISIBLE 700
#define __printflike(a,b) __attribute__((format(printf,a,b)))
#define __scanflike(a,b) __attribute__((format(scanf,a,b)))
""")
        (bridge / "sys/_null.h").write_text("#include <stddef.h>\n")
        (bridge / "sys/_types.h").write_text(r"""
#include <sys/types.h>
typedef __SIZE_TYPE__ __size_t;
typedef __builtin_va_list __va_list;
typedef struct __sFILE FILE;
typedef struct __sFILE __FILE;
#define _STDFILE_DECLARED
#define __FILE_defined 1
#define ____FILE_defined 1
""")
        return bridge

    def host(self, text, sources=(), includes=(), flags=(), cpp=False):
        compiler = shutil.which("clang++-18" if cpp else "clang-18")
        if not compiler:
            self.skipTest("clang18 host sanitizer compiler required")
        bridge = self.host_bridge()
        probe = self.work / ("probe.cpp" if cpp else "probe.c")
        probe.write_text(text)
        output = self.work / "probe"
        command = [compiler, "-std=c++17" if cpp else "-std=c11", "-O1", "-g",
                   "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                   "-I" + str(bridge), "-I" + str(OVERLAY),
                   "-include", str(OVERLAY / "stdio.h")]
        command += ["-I" + str(p) for p in includes]
        command += [str(probe), *map(str, sources), *flags, "-o", str(output)]
        env = {**os.environ, "TMPDIR": str(self.work), "ASAN_OPTIONS": "detect_leaks=0"}
        compiled = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        result = subprocess.run([str(output)], cwd=self.work, capture_output=True, text=True,
                                env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_complete_mapping_preserves_originals_and_configuration(self):
        path = STAGE / "archive-map.json"
        if not path.exists():
            self.skipTest("build migration first")
        mapping = json.loads(path.read_text())
        self.assertEqual(len(mapping), 8)
        bz2 = [p for p in mapping if p.endswith("/libbz2.a")]
        self.assertEqual(len(bz2), 2)
        self.assertEqual(mapping[bz2[0]], mapping[bz2[1]])
        for package in migration.PACKAGES:
            with self.subTest(package=package):
                receipt = json.loads((STAGE / package / "receipt.json").read_text())
                self.assertEqual(receipt["identity"]["overlay"], migration.digest(OVERLAY / "stdio.h"))
                for old, expected in receipt["identity"]["original_archives"].items():
                    self.assertEqual(migration.digest(old), expected, "installation changed")
                for item in receipt["outputs"]:
                    self.assertEqual(mapping[item["original"]], item["rebuilt"])
                    self.assertEqual(migration.digest(item["rebuilt"]), item["sha256"])
                for filename, expected in receipt["feature_headers"].items():
                    self.assertEqual(migration.digest(STAGE / package / "source" / filename), expected)
                self.assertTrue(receipt["validation"]["opaque_FILE_rejected"])
                self.assertEqual(receipt["validation"]["fpos_size"], 24)
                self.assertEqual(receipt["validation"]["fpos_align"], 8)
                records = [json.loads(p.read_text()) for p in
                           (STAGE / package / "compile-records").glob("*.json")]
                self.assertGreater(len(records), 1)
                for record in records:
                    command = record["command"]
                    self.assertIn(str(OVERLAY / "stdio.h"), command)
                    self.assertIn("-include", command)
                    self.assertEqual(record["overlay_sha256"], receipt["identity"]["overlay"])
                archive = Path(receipt["outputs"][0]["rebuilt"])
                symbols = migration.archive_symbols(SDK / "bin/llvm-nm", archive)
                self.assertFalse(symbols & migration.BAD_IMPORTS)
                definitions = migration.archive_symbols(SDK / "bin/llvm-nm", archive, defined=True)
                for item in receipt["outputs"]:
                    baseline = migration.archive_symbols(
                        SDK / "bin/llvm-nm", Path(item["original"]), defined=True)
                    missing = baseline - definitions
                    if package == "fmt":
                        missing = {symbol for symbol in missing if "file_print_buffer" not in symbol}
                    self.assertFalse(missing, missing)
                objects = migration.validate_objects(archive, records)
                self.assertEqual(objects, receipt["object_validation"])
        hb = SDK / "target/user/homebrew/include"
        for name, headers in (("fmt", ("fmt/base.h", "fmt/format.h", "fmt/format-inl.h", "fmt/os.h")),
                              ("libpng", ("png.h", "pngconf.h")),
                              ("bzip2", ("bzlib.h",))):
            for header in headers:
                source = STAGE / name / "source" / ("include/" + header if name == "fmt" else header)
                self.assertEqual(source.read_bytes(), (hb / header).read_bytes())

    def test_known_bad_members_now_call_public_functions(self):
        checks = {
            "curl": {"curl_get_line.c.o": {"feof", "ferror"},
                     "curl_fopen.c.o": {"fileno"}},
            "bzip2": {"bzlib.o": {"ferror"}},
            "openssl": {"libcrypto-lib-bss_file.o": {"feof", "ferror"},
                        "libcrypto-lib-randfile.o": {"fileno", "clearerr"},
                        "libcrypto-lib-ui_openssl.o": {"fileno", "feof", "ferror"}},
            "gettext": {"localealias.o": {"feof_unlocked"}, "log.o": {"putc"}},
            "libsmb2": {"init.c.o": {"feof"}},
            "libpng": {"pngwrite.o": {"ferror"}},
        }
        for package, members in checks.items():
            receipt = STAGE / package / "receipt.json"
            if not receipt.exists():
                self.skipTest("complete migration build required")
            archive = json.loads(receipt.read_text())["outputs"][0]["rebuilt"]
            for member, expected in members.items():
                with self.subTest(package=package, member=member):
                    raw = subprocess.check_output([str(SDK / "bin/llvm-ar"), "p", archive, member])
                    obj = self.work / member
                    obj.write_bytes(raw)
                    symbols = migration.archive_symbols(SDK / "bin/llvm-nm", obj)
                    self.assertTrue(expected <= symbols, (expected, symbols))
        fmt = STAGE / "fmt/libfmt.a"
        if fmt.exists():
            symbols = subprocess.check_output(
                [str(SDK / "bin/llvm-nm"), "-C", str(fmt)], text=True)
            self.assertNotIn("apple_file", symbols)
            self.assertNotIn("glibc_file", symbols)
            self.assertNotIn("file_print_buffer<__sFILE", symbols)

    def test_bzip2_stdio_stream_round_trip(self):
        source = self.source("bzip2")
        sources = [source / (name + ".c") for name in
                   ("blocksort", "huffman", "crctable", "randtable", "compress", "decompress", "bzlib")]
        self.host(r"""
#include <bzlib.h>
#include <stdlib.h>
#include <string.h>
#define CHECK(x) do { if (!(x)) abort(); } while (0)
FILE *__stdinp, *__stdoutp, *__stderrp;
int main(void) {
  const char input[] = "opaque stdio: bzip2 stream round trip\n";
  int error;
  FILE *file = fopen("roundtrip.bz2", "w+b");
  CHECK(file);
  BZFILE *writer = BZ2_bzWriteOpen(&error, file, 9, 0, 30);
  CHECK(writer && error == BZ_OK);
  BZ2_bzWrite(&error, writer, (void *)input, sizeof(input));
  CHECK(error == BZ_OK);
  BZ2_bzWriteClose(&error, writer, 0, NULL, NULL);
  CHECK(error == BZ_OK);
  rewind(file);
  BZFILE *reader = BZ2_bzReadOpen(&error, file, 0, 0, NULL, 0);
  CHECK(reader && error == BZ_OK);
  char output[128];
  int count = BZ2_bzRead(&error, reader, output, sizeof(output));
  CHECK(error == BZ_STREAM_END && count == sizeof(input));
  CHECK(!memcmp(input, output, sizeof(input)));
  BZ2_bzReadClose(&error, reader);
  CHECK(error == BZ_OK && fclose(file) == 0);
  return 0;
}
""", sources=sources, includes=[source])

    def test_fmt_public_stdio_fallback(self):
        source = self.source("fmt")
        self.host(r"""
#include <fmt/format.h>
#include <cstdlib>
#include <cstring>
FILE *__stdinp, *__stdoutp, *__stderrp;
int main() {
  FILE *file = fopen("formatted.txt", "w+b");
  if (!file) std::abort();
  __stdoutp = __stderrp = file;
  fmt::print(file, "{} {}\n", "opaque", 24);
  fflush(file);
  rewind(file);
  char buffer[64] = {};
  auto size = fread(buffer, 1, sizeof(buffer), file);
  if (size != 10 || std::strcmp(buffer, "opaque 24\n")) std::abort();
  return fclose(file);
}
""", sources=[source / "src/format.cc", source / "src/os.cc"],
                  includes=[source / "include"], flags=["-DFMT_USE_FALLBACK_FILE=1"], cpp=True)

    def test_png_stdio_pixel_round_trip(self):
        native_source = self.source("libpng")
        source = self.work / "png-host"
        shutil.copytree(native_source, source,
                        ignore=shutil.ignore_patterns(".libs", ".deps", "*.o", "*.a", "*.la"))
        compiler = shutil.which("clang-18")
        if not compiler:
            self.skipTest("clang18 host compiler required")
        macros = subprocess.check_output(
            [compiler, "-dM", "-E", "-x", "c", "-include", "zlib.h", "-"],
            input="", text=True, env={**os.environ, "TMPDIR": str(self.work)})
        version = re.search(r"^#define ZLIB_VERNUM (\S+)", macros, re.M).group(1)
        # Only this host fixture follows host zlib; native feature headers stay byte-identical.
        header = source / "pnglibconf.h"
        text, count = re.subn(r"(#define PNG_ZLIB_VERNUM) \S+", r"\g<1> " + version,
                             header.read_text())
        self.assertEqual(count, 1)
        header.write_text(text)
        names = ("png", "pngerror", "pngget", "pngmem", "pngpread", "pngread",
                 "pngrio", "pngrtran", "pngrutil", "pngset", "pngtrans",
                 "pngwio", "pngwrite", "pngwtran", "pngwutil")
        sources = [source / (name + ".c") for name in names]
        sources += list((source / "intel").glob("*.c"))
        self.host(r"""
#include <png.h>
#include <stdlib.h>
#include <string.h>
#define CHECK(x) do { if (!(x)) abort(); } while (0)
FILE *__stdinp, *__stdoutp, *__stderrp;
int main(void) {
  FILE *file = fopen("pixel.png", "w+b");
  CHECK(file);
  png_structp writer = png_create_write_struct(PNG_LIBPNG_VER_STRING, 0, 0, 0);
  png_infop info = png_create_info_struct(writer);
  CHECK(writer && info);
  if (setjmp(png_jmpbuf(writer))) abort();
  png_init_io(writer, file);
  png_set_IHDR(writer, info, 1, 1, 8, PNG_COLOR_TYPE_RGB, PNG_INTERLACE_NONE,
               PNG_COMPRESSION_TYPE_DEFAULT, PNG_FILTER_TYPE_DEFAULT);
  png_write_info(writer, info);
  unsigned char pixel[3] = {17, 33, 99};
  png_write_row(writer, pixel);
  png_write_end(writer, info);
  png_destroy_write_struct(&writer, &info);
  rewind(file);
  png_structp reader = png_create_read_struct(PNG_LIBPNG_VER_STRING, 0, 0, 0);
  info = png_create_info_struct(reader);
  CHECK(reader && info);
  if (setjmp(png_jmpbuf(reader))) abort();
  png_init_io(reader, file);
  png_read_png(reader, info, PNG_TRANSFORM_IDENTITY, 0);
  CHECK(png_get_image_width(reader, info) == 1);
  CHECK(!memcmp(png_get_rows(reader, info)[0], pixel, 3));
  png_destroy_read_struct(&reader, &info, 0);
  return fclose(file);
}
""", sources=sources, includes=[source], flags=["-lz", "-lm"])

    def test_curl_line_eof_and_error_paths(self):
        source = self.source("curl") / "lib/curl_get_line.c"
        self.host(r"""
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#define HEADER_CURL_SETUP_H
#define HEADER_CURL_GET_LINE_H
#define CHECK(x) do { if (!(x)) abort(); } while (0)
typedef int CURLcode;
enum { CURLE_OK, CURLE_READ_ERROR, CURLE_OUT_OF_MEMORY };
struct dynbuf { char bytes[512]; size_t size; };
static void curlx_dyn_reset(struct dynbuf *b) { b->size = 0; b->bytes[0] = 0; }
static CURLcode curlx_dyn_addn(struct dynbuf *b, const char *p, size_t n) {
  if (n + b->size >= sizeof(b->bytes)) return CURLE_OUT_OF_MEMORY;
  memcpy(b->bytes + b->size, p, n); b->size += n; b->bytes[b->size] = 0;
  return CURLE_OK;
}
static size_t curlx_dyn_len(struct dynbuf *b) { return b->size; }
static char *curlx_dyn_ptr(struct dynbuf *b) { return b->bytes; }
""" + '#include "' + str(source) + '"\n' + r"""
FILE *__stdinp, *__stdoutp, *__stderrp;
int main(void) {
  FILE *file = fopen("lines.txt", "w+b");
  CHECK(file && fputs("first\nlast", file) >= 0);
  rewind(file);
  struct dynbuf buffer;
  bool eof = false;
  CHECK(Curl_get_line(&buffer, file, &eof) == CURLE_OK);
  CHECK(!eof && !strcmp(buffer.bytes, "first\n"));
  CHECK(Curl_get_line(&buffer, file, &eof) == CURLE_OK);
  CHECK(eof && !strcmp(buffer.bytes, "last\n"));
  CHECK(!fclose(file));
  file = fopen("write-only.txt", "wb");
  CHECK(file);
  CHECK(Curl_get_line(&buffer, file, &eof) == CURLE_READ_ERROR);
  return fclose(file);
}
""")

    def test_gettext_log_escaping_uses_public_putc(self):
        source = self.source("gettext") / "gettext-runtime/intl/log.c"
        (self.work / "glthread").mkdir()
        (self.work / "glthread/lock.h").write_text(
            "#define __libc_lock_define_initialized(a,b)\n"
            "#define __libc_lock_lock(a) ((void)0)\n"
            "#define __libc_lock_unlock(a) ((void)0)\n")
        self.host("#define _GETTEXTP_H\n" + '#include "' + str(source) + '"\n' + r"""
FILE *__stdinp, *__stdoutp, *__stderrp;
int main(void) {
  _nl_log_untranslated("messages.log", "domain", "a\"b\n", 0, 0);
  if (fflush(last_logfile)) abort();
  FILE *file = fopen("messages.log", "rb");
  if (!file) abort();
  char buffer[256] = {0};
  (void)fread(buffer, 1, sizeof(buffer)-1, file);
  if (!strstr(buffer, "msgid \"a\\\"b\\n\"")) abort();
  if (!strstr(buffer, "msgstr \"\"")) abort();
  return fclose(file);
}
""", includes=[self.work])

    def test_gettext_native_thread_configuration_and_imports(self):
        source = self.source("gettext")
        receipt = json.loads((STAGE / "gettext/receipt.json").read_text())
        upstream = Path(receipt["provenance"]["configured_source"])
        for filename, override in receipt["provenance"]["native_thread_configuration"].items():
            with self.subTest(configuration=filename):
                original = (upstream / filename).read_text()
                self.assertEqual(migration.digest(upstream / filename), override["original_sha256"])
                text = (source / filename).read_text()
                self.assertEqual(migration.digest(source / filename), override["native_sha256"])
                if filename.endswith(".h"):
                    expected = original
                else:
                    expected = original.replace(str(upstream), str(source))
                    for old, new in ((SDK / "bin/prospero-clang++", STAGE / "gettext/cxx"),
                                     (SDK / "bin/prospero-clang", STAGE / "gettext/cc")):
                        expected = expected.replace("/usr/bin/ccache " + str(old), str(new))
                        expected = expected.replace(str(old), str(new))
                for name, value in migration.GETTEXT_THREAD_CONFIG.items():
                    old = f"#define {name} 1" if filename.endswith(".h") else f'D["{name}"]=" 1"'
                    new = f"#define {name} {value}" if filename.endswith(".h") else f'D["{name}"]=" {value}"'
                    expected = expected.replace(old, new)
                self.assertEqual(text, expected, "unrelated configuration changed")
        archive = Path(receipt["outputs"][0]["rebuilt"])
        self.assertEqual(migration.validate_gettext_threads(SDK / "bin/llvm-nm", archive),
                         receipt["validation"]["threading"])
        members, _ = migration.archive_members(archive.read_bytes())
        for member, raw in members.items():
            obj = self.work / member
            obj.write_bytes(raw)
            symbols = migration.archive_symbols(SDK / "bin/llvm-nm", obj)
            self.assertNotIn("thrd_exit", symbols, member)
        for member in ("localealias.o", "log.o", "dcigettext.o", "libgnu_la-lock.o"):
            obj = self.work / member
            imports = migration.archive_symbols(SDK / "bin/llvm-nm", obj)
            self.assertTrue({"pthread_mutex_lock", "pthread_mutex_unlock"} <= imports, member)

    def test_gettext_pthread_locks_are_functional(self):
        source = self.source("gettext") / "gettext-runtime/intl"
        # Run unchanged gnulib lock sources with the native threading flags,
        # but host pthread types/functions. This is not execution of PS5 objects.
        configuration = (source / "config.h").read_text().splitlines()
        names = {"_GL_CONFIG_H_INCLUDED", "HAVE_PTHREAD_API", "HAVE_PTHREAD_MUTEX_RECURSIVE",
                 "HAVE_PTHREAD_RWLOCK", "HAVE_PTHREAD_RWLOCK_RDLOCK_PREFER_WRITER",
                 "HAVE_THREADS_H", "USE_POSIX_THREADS", "USE_POSIX_THREADS_WEAK",
                 "USE_POSIX_THREADS_FROM_LIBC", "PTHREAD_IN_USE_DETECTION_HARD",
                 "USE_ISOC_THREADS", "USE_ISOC_AND_POSIX_THREADS", "USE_WINDOWS_THREADS"}
        defines = [line for line in configuration
                   if line.startswith("#define ") and line.split()[1] in names]
        (self.work / "config.h").write_text(
            "\n".join(defines) + "\n#include <pthread.h>\n"
            "#undef PTHREAD_RWLOCK_WRITER_NONRECURSIVE_INITIALIZER_NP\n")
        self.host(r"""
#include <config.h>
#include <pthread.h>
#include <sched.h>
#include "glthread/lock.h"
#define CHECK(x) do { if (!(x)) abort(); } while (0)
FILE *__stdinp, *__stdoutp, *__stderrp;
gl_lock_define_initialized(static, mutex)
gl_rwlock_define_initialized(static, rwlock)
gl_recursive_lock_define_initialized(static, recursive)
gl_once_define(static, once)
static int counter, rwcounter, initialized;
static void initialize(void) { ++initialized; }
static void *worker(void *unused) {
  (void)unused;
  for (int i = 0; i < 2000; ++i) {
    CHECK(!glthread_once(&once, initialize));
    CHECK(!glthread_lock_lock(&mutex));
    int value = counter;
    sched_yield();
    counter = value + 1;
    CHECK(!glthread_lock_unlock(&mutex));
    CHECK(!glthread_rwlock_wrlock(&rwlock));
    value = rwcounter;
    sched_yield();
    rwcounter = value + 1;
    CHECK(!glthread_rwlock_unlock(&rwlock));
    CHECK(!glthread_rwlock_rdlock(&rwlock));
    CHECK(rwcounter > 0);
    CHECK(!glthread_rwlock_unlock(&rwlock));
  }
  return NULL;
}
int main(void) {
  CHECK(pthread_in_use());
  CHECK(!c11_threads_in_use());
  CHECK(!glthread_lock_lock(&mutex));
  CHECK(pthread_mutex_trylock(&mutex) == EBUSY);
  CHECK(!glthread_lock_unlock(&mutex));
  CHECK(!glthread_recursive_lock_lock(&recursive));
  CHECK(!glthread_recursive_lock_lock(&recursive));
  CHECK(!glthread_recursive_lock_unlock(&recursive));
  CHECK(!glthread_recursive_lock_unlock(&recursive));
  pthread_t workers[4];
  for (int i = 0; i < 4; ++i) CHECK(!pthread_create(&workers[i], NULL, worker, NULL));
  for (int i = 0; i < 4; ++i) CHECK(!pthread_join(workers[i], NULL));
  CHECK(counter == 8000 && rwcounter == 8000 && initialized == 1);
  CHECK(!glthread_lock_destroy(&mutex));
  CHECK(!glthread_rwlock_destroy(&rwlock));
  CHECK(!glthread_recursive_lock_destroy(&recursive));
  return 0;
}
""", sources=[source / "gnulib-lib/glthread/lock.c"],
                  includes=[self.work],
                  flags=["-pthread", "-D_GNU_SOURCE", "-iquote", str(source / "gnulib-lib")])

    def test_openssl_file_read_source_success_eof_error(self):
        source = self.source("openssl") / "crypto/bio/bss_file.c"
        function = self.source_function(source, "file_read")
        self.host(r"""
#include <stdlib.h>
#include <string.h>
#define CHECK(x) do { if (!(x)) abort(); } while (0)
typedef struct { int init; int flags; void *ptr; } BIO;
enum { BIO_FLAGS_UPLINK_INTERNAL = 1, ERR_LIB_SYS = 1, ERR_LIB_BIO = 2, ERR_R_SYS_LIB = 3 };
static int errors;
#define UP_fread fread
#define UP_ferror ferror
#define get_last_sys_error() 1
#define ERR_raise_data(a,b,c) (++errors)
#define ERR_raise(a,b) (++errors)
""" + function + r"""
FILE *__stdinp, *__stdoutp, *__stderrp;
int main(void) {
  FILE *file = fopen("bio.txt", "w+b");
  CHECK(file && fwrite("BIO", 1, 3, file) == 3);
  rewind(file);
  BIO bio = {1, 0, file};
  char bytes[8] = {0};
  CHECK(file_read(&bio, bytes, sizeof(bytes)) == 3 && !memcmp(bytes, "BIO", 3));
  CHECK(file_read(&bio, bytes, sizeof(bytes)) == 0 && errors == 0);
  CHECK(!fclose(file));
  file = fopen("write-only.txt", "wb");
  CHECK(file);
  bio.ptr = file;
  CHECK(file_read(&bio, bytes, sizeof(bytes)) == -1 && errors == 2);
  return fclose(file);
}
""")

    def test_smb2_password_file_source_eof(self):
        source = self.source("libsmb2") / "lib/init.c"
        function = self.source_function(source, "smb2_set_password_from_file")
        self.host(r"""
#include <stdlib.h>
#include <string.h>
#define CHECK(x) do { if (!(x)) abort(); } while (0)
struct smb2_context { const char *user; char password[256]; };
static void smb2_set_password(struct smb2_context *s, const char *p) {
  if (!p) s->password[0] = 0;
  else { CHECK(strlen(p) < sizeof(s->password)); strcpy(s->password, p); }
}
""" + function + r"""
FILE *__stdinp, *__stdoutp, *__stderrp;
int main(void) {
  FILE *file = fopen("passwords.txt", "wb");
  CHECK(file && fputs("malformed\ndomain:other:wrong\ndomain:alice:correct", file) >= 0);
  CHECK(!fclose(file));
  CHECK(!setenv("NTLM_USER_FILE", "passwords.txt", 1));
  struct smb2_context context = {"alice", ""};
  smb2_set_password_from_file(&context);
  CHECK(!strcmp(context.password, "correct"));
  return 0;
}
""", flags=["-D_POSIX_C_SOURCE=200809L"])


if __name__ == "__main__":
    program = unittest.main(exit=False)
    result = program.result
    if STAGE.is_dir():
        migration.write_json(STAGE / "test-results.json", {
            "tests": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "complete": (result.wasSuccessful() and not result.skipped and
                         result.testsRun == len(unittest.defaultTestLoader.getTestCaseNames(MigrationTest))),
            "test_sha256": migration.digest(__file__),
            "receipt_sha256": {name: migration.digest(STAGE / name / "receipt.json")
                               for name in migration.PACKAGES
                               if (STAGE / name / "receipt.json").is_file()},
            "limitations": [
                "Native archives are structurally/compile validated, not executed on a console.",
                "Host source probes use opaque FILE with host public stdio and sanitizers.",
                "OpenSSL/SMB function excerpts use BIO/context fixtures; curl dynbuf and gettext "
                "locking are fixtures. PNG host-only config follows host zlib version; native "
                "pnglibconf.h stays byte-identical to the installed1.6.43 configuration.",
            ],
        })
    sys.exit(0 if result.wasSuccessful() else 1)
