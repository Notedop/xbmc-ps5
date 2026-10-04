# PS5 build entrypoint (migration worktree)

This branch keeps PS5 integration in tracked Kodi source. Configure does **not**
copy overlays or apply patch files.

## Configure

```bash
cd /home/raoul/kodi-fork/xbmc-ps5-fork
bash tools/ps5/configure.sh
```

Optional overrides:

- `BUILD=/abs/path/to/build-dir`
- `BUILD_TYPE=Debug|Release`
- `NATIVE=/home/raoul/kodi-ps5-native`
- `PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk`
- `PS5_OPENGL_PREFIX=/opt/ps5-opengl-gl46`
- `PS5_DEPENDS_PREFIX=/abs/path/to/tools/depends/target/prefix` (preferred
  dependency pkg-config root when present)

## Build

```bash
cmake --build /home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-release -j$(nproc)
```

## Package targets

`ps5-title` creates the deployable title folder, and `ps5-package` additionally
creates a zip artifact.

```bash
# Build title folder under build/ps5-stage/app/dist/PPSA99420
cmake --build /home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-release --target ps5-title

# Build zip package under build/ps5-stage/artifacts/PPSA99420.zip
cmake --build /home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-release --target ps5-package
```

Both targets call `tools/ps5/package.sh`, which uses:

- `BUILD` (default: `/home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-release`)
- `STAGE` (default: `/home/raoul/kodi-fork/xbmc-ps5-fork/build/ps5-stage`)
- `APP_TEMPLATE` (default: `$HOME/ps5-work/ps5-opengl/build/native-app/PPSA99005`)
