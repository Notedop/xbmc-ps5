#!/usr/bin/env python3
"""Exercise PS5 entry-point delegation and Make/CMake graphs without PS5 builds."""

import ps5_test_paths
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = ps5_test_paths.ROOT
STEPS = ROOT / "tools/buildsteps/ps5"


class BuildstepTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "build").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "build")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "commands"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        buildsteps = self.root / "tools/buildsteps"
        buildsteps.mkdir(parents=True)
        shutil.copy(ROOT / "tools/buildsteps/defaultenv", buildsteps)
        stub = ('#!/usr/bin/python3\nimport json, os, sys\n'
                'with open(os.environ["CAPTURE"], "a") as out:\n'
                ' out.write(json.dumps({"argv": sys.argv, "cwd": os.getcwd(),'
                ' "env": dict(os.environ)}) + "\\n")\n'
                'sys.exit(int(os.environ.get("RESULT", "0")))\n')
        for name in ("make", "cmake", "python3"):
            path = self.bin / name
            path.write_text(stub)
            path.chmod(0o755)
        for name in ("tools/ps5/bootstrap.sh", "tools/ps5/configure.sh",
                     "tools/ps5/deploy.sh", "tools/depends/bootstrap",
                     "tools/depends/configure"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#!/bin/bash\nexec python3 "$0" "$@"\n')
            path.chmod(0o755)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("PS5_") and key not in
                       ("BUILD", "BUILD_TYPE", "WORKSPACE", "WORK", "APP_TEMPLATE",
                        "NATIVE", "Configuration", "XBMC_DEPENDS_ROOT")}
        self.env = {**environment, "WORKSPACE": str(self.root),
                    "PATH": str(self.bin) + os.pathsep + os.defpath,
                    "CAPTURE": str(self.log), "BUILDTHREADS": "3",
                    "WORK": str(self.root / "external choice"),
                    "XBMC_DEPENDS_ROOT": str(self.root / "dependencies")}

    def run_step(self, name, *arguments, **env):
        self.log.unlink(missing_ok=True)
        result = subprocess.run(["bash", str(STEPS / name), *arguments],
                                env={**self.env, **env}, capture_output=True, text=True)
        records = [json.loads(line) for line in self.log.read_text().splitlines()] \
            if self.log.exists() else []
        return result, records

    def test_default_dependency_root_matches_other_platforms(self):
        result, calls = self.run_step("configure-depends", XBMC_DEPENDS_ROOT="Default")
        self.assertEqual(result.returncode, 0, result.stderr)
        prefix = str(self.root / "tools/depends/xbmc-depends")
        self.assertIn("--prefix=" + prefix, calls[-1]["argv"])
        result, calls = self.run_step("prepare-xbmc", XBMC_DEPENDS_ROOT="Default")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = calls[0]["argv"]
        self.assertEqual(args[args.index("--depends") + 1],
                         prefix + "/x86_64-unknown-freebsd-debug")
        result, calls = self.run_step("configure-xbmc", XBMC_DEPENDS_ROOT="Default")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0]["env"]["NATIVE"],
                         prefix + "/x86_64-linux-gnu-native")

    def test_setup_delegates_in_order_and_preserves_choices(self):
        result, calls = self.run_step("prepare-depends", "STEP=opengl")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([Path(c["argv"][1]).name for c in calls], ["bootstrap.sh", "bootstrap"])
        self.assertEqual(calls[0]["argv"][2:], ["STEP=opengl"])
        result, calls = self.run_step("configure-depends")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(calls[0]["argv"][1]).name, "check-sdk.py")
        self.assertIn("--host=x86_64-unknown-freebsd", calls[-1]["argv"])
        self.assertIn("--prefix=" + self.env["XBMC_DEPENDS_ROOT"], calls[-1]["argv"])
        for step, subdir in (("make-native-depends", "native"), ("make-depends", "target")):
            result, calls = self.run_step(step)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls[-1]["argv"][1:],
                             ["-C", str(self.root / "tools/depends" / subdir), "-j3"])

    def test_native_preparation_and_configuration_forward_paths(self):
        result, calls = self.run_step("prepare-xbmc", "--system-sdk", "/contracts")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = calls[0]["argv"]
        for flag, value in (("--source", self.env["WORK"] + "/ps5-opengl"),
                            ("--native-tool", self.env["WORK"] +
                             "/ps5-opengl/build/native-app/PPSA99005/build/host/ps5-native-tool"),
                            ("--depends", self.env["XBMC_DEPENDS_ROOT"] +
                             "/x86_64-unknown-freebsd-debug")):
            self.assertEqual(args[args.index(flag) + 1], value)
        result, calls = self.run_step("configure-xbmc", "-DEXTRA=ON",
                                      Configuration="Debug", APP_TEMPLATE="/existing template")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-DPS5_APP_TEMPLATE=/existing template", calls[0]["argv"])
        self.assertEqual(calls[0]["env"]["BUILD_TYPE"], "Debug")
        self.assertEqual(calls[0]["argv"][-1], "-DEXTRA=ON")

    def test_build_package_and_addons_are_separate(self):
        for step, selector in (("make-xbmc", []), ("package", ["--target", "ps5-package"])):
            result, calls = self.run_step(step)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls[-1]["argv"][1:],
                             ["--build", str(self.root / "build/ps5-release"),
                              *selector, "--parallel", "3"])
        result, calls = self.run_step("make-binary-addons")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("peripheral.joystick", calls[-1]["argv"])
        self.assertIn("inputstream.adaptive", calls[-1]["argv"])
        self.assertNotIn("--package", calls[-1]["argv"])
        result, calls = self.run_step("deploy", "console.invalid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[0]["argv"][1:],
                         [str(self.root / "tools/ps5/deploy.sh"), "console.invalid"])

    def test_host_test_selection_and_failures_propagate(self):
        result, calls = self.run_step("run-tests", "test-package-stage.py", RESULT="7")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(calls[0]["argv"][1:],
                         [str(self.root / "tools/ps5/test.py"), "test-package-stage.py"])
        self.assertIn("target/runtime tests are not run", result.stdout)
        result, calls = self.run_step("prepare-depends", RESULT="7")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(len(calls), 1)

    def test_sdk_verification_failure_stops_configure_and_compilation(self):
        for step in ("configure-depends", "make-depends", "make-xbmc", "make-binary-addons"):
            with self.subTest(step=step):
                result, calls = self.run_step(step, RESULT="7")
                self.assertEqual(result.returncode, 7)
                self.assertEqual(len(calls), 1)
                self.assertEqual(Path(calls[0]["argv"][1]).name, "check-sdk.py")

    def test_ps5_dependency_graph_uses_only_supported_recipes(self):
        depends = self.root / "graph"
        target = depends / "target"
        target.mkdir(parents=True)
        prefix = depends / "prefix"
        (prefix / "share").mkdir(parents=True)
        (prefix / "share/config.site").touch()
        (depends / "Makefile.include").write_text(
            f"PREFIX={prefix}\nOS=ps5\nPLATFORM=test\nNATIVEPLATFORM=host\n"
            f"TOOLCHAIN={depends}/selected-sdk\nHAS_ZLIB=1\nNEED_LIBICONV=0\n")
        shutil.copy(ROOT / "tools/depends/target/Makefile", target)
        expected = ("bzip2 expat gettext libffi libxml2 openssl sqlite3 xz zlib dav1d ffmpeg "
                    "python3 brotli nghttp2 curl libiconv tinyxml tinyxml2 fstrcmp libuuid "
                    "libprocstat libkodishim libsmb2 ps5-opengl").split()
        for recipe in expected:
            directory = target / recipe
            directory.mkdir()
            check = ("@test -f ../brotli/done && test -f ../nghttp2/done\n\t"
                     if recipe == "curl" else "")
            if recipe == "ps5-opengl":
                check = f'@test "$$PS5_PAYLOAD_SDK" = "{depends}/selected-sdk"\n\t'
            (directory / "Makefile").write_text("all:\n\t" + check + "@touch done\n")
        result = subprocess.run(["/usr/bin/make", "-C", str(target), "-j8"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.parent.name for p in target.glob("*/done")), sorted(expected))
        # Existing aggregate markers must not hide a changed child recipe.
        shutil.copy(ROOT / "tools/depends/Makefile", depends)
        native = depends / "native"
        native.mkdir()
        (native / "Makefile").write_text("all:\n\t@touch visited\n")
        (native / ".installed-host").touch()
        (target / "curl/done").unlink()
        result = subprocess.run(["/usr/bin/make", "-C", str(depends), "-j8"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((native / "visited").exists())
        self.assertTrue((target / "curl/done").exists())

    def test_python_uses_title_allocator_only_on_ps5(self):
        depends = self.root / "python-graph"
        recipe = depends / "target/python3"
        recipe.mkdir(parents=True)
        (depends / "Makefile.include").write_text(
            "PREFIX=/target\nNATIVEPREFIX=/native\nPLATFORM=test\nPYTHON_VERSION=3.14\n")
        (depends / "download-files.include").touch()
        for name in ("Makefile", "PYTHON3-VERSION"):
            shutil.copy(ROOT / "tools/depends/target/python3" / name, recipe)
        for platform in ("ps5", "linux", "wasm"):
            with self.subTest(platform=platform):
                result = subprocess.run(
                    ["/usr/bin/make", "-s", "-C", str(recipe), "-f", "Makefile",
                     "-f", "-", "python-config", f"OS={platform}"],
                    input='python-config:\n\t@printf \'%s\\n\' "$(CONFIGURE)"\n',
                    env=self.env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--without-pymalloc", result.stdout)
                self.assertEqual("--without-mimalloc" in result.stdout, platform == "ps5")

    def test_graphics_recipe_always_delegates_to_its_cache_validating_backend(self):
        recipe = self.root / "tools/depends/target/ps5-opengl"
        recipe.mkdir(parents=True)
        shutil.copy(ROOT / "tools/depends/target/ps5-opengl/Makefile", recipe)
        backend = self.root / "tools/ps5/bootstrap/ps5-opengl.sh"
        backend.parent.mkdir(parents=True, exist_ok=True)
        backend.write_text('#!/bin/bash\nexec python3 "$0" "$@"\n')
        (recipe / ".installed-test").touch()
        self.env["PS5_PAYLOAD_SDK"] = str(self.root / "selected sdk")
        self.env["PS5_OPENGL_PREFIX"] = str(self.root / "selected graphics")
        for _ in range(2):
            subprocess.run(["/usr/bin/make", "-C", str(recipe)],
                           env=self.env, check=True, capture_output=True)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call["argv"][1], str(backend))
            self.assertEqual(call["env"]["PS5_OPENGL_SOURCE"],
                             self.env["WORK"] + "/ps5-opengl")
            self.assertEqual(call["env"]["PS5_PAYLOAD_SDK"], self.env["PS5_PAYLOAD_SDK"])
            self.assertEqual(call["env"]["PS5_OPENGL_PREFIX"], self.env["PS5_OPENGL_PREFIX"])
        self.log.unlink()
        subprocess.run(["/usr/bin/make", "-n", "-C", str(recipe)],
                       env=self.env, check=True, capture_output=True)
        self.assertFalse(self.log.exists(), "Make dry runs must not execute graphics builders")
        bootstrap = (ROOT / "tools/ps5/bootstrap.sh").read_text()
        self.assertIn('make -C "$ROOT/tools/depends/target/ps5-opengl"', bootstrap)
        self.assertNotIn('bash "$HERE/bootstrap/ps5-opengl.sh"', bootstrap)

    def test_cmake_targets_forward_configured_inputs_and_deploy_without_building(self):
        source = self.root / "cmake source"
        scripts = source / "tools/ps5"
        scripts.mkdir(parents=True)
        for name in ("package.sh", "deploy.sh"):
            script = scripts / name
            script.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$CAPTURE"\n'
                              'env >> "$CAPTURE"\n')
            script.chmod(0o755)
        (source / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.20)\nproject(Targets NONE)\n"
            "set(APP_NAME_LC kodi)\n"
            'add_custom_target(kodi COMMAND "${CMAKE_COMMAND}" -E touch "${CMAKE_BINARY_DIR}/built")\n'
            f'include("{ROOT}/cmake/scripts/ps5/ExtraTargets.cmake")\n')
        build = source / "out"
        paths = {name: str(self.root / name / "custom choice") for name in (
            "PS5_PAYLOAD_SDK", "PS5_OPENGL_PREFIX", "PS5_NATIVE_RUNTIME_SDK",
            "PS5_NATIVE_DEPENDS_MAP", "PS5_WORK", "PS5_OPENGL_SOURCE", "PS5_APP_TEMPLATE")}
        subprocess.run(["/usr/bin/cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
                        "-DPS5_DEPLOY_HOST=explicit.invalid",
                        *[f"-D{name}={value}" for name, value in paths.items()]],
                       check=True, capture_output=True)
        env = {**os.environ, "CAPTURE": str(self.log), "PS5_HOST": "ambient.invalid"}
        subprocess.run(["/usr/bin/cmake", "--build", str(build), "--target", "ps5-package"],
                       env=env, check=True, capture_output=True)
        output = self.log.read_text()
        for name, value in paths.items():
            env_name = {"PS5_WORK": "WORK", "PS5_APP_TEMPLATE": "APP_TEMPLATE"}.get(name, name)
            self.assertIn(f"{env_name}={value}\n", output)
        self.assertIn("PACKAGE_ZIP=1", output)
        (build / "built").unlink()
        self.log.unlink()
        subprocess.run(["/usr/bin/cmake", "--build", str(build), "--target", "ps5-deploy"],
                       env=env, check=True, capture_output=True)
        self.assertFalse((build / "built").exists())
        output = self.log.read_text()
        self.assertIn("deploy.sh explicit.invalid", output)
        self.assertNotIn("package.sh", output)


if __name__ == "__main__":
    unittest.main()
