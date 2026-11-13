#!/usr/bin/env python3
"""Verify system import contracts preserve the shared core's DNS ownership."""

import ps5_test_paths
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "native_preparation", (ps5_test_paths.HERE / 'scripts/prepare-native-runtime.py'))
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class StageCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.c"
        self.source.write_text("original")
        self.output = self.root / "output"
        self.output.mkdir()
        self.calls = []

    def stage(self, name="core", source=None, identity=None):
        def build(work):
            self.calls.append(name)
            (work / "result").write_text("built")
        return PREPARE.StageCache(self.output).run(
            name, [source or self.source], build, lambda work: [work / "result"], identity)

    def test_unchanged_inputs_reuse_integrity_checked_outputs(self):
        first = self.stage()
        self.assertEqual(self.stage(), first)
        self.assertEqual(self.calls, ["core"])
        (first / "result").write_text("corrupted")
        with self.assertRaisesRegex(ValueError, "cached core changed"):
            self.stage()
        self.assertEqual(self.calls, ["core"])

    def test_core_implementation_change_does_not_invalidate_independent_stages(self):
        graphics = self.root / "graphics.c"
        graphics.write_text("graphics")
        dependencies = self.root / "dependency.c"
        dependencies.write_text("dependency")
        for _ in range(2):
            self.stage()
            self.stage("graphics", graphics, {"abi": 1})
            self.stage("depends", dependencies)
            self.source.write_text("changed")
        self.assertEqual(self.calls, ["core", "graphics", "depends", "core"])

    def test_abi_recipe_and_compiler_inputs_invalidate_consumers(self):
        before = self.stage("graphics", identity={"abi": 1})
        after = self.stage("graphics", identity={"abi": 2})
        self.assertNotEqual(before, after)
        self.source.write_text("changed recipe/compiler")
        self.assertNotEqual(after, self.stage("graphics", identity={"abi": 2}))

    def test_changed_sources_are_not_receipted(self):
        def build(work):
            (work / "result").write_text("built")
            self.source.write_text("changed during build")
        with self.assertRaisesRegex(ValueError, "inputs changed while building"):
            PREPARE.StageCache(self.output).run(
                "core", [self.source], build, lambda work: [work / "result"])
        self.assertFalse(list(self.output.glob("stages/core/*/stage.json")))

    def test_failure_retains_diagnostics_without_receipt(self):
        def build(work):
            (work / "failure.log").write_text("failure")
            raise ValueError("builder failed")
        with self.assertRaisesRegex(ValueError, "builder failed"):
            PREPARE.StageCache(self.output).run(
                "core", [self.source], build, lambda work: [work / "result"])
        self.assertTrue(list(self.output.glob("stages/core/*/failure.log")))
        self.assertFalse(list(self.output.glob("stages/core/*/stage.json")))
        with self.assertRaisesRegex(ValueError, "incomplete core"):
            self.stage()

    def test_check_inputs_detects_changes_before_publication(self):
        cache = PREPARE.StageCache(self.output)
        cache.run("core", [self.source], lambda work: (work / "result").write_text("built"),
                  lambda work: [work / "result"])
        self.source.write_text("changed after core build")
        with self.assertRaisesRegex(ValueError, "current SDK not replaced"):
            cache.check_inputs()

    def test_removed_receipt_output_is_rejected(self):
        work = self.stage()
        receipt = json.loads((work / "stage.json").read_text())
        receipt["files"] = {"unrelated": "checksum"}
        (work / "stage.json").write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "incomplete completed stage"):
            self.stage()

    def test_recipe_closure_contains_dynamic_qualifier_not_unrelated_tools(self):
        files = PREPARE.recipe_inputs(PREPARE.HERE / "build-shared-runtime.py")
        self.assertIn(PREPARE.HERE / "qualify-shared-cxx.py", files)
        self.assertIn(PREPARE.HERE / "prepare_runtime_archive.py", files)
        self.assertNotIn(PREPARE.HERE / "addon_build.py", files)

    def test_preflight_tracks_dependency_headers_without_invalidating_core_for_homebrew(self):
        source = self.root / "graphics"
        config = source / "build/mesa-ps5-probe/meson-private/cmd_line.txt"
        config.parent.mkdir(parents=True)
        config.write_text("[properties]\ncross_file = []\n")
        sdk = self.root / "sdk"
        homebrew = sdk / "target/user/homebrew"
        homebrew.mkdir(parents=True)
        tool = self.root / "native-tool"
        tool.write_text("fixture")
        systems = self.root / "system"
        modules = ("libSceLibcInternal", "libkernel", "libSceNet", "libSceAgc",
                   "libSceAgcDriver", "libSceVideoOut", "libSceSystemService")
        for module in modules:
            path = systems / module / "manifest.json"
            path.parent.mkdir(parents=True)
            path.write_text("{}")
        args = SimpleNamespace(
            output=self.output, source=source, opengl=self.root / "opengl",
            native_tool=tool, sdk=sdk, system_sdk=[systems],
            depends=self.root / "depends", homebrew_sources=self.root / "sources",
            check_inputs=True)
        with patch.object(PREPARE, "qualify_graphics", return_value={}), \
                patch.object(PREPARE.os, "access", return_value=True), \
                patch.object(PREPARE.shutil, "which", side_effect=lambda name: "/tools/" + name), \
                patch.object(PREPARE.subprocess, "run",
                             return_value=SimpleNamespace(stdout="/tools/clang-resources")), \
                patch.object(PREPARE, "load_provider", side_effect=lambda path: {
                    "module": path.parent.name, "import_library": {"path": "lib/import.so"}}), \
                patch.object(PREPARE, "StageCache") as cache:
            PREPARE.prepare(args)
        paths = [call.args[0] for call in cache.return_value.hasher.tree_digest.call_args_list]
        self.assertNotIn(homebrew, paths[0])
        self.assertIn(homebrew, paths[1])
        for dependency in paths[3:]:
            self.assertIn(homebrew, dependency)
            self.assertIn(args.depends / "include", dependency)
        cache.return_value.run.assert_not_called()

    def test_provider_abi_ignores_function_implementation_but_not_data_layout(self):
        provider = {
            "module": "core", "module_version": 1, "library": "core",
            "library_version": 1, "soname": "core.sprx",
            "link_contract": {"network": {"implementation": "dns-v1", "source_sha256": "old"}},
            "symbols": [{"name": "function", "nid": "f", "info": 18, "other": 0,
                         "size": 20, "value": 100},
                        {"name": "data", "nid": "d", "info": 17, "other": 0,
                         "size": 8, "value": 200}],
        }
        with patch.object(PREPARE, "load_provider", return_value=provider):
            before = PREPARE.provider_abi([Path("manifest")])
            provider["symbols"][0].update(size=100, value=2000)
            provider["link_contract"]["network"]["source_sha256"] = "new"
            self.assertEqual(before, PREPARE.provider_abi([Path("manifest")]))
            provider["symbols"][1]["size"] = 16
            self.assertNotEqual(before, PREPARE.provider_abi([Path("manifest")]))

    def test_aliases_switch_via_one_generation_pointer_and_preserve_legacy(self):
        old = self.output / "artifacts/old"
        new = self.output / "artifacts/new"
        for generation in (old, new):
            for name in ("sdk", "depends"):
                (generation / name).mkdir(parents=True)
        for name in ("sdk", "depends"):
            (self.output / name).symlink_to(old / name)
        PREPARE.publish_generation(self.output, new)
        self.assertEqual((self.output / "current").resolve(), new)
        for name in ("sdk", "depends"):
            self.assertEqual((self.output / name).readlink(), Path("current") / name)
            self.assertEqual((self.output / name).resolve(), new / name)
            self.assertTrue((old / name).is_dir())
        PREPARE.publish_generation(self.output, old)
        for name in ("sdk", "depends"):
            self.assertEqual((self.output / name).resolve(), old / name)

    def test_publication_rejects_unowned_directories(self):
        work = self.output / "artifacts/new"
        work.mkdir(parents=True)
        (self.output / "sdk").mkdir()
        with self.assertRaisesRegex(ValueError, "unowned preparation path"):
            PREPARE.publish_generation(self.output, work)
        self.assertFalse((self.output / "current").exists())


