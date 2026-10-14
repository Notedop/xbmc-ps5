#!/usr/bin/env bash
# Build an import-only ELF probe for Kodi's PS5 in-process loader.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
BUILD="${BUILD:-$ROOT/build/ps5-release}"
OUT="$BUILD/test-addons/test.binary.ps5"
HEADERS="$ROOT/xbmc/addons/kodi-dev-kit/include"
SOURCE="$ROOT/addons/test.binary.ps5"
CXX="$SDK/bin/prospero-clang++"
READELF="${READELF:-readelf}"
LINKER="$("$SDK/bin/prospero-llvm-config" --bindir)/ld.lld"

for tool in "$CXX" "$LINKER" "$READELF" python3 sha256sum; do
  command -v "$tool" >/dev/null || { echo "!! missing tool: $tool" >&2; exit 1; }
done
mkdir -p "$OUT"
TMP="$(mktemp -d "$OUT/.build.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

{
  printf '%s\0' "$SOURCE/test_binary_ps5.cpp" "$SOURCE/addon.xml" \
    "$ROOT/tools/ps5/build-test-addon.sh" "$CXX" "$SDK/bin/clang++" "$LINKER"
  find "$HEADERS" "$SDK/target/include" "$SDK/target/include_common" -type f -print0
} | sort -z | xargs -0 sha256sum > "$TMP/inputs.sha256"
IDENTITY="$(sha256sum "$TMP/inputs.sha256" | cut -c1-16)"

# Bypass prospero-lld's executable linker script: this must be an ordinary
# ET_DYN with .init_array, not a title. The FreeBSD ABI uses the same SDK
# headers/runtime without the SCE driver's forced legacy .ctors output.
"$CXX" --target=x86_64-unknown-freebsd -std=c++17 -O2 -g -fPIC -fvisibility=hidden \
  -idirafter "$SDK/target/include" -idirafter "$SDK/target/include_common" -fuse-init-array \
  -fexceptions -frtti -funwind-tables -c -nostdlib \
  "-I$HEADERS" "-DPS5_TEST_BUILD_ID=\"$IDENTITY\"" \
  "$SOURCE/test_binary_ps5.cpp" -o "$TMP/probe.o"
"$LINKER" --shared -m elf_x86_64 --hash-style=both --eh-frame-hdr \
  -z max-page-size=0x4000 "$TMP/probe.o" -o "$TMP/test_binary_ps5.so"

"$READELF" -hW -lW -dW -sW -rW "$TMP/test_binary_ps5.so" > "$TMP/elf-report.txt"
python3 - "$TMP/test_binary_ps5.so" "$SOURCE/addon.xml" "$HEADERS/kodi/versions.h" <<'PY'
import pathlib
import re
import struct
import sys
import xml.etree.ElementTree as ET

data = pathlib.Path(sys.argv[1]).read_bytes()

def require(condition, message):
    if not condition:
        raise SystemExit("!! incompatible test add-on: " + message)

require(len(data) >= 64 and data[:6] == b"\x7fELF\x02\x01", "not ELF64 little-endian")
header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
require(header[1:3] == (3, 62), "expected x86-64 ET_DYN")
phoff, shoff = header[5:7]
phsize, phnum, shsize, shnum = header[9:13]
require(phsize == 56 and shsize == 64 and phnum and shnum, "missing ELF tables")
require(phoff + phsize * phnum <= len(data) and shoff + shsize * shnum <= len(data),
        "truncated ELF tables")
programs = [struct.unpack_from("<IIQQQQQQ", data, phoff + i * phsize) for i in range(phnum)]
sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shsize) for i in range(shnum)]

def section_bytes(section):
    offset, size = section[4:6]
    require(offset + size <= len(data), "truncated section")
    return data[offset:offset + size]

def string(table, offset):
    end = table.find(b"\0", offset)
    require(0 <= offset < len(table) and end >= offset, "invalid ELF string")
    return table[offset:end].decode("ascii")

