#!/usr/bin/env python3
"""Exercise C-locale rune behavior and check the target SDK's exact layout."""
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "shims/native-app/libc_locale.c"
SDK = Path(os.environ.get("PS5_PAYLOAD_SDK", "/opt/ps5-payload-sdk"))


class NativeLocaleTest(unittest.TestCase):
    def compile_probe(self, compiler, source, flags, execute=False):
        with tempfile.TemporaryDirectory(prefix="ps5-locale-") as temporary:
            root = Path(temporary)
            probe, output = root / "probe.c", root / "probe"
            probe.write_text(source)
            command = [str(compiler), "-std=c11", "-I" + str(SOURCE.parent),
                       *flags, str(probe), "-o", str(output)]
            subprocess.run(command, check=True, capture_output=True, text=True)
            if execute:
                subprocess.run([str(output)], check=True, capture_output=True, text=True)

    @unittest.skipUnless(shutil.which("clang-18"), "clang-18 is required")
    def test_c_locale_classification_and_case_conversion(self):
        self.compile_probe("clang-18", r'''
#include <assert.h>
#include <limits.h>
#include "libc_locale.c"

int main(void)
{
  int limit = 0;
  const ps5_rune_locale* runes = __runes_for_locale(NULL, &limit);
  assert(runes == &_DefaultRuneLocale);
  assert(limit == 256);
  assert(___mb_cur_max_l(NULL) == 1);
  assert(__runes_for_locale(NULL, NULL) == runes);
  for (int c = 0; c < 256; ++c)
  {
    int lower = c >= 'A' && c <= 'Z' ? c + ('a' - 'A') : c;
    int upper = c >= 'a' && c <= 'z' ? c - ('a' - 'A') : c;
    assert(___tolower_l(c, NULL) == lower);
    assert(___toupper_l(c, NULL) == upper);
    assert(___runetype_l(c, NULL) == runes->runetype[c]);
    if (c >= 'A' && c <= 'Z')
      assert(runes->runetype[c] & 0x100UL);
    if (c >= '0' && c <= '9')
      assert(runes->runetype[c] & 0x400UL);
  }
  const int uncached[] = {-1, INT_MIN, 256, 0x10000, INT_MAX};
  for (unsigned i = 0; i < sizeof(uncached) / sizeof(uncached[0]); ++i)
  {
    int c = uncached[i];
    assert(___runetype_l(c, NULL) == 0);
    assert(___tolower_l(c, NULL) == c);
    assert(___toupper_l(c, NULL) == c);
  }
  assert(runes->magic[7] == 'i');
  return 0;
}
''', ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"], execute=True)

    @unittest.skipUnless((SDK / "bin/prospero-clang").is_file(), "PS5 SDK is required")
    def test_rune_layout_matches_actual_sdk(self):
        self.compile_probe(SDK / "bin/prospero-clang", r'''
#define _DefaultRuneLocale __sdk_DefaultRuneLocale
#include <runetype.h>
#undef _DefaultRuneLocale
#include <stddef.h>
#include "libc_locale.c"

_Static_assert(sizeof(ps5_rune_locale) == sizeof(_RuneLocale), "rune locale size");
_Static_assert(_Alignof(ps5_rune_locale) == _Alignof(_RuneLocale), "rune locale alignment");
#define CHECK(local, sdk) \
  _Static_assert(offsetof(ps5_rune_locale, local) == offsetof(_RuneLocale, sdk), #local); \
  _Static_assert(sizeof(((ps5_rune_locale*)0)->local) == sizeof(((_RuneLocale*)0)->sdk), #local)
CHECK(magic, __magic);
CHECK(encoding, __encoding);
CHECK(sgetrune, __sgetrune);
CHECK(sputrune, __sputrune);
CHECK(invalid_rune, __invalid_rune);
CHECK(runetype, __runetype);
CHECK(maplower, __maplower);
CHECK(mapupper, __mapupper);
CHECK(runetype_ext, __runetype_ext);
CHECK(maplower_ext, __maplower_ext);
CHECK(mapupper_ext, __mapupper_ext);
CHECK(variable, __variable);
CHECK(variable_len, __variable_len);
''', ["--target=x86_64-unknown-freebsd", "-fPIC", "-c"])


if __name__ == "__main__":
    unittest.main()