@unittest.skipUnless(shutil.which("clang-18") and shutil.which("ld.lld"),
                     "requires clang-18 and ld.lld")
class SystemContractTests(unittest.TestCase):
    def test_system_imports_do_not_compete_with_shared_dns(self):
        with tempfile.TemporaryDirectory(prefix="native-system-contract-") as directory:
            root = Path(directory)
            source = root / "imports.c"
            source.write_text("\n".join(
                f"int {name}(void) {{ return 0; }}"
                for name in sorted(PREPARE.NETWORK_SYMBOLS | {"keep_sdk_import"})))
            library = root / "imports.so"
            subprocess.run(["clang-18", "-shared", "-fPIC", "-nostdlib", str(source),
                            "-o", str(library)], check=True, capture_output=True)
            original = PREPARE.digest(library)
            manifest = PREPARE.system_contract(
                library, "libSystemTest", root / "contract", shutil.which("ld.lld"))
            provider = PREPARE.load_provider(manifest)
            self.assertEqual({symbol["name"] for symbol in provider["symbols"]},
                             {"keep_sdk_import"})
            self.assertEqual(provider["source_import_library_sha256"], original)
            self.assertEqual(PREPARE.digest(library), original)
            self.assertFalse(provider["runtime_implementation_qualified"])
            self.assertEqual(provider["module_version"], 257)
            self.assertEqual(provider["library_version"], 1)


if __name__ == "__main__":
    unittest.main()
