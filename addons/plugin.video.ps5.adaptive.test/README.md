# PS5 Adaptive Playback Test

Diagnostic-only Python video add-on. It explicitly selects InputStream Adaptive
and opens Akamai's public, unencrypted Big Buck Bunny DASH sample (H.264/AAC).
It does not use YouTube, account credentials, a local server or DRM.

Install the generated ZIP through **Add-ons -> Install from zip file** (enable
Unknown sources for this local test if necessary). Open **Video add-ons ->
PS5 Adaptive Playback Test** and select the single video. Stop and repeat.

The log marker `[PS5AdaptiveTest]` identifies the selected URL. A resolved URL
is not a playback success: inspect adaptive manifest/stream messages and actual
audio/video, followed by instance destruction and loader cleanup after stopping.

For future automatic title deployment, set `PS5_ADAPTIVE_TEST=1` when packaging.
`PS5_ADAPTIVE_PREFIX` optionally overrides the installed depends prefix; its
default is `build/ps5-depends-test/x86_64-unknown-freebsd-debug`. Packaging selects
adaptive's runtime exports, stages its manifest/resources/binary and this test,
and adds `kodi-addon-debug` before the usual `PS5_HOST` upload.