loads = [p for p in programs if p[0] == 1]
require(loads and any(p[0] == 2 for p in programs), "missing PT_LOAD/PT_DYNAMIC")
require(not any(p[0] == 7 for p in programs), "PT_TLS is unsupported")
require(any(p[0] == 0x6474e550 for p in programs), "missing PT_GNU_EH_FRAME")
page = 0x4000
for i, p in enumerate(loads):
    require(p[5] <= p[6] and p[2] + p[5] <= len(data), "invalid PT_LOAD bounds")
    require(p[1] & 3 != 3, "writable executable segment")
    start, end = p[3] // page, (p[3] + p[6] + page - 1) // page
    for q in loads[:i]:
        qstart, qend = q[3] // page, (q[3] + q[6] + page - 1) // page
        require(max(start, qstart) >= min(end, qend) or p[1] == q[1],
                "PT_LOAD protections overlap on a 16 KiB page")

dynamic = next(s for s in sections if s[1] == 6)
tags = dict(struct.iter_unpack("<qQ", section_bytes(dynamic)))
require(1 not in tags, "DT_NEEDED dependencies are not loaded")
require(4 in tags or 0x6ffffef5 in tags, "missing dynamic hash")
require(5 in tags and 6 in tags, "missing dynamic string/symbol tables")
require(25 in tags and tags.get(27, 0) > 0, "missing static constructor array")
require(36 not in tags and 17 not in tags, "RELR/REL relocations are unsupported")
require(0x6ffffffe not in tags, "versioned runtime dependencies are unsupported")

dynsym = next(s for s in sections if s[1] == 11)
require(dynsym[9] == 24, "wrong dynamic symbol size")
strings = section_bytes(sections[dynsym[6]])
symbols = list(struct.iter_unpack("<IBBHQQ", section_bytes(dynsym)))
exports = {string(strings, s[0]) for s in symbols if s[3] != 0 and s[1] >> 4 in (1, 2)}
require({"ADDON_Create", "ADDON_GetTypeVersion", "ADDON_GetTypeMinVersion"} <= exports,
        "missing dev-kit entry points")
require(not any(s[1] & 15 == 6 for s in symbols), "TLS symbols are unsupported")
relocations = 0
for section in sections:
    if section[1] != 4 or not section[2] & 2:
        continue
    require(section[9] == 24, "wrong RELA entry size")
    for offset, info, addend in struct.iter_unpack("<QQq", section_bytes(section)):
        kind, index = info & 0xffffffff, info >> 32
        require(kind in (1, 6, 7, 8), "unsupported relocation type " + str(kind))
        require(index < len(symbols), "invalid relocation symbol index")
        require(any(p[3] <= offset and offset + 8 <= p[3] + p[6] for p in loads),
                "relocation target outside PT_LOAD")
        relocations += 1

manifest = ET.parse(sys.argv[2]).getroot()
versions = pathlib.Path(sys.argv[3]).read_text()
version = re.search(r'#define\s+ADDON_INSTANCE_VERSION_SCREENSAVER\s+"([^"]+)"', versions)
require(version is not None, "cannot find screensaver dev-kit version")
dependency = manifest.find("./requires/import[@addon='kodi.binary.instance.screensaver']")
require(dependency is not None and dependency.get("version") == version[1],
        "manifest screensaver version differs from dev-kit")
extension = manifest.find("./extension[@point='xbmc.ui.screensaver']")
require(extension is not None and extension.get("library_freebsd") == "test_binary_ps5.so",
        "wrong manifest library")
imports = sorted(string(strings, s[0]) for s in symbols if s[3] == 0 and s[0])
print("ELF preflight PASS: %d relocations; no TLS/dependencies; 16 KiB layout" % relocations)
print("Runtime imports (must resolve through the console eboot's GOT):")
print("\n".join("  " + name for name in imports))
PY

cp "$SOURCE/addon.xml" "$TMP/addon.xml"
printf '%s\n' "$IDENTITY" > "$TMP/build-id.txt"
(cd "$TMP" && sha256sum addon.xml test_binary_ps5.so > artifacts.sha256)
for file in addon.xml test_binary_ps5.so inputs.sha256 artifacts.sha256 build-id.txt elf-report.txt; do
  mv "$TMP/$file" "$OUT/$file"
done
echo "Probe build: $IDENTITY"
echo "Verified output: $OUT"
