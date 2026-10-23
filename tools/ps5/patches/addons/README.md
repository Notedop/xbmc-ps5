# PS5 add-on source patches

PS5 configuration of the binary add-ons (`cmake/addons/CMakeLists.txt`) applies
the series in `<addon-id>/` to that add-on's freshly extracted source tree.
Other platforms never see these patches.

## Conventions

- One directory per add-on ID, one concern per patch, named
  `NNNN-short-slug.patch` (`^[0-9]{4}-[a-z0-9-]+\.patch$`). Patches apply in
  name order with `patch -p1` at fuzz 0; later patches may build on earlier ones.
- Each patch starts with free text before the first `---` line: a one-line
  summary, why it is needed (with the observed failure where there is one) and
  an `Upstream:` line saying whether it is a generic fix worth sending to the
  add-on repository or PS5-only.
- Never edit an applied patch in place without re-extracting the source:
  `apply-patch-series.cmake` stores each applied patch's SHA-256 in
  `<source>/.ps5-applied-patches` and stops when a recorded patch changed or
  left the series. Delete the source directory and reconfigure.
- Patches keep the line endings of the files they touch (lazyusf is CRLF), so
  `.gitattributes` marks `*.patch` as binary-safe (`-text`).

## Series

| Add-on | Patch | Purpose | Upstream |
|---|---|---|---|
| asap | 0001-readtag-free-player-on-load-failure | free the player when a tag read is rejected | yes |
| dumb | 0001-include-stdint | `dumb.h` needs `<stdint.h>` with the SDK libc | yes |
| dumb | 0002-fix-double-free-on-open-failure | buffer was freed twice when DUMB rejects a file | yes |
| gme | 0001-check-errors-and-free-track-info | track info leaked; open/info/start errors were ignored | yes |
| gme | 0002-end-and-fade-out-tracks | looping tracks never ended; fade out before the track length | yes |
| gme | 0003-fix-decimal-m3u-track-numbers | 1-based m3u track numbers were off by one | yes |
| gme | 0004-load-same-named-m3u-playlist | load `<file>.m3u` for track names and lengths (default was 150 s) | yes |
| gsf | 0001-release-emulator-state | `inited` was never set: ~33 MB leaked per track | yes |
| hvl | 0001-readtag-free-tune-without-title | tune leaked on untitled files | yes |
| nosefart | 0001-link-isolated-static-engines | embed separate tag/two playback engines instead of shipping ordinary ELF helpers | PS5-only |
| openmpt | 0001-readtag-use-local-module | tag reads leaked a module and clobbered the playing one | yes |
| organya | 0001-readtag-use-local-decoder | tag reads leaked a decoder and clobbered the playing one | yes |
| pvr.hts | 0001-join-connection-before-demuxer-destruction | join connection callbacks before freeing demuxers on client shutdown | yes |
| sidplay | 0001-pass-toolchain-flags-to-configure | `sidplay2` configure must use the cross linker | yes |
| sidplay | 0002-release-resid-builder | ReSIDBuilder and its engines leaked per track | yes |
| sidplay | 0003-validate-tune-and-track | invalid tunes were accepted; sub-song member uninitialised | yes |
| sidplay | 0004-use-native-config-site-flags | use shared native compiler ABI flags without passing literal CMake quotes to Autoconf | PS5-only |
| snesapu | 0001-readtag-free-buffer-on-parse-failure | file buffer leaked on rejected tags | yes |
| stsound | 0001-readtag-free-player-on-open-failure | player leaked when a file cannot be opened | yes |
| usf | 0001-release-emulator-state | ~24 MB leaked per track; crashed the console after a few tracks | yes |
| usf | 0002-report-allocation-failures | `MAP_FAILED` was used as a pointer (`memset(-1)` crash) | yes |
| usf | 0003-end-on-sustained-silence | untagged tracks stopped on their first quiet buffer | yes |
| wsr | 0001-report-helper-load-failure | `bool` functions returned -1 (true) when a helper failed to load | yes |
| wsr | 0002-link-isolated-static-engines | embed separate track/two playback engines instead of shipping ordinary ELF helpers | PS5-only |
| screensaver.asterwave | 0001-disable-x11-soil2 | select SOIL2's non-X11 path on PS5 | PS5-only |
| screensaver.matrixtrails | 0001-disable-x11-soil2 | select SOIL2's non-X11 path on PS5 | PS5-only |
| screensavers.rsxs | 0001-use-posix-memalign | replace SDK-rejected `malloc.h`/`memalign`, preserving 16-byte alignment | see patch |
| screensavers.rsxs | 0002-preserve-published-microcosm-identity | align Microcosm build/resources/install layout with its existing manifest and public add-on ID | yes |

`inputstream.adaptive` predates this layout and still uses
`tools/ps5/patches/inputstream-adaptive-ps5.patch` through
`tools/ps5/apply-adaptive-patch.cmake`.
