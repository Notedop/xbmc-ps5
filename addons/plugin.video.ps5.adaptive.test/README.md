# PS5 Adaptive Playback Test

Diagnostic-only Python video add-on. It explicitly selects InputStream Adaptive
and opens Akamai's public, unencrypted Big Buck Bunny DASH sample (H.264/AAC).
It does not use YouTube, account credentials, a local server or DRM.

Set `PS5_BUNNY_DIAGNOSTIC=1` when running `tools/ps5/package.sh` to include and
register this diagnostic in a fresh native title. Native InputStream Adaptive
must already be built through the catalogue and selected for title staging.
The diagnostic is excluded by default and is not a catalogue binary.

Open **Video add-ons -> PS5 Adaptive Playback Test** and select the single video.
Stop and repeat. Packaging does not upload or launch the title.

The log marker `[PS5AdaptiveTest]` identifies the selected URL. A resolved URL
is not a playback success: inspect adaptive manifest/stream messages and actual
audio/video, followed by instance destruction and loader cleanup after stopping.

Use `PS5_ADDON_PREFIXES` for explicit native catalogue prefixes. Legacy
`PS5_ADAPTIVE_TEST`/`PS5_ADAPTIVE_PREFIX` and title-export staging are unsupported.
