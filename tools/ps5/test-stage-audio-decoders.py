#!/usr/bin/env python3
"""Regression for audio decoder staging and generated helper manifests."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/ps5/stage-audio-decoders.py"
STATE = ROOT / "tools/ps5/tests/auxiliary-state.cpp"
COMMON = ["clang++-18", "-std=c++17", "-O2", "-fPIC", "-shared", "-nostdlib", "-fuse-ld=lld"]
ADDON = "audiodecoder.fake"
PRIMARY = "audiodecoder.fake.so.1.2.3"


def build(output, *flags):
    subprocess.run([*COMMON, *flags, str(STATE), "-o", str(output)], check=True)


def prefix_with(root, helpers):
    data = root / "share/kodi/addons" / ADDON
    lib = root / "lib/kodi/addons" / ADDON
    data.mkdir(parents=True)
    lib.mkdir(parents=True)
    (data / "addon.xml").write_text(
        f'<addon id="{ADDON}"><extension point="kodi.audiodecoder" '
        f'library_freebsd="{PRIMARY}"/></addon>')
    build(lib / PRIMARY)
    for name, flags in helpers.items():
        build(lib / name, *flags)
    return root


def stage(prefix, destination=None):
    command = ["python3", str(SCRIPT), "--prefix", str(prefix)]
    if destination:
        command += ["--destination", str(destination)]
    return subprocess.run(command + [ADDON], capture_output=True, text=True)


with tempfile.TemporaryDirectory(prefix="ps5-stage-test-") as temp:
    temp = Path(temp)
    good = prefix_with(temp / "good", {"libhelper_0.so": [], "libhelper_tag.so": ["-DHELPER_ID=2"]})
    (good / "lib/kodi/addons" / ADDON / "libalias.so").symlink_to("libhelper_0.so")
    listed = stage(good)
    assert listed.returncode == 0, listed.stderr
    assert [Path(p).name for p in listed.stdout.split()] == [PRIMARY, "libhelper_0.so", "libhelper_tag.so"]
    output = temp / "out"
    staged = stage(good, output)
    assert staged.returncode == 0, staged.stderr
    manifest = (output / "addons" / ADDON / "ps5-auxiliaries.list").read_text().splitlines()
    assert manifest[1:] == ["format 1", f"primary {PRIMARY}",
                            "helper libhelper_0.so", "helper libhelper_tag.so"]
    assert not (output / "addons" / ADDON / "libalias.so").exists()

    plain = prefix_with(temp / "plain", {})
    staged = stage(plain, temp / "plain-out")
    assert staged.returncode == 0, staged.stderr
    assert not (temp / "plain-out/addons" / ADDON / "ps5-auxiliaries.list").exists()

    for name, flags, expected in [("tls", ["-DHELPER_TLS"], "PT_TLS"),
                                  ("needed", ["-Wl,--no-as-needed", "-lc"], "DT_NEEDED")]:
        bad = prefix_with(temp / name, {f"lib{name}.so": flags})
        result = stage(bad, temp / f"{name}-out")
        assert result.returncode != 0 and expected in result.stderr, result.stderr
        assert not (temp / f"{name}-out").exists()

    forged = prefix_with(temp / "forged", {})
    (forged / "share/kodi/addons" / ADDON / "ps5-auxiliaries.list").write_text("format 1\n")
    result = stage(forged)
    assert result.returncode != 0 and "must not provide" in result.stderr, result.stderr

print("PASS: audio decoder staging, helper validation and generated auxiliary manifests")
