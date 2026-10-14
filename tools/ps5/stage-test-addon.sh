#!/usr/bin/env bash
# Validate the isolated probe before staging is cleared, or copy it after install.
set -euo pipefail

OUT="${1:?expected probe build directory}"
DEST="${2:---check}"
for file in addon.xml test_binary_ps5.so inputs.sha256 artifacts.sha256 build-id.txt elf-report.txt; do
  [ -s "$OUT/$file" ] || {
    echo "!! missing probe output: $OUT/$file; run tools/ps5/build-test-addon.sh" >&2
    exit 1
  }
done
if ! sha256sum --check --status "$OUT/inputs.sha256" ||
   ! (cd "$OUT" && sha256sum --check --status artifacts.sha256) ||
   [ "$(<"$OUT/build-id.txt")" != "$(sha256sum "$OUT/inputs.sha256" | cut -c1-16)" ]; then
  echo "!! stale or modified probe; rerun tools/ps5/build-test-addon.sh" >&2
  exit 1
fi

if [ "$DEST" != "--check" ]; then
  mkdir -p "$DEST/addons/test.binary.ps5"
  cp "$OUT/addon.xml" "$OUT/test_binary_ps5.so" "$DEST/addons/test.binary.ps5/"
  (cd "$DEST/addons/test.binary.ps5" && sha256sum --check --status "$OUT/artifacts.sha256")
fi
echo "Verified binary add-on probe: $(<"$OUT/build-id.txt")"
