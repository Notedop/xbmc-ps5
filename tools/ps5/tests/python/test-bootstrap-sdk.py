#!/usr/bin/env python3
"""Verify pinned SDK packaging, qualification and response-file wrappers."""

import ps5_test_paths
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from native_c_link import Elf

HERE = ps5_test_paths.HERE
ROOT = ps5_test_paths.ROOT
SPEC = importlib.util.spec_from_file_location("payload_sdk_check", HERE / "check-sdk.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def assets():
    return [HERE / "sdk/PKGBUILD", HERE / "check-sdk.py",
            *HERE.glob("patches/ps5-payload-sdk/*.patch"),
            *ROOT.glob("shims/sce_stubs/*.c")]


def fixture_sdk(root):
    root.mkdir(parents=True, exist_ok=True)
    for relative in CHECK.REQUIRED:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    for name in ("prospero-clang", "prospero-clang++"):
        (root / "bin" / name).chmod(0o755)
    CHECK.record(root, None)


class SDKReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = self.root / "sdk"
        fixture_sdk(self.sdk)

    def test_receipt_checks_inputs_and_installed_files_but_allows_other_packages(self):
        CHECK.verify(self.sdk)
        (self.sdk / "target/user/homebrew").mkdir(parents=True)
        (self.sdk / "target/user/homebrew/extra").write_text("another package")
        CHECK.verify(self.sdk)
        (self.sdk / "bin/ps5-wrapper-args.py").write_text("changed")
        with self.assertRaisesRegex(ValueError, "SDK file changed"):
            CHECK.verify(self.sdk)

    def test_recipe_change_and_omitted_stubs_are_rejected(self):
        with patch.object(CHECK, "inputs", return_value={"revision": "different"}):
            with self.assertRaisesRegex(ValueError, "recipe/customizations differ"):
                CHECK.verify(self.sdk)
        path = self.sdk / CHECK.RECEIPT
        saved = json.loads(path.read_text())
        del saved["files"]["target/lib/libSceMbus.so"]
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, "omits required"):
            CHECK.verify(self.sdk)

    def test_receipt_cannot_escape_sdk(self):
        path = self.sdk / CHECK.RECEIPT
        saved = json.loads(path.read_text())
        saved["files"]["../outside"] = {}
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, "invalid SDK receipt path"):
            CHECK.verify(self.sdk)

    def test_staged_inputs_match_recipe_and_manifest_has_no_competing_pin(self):
        directory = self.root / "inputs"
        directory.mkdir()
        for path in assets():
            shutil.copyfile(path, directory / path.name)
        self.assertEqual(CHECK.inputs(directory), CHECK.inputs())
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; printf "%s\\n" "${source[@]}" "${sha256sums[@]}"',
             "recipe", str(directory / "PKGBUILD")],
            cwd=directory, text=True, capture_output=True, check=True)
        rows = result.stdout.splitlines()
        count = len(rows) // 2
        sources, checksums = rows[:count], rows[count:]
        self.assertEqual(len(sources), len(checksums))
        self.assertEqual(set(sources[1:]), {path.name for path in assets()} - {"PKGBUILD"})
        self.assertIn("#commit=" + CHECK.inputs()["revision"], sources[0])
        for name, checksum in zip(sources[1:], checksums[1:]):
            self.assertEqual(CHECK.digest(directory / name), checksum, name)
        manifest = json.loads((HERE / "dependency-manifest.json").read_text())
        sdk = next(row for row in manifest["dependencies"] if row["name"] == "ps5-payload-sdk")
        self.assertEqual(sdk["recipe"], "tools/ps5/sdk/PKGBUILD")
        self.assertTrue({"pin", "version", "source", "path"}.isdisjoint(sdk))


class BootstrapSDKTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        here = self.root / "tools/ps5"
        for path in assets() + [HERE / "bootstrap.sh", HERE / "bootstrap/ps5-sdk.sh",
                                HERE / "bootstrap/sce-stubs.sh", HERE / "bootstrap/pacbrew.sh",
                                HERE / "patches/pacbrew-libminizip-source.patch",
                                HERE / "patches/pacbrew-lua-source.patch",
                                HERE / "dependency-manifest.json",
                                HERE / "sdk/pacman.conf"]:
            destination = self.root / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        self.script = here / "bootstrap.sh"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands"
        for name, body in (
                ("sudo", 'case "$*" in "-v") exit 0;; "-n true") exit 1;; esac\n'
                 '[ "${INSTALL_RESULT:-0}" = 0 ] || exit "$INSTALL_RESULT"\n'
                 'exec /usr/bin/python3 -c \'import os,shutil; '
                 'shutil.copytree(os.environ["SDK_FIXTURE"],os.environ["PS5_PAYLOAD_SDK"],dirs_exist_ok=True)\''),
                ("pacman", 'printf "%s\\n" "${PACMAN_OWNER:-ps5-payload-sdk}"'),
                ("makepkg", 'case "$1" in --packagelist) echo "$PWD/sdk.pkg.tar.zst";;\n'
                 '*) [ "${BUILD_RESULT:-0}" = 0 ] || exit "$BUILD_RESULT"; '
                 'touch sdk.pkg.tar.zst;; esac'),
                ("git", 'case "$1" in apply) exit 0;;\n'
                 'clone) mkdir -p "$4/.git";;\n'
                 '-C) case "$3" in\n'
                 'fetch) exit "${SOURCE_FETCH_RESULT:-0}";;\n'
                 'checkout) printf "%s\\n" "$5" > "$2/.git/fixture-head"; '
                 'touch "$2/.git/index";;\n'
                 'rev-parse) if [ -f "$2/.git/fixture-head" ]; then '
                 'cat "$2/.git/fixture-head"; else printf "%s\\n" "$CHECKOUT_HEAD"; fi;;\n'
                 '*) exit 98;; esac;;\n*) exit 98;; esac'),
                ("make", "exit 98"), ("apt-get", "exit 98")):
            path = self.bin / name
            path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$CAPTURE"\n{body}\n')
            path.chmod(0o755)
        self.sdk = self.root / "sdk"
        self.fixture = self.root / "fixture-sdk"
        fixture_sdk(self.fixture)
        marker = self.fixture / "target/user/homebrew/lib/pkgconfig/fontconfig.pc"
        marker.parent.mkdir(parents=True)
        marker.touch()
        self.work = self.root / "work"
        (self.work / "pacbrew-repo").mkdir(parents=True)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.defpath,
                    "STEP": "sdk", "PS5_PAYLOAD_SDK": str(self.sdk),
                    "SDK_FIXTURE": str(self.fixture),
                    "WORK": str(self.work), "CAPTURE": str(self.log),
                    "CHECKOUT_HEAD": next(item["pin"] for item in
                        json.loads((HERE / "dependency-manifest.json").read_text())["dependencies"]
                        if item["name"] == "pacbrew-repo")}

    def run_bootstrap(self, **overrides):
        self.log.unlink(missing_ok=True)
        return subprocess.run(["bash", str(self.script)], env={**self.env, **overrides},
                              text=True, capture_output=True)

    def test_existing_unqualified_sdk_is_preserved_before_sudo(self):
        self.sdk.mkdir()
        evidence = self.sdk / "owner-file"
        evidence.write_text("preserve this SDK")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unqualified SDK preserved", result.stderr)
        self.assertEqual(evidence.read_text(), "preserve this SDK")
        self.assertFalse(self.log.exists())

    def test_clean_install_and_verified_reuse(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        CHECK.verify(self.sdk)
        commands = self.log.read_text()
        self.assertIn("makepkg --force --cleanbuild", commands)
        self.assertIn("sudo pacman --config", commands)
        self.assertIn("--noconfirm -U", commands)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("makepkg", self.log.read_text())
        self.assertNotIn("pacman", self.log.read_text())

    def test_failed_install_keeps_package_for_retry(self):
        result = self.run_bootstrap(INSTALL_RESULT="33")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.sdk.exists())
        self.assertEqual(len(list(self.work.glob("sdk-packages/*/sdk.pkg.tar.zst"))), 1)
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("makepkg --force", self.log.read_text())

    def test_build_failure_never_installs_or_creates_sdk(self):
        result = self.run_bootstrap(BUILD_RESULT="17")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.sdk.exists())
        self.assertNotIn("sudo pacman", self.log.read_text())

    def test_changed_qualified_sdk_is_rejected_before_sudo(self):
        shutil.copytree(self.fixture, self.sdk)
        (self.sdk / "target/lib/libSceSaveData.so").write_text("changed")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SDK file changed", result.stderr)
        self.assertFalse(self.log.exists())

    def test_managed_recipe_upgrade_preserves_homebrew_libraries(self):
        shutil.copytree(self.fixture, self.sdk)
        path = self.sdk / CHECK.RECEIPT
        receipt = json.loads(path.read_text())
        receipt["inputs"]["files"]["PKGBUILD"] = "old recipe"
        path.write_text(json.dumps(receipt))
        library = self.sdk / "target/user/homebrew/keep.a"
        library.write_text("keep this expensive library")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        CHECK.verify(self.sdk)
        self.assertEqual(library.read_text(), "keep this expensive library")
        self.assertIn("--noconfirm -U", self.log.read_text())

    def test_unmanaged_recipe_upgrade_is_not_overwritten(self):
        shutil.copytree(self.fixture, self.sdk)
        path = self.sdk / CHECK.RECEIPT
        receipt = json.loads(path.read_text())
        receipt["inputs"]["files"]["PKGBUILD"] = "old recipe"
        path.write_text(json.dumps(receipt))
        before = path.read_bytes()
        result = self.run_bootstrap(PACMAN_OWNER="other-package")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installation preserved", result.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("sudo pacman", self.log.read_text())

    def test_empty_relative_and_system_prefixes_fail_before_sudo(self):
        root_link = self.root / "root-link"
        root_link.symlink_to("/", target_is_directory=True)
        for prefix in ("", "relative-sdk", "/", "/opt/..", "/usr", str(root_link)):
            with self.subTest(prefix=prefix):
                result = self.run_bootstrap(PS5_PAYLOAD_SDK=prefix)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PS5_PAYLOAD_SDK", result.stderr)
                self.assertFalse(self.log.exists(), result.stderr)

    def test_pacbrew_cannot_replace_the_pinned_sdk(self):
        repo = self.work / "pacbrew-repo"
        (repo / "ci-libs.sh").write_text("PKGS=(\n sdk\n)\n")
        result = subprocess.run(["bash", str(self.script.parent / "bootstrap/pacbrew.sh"), "sdk"],
                                env={**self.env, "SKIP": ""}, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sdk (skipped)", result.stdout)
        self.assertNotIn("makepkg", self.log.read_text())

    def test_cached_pacbrew_revision_mismatch_preserves_sources(self):
        shutil.copytree(self.fixture, self.sdk)
        sentinel = self.work / "pacbrew-repo/local-changes"
        sentinel.write_text("preserve this cache")
        result = self.run_bootstrap(CHECKOUT_HEAD="0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing sources were preserved", result.stderr)
        self.assertEqual(sentinel.read_text(), "preserve this cache")
        self.assertNotIn("makepkg", self.log.read_text())
        self.assertNotIn("checkout", self.log.read_text())

    def test_new_recipe_cache_fetches_exact_pin_before_checkout(self):
        shutil.copytree(self.fixture, self.sdk)
        source = self.work / "pacbrew-repo"
        source.rmdir()
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.log.read_text()
        self.assertIn("clone --no-checkout", commands)
        self.assertIn("fetch --no-tags --depth 1", commands)
        self.assertIn("checkout --detach " + self.env["CHECKOUT_HEAD"], commands)
        self.assertLess(commands.index("fetch "), commands.index("checkout --detach"))

    def test_interrupted_source_fetch_resumes_without_deleting_cache(self):
        shutil.copytree(self.fixture, self.sdk)
        source = self.work / "pacbrew-repo"
        (source / ".git").mkdir()
        failed = self.run_bootstrap(SOURCE_FETCH_RESULT="7")
        self.assertEqual(failed.returncode, 7, failed.stdout + failed.stderr)
        self.assertTrue((source / ".git").is_dir())
        self.assertNotIn("checkout", self.log.read_text())
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("clone ", self.log.read_text())
        self.assertTrue((source / ".git/index").is_file())

    def test_cached_template_revision_mismatch_never_falls_back_to_main(self):
        source = self.work / "ps5-native-app-boilerplate"
        source.mkdir()
        sentinel = source / "local-changes"
        sentinel.write_text("preserve this template")
        result = self.run_bootstrap(STEP="template", CHECKOUT_HEAD="0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing sources were preserved", result.stderr)
        self.assertEqual(sentinel.read_text(), "preserve this template")
        self.assertNotIn("checkout", self.log.read_text())


class SDKWrapperTests(unittest.TestCase):
    def test_real_recipe_build_and_package_record_a_relocatable_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sdk"
            shutil.copytree(HERE / "tests/sdk-upstream", source)
            (source / "sce_stubs").mkdir()
            (source / "sce_stubs/libSceVideoOut.c").write_text("void sceVideoOutOpen(void) {}\n")
            for path in assets():
                shutil.copyfile(path, root / path.name)
            install = []
            for relative in CHECK.REQUIRED:
                parent = Path(relative).parent
                install.append(f'\tmkdir -p "$(DESTDIR)/{parent}"')
                if relative.startswith("bin/"):
                    install.append(f'\tinstall host/{relative} "$(DESTDIR)/{relative}"')
                else:
                    install.append(f'\tprintf fixture > "$(DESTDIR)/{relative}"')
            (source / "Makefile").write_text(
                "all:\n\ttouch built\ninstall:\n" + "\n".join(install) + "\n")
            result = subprocess.run(
                ["bash", "-c", 'set -e; source "$1"; srcdir="$2"; startdir="$2"; pkgdir="$2/pkg"; '
                 'export PS5_PAYLOAD_SDK=/test-sdk; prepare; build; package',
                 "package", str(root / "PKGBUILD"), str(root)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((source / "built").is_file())
            sdk = root / "pkg/test-sdk"
            CHECK.verify(sdk)
            relocated = root / "relocated"
            sdk.rename(relocated)
            CHECK.verify(relocated)

    def test_real_recipe_prepares_wrappers_and_stubs_without_firmware(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "sdk"
            shutil.copytree(HERE / "tests/sdk-upstream", sdk)
            (sdk / "sce_stubs").mkdir()
            (sdk / "sce_stubs/libSceVideoOut.c").write_text("void sceVideoOutOpen(void) {}\n")
            for path in assets():
                shutil.copyfile(path, root / path.name)
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; srcdir="$2"; prepare',
                 "prepare", str(root / "PKGBUILD"), str(root)],
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("sceVideoOutVrrUnpegFromFixedRate",
                          (sdk / "sce_stubs/libSceVideoOut.c").read_text())
            for path in ROOT.glob("shims/sce_stubs/*.c"):
                self.assertEqual((sdk / "sce_stubs" / path.name).read_bytes(), path.read_bytes())
            binary = sdk / "host/bin"
            for name in ("clang", "clang++"):
                path = binary / name
                path.write_text('#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
                path.chmod(0o755)
            config = binary / "prospero-llvm-config"
            config.write_text("#!/bin/sh\necho 18.1.3\n")
            config.chmod(0o755)
            response = root / "response with spaces.rsp"
            response.write_text('-c -E "source with spaces.cpp"')
            nested = root / "nested.rsp"
            nested.write_text('@"' + str(response) + '"')
            for wrapper in ("prospero-clang", "prospero-clang++"):
                for args, expect_crt in [
                        (["source.cpp"], True), (["-shared", "source.cpp"], False),
                        *[([flag, "source.cpp"], False) for flag in
                          ("-c", "-E", "-S", "-M", "-MM", "-fsyntax-only")],
                        (["@" + str(response)], False), (["@" + str(nested)], False)]:
                    with self.subTest(wrapper=wrapper, args=args):
                        result = subprocess.run([str(binary / wrapper), *args],
                                                text=True, capture_output=True, check=True)
                        actual = json.loads(result.stdout)
                        self.assertEqual(any(arg.endswith("/crt1.o") for arg in actual), expect_crt)
                        self.assertTrue(all(arg in actual for arg in args))
                for name in ("missing.rsp", "recursive.rsp"):
                    bad = root / name
                    if name == "recursive.rsp":
                        bad.write_text("@" + str(bad))
                    result = subprocess.run([str(binary / wrapper), "@" + str(bad)],
                                            text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("PS5 compiler wrapper", result.stderr)


@unittest.skipUnless(shutil.which("clang-18") and shutil.which("ld.lld"),
                     "Clang 18 and LLD required")
class SDKStubTests(unittest.TestCase):
    def test_compact_stubs_preserve_function_exports_and_sonames(self):
        counts = {"libSceMbus": 132, "libSceMediaFrameworkInterface": 139,
                  "libSceSaveData": 105, "libSceUsbStorage": 24, "libSceVideodec2": 9}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for module, count in counts.items():
                with self.subTest(module=module):
                    source = ROOT / "shims/sce_stubs" / (module + ".c")
                    names = re.findall(r"^STUB\((\w+)\);$", source.read_text(), re.MULTILINE)
                    self.assertEqual(len(names), count)
                    original = root / (module + "-expanded.c")
                    original.write_text("".join(
                        f'asm(".global {name}\\n.type {name} @function\\n{name}:\\n");\n'
                        for name in names))
                    exports = []
                    for index, path in enumerate((source, original)):
                        obj, lib = root / f"{index}.o", root / f"{index}.so"
                        subprocess.run(["clang-18", "-target", "x86_64-sie-ps5",
                                        "-ffreestanding", "-fPIC", "-c", str(path),
                                        "-o", str(obj)], check=True, capture_output=True)
                        subprocess.run(["ld.lld", "-shared", "--soname=" + module + ".sprx",
                                        str(obj), "-o", str(lib)], check=True, capture_output=True)
                        exports.append({name: row[1:] for name, row in
                                        Elf(lib.read_bytes()).symbols(".dynsym") if name})
                    self.assertEqual(exports[0], exports[1])
                    self.assertEqual(set(exports[0]), set(names))
                    self.assertTrue(all(row[0] == 18 for row in exports[0].values()))


if __name__ == "__main__":
    unittest.main()
