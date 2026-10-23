#!/usr/bin/env python3
"""Host-only tests for the persistent PS5 add-on builder."""

from __future__ import annotations

import json
import io
from contextlib import redirect_stderr, redirect_stdout
import os
import shlex
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import addon_build
import native_addon


def elf_fixture(*, needed: bool = False) -> bytes:
    data = bytearray(280)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHIQQQIHHHHHH", data, 16,
                     3, 62, 1, 0, 64, 120, 0, 64, 56, 1, 64, 2, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 4, 0, 0, 0, len(data), len(data), 1)
    struct.pack_into("<IIQQQQIIQQ", data, 184, 0, 6, 0, 0, 248, 32, 0, 0, 8, 16)
    struct.pack_into("<qQ", data, 248, 1 if needed else 0, 0)
    return bytes(data)


class IncrementalBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "kodi"
        self.workspace = self.base / "work"
        self.sdk = self.base / "sdk"
        self.depends = self.base / "depends"
        self._create_fixture()
        self.runtime = self.root / "build/ps5-native/sdk"
        self.runtime.mkdir(parents=True)
        (self.runtime / "manifest.json").write_text("{}")
        (self.runtime / "receipt.json").write_text(json.dumps({
            "files": {"manifest.json": "a" * 64},
        }))
        mapping = self.root / "build/ps5-native/depends/archive-map.json"
        mapping.parent.mkdir()
        mapping.write_text("{}")
        contract = mock.patch.object(addon_build, "validate_sdk", return_value={})
        contract.start()
        self.addCleanup(contract.stop)
        installs = mock.patch.object(
            addon_build, "native_installed",
            side_effect=lambda directory, prefix, sdk=None: native_addon.installed(directory, prefix))
        installs.start()
        self.addCleanup(installs.stop)
        self.catalogue = {
            "schema": 1,
            "runtime": "piers-ps5-1",
            "targets": {
                "addon.bundle": {
                    "url": "https://github.com/example/addon.bundle",
                    "ref": "Piers",
                    "addons": ["addon.bundle"],
                }
            },
            "addons": {
                "addon.bundle": {
                    "target": "addon.bundle",
                    "bundled": False,
                    "channel": "testing",
                    "revision": 1,
                }
            },
        }
        self.sha = "a" * 40
        self.resolver = mock.patch.object(addon_build, "_resolve_ref", return_value=self.sha)
        self.resolve_mock = self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.fetcher = mock.patch.object(addon_build, "_fetch_source", side_effect=self._fake_fetch)
        self.fetch_mock = self.fetcher.start()
        self.addCleanup(self.fetcher.stop)
        self.runner = mock.patch.object(addon_build, "_run", side_effect=self._fake_run)
        self.run_mock = self.runner.start()
        self.addCleanup(self.runner.stop)
        self.configure_dirs: dict[str, Path] = {}
        self.configure_targets: dict[str, list[str]] = {}
        self.multi_output = False
        self.invalid_primary = False
        self.invalid_helper = False

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_fixture(self) -> None:
        for relative in (
            "cmake/addons",
            "cmake/addons/addons/addon.bundle",
            "toolchain",
            "tools/depends/target",
            "tools/depends",
            "tools/ps5/patches/addons/addon.bundle",
            "xbmc/addons/kodi-dev-kit/include/kodi",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        (self.root / "cmake/addons/CMakeLists.txt").write_text("cmake machinery\n", encoding="utf-8")
        (self.root / "cmake/addons/addons/addon.bundle/addon.bundle.txt").write_text(
            "addon.bundle https://github.com/example/addon.bundle Piers\n", encoding="utf-8"
        )
        for relative in (
            "tools/depends/target/config-binaddons.site",
            "tools/depends/xbmc-addons.include",
            "tools/depends/Makefile.include",
            "cmake/KodiConfig.cmake.in",
            "toolchain/ps5-native-addon.cmake",
            "tools/ps5/native_addon.py",
            "xbmc/addons/kodi-dev-kit/include/kodi/header.h",
            "tools/ps5/patches/addons/addon.bundle/0001.patch",
        ):
            (self.root / relative).write_text(relative, encoding="utf-8")
        (self.root / "tools/depends/target/Toolchain_binaddons.cmake.in").write_text(
            'set(OS "ps5")\n'
            "set(CMAKE_FIND_ROOT_PATH @CMAKE_FIND_ROOT_PATH@)\n"
            "set(CMAKE_C_COMPILER /opt/ps5-payload-sdk/bin/prospero-clang)\n"
            "set(CMAKE_CXX_COMPILER @CXX@)\n"
            'set(CMAKE_C_FLAGS_RELEASE "@platform_cppflags_release@ @platform_cflags_release@ @platform_includes_release@")\n',
            encoding="utf-8",
        )

        for relative in (
            "bin/prospero-clang",
            "bin/prospero-clang++",
            "bin/prospero-llvm-config",
            "bin/prospero-ar",
            "bin/prospero-lld",
            "bin/prospero-nm",
            "bin/prospero-ranlib",
            "target/include",
            "target/include_common",
        ):
            path = self.sdk / relative
            if path.suffix or path.name.startswith("prospero-"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
                path.chmod(0o755)
            else:
                path.mkdir(parents=True, exist_ok=True)
        (self.sdk / "target/include/ps5.h").write_text("sdk header", encoding="utf-8")
        (self.depends / "include").mkdir(parents=True)
        (self.depends / "lib").mkdir(parents=True)
        (self.depends / "lib/libconfigured.a").write_text("configured", encoding="utf-8")

    def _fake_fetch(self, workspace: Path, target: str, url: str, commit: str) -> Path:
        source = workspace / "sources" / target / commit
        marker = source.parent / f"{commit}.json"
        if not marker.is_file() or not source.is_dir():
            source.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"url": url, "commit": commit}), encoding="utf-8"
            )
            (source / "addon.xml").write_text("<addon id='addon.bundle'/>", encoding="utf-8")
            (source / "CMakeLists.txt").write_text("project(addon.bundle)\n", encoding="utf-8")
        return source

    def _fake_run(self, command: list[str], *, env: dict[str, str] | None = None,
                  stream: bool = False):
        if "--build" not in command:
            build_dir = Path(command[command.index("-B") + 1])
            install_prefix = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("-DCMAKE_INSTALL_PREFIX=")))
            self.configure_dirs[str(build_dir)] = install_prefix
            requested = next(arg.split("=", 1)[1] for arg in command if arg.startswith("-DADDONS_TO_BUILD="))
            self.configure_targets[str(build_dir)] = requested.split()
        else:
            build_dir = Path(command[command.index("--build") + 1])
            install_prefix = self.configure_dirs[str(build_dir)]
            addon_ids = self.configure_targets[str(build_dir)]
            if addon_ids == ["screensavers.rsxs"] and self.multi_output:
                addon_ids = ["screensaver.one", "screensaver.two"]
            for addon_id in addon_ids:
                addon_dir = install_prefix / "share/kodi/addons" / addon_id
                addon_dir.mkdir(parents=True)
                lib_dir = install_prefix / "lib/kodi/addons" / addon_id
                lib_dir.mkdir(parents=True)
                (addon_dir / "addon.xml").write_text(
                    f"<addon id='{addon_id}'><extension "
                    f"library_freebsd='{addon_id}.sprx'/>"
                    "<extension point='xbmc.addon.metadata'><platform>ps5</platform>"
                    "</extension></addon>", encoding="utf-8"
                )
                raw = elf_fixture() if self.invalid_primary else native_addon.FSELF + b"fixture"
                binary = lib_dir / f"{addon_id}.sprx"
                binary.write_bytes(raw)
                requirement = {"module": "libkodi-runtime", "soname": "libkodi-runtime.sprx"}
                binary.with_suffix(".sprx.json").write_text(json.dumps({
                    "schema": 1, "format": "ps5-native-addon", "module": addon_id,
                    "output_sha256": native_addon.digest(raw),
                    "compiler_abi": native_addon.COMPILER_ABI, "sign_extract_equal": True,
                    "provider_requirements": [requirement],
                    "needed": ["libkodi-runtime.sprx"],
                    "native_dependencies": {"needed": ["libkodi-runtime.sprx"]},
                }))
                if self.invalid_helper:
                    (lib_dir / "libhelper.so").write_bytes(elf_fixture())
                (addon_dir / "settings.xml").write_text("resource fixture")
        return mock.Mock(stdout="", stderr="", returncode=0)

    def build(self, only: list[str] | None = None, *, refresh: bool = False) -> list[Path]:
        return addon_build.build(
            self.root,
            self.catalogue,
            self.workspace,
            only or ["addon.bundle"],
            3,
            self.sdk,
            self.depends,
            refresh,
        )

    def test_repeated_build_is_a_noop_and_does_not_rewrite_state(self) -> None:
        first = self.build()
        toolchain = next((self.workspace / "build").glob("addon.bundle/*/toolchain/Toolchain_binaddons.cmake"))
        rendered = toolchain.read_text(encoding="utf-8")
        self.assertIn(str(self.sdk), rendered)
        self.assertIn(str(self.depends), rendered)
        self.assertNotIn("/opt/ps5-payload-sdk", rendered)
        self.assertNotIn("@", rendered)
        self.assertIn("ps5-native-addon.cmake", rendered)
        self.assertIn(str(self.runtime), rendered)
        configure = self.run_mock.call_args_list[0]
        environment = configure.kwargs["env"]
        self.assertIn(str(self.depends / "lib" / "pkgconfig"), environment["PKG_CONFIG_LIBDIR"])
        self.assertNotIn("PKG_CONFIG_SYSROOT_DIR", environment)
        self.assertTrue(Path(environment["PKG_CONFIG"]).is_file())
        self.assertTrue(environment["PATH"].startswith(str(Path(environment["PKG_CONFIG"]).parent)))
        record = self.workspace / "build-record.json"
        lock = self.workspace / "source-lock.json"
        cache = self.workspace / "input-hash-cache.json"
        state = {path: path.stat().st_mtime_ns for path in (record, lock, cache)}
        self.assertEqual(first, self.build())
        self.assertEqual(self.run_mock.call_count, 2)
        self.assertEqual(self.resolve_mock.call_count, 1)
        self.assertEqual(state, {path: path.stat().st_mtime_ns for path in state})

    def test_sdk_target_layout_without_sysroot(self) -> None:
        self.assertFalse((self.sdk / "sysroot").exists())
        addon_build._validate_environment(self.root, self.sdk, self.depends)
        self.build()
        toolchain = next((self.workspace / "build").glob(
            "addon.bundle/*/toolchain/Toolchain_binaddons.cmake"))
        self.assertNotIn(str(self.sdk / "sysroot"), toolchain.read_text())
        self.assertIn(str(self.sdk / "target/user/homebrew"), toolchain.read_text())

    def test_build_streams_commands_and_reports_cached_progress(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.build()
            self.build()
        self.assertIn("[1/1] addon.bundle: building", output.getvalue())
        self.assertIn("[1/1] addon.bundle: complete", output.getvalue())
        self.assertIn("[1/1] addon.bundle: cached - skipping build", output.getvalue())
        self.assertTrue(all(call.kwargs["stream"] for call in self.run_mock.call_args_list))

    def test_streaming_runner_inherits_console_and_propagates_failure(self) -> None:
        self.runner.stop()
        failure = subprocess.CalledProcessError(1, ["cmake"])
        with mock.patch.object(addon_build.subprocess, "run", side_effect=failure) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                addon_build._run(["cmake"], stream=True)
        self.assertIsNone(run.call_args.kwargs["stdout"])
        self.assertIsNone(run.call_args.kwargs["stderr"])

    def test_autoconf_site_selects_sdk_tools_and_cross_host(self) -> None:
        self.build()
        environment = self.run_mock.call_args_list[1].kwargs["env"]
        result = subprocess.run(
            ["sh", "-c", '. "$CONFIG_SITE"; printf "%s\\n" "$CC" "$CXX" "$AR" '
             '"$host_alias" "$cross_compiling"'],
            env=environment, check=True, capture_output=True, text=True)
        lines = result.stdout.splitlines()
        for line, compiler in zip(lines[:2], ("clang", "clang++")):
            command = shlex.split(line)
            self.assertEqual(command[:3], [
                os.sys.executable, str(self.runtime / "tools/native-clang.py"),
                str(self.sdk / "bin" / ("prospero-" + compiler))])
            self.assertIn("-femulated-tls", command)
            self.assertIn(str(self.runtime / "include/ps5_native_abi.h"), command)
            self.assertIn("-I" + str(self.runtime / "include"), command)
            if compiler == "clang++":
                self.assertLess(command.index("-I" + str(self.sdk / "target/include/c++/v1")),
                                command.index("-I" + str(self.runtime / "include")))
        self.assertEqual(lines[2:], [
            str(self.sdk / "bin/prospero-ar"),
            "x86_64-unknown-freebsd", "yes",
        ])
        self.assertEqual(environment["CONFIG_SITE"],
                         self.run_mock.call_args_list[0].kwargs["env"]["CONFIG_SITE"])

    def test_dependency_external_project_preserves_prefix_list(self) -> None:
        real_root = Path(__file__).resolve().parents[2]
        dependency = self.base / "dependencies/common/child"
        dependency.mkdir(parents=True)
        (dependency / "child.txt").write_text("child\n")
        (dependency / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.12)\nproject(child NONE)\n'
            'file(WRITE "${CMAKE_BINARY_DIR}/prefixes.txt" "${CMAKE_PREFIX_PATH}")\n'
            'install(FILES "${CMAKE_BINARY_DIR}/prefixes.txt" DESTINATION share)\n')
        (dependency / "noinstall.txt").touch()
        wrapper = self.base / "dependency-wrapper"
        wrapper.mkdir()
        prefixes = [str(self.base / "first prefix"), str(self.base / "second")]
        (wrapper / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.12)\nproject(wrapper NONE)\n'
            'include(ExternalProject)\nset(CORE_PLATFORM_NAME ps5)\n'
            'set(CORE_SYSTEM_NAME freebsd)\n'
            f'set(CORE_SOURCE_DIR "{real_root}")\n'
            'set(BUILD_DIR "${CMAKE_BINARY_DIR}/build")\n'
            'set(ADDON_DEPENDS_PATH "${CMAKE_BINARY_DIR}/depends")\n'
            f'set(CMAKE_PREFIX_PATH "{";".join(prefixes)}")\n'
            f'include("{real_root}/cmake/scripts/common/HandleDepends.cmake")\n'
            f'add_addon_depends(parent "{dependency.parents[1]}")\n')
        build = self.base / "dependency-build"
        subprocess.run(["cmake", "-S", str(wrapper), "-B", str(build), "-G", "Ninja"],
                       check=True, capture_output=True, text=True)
        result = subprocess.run(["cmake", "--build", str(build)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((build / "build/child/src/child-build/prefixes.txt").read_text(),
                         ";".join(prefixes))

    def test_sdk_missing_required_target_headers_still_fails(self) -> None:
        (self.sdk / "target/include_common").rmdir()
        with self.assertRaisesRegex(ValueError, "target/include_common"):
            addon_build._validate_environment(self.root, self.sdk, self.depends)

    def test_source_root_is_not_confused_with_resource_directory(self) -> None:
        source = self.base / "source"
        resource = source / "addon.bundle"
        resource.mkdir(parents=True)
        (resource / "addon.xml.in").write_text("<addon/>")
        (source / "CMakeLists.txt").write_text("project(addon.bundle)")
        (source / "src").mkdir()
        (source / "src/decoder.cpp").write_text("source")
        staged = self.workspace / "source-prefix"
        addon_build._stage_sources(source, staged, ["addon.bundle"], "addon.bundle",
                                   ["addon.bundle"], self.workspace)
        self.assertTrue((staged / "addon.bundle/CMakeLists.txt").is_file())
        self.assertTrue((staged / "addon.bundle/src/decoder.cpp").is_file())
        self.assertTrue((staged / "addon.bundle/addon.bundle/addon.xml.in").is_file())

    def test_nested_build_root_requires_cmake_not_just_metadata(self) -> None:
        source = self.base / "source"
        nested = source / "addons/addon.bundle"
        nested.mkdir(parents=True)
        (nested / "addon.xml").write_text("<addon/>")
        with self.assertRaisesRegex(ValueError, "CMake build root"):
            addon_build._locate_addon_source(source, "addon.bundle", "collection", 2)
        (nested / "CMakeLists.txt").write_text("project(addon.bundle)")
        self.assertEqual(addon_build._locate_addon_source(
            source, "addon.bundle", "collection", 2), nested)

    def test_subprocess_failure_surfaces_cmake_diagnostics(self) -> None:
        diagnostics = io.StringIO()
        self.runner.stop()
        with redirect_stderr(diagnostics), self.assertRaises(subprocess.CalledProcessError):
            addon_build._run([os.sys.executable, "-c",
                              "import sys; print('configure output'); "
                              "print('actual compiler error', file=sys.stderr); sys.exit(1)"])
        self.assertIn("configure output", diagnostics.getvalue())
        self.assertIn("actual compiler error", diagnostics.getvalue())

    def test_external_project_preserves_prefix_list(self) -> None:
        real_root = Path(__file__).resolve().parents[2]
        cmake_source = (real_root / "cmake/addons/CMakeLists.txt").read_text()
        setup = cmake_source.split('set(_addon_prefix_path "${CMAKE_PREFIX_PATH}")', 1)[1]
        setup = 'set(_addon_prefix_path "${CMAKE_PREFIX_PATH}")' + setup.split("if(MSVC)", 1)[0]
        child = self.base / "child"
        child.mkdir()
        (child / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.12)\nproject(child NONE)\n'
            'file(WRITE "${CMAKE_BINARY_DIR}/prefixes.txt" "${CMAKE_PREFIX_PATH}")\n')
        wrapper = self.base / "wrapper"
        wrapper.mkdir()
        prefixes = [str(self.base / "first prefix"), str(self.base / "second")]
        (wrapper / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.12)\nproject(wrapper NONE)\n'
            'include(ExternalProject)\nset(CORE_PLATFORM_NAME ps5)\n'
            f'set(CMAKE_PREFIX_PATH "{";".join(prefixes)}")\n' + setup +
            f'\nExternalProject_Add(child SOURCE_DIR "{child}" '
            'BINARY_DIR "${CMAKE_BINARY_DIR}/child" ${_addon_externalproject_args} '
            'CMAKE_ARGS ${BUILD_ARGS} BUILD_COMMAND "" INSTALL_COMMAND "")\n')
        build = self.base / "prefix-build"
        subprocess.run(["cmake", "-S", str(wrapper), "-B", str(build), "-G", "Ninja"],
                       check=True, capture_output=True, text=True)
        subprocess.run(["cmake", "--build", str(build)], check=True,
                       capture_output=True, text=True)
        self.assertEqual((build / "child/prefixes.txt").read_text(), ";".join(prefixes))

    def test_ps5_requires_native_graphics_sdk_without_affecting_freebsd(self) -> None:
        real_root = Path(__file__).resolve().parents[2]
        source = self.base / "opengl"
        source.mkdir()
        headers = source / "include/GL"
        headers.mkdir(parents=True)
        (headers / "gl.h").write_text("void glClear(unsigned int mask);\n")
        (source / "addon.c").write_text(
            '#include <GL/gl.h>\nvoid render(void) { glClear(0); }\n')
        (source / "sdk.c").write_text("void glClear(unsigned int mask) {}\n")
        (source / "FindOpenGl.cmake").write_text(
            'find_library(OPENGL_gl_LIBRARY NAMES GL REQUIRED)\n'
            'find_path(OPENGL_INCLUDE_DIR GL/gl.h REQUIRED)\n'
            'include(FindPackageHandleStandardArgs)\n'
            'find_package_handle_standard_args(OpenGl REQUIRED_VARS '
            'OPENGL_gl_LIBRARY OPENGL_INCLUDE_DIR)\n'
            'set(OPENGL_LIBRARIES "${OPENGL_gl_LIBRARY}")\n')
        (source / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.18)\nproject(opengl C CXX)\n'
            'set(APP_NAME Kodi)\nset(APP_NAME_LC kodi)\nset(APP_NAME_UC KODI)\n'
            f'set(APP_LIB_DIR "{real_root}/cmake/scripts/common")\n'
            'set(CORE_PLATFORM_NAME "${TEST_PLATFORM}")\n'
            'set(OPENGL_INCLUDE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/include" CACHE PATH "")\n'
            # A stale discovery result must not reintroduce the SDK shared library.
            'set(OPENGL_gl_LIBRARY "${CMAKE_BINARY_DIR}/libGL.so" CACHE FILEPATH "")\n'
            f'configure_file("{real_root}/cmake/KodiConfig.cmake.in" '
            '"${CMAKE_BINARY_DIR}/KodiConfig.cmake" @ONLY)\n'
            'include("${CMAKE_BINARY_DIR}/KodiConfig.cmake")\n'
            'list(PREPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}")\n'
            'find_package(OpenGl REQUIRED)\n'
            'add_library(GL SHARED sdk.c)\n'
            'add_library(legacy SHARED addon.c)\n'
            'target_include_directories(legacy PRIVATE "${OPENGL_INCLUDE_DIR}")\n'
            'target_link_libraries(legacy PRIVATE ${OPENGL_LIBRARIES})\n'
            'target_link_options(legacy PRIVATE -nostdlib)\n'
            'add_dependencies(legacy GL)\n'
            'if(TEST_PLATFORM STREQUAL "ps5")\n'
            '  list(REMOVE_ITEM CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}")\n'
            f'  list(PREPEND CMAKE_MODULE_PATH "{real_root}/cmake/modules")\n'
            '  find_package(OpenGl REQUIRED)\n'
            '  add_library(modern SHARED addon.c)\n'
            '  target_link_libraries(modern PRIVATE kodi::OpenGl)\n'
            '  target_link_options(modern PRIVATE -nostdlib)\n'
            'endif()\n')
        for platform in ("ps5", "freebsd"):
            with self.subTest(platform=platform):
                build = self.base / f"opengl-{platform}"
                result = subprocess.run(
                    ["cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
                     f"-DTEST_PLATFORM={platform}"],
                    capture_output=True, text=True)
                if platform == "ps5":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("require a prepared PS5_NATIVE_RUNTIME_SDK",
                                  result.stdout + result.stderr)
                    continue
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = subprocess.run(["cmake", "--build", str(build)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                dynamic = subprocess.run(
                    ["readelf", "-d", str(build / "liblegacy.so")],
                    check=True, capture_output=True, text=True).stdout
                self.assertIn("libGL.so", dynamic)

    def test_refresh_re_resolves_pin_and_rebuilds(self) -> None:
        self.build()
        second_sha = "b" * 40
        self.resolve_mock.side_effect = [second_sha]
        result = self.build(refresh=True)
        self.assertEqual(self.resolve_mock.call_count, 2)
        self.assertEqual(self.run_mock.call_count, 4)
        lock = json.loads((self.workspace / "source-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["targets"]["addon.bundle"]["sha"], second_sha)
        record = json.loads((self.workspace / "build-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["targets"]["addon.bundle"]["source_revision"], second_sha)
        self.assertTrue(result[0].is_dir())

    def test_changes_to_relevant_inputs_rebuild(self) -> None:
        self._assert_change_rebuilds("tools/ps5/patches/addons/addon.bundle/0001.patch")

    def _assert_change_rebuilds(self, relative: str) -> None:
        self.build()
        path = self.base / relative if relative.startswith("depends/") else self.root / relative
        path.write_text(path.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
        self.build()
        self.assertEqual(self.run_mock.call_count, 4)

    def test_header_changes_rebuild(self) -> None:
        self._assert_change_rebuilds("xbmc/addons/kodi-dev-kit/include/kodi/header.h")

    def test_toolchain_changes_rebuild(self) -> None:
        self._assert_change_rebuilds("toolchain/ps5-native-addon.cmake")

    def test_native_sdk_input_changes_rebuild(self) -> None:
        self._assert_change_rebuilds("build/ps5-native/sdk/manifest.json")

    def test_native_sdk_python_cache_does_not_invalidate_build(self) -> None:
        self.build()
        calls = self.run_mock.call_count
        cache = self.runtime / "tools/__pycache__"
        cache.mkdir(parents=True)
        (cache / "native_shared_link.cpython-312.pyc").write_bytes(b"generated bytecode")
        self.build()
        self.assertEqual(self.run_mock.call_count, calls)

    def test_kodi_config_changes_rebuild(self) -> None:
        self._assert_change_rebuilds("cmake/KodiConfig.cmake.in")

    def test_native_validation_changes_rebuild(self) -> None:
        self._assert_change_rebuilds("tools/ps5/native_addon.py")

    def test_loader_incompatible_builds_do_not_record_success(self) -> None:
        for attribute, filename in (
            ("invalid_primary", "addon.bundle.sprx"),
            ("invalid_helper", "libhelper.so"),
        ):
            with self.subTest(artifact=filename):
                setattr(self, attribute, True)
                with self.assertRaisesRegex(
                    addon_build.AddonBuildError, "signed SPRX|unmodelled native dependency"
                ):
                    self.build()
                record = self.workspace / "build-record.json"
                if record.exists():
                    contents = json.loads(record.read_text())
                    self.assertEqual(contents["targets"], {})
                    self.assertEqual(contents["prefixes"], [])
                setattr(self, attribute, False)

    def test_cached_loader_incompatible_binary_invalidates_record(self) -> None:
        output = self.build()[0]
        binary = output / "lib/kodi/addons/addon.bundle/addon.bundle.sprx"
        binary.write_bytes(elf_fixture(needed=True))
        record_path = self.workspace / "build-record.json"
        record = json.loads(record_path.read_text())
        relative = str(binary.relative_to(output))
        record["targets"]["addon.bundle"]["artifacts"][relative] = (
            addon_build.hashlib.sha256(binary.read_bytes()).hexdigest())
        record_path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "missing, stale"):
            addon_build.validate_build(
                self.root, self.catalogue, self.workspace, self.sdk, self.depends)
        self.assertEqual(self.build(), [output])
        self.assertEqual(self.run_mock.call_count, 4)

    def test_configured_depends_changes_rebuild(self) -> None:
        self._assert_change_rebuilds("depends/lib/libconfigured.a")

    def test_missing_installed_binary_invalidates_success_record(self) -> None:
        output = self.build()[0]
        (output / "lib/kodi/addons/addon.bundle/addon.bundle.sprx").unlink()
        rebuilt = self.build()
        self.assertEqual(rebuilt, [output])
        self.assertEqual(self.run_mock.call_count, 4)

    def test_changed_resources_and_helpers_invalidate_success_record(self) -> None:
        output = self.build()[0]
        resource = output / "share/kodi/addons/addon.bundle/settings.xml"
        resource.write_text("changed resource")
        self.build()
        self.assertEqual(self.run_mock.call_count, 4)
        helper = output / "lib/kodi/addons/addon.bundle/addon.bundle.sprx.json"
        helper.unlink()
        self.build()
        self.assertEqual(self.run_mock.call_count, 6)

    def test_missing_staged_source_invalidates_success_record(self) -> None:
        output = self.build()[0]
        staged = next((self.workspace / "build").glob("addon.bundle/*/source-prefix/addon.bundle"))
        shutil.rmtree(staged)
        self.assertEqual(self.build(), [output])
        self.assertEqual(self.run_mock.call_count, 4)

    def test_missing_cached_source_invalidates_success_record(self) -> None:
        output = self.build()[0]
        source = self.workspace / "sources" / "addon.bundle" / self.sha
        shutil.rmtree(source)
        self.assertEqual(self.build(), [output])
        self.assertEqual(self.run_mock.call_count, 4)

    def test_screensavers_rsxs_returns_multiple_installs(self) -> None:
        self.multi_output = True
        self.catalogue = {
            "schema": 1,
            "runtime": "piers-ps5-1",
            "targets": {
                "screensavers.rsxs": {
                    "url": "https://github.com/example/screensavers.rsxs",
                    "ref": "Piers",
                    "addons": ["screensaver.one", "screensaver.two"],
                }
            },
            "addons": {
                "screensaver.one": {
                    "target": "screensavers.rsxs",
                    "bundled": False,
                    "channel": "testing",
                    "revision": 1,
                },
                "screensaver.two": {
                    "target": "screensavers.rsxs",
                    "bundled": False,
                    "channel": "testing",
                    "revision": 1,
                }
            },
        }
        outputs = self.build(["screensavers.rsxs"])
        self.assertEqual(len(outputs), 1)
        self.assertEqual(
            sorted(path.name for path in (outputs[0] / "share/kodi/addons").iterdir()),
            ["screensaver.one", "screensaver.two"],
        )
        self.assertTrue(outputs[0].is_absolute())
        source = next((self.workspace / "build").glob(
            "screensavers.rsxs/*/source-prefix/screensavers.rsxs/CMakeLists.txt"))
        self.assertTrue(source.is_file())
        self.assertEqual(self.build(["screensaver.one"]), outputs)
        self.assertEqual(self.run_mock.call_count, 2)

        patches = self.root / "tools/ps5/patches/addons/screensavers.rsxs"
        patches.mkdir(parents=True)
        patch = patches / "0001-native-fixture.patch"
        for content, calls in (("initial target patch", 4), ("changed target patch", 6)):
            patch.write_text(content)
            with self.assertRaisesRegex(addon_build.AddonBuildError, "missing, stale"):
                addon_build.validate_build(
                    self.root, self.catalogue, self.workspace, self.sdk, self.depends)
            outputs = self.build(["screensavers.rsxs"])
            self.assertEqual(self.run_mock.call_count, calls)
            self.assertEqual(self.build(["screensaver.two"]), outputs)
            self.assertEqual(self.run_mock.call_count, calls)

    def test_source_archive_rejects_path_traversal(self) -> None:
        archive = self.base / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            member = tarfile.TarInfo("repository/../../escaped")
            member.size = 1
            stream.addfile(member, io.BytesIO(b"x"))
        destination = self.base / "extracted"
        with self.assertRaises(addon_build.AddonBuildError):
            addon_build._extract_archive_safely(archive, destination)
        self.assertFalse(destination.exists())
        self.assertFalse((self.base / "escaped").exists())

    def test_failed_build_does_not_create_success_record(self) -> None:
        def fail_build(command: list[str], *, env: dict[str, str] | None = None,
                       stream: bool = False):
            if "--build" in command:
                raise subprocess.CalledProcessError(1, command, stderr="fixture failure")
            return self._fake_run(command, env=env)

        self.run_mock.side_effect = fail_build
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()
        record = self.workspace / "build-record.json"
        if record.exists():
            contents = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(contents["targets"], {})
            self.assertEqual(contents["prefixes"], [])

    def test_record_schema_and_standalone_validation(self) -> None:
        self.build()
        record = json.loads((self.workspace / "build-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], 3)
        self.assertEqual(record["prefixes"], [record["targets"]["addon.bundle"]["prefix"]])
        entry = record["targets"]["addon.bundle"]
        self.assertEqual(entry["url"], self.catalogue["targets"]["addon.bundle"]["url"])
        self.assertEqual(entry["source_revision"], self.sha)
        self.assertEqual(len(entry["fingerprint"]), 64)

        validated = addon_build.validate_build(
            self.root, self.catalogue, self.workspace, self.sdk, self.depends
        )
        self.assertEqual(validated, record)
        self.assertEqual(self.resolve_mock.call_count, 1)
        self.assertEqual(self.fetch_mock.call_count, 1)

    def test_packaging_policy_changes_do_not_invalidate_compilation(self) -> None:
        outputs = self.build()
        record_path = self.workspace / "build-record.json"
        original = record_path.read_bytes()
        policy = self.catalogue["addons"]["addon.bundle"]
        policy.update(bundled=True, channel="stable", revision=7)
        self.assertEqual(self.build(), outputs)
        validated = addon_build.validate_build(
            self.root, self.catalogue, self.workspace, self.sdk, self.depends
        )
        self.assertEqual(record_path.read_bytes(), original)
        self.assertEqual(len(validated["targets"]), 1)
        self.assertEqual(self.run_mock.call_count, 2)
        self.assertEqual(self.resolve_mock.call_count, 1)

    def test_validation_rejects_stale_inputs_without_network(self) -> None:
        self.build()
        (self.root / "tools/ps5/patches/addons/addon.bundle/0001.patch").write_text(
            "changed patch", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "missing, stale"):
            addon_build.validate_build(
                self.root, self.catalogue, self.workspace, self.sdk, self.depends
            )
        self.assertEqual(self.resolve_mock.call_count, 1)
        self.assertEqual(self.fetch_mock.call_count, 1)

    def test_only_preserves_other_target_records_and_prefixes(self) -> None:
        self.build()
        self.catalogue["targets"]["addon.second"] = {
            "url": "https://github.com/example/addon.second",
            "ref": "Piers",
            "addons": ["addon.second"],
        }
        self.catalogue["addons"]["addon.second"] = {
            "target": "addon.second",
            "bundled": False,
            "channel": "testing",
            "revision": 1,
        }
        second_prefix = self.build(["addon.second"])[0]
        record = json.loads((self.workspace / "build-record.json").read_text(encoding="utf-8"))
        self.assertEqual(set(record["targets"]), {"addon.bundle", "addon.second"})
        self.assertEqual(set(record["prefixes"]), {
            record["targets"]["addon.bundle"]["prefix"],
            str(second_prefix.resolve()),
        })
        validated = addon_build.validate_build(
            self.root, self.catalogue, self.workspace, self.sdk, self.depends
        )
        self.assertEqual(set(validated["targets"]), {"addon.bundle", "addon.second"})


class RsxsIdentityTests(unittest.TestCase):
    def test_microcosm_build_and_resources_keep_published_identity(self):
        root = Path(__file__).resolve().parents[2]
        work = root / "build/ps5-addon-repository/work"
        lock = work / "source-lock.json"
        if not all(shutil.which(tool) for tool in ("cmake", "cc", "c++", "patch")):
            self.skipTest("CMake, host C/C++ compilers and patch required")
        if not lock.is_file():
            self.skipTest("locked RSXS source cache required")
        entry = json.loads(lock.read_text())["targets"].get("screensavers.rsxs")
        if entry is None:
            self.skipTest("locked RSXS source cache required")
        cached = work / "sources/screensavers.rsxs" / entry["sha"]
        if not cached.is_dir():
            self.skipTest("locked RSXS source cache required")

        with tempfile.TemporaryDirectory(prefix="test-rsxs-identity-",
                                         dir=root / "build") as temporary:
            directory = Path(temporary)
            source = directory / "source"
            shutil.copytree(cached, source)

            def run(*command):
                result = subprocess.run(list(map(str, command)), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            run("cmake", "-DSOURCE_DIR=" + str(source),
                "-DSERIES_DIR=" + str(root / "tools/ps5/patches/addons/screensavers.rsxs"),
                "-P", root / "tools/ps5/apply-patch-series.cmake")
            (source / "CMakeLists.txt").write_text("""
cmake_minimum_required(VERSION 3.18)
project(rsxs_identity NONE)
macro(build_addon target prefix libs)
  file(WRITE "${CMAKE_BINARY_DIR}/addon-target.txt" "${target}")
endmacro()
include(src/microcosm/CMakeLists.txt)
""")
            binary = directory / "build"
            run("cmake", "-S", source, "-B", binary)
            addon = (binary / "addon-target.txt").read_text()
            self.assertEqual(addon, "screensaver.microcosm.biof")
            original = source / "screensaver.rsxs.microcosm"
            staged = source / addon
            metadata = (staged / "addon.xml.in").read_text()
            self.assertIn('id="' + addon + '"', metadata)
            expected = {p.relative_to(original): p.read_bytes()
                        for p in original.rglob("*") if p.is_file()}
            actual = {p.relative_to(staged): p.read_bytes()
                      for p in staged.rglob("*") if p.is_file()}
            self.assertEqual(actual, expected)


class SidplayConfigureTests(unittest.TestCase):
    def test_native_configure_does_not_duplicate_cmake_quoted_abi_flags(self):
        root = Path(__file__).resolve().parents[2]
        work = root / "build/ps5-addon-repository/work"
        lock = work / "source-lock.json"
        if not all(shutil.which(tool) for tool in ("cmake", "cc", "c++", "patch")):
            self.skipTest("CMake, host C/C++ compilers and patch required")
        if not lock.is_file():
            self.skipTest("locked Sidplay source cache required")
        entry = json.loads(lock.read_text())["targets"].get("audiodecoder.sidplay")
        if entry is None:
            self.skipTest("locked Sidplay source cache required")
        cached = work / "sources/audiodecoder.sidplay" / entry["sha"]
        if not cached.is_dir():
            self.skipTest("locked Sidplay source cache required")

        with tempfile.TemporaryDirectory(prefix="test-sidplay-configure-",
                                         dir=root / "build") as temporary:
            directory = Path(temporary)
            source = directory / "source"
            shutil.copytree(cached, source)
            tools = directory / "host-tools"
            tools.mkdir()
            autoreconf = tools / "autoreconf"
            autoreconf.write_text("#!/bin/sh\nexit 0\n")
            autoreconf.chmod(0o755)
            environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                               CONFIG_SITE=str(directory / "native-config.site"))

            def run(*command):
                result = subprocess.run(list(map(str, command)), env=environment,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            run("cmake", "-DSOURCE_DIR=" + str(source),
                "-DSERIES_DIR=" + str(root / "tools/ps5/patches/addons/audiodecoder.sidplay"),
                "-P", root / "tools/ps5/apply-patch-series.cmake")
            dependency = source / "depends/common/sidplay2"
            configure = dependency / "configure"
            configure.write_text(
                "#!" + os.sys.executable + "\n"
                "import json, os\nfrom pathlib import Path\n"
                "Path(__file__).with_name('configure-flags.json').write_text(json.dumps("
                "{key: os.environ.get(key) for key in "
                "('CFLAGS', 'CXXFLAGS', 'LDFLAGS', 'CONFIG_SITE')}))\n")
            configure.chmod(0o755)
            cmake = dependency / "CMakeLists.txt"
            cmake.write_text(cmake.read_text() +
                             "\nExternalProject_Add_StepTargets(sidplay2 configure)\n")
            header = directory / "abi.h"
            header.write_text("\n")
            binary = directory / "build"
            run("cmake", "-S", dependency, "-B", binary,
                "-DCMAKE_C_COMPILER=" + shutil.which("cc"),
                "-DCMAKE_CXX_COMPILER=" + shutil.which("c++"),
                '-DCMAKE_C_FLAGS=-include "' + str(header) + '"',
                '-DCMAKE_CXX_FLAGS=-include "' + str(header) + '"',
                "-DCMAKE_EXE_LINKER_FLAGS=-Wl,--as-needed")
            run("cmake", "--build", binary, "--target", "sidplay2-configure")
            flags = json.loads((dependency / "configure-flags.json").read_text())
            self.assertEqual(flags["CFLAGS"], "-DTARGET_POSIX -DTARGET_FREEBSD")
            self.assertEqual(flags["CXXFLAGS"],
                             "-DTARGET_POSIX -DTARGET_FREEBSD -DHAVE_UNIX")
            self.assertEqual(flags["LDFLAGS"], "-Wl,--as-needed")
            self.assertEqual(flags["CONFIG_SITE"], environment["CONFIG_SITE"])


if __name__ == "__main__":
    unittest.main()
