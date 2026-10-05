#!/usr/bin/env bash
# Build pacbrew packages in upstream's ci-libs.sh order, from a given package
# (through an optional last one). ci-libs.sh has no resume of its own: after
# any failure it starts over from the SDK.
#
#   pacbrew.sh sdk                   everything (what bootstrap.sh runs)
#   pacbrew.sh miniupnpc             resume: miniupnpc through the end
#   pacbrew.sh glu glew              a range: glu through glew only
#   SKIP="glu glew llvm mesa love"   packages to leave out (Kodi needs none of
#          pacbrew's GL stack - ps5-opengl builds its own compiler and Mesa;
#          upstream also lists glu/glew before the mesa they depend on)
#   REUSE=1 pacbrew.sh <package>     install the package's already-built
#          .pkg.tar.gz instead of rebuilding it (e.g. llvm built fine but the
#          `sudo pacman -U` step failed) - later packages are built as usual.
#
# Installs the ps5-payload-sdk package itself first (it is the first entry in
# ci-libs.sh), so this is also what puts $PS5_PAYLOAD_SDK on the machine.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
WORK="${WORK:-$ROOT/build/ps5-external}"
REPO="$WORK/pacbrew-repo"
START="${1:?usage: $0 <first-package> [last-package]  (names as in ci-libs.sh)}"
END="${2:-}"
SKIP="${SKIP:-}"
export MAKEFLAGS="${MAKEFLAGS:--j$(nproc)}"
cd "$REPO"

# sudo keep-alive: llvm/mesa outlast sudo's 15 minute credential cache, and
# every package ends in `sudo pacman -U`.
sudo -v
( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 60; done ) &
SUDO_KEEPALIVE=$!
trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null' EXIT

# The ordered package list, pulled out of ci-libs.sh itself (comments dropped).
PKGS=$(sed -n '/^PKGS=(/,/^ *)/p' ci-libs.sh | sed -e 's/^PKGS=(//' -e 's/#.*//' -e 's/)//' | tr -s ' \n' '\n' | sed '/^$/d')

FOUND=0
for PKG in $PKGS; do
  [ "$PKG" = "$START" ] && FOUND=1
  [ "$FOUND" -eq 1 ] || continue
  case " $SKIP " in *" $PKG "*) echo "==> $PKG (skipped)"; continue;; esac
  echo "==> $PKG"
  if [ "$PKG" = "$START" ] && [ "${REUSE:-0}" = 1 ] && ls "$PKG"/ps5-payload-*.pkg.tar.gz >/dev/null 2>&1; then
    echo "    reusing already-built package"
    ( cd "$PKG" && sudo pacman --config "$REPO/pacman.conf" --noconfirm -U ./ps5-payload-*.pkg.tar.gz ) \
      || { echo "!! installing $PKG failed"; exit 1; }
    continue
  fi
  ( cd "$PKG" && rm -f ./*.pkg.tar.gz && rm -rf src pkg && makepkg -c -f -C \
      && sudo pacman --config "$REPO/pacman.conf" --noconfirm -U ./ps5-payload-*.pkg.tar.gz ) \
    || { echo "!! $PKG failed; fix and re-run: $0 $PKG${END:+ $END}   (REUSE=1 if only the install step failed)"; exit 1; }
  [ "$PKG" = "$END" ] && break
done
[ "$FOUND" -eq 1 ] || { echo "'$START' is not in ci-libs.sh's package list"; exit 1; }
echo "==> pacbrew done${SKIP:+ (skipped: $SKIP)}"
