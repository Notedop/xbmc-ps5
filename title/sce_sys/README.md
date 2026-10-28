# title/sce_sys

`tools/ps5/package.sh` copies `param.json` unchanged into every title and verifies
that the packaged copy matches. Edit `contentVersion` here manually to bump the
version (format `NN.NNN.NNN`, for example `22.000.001`); there is no automatic
increment. `titleId`, `masterVersion`, category, HDR flags, memory limits and
save-data settings also come from this file. The former `TITLE_ID`,
`KODI_CATEGORY` and `KODI_HDR_TITLE` environment overrides are rejected.

The initial metadata was taken from the completed stage
`build/ps5-release/title-20261028T164255-2081569/app/dist/PPSA99420/sce_sys/param.json`
with content version `22.000.000` and master version `22.00`.

`icon0.png` (Kodi's 512x512 icon) is also copied into every build.
Optional extras you can add here:

- `icon0.png` — replace to use a different 512x512 title icon.

## Home-screen backgrounds and music (optional)

- `pic0.dds` — background while Kodi is selected on the home screen
- `pic1.dds` — background during the launch transition
- `snd0.at9` — music while selected

Both pictures or neither; 3840x2160 BC7 DX10 DDS. Create them with the
native-app boilerplate's converter (needs Windows `texconv`:
`winget install Microsoft.DirectXTex.Texconv`):

    cd ~/ps5-work/ps5-native-app-boilerplate
    ./tools/prepare-assets.sh --background /mnt/c/art/kodi-bg.png \
        --output-directory /mnt/c/kodi-ps5/title/sce_sys

Without them the shell shows its default background.

## Resetting or uninstalling Kodi

Files Kodi creates can't be deleted over FTP (the console protects a title's
own files from outside processes). Create an empty file in the title folder
(`/data/homebrew/<TITLE_ID>/`) and start Kodi once:

- `kodi-reset` - Kodi wipes its data and starts fresh.
- `kodi-uninstall` - Kodi wipes its data and quits immediately; the whole
  title folder can then be deleted over FTP.

And one that stays in place:

- `kodi-debug` - debug-level logging (kodi.log and klog). Slows Kodi down;
  remove it again when done.
- `kodi-swdecode` - use software (FFmpeg) video decoding only, e.g. to
  compare with the hardware decoder.
