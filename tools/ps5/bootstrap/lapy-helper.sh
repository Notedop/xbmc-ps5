#!/usr/bin/env bash
# Check out the pinned Lapy jailbreak daemon and build the one-shot owned
# root helper ELF for Kodi's own title, for tools/ps5/package.sh to bundle
# into app0/share/kodi/system/elevation.
#
# Upstream: https://github.com/mpereiraesaa/PS5-Lapy-JB-Daemon (MIT, see its
# LICENSE, which package.sh stages next to the ELF). Pinned commit:
# patches/lapy/LAPY-JB-DAEMON-COMMIT. The elevation protocol header
# (source/lapy_elevation_protocol.h, LGPL-2.1-or-later - Kodi does NOT link
# it; the caller in xbmc/platform/ps5/ElevationElf.cpp reimplements the ABI)
# is checked against patches/lapy/LAPY-PROTOCOL-SHA256 so a silent upstream
# wire change cannot slip into a Kodi build.
#
# Logging client: the helper includes "ps5log.h"; the revision used by the
# inspected reference build is mpereiraesaa/ps5-agc-gears
# 1ae1f9182abd2770c131b97419034fb85173c2dc (native/ps5log/ps5log.h), checked
# against patches/lapy/PS5LOG-SHA256.
#
# Output: $WORK/ps5-lapy-jb-daemon/build/owned_root_helper-<TITLE_ID>/
#         {lapy.elf, lapy-manifest.json}
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # tools/ps5
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="${WORK:-$ROOT/build/ps5-external}"
SRC="$WORK/ps5-lapy-jb-daemon"
SOURCE_URL="https://github.com/mpereiraesaa/PS5-Lapy-JB-Daemon.git"
PS5LOG="$WORK/ps5log-1ae1f918"
PS5LOG_URL="https://raw.githubusercontent.com/mpereiraesaa/ps5-agc-gears/1ae1f9182abd2770c131b97419034fb85173c2dc/native/ps5log/ps5log.h"
export PS5_PAYLOAD_SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"

COMMIT="$(tr -d '[:space:]' < "$HERE/patches/lapy/LAPY-JB-DAEMON-COMMIT")"
PROTOCOL_SHA256="$(tr -d '[:space:]' < "$HERE/patches/lapy/LAPY-PROTOCOL-SHA256")"
PS5LOG_SHA256="$(tr -d '[:space:]' < "$HERE/patches/lapy/PS5LOG-SHA256")"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "!! invalid LAPY-JB-DAEMON-COMMIT" >&2; exit 1; }

TITLE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["titleId"])' \
  "$ROOT/title/sce_sys/param.json")"

mkdir -p "$WORK"

if [ ! -d "$SRC/source" ]; then
  echo "==> cloning PS5-Lapy-JB-Daemon at $COMMIT"
  [ -e "$SRC/.git" ] || git clone --no-checkout "$SOURCE_URL" "$SRC"
  git -C "$SRC" fetch --no-tags --quiet "$SOURCE_URL" "$COMMIT"
  git -C "$SRC" checkout --detach -q "$COMMIT"
fi
ACTUAL="$(git -C "$SRC" rev-parse HEAD)"
if [ "$ACTUAL" != "$COMMIT" ]; then
  echo "!! PS5-Lapy-JB-Daemon at $SRC is $ACTUAL, but Kodi requires $COMMIT" >&2
  echo "!! Existing sources were preserved. Use a new WORK directory for the upgrade." >&2
  exit 1
fi
[ -z "$(git -C "$SRC" status --porcelain)" ] || {
  echo "!! $SRC has local changes; the helper must build from pristine pinned sources" >&2
  exit 1; }
[ "$(sha256sum "$SRC/source/lapy_elevation_protocol.h" | cut -d' ' -f1)" = "$PROTOCOL_SHA256" ] || {
  echo "!! elevation protocol header differs from patches/lapy/LAPY-PROTOCOL-SHA256" >&2
  exit 1; }

if [ "$(sha256sum "$PS5LOG/ps5log.h" 2>/dev/null | cut -d' ' -f1)" != "$PS5LOG_SHA256" ]; then
  echo "==> fetching pinned ps5log.h"
  mkdir -p "$PS5LOG"
  curl -fsSL "$PS5LOG_URL" -o "$PS5LOG/ps5log.h"
  [ "$(sha256sum "$PS5LOG/ps5log.h" | cut -d' ' -f1)" = "$PS5LOG_SHA256" ] || {
    echo "!! ps5log.h differs from patches/lapy/PS5LOG-SHA256" >&2
    exit 1; }
fi

OUT="$SRC/build/owned_root_helper-$TITLE_ID"
if [ ! -f "$OUT/lapy.elf" ] || [ ! -f "$OUT/lapy-manifest.json" ]; then
  echo "==> building owned-helper for $TITLE_ID"
  make -C "$SRC" owned-helper TARGET_TITLE="$TITLE_ID" LOGGING_CLIENT="$PS5LOG"
else
  echo "==> owned-helper already built for $TITLE_ID"
fi

python3 - "$OUT/lapy-manifest.json" "$TITLE_ID" "$PROTOCOL_SHA256" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1]))
assert manifest.get("target_title") == sys.argv[2], "helper built for another title"
assert manifest.get("mode") == "elf-helper", "helper is not the one-shot ELF-helper mode"
assert manifest.get("max_requests") == 1, "helper may serve more than one request"
assert manifest.get("protocol_sha256") == sys.argv[3], "helper protocol differs from pin"
PY
head -c4 "$OUT/lapy.elf" | grep -q $'\x7fELF' || { echo "!! helper output is not ELF"; exit 1; }
echo "Lapy owned-helper ready: $OUT (build_id $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build_id"])' "$OUT/lapy-manifest.json" | cut -c1-12)...)"
