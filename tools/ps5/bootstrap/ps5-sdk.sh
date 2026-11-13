#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SDK="${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}"
WORK="${WORK:-$ROOT/build/ps5-external}"

identity="$(python3 "$HERE/check-sdk.py" --fingerprint)"
if [ -e "$SDK" ] || [ -L "$SDK" ]; then
  installed="$(python3 "$HERE/check-sdk.py" --fingerprint --sdk "$SDK")"
  if [ "$installed" = "$identity" ]; then
    exec python3 "$HERE/check-sdk.py" --sdk "$SDK"
  fi
  if ! pacman -Qqo "$SDK/.kodi-sdk.json" 2>/dev/null | grep -Fxq ps5-payload-sdk; then
    echo "!! Existing SDK is not owned by ps5-payload-sdk; installation preserved: $SDK" >&2
    exit 1
  fi
fi

prefix_identity="$(printf '%s' "$SDK" | sha256sum | cut -d' ' -f1)"
stage="$WORK/sdk-packages/$identity-$prefix_identity"
mkdir -p "$stage"
cp "$HERE/sdk/PKGBUILD" "$HERE/check-sdk.py" \
  "$HERE"/patches/ps5-payload-sdk/*.patch "$ROOT"/shims/sce_stubs/*.c "$stage/"
cd "$stage"
package="$(makepkg --packagelist)"
if [ -z "$package" ] || [[ "$package" == *$'\n'* ]]; then
  echo "!! SDK recipe must produce exactly one package" >&2
  exit 1
fi
if [ ! -f "$package" ]; then
  makepkg --force --cleanbuild --noconfirm
fi
sudo pacman --config "$HERE/sdk/pacman.conf" --noconfirm -U "$package"
python3 "$HERE/check-sdk.py" --sdk "$SDK"
