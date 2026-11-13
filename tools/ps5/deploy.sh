#!/usr/bin/env bash
# Upload a completed title stage; never build, package or control applications.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ "$#" != 1 ] || [ -z "$1" ]; then
  echo "Usage: BUILD=<configured-build> [STAGE=<existing-stage>] $0 <console-host>" >&2
  exit 1
fi
HOST="$1"
[[ "$HOST" =~ ^[a-zA-Z0-9._:-]+$ ]] && [[ "$HOST" != -* ]] || {
  echo "Invalid console host" >&2; exit 1; }
BUILD="${BUILD:-$ROOT/build/ps5-release}"
cache_path() { sed -n "s/^$1:[^=]*=//p" "$BUILD/CMakeCache.txt"; }
STAGE="${STAGE:-$(cache_path PS5_PACKAGE_STAGE)}"
STAGE="${STAGE:-$BUILD/title}"
PS5_DEPLOY_JOBS="${PS5_DEPLOY_JOBS:-8}"
PS5_FTP_PORT="${PS5_FTP_PORT:-2121}"
[[ "$PS5_DEPLOY_JOBS" =~ ^[1-8]$ ]] || {
  echo "!! PS5_DEPLOY_JOBS must be between 1 and 8" >&2; exit 1; }
[[ "$PS5_FTP_PORT" =~ ^[0-9]+$ ]] && (( 10#$PS5_FTP_PORT >= 1 && 10#$PS5_FTP_PORT <= 65535 )) || {
  echo "!! PS5_FTP_PORT must be between 1 and 65535" >&2; exit 1; }
command -v lftp >/dev/null || {
  echo "!! lftp is required for deployment; install lftp or copy the folder manually" >&2; exit 1; }
STAGE="$(realpath -ms "$STAGE")"
[ -f "$STAGE/.ps5-package-stage" ] && [ ! -L "$STAGE" ] || {
  echo "!! Not a managed completed package stage: $STAGE" >&2; exit 1; }
STAGE="$(realpath -e "$STAGE")"
exec 9>"$STAGE.lock"
flock -n 9 || { echo "!! Stage is in use: $STAGE" >&2; exit 1; }
SDK="${PS5_NATIVE_RUNTIME_SDK:-$(cache_path PS5_NATIVE_RUNTIME_SDK)}"
MAP="${PS5_NATIVE_DEPENDS_MAP:-$(cache_path PS5_NATIVE_DEPENDS_MAP)}"
APP="$STAGE/app"
TITLE_ID="$(python3 - "$APP/sce_sys/param.json" <<'PY'
import json, re, sys
with open(sys.argv[1]) as stream:
    title = json.load(stream)["titleId"]
if not isinstance(title, str) or not re.fullmatch(r"[A-Z]{4}[0-9]{5}", title):
    sys.exit("Invalid staged titleId")
print(title)
PY
)"
DIST="$APP/dist/$TITLE_ID"
[ -f "$DIST/ps5-shared-title-proof.json" ] || {
  echo "!! Missing completed-title verification receipt; package first" >&2; exit 1; }
python3 "$ROOT/tools/ps5/native_contract.py" --sdk "$SDK" --archive-map "$MAP" >/dev/null
python3 "$ROOT/tools/ps5/native_graphics.py" --prepared-sdk "$SDK"
python3 "$ROOT/tools/ps5/verify-shared-title.py" --app "$APP" --sdk "$SDK" --archive-map "$MAP"
# lftp's command language quotes paths separately from the shell.
[[ "$DIST" != *"'"* && "$DIST" != *$'\n'* && "$DIST" != *$'\r'* ]] || {
  echo "!! Stage path cannot contain quotes or newlines for FTP deployment" >&2; exit 1; }
echo "==> uploading to ftp://$HOST:$PS5_FTP_PORT/data/homebrew/$TITLE_ID"
# Console timestamps can run ahead; always re-send the launcher and all modules.
lftp -e "set cmd:fail-exit yes; mirror -R --delete --exclude '^config[.]json$' --no-perms --parallel=$PS5_DEPLOY_JOBS --verbose '$DIST' '/data/homebrew/$TITLE_ID'; put -O '/data/homebrew/$TITLE_ID' '$DIST/eboot.bin'; mirror -R --transfer-all --no-perms --parallel=$PS5_DEPLOY_JOBS --verbose '$DIST/sce_module' '/data/homebrew/$TITLE_ID/sce_module'; quit" -p "$PS5_FTP_PORT" "$HOST"
echo "Uploaded title folder: $DIST"
