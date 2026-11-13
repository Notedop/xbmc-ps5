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
| peripheral.joystick | 0001-native-buttonmap-provider | retain upstream mapping/storage while Kodi's native bus owns PS5 enumeration, events and rumble; omit Linux driver settings | PS5-only |
| peripheral.joystick | 0002-use-kodi-logging | omit the unused syslog sink whose `closelog` symbol is unavailable in native imports; retain Kodi and console logging | PS5-only |
| inputstream.ffmpegdirect | 0001-build-iconv-library-only | build/install the required libiconv library and header without unused gnulib utilities | PS5-only |
| inputstream.ffmpegdirect | 0002-use-native-config-site-flags | use native ABI compiler commands without literal CMake quotes in libzvbi, Nettle and GnuTLS Autoconf flags | PS5-only |
| inputstream.ffmpegdirect | 0003-use-sdk-assembler-for-gmp | assemble GMP probes directly with the SDK driver while retaining native C ABI lowering | PS5-only |
| inputstream.ffmpegdirect | 0004-use-sdk-assembler-for-nettle | use the SDK driver for Nettle assembly rules without bypassing native C ABI lowering | PS5-only |
| inputstream.ffmpegdirect | 0005-omit-unused-gnutls-fseterr | omit MinGW-only callers' unused gnulib helper without exposing private native FILE internals | PS5-only |
| inputstream.ffmpegdirect | 0006-use-sdk-assembler-for-gnutls | use the SDK driver for GnuTLS assembly without lowering assembled objects as C bitcode | PS5-only |
| inputstream.ffmpegdirect | 0007-build-gnutls-library-only | build/install TLS libraries, headers and pkg-config metadata without unused command-line support code | PS5-only |
| inputstream.ffmpegdirect | 0008-use-native-ffmpeg-compilers | reuse CONFIG_SITE compiler drivers for FFmpeg's non-CMake build, with direct SDK assembly and explicit cross-target selection | PS5-only |
| inputstream.ffmpegdirect | 0009-link-private-ffmpeg-archives | select private FFmpeg metadata and absolute static archives, retaining local symbol binding instead of linking SDK/Kodi copies | PS5-only |
| inputstream.ffmpegdirect | 0010-adapt-native-multibyte-limit | implement libiconv's FreeBSD locale accessor using native libc's exported multibyte limit | PS5-only |
| inputstream.ffmpegdirect | 0011-bind-absent-c11-thread-probe | bind optional weak C11-thread detection and Zstd tracing to null without adding unsupported SPRX imports | PS5-only |
| inputstream.ffmpegdirect | 0012-disable-execute-only-unsafe-crypto-assembly | use portable Nettle/GnuTLS crypto to avoid constant reads from execute-only code; retain TLS verification and FFmpeg codec optimizations | PS5-only |
| inputstream.ffmpegdirect | 0013-use-kodi-ca-bundle | trust Kodi's bundled CA certificates for initial and nested FFmpeg/HLS requests while retaining TLS verification and native I/O handling | PS5-only |
| inputstream.ffmpegdirect | 0014-batch-timeshift-segment-writes | batch segment persistence in bounded 256 KiB chunks, publish packets before I/O, finalize rollover outside playback locks and retain failed history in memory | PS5-only |
| inputstream.rtmp | 0001-build-native-portable-openssl | select portable BSD OpenSSL and the native compiler instead of host-Linux configuration; omit unavailable optional interfaces | PS5-only |
| inputstream.rtmp | 0002-build-native-librtmp-and-private-crypto | preserve native ABI lowering in raw Make and select matching private RTMP, OpenSSL and zlib archives/headers | PS5-only |
| inputstream.rtmp | 0003-use-make-with-ninja-generator | invoke GNU Make explicitly instead of generating an invalid Ninja dollar escape | yes |
| vfs.rar | 0001-adapt-native-unrar-metadata | omit unsupported Unix owner restoration and symlink timestamps while retaining archive extraction and file timestamps | PS5-only |
| vfs.rar | 0002-adapt-native-multibyte-limit | read native libc's exported multibyte limit for Unrar filename conversions | PS5-only |
| vfs.sftp | 0001-native-openssl-recipe | select portable BSD OpenSSL and the native compiler; omit unavailable DSO, async and batched datagram interfaces | PS5-only |
| vfs.sftp | 0002-native-private-crypto-and-ssh-home | select matching private OpenSSL archives and Kodi's writable SSH home | PS5-only |
| vfs.sftp | 0003-enable-ps5-platform-guard | define the add-on-local PS5 guard for the writable SSH directory | PS5-only |
| vfs.sftp | 0004-libssh-private-openssl | pin libssh's headers and crypto archives to the private dependency prefix | PS5-only |
| vfs.sftp | 0005-libssh-static-feature-checks | check declarations for optional OpenSSL and secure-wipe APIs during static cross-compilation | yes |
| vfs.sftp | 0006-libssh-client-platform-apis | build client-only SSH using supported glob and scoped-address branches | PS5-only |
| vfs.libarchive | 0001-build-native-portable-openssl | preserve native ABI compiler commands and portable crypto with private zlib | PS5-only |
| vfs.libarchive | 0002-link-private-native-archives | select matching private archives and dependency headers explicitly | PS5-only |
| vfs.libarchive | 0003-disable-unavailable-acl | omit unsupported filesystem ACL restoration from archive decoding | PS5-only |
| vfs.libarchive | 0004-use-native-libc-capabilities | correct compile-only probes using the native provider inventory | PS5-only |
| vfs.libarchive | 0005-disable-unavailable-openssl-interfaces | omit unsupported DSO, async and batched datagram interfaces | PS5-only |
| vfs.libarchive | 0006-adapt-native-multibyte-limit | use native libc's exported locale multibyte limit | PS5-only |
| vfs.pdf | 0001-build-iconv-library-only | build the required iconv library/header through native CONFIG_SITE without unused gnulib utilities | PS5-only |
| vfs.pdf | 0002-link-private-poppler-archives | resolve the complete Poppler dependency stack to private static archives and headers | PS5-only |
| vfs.pdf | 0003-adapt-native-multibyte-limit | read native libc's multibyte limit for iconv conversions | PS5-only |
| vfs.pdf | 0004-use-native-large-file-functions | use native 64-bit `pread`/`lseek` instead of unavailable Linux aliases | PS5-only |
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
`tools/ps5/scripts/apply-adaptive-patch.cmake`.
