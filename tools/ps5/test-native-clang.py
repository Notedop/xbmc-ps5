#!/usr/bin/env python3
"""Check native frontend selection, dependency targets and object publication."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "native_clang", Path(__file__).with_name("native-clang.py"))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class NativeClangTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-clang-test-")
        self.output = Path(self.temp.name) / "main.o"
        self.output.write_bytes(b"previous")

    def tearDown(self):
        self.temp.cleanup()

    def test_native_source_frontend_and_backend_only_ctor_lowering(self):
        commands = []

        def run(command):
            commands.append(command)
            Path(command[command.index("-o") + 1]).write_bytes(b"compiled")
            return 0

        with patch.object(launcher.subprocess, "call", side_effect=run):
            code = launcher.launch("prospero-clang++", [
                "-O2", "-fPIC", "-MMD", "-MF", "main.d", "-c", "main.cpp",
                "-o", str(self.output)])
        self.assertEqual(code, 0)
        self.assertFalse(any(arg.startswith("--target") for arg in commands[0]))
        self.assertIn("-emit-llvm", commands[0])
        self.assertIn("-MT", commands[0])
        self.assertEqual(commands[0][commands[0].index("-MT") + 1], str(self.output))
        self.assertIn("--target=x86_64-unknown-freebsd9", commands[1])
        self.assertIn("-fuse-init-array", commands[1])
        self.assertIn("-O2", commands[1])
        self.assertNotIn("main.cpp", commands[1])
        self.assertEqual(self.output.read_bytes(), b"compiled")
        self.assertFalse(list(self.output.parent.glob(".native-clang-*")))

    def test_frontend_failure_preserves_previous_object(self):
        with patch.object(launcher.subprocess, "call", return_value=3) as run:
            code = launcher.launch("prospero-clang", ["-c", "main.c", "-o", str(self.output)])
        self.assertEqual(code, 3)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.output.read_bytes(), b"previous")

    def test_implicit_output_uses_driver_plan_and_native_lowering(self):
        commands = []

        def run(command):
            commands.append(command)
            Path(command[command.index("-o") + 1]).write_bytes(b"compiled")
            return 0

        plan = Mock(returncode=0, stderr=(
            'clang version 18\n "/sdk/clang" "-cc1" "-emit-obj" "-o" "'
            + str(self.output) + '" "-x" "c" "main.c"\n'))
        with patch.object(launcher.subprocess, "run", return_value=plan) as query, \
                patch.object(launcher.subprocess, "call", side_effect=run):
            self.assertEqual(launcher.launch("prospero-clang", [
                "-include", "header.c", "-MMD", "-c", "main.c"]), 0)
        query.assert_called_once_with(
            ["prospero-clang", "-include", "header.c", "-MMD", "-c", "main.c", "-###"],
            capture_output=True, text=True)
        self.assertIn("-emit-llvm", commands[0])
        self.assertEqual(commands[0][commands[0].index("-MT") + 1], str(self.output))
        self.assertEqual(commands[0][commands[0].index("-MF") + 1],
                         str(self.output.with_suffix(".d")))
        self.assertIn("-fuse-init-array", commands[1])
        self.assertEqual(self.output.read_bytes(), b"compiled")

    def test_invalid_or_multiple_implicit_outputs_do_not_compile(self):
        plans = (
            Mock(returncode=1, stderr="clang: error: no such file"),
            Mock(returncode=0, stderr=' "/sdk/clang" "-cc1" "-E" "-o" "-"\n'),
            Mock(returncode=0, stderr=(
                ' "/sdk/clang" "-cc1" "-emit-obj" "-o" "a.o"\n'
                ' "/sdk/clang" "-cc1" "-emit-obj" "-o" "b.o"\n')),
        )
        for plan in plans:
            with self.subTest(plan=plan), \
                    patch.object(launcher.subprocess, "run", return_value=plan), \
                    patch.object(launcher.subprocess, "call") as compile_command:
                with self.assertRaisesRegex(ValueError, "object output"):
                    launcher.launch("prospero-clang", ["-c", "main.c"])
                compile_command.assert_not_called()
                self.assertEqual(self.output.read_bytes(), b"previous")

    def test_backend_failure_preserves_previous_object(self):
        with patch.object(launcher.subprocess, "call", side_effect=[0, 4]):
            code = launcher.launch("prospero-clang", ["-c", "main.c", "-o", str(self.output)])
        self.assertEqual(code, 4)
        self.assertEqual(self.output.read_bytes(), b"previous")

    def test_explicit_dependency_target_is_preserved(self):
        with patch.object(launcher.subprocess, "call", return_value=1) as run:
            launcher.launch("prospero-clang", [
                "-MMD", "-MT", "chosen.o", "-c", "main.c", "-o", str(self.output)])
        command = run.call_args.args[0]
        self.assertEqual(command.count("-MT"), 1)
        self.assertIn("chosen.o", command)
        self.assertEqual(command[command.index("-MF") + 1], str(self.output.with_suffix(".d")))

    def test_queries_and_link_commands_are_forwarded_unchanged(self):
        for arguments in (["--version"], ["-E", "main.c"], ["main.o", "-o", "a.out"]):
            with self.subTest(arguments=arguments), \
                    patch.object(launcher.subprocess, "call", return_value=0) as run:
                self.assertEqual(launcher.launch("prospero-clang", arguments), 0)
                run.assert_called_once_with(["prospero-clang", *arguments])

    def test_foreign_source_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "retain the PS5 target"):
            launcher.launch("prospero-clang", [
                "--target=x86_64-unknown-freebsd9", "-c", "main.c", "-o", str(self.output)])
        self.assertEqual(self.output.read_bytes(), b"previous")

    def test_compiler_cache_prefix_wraps_both_compilation_stages(self):
        commands = []

        def run(command):
            commands.append(command)
            Path(command[command.index("-o") + 1]).write_bytes(b"cached")
            return 0

        with patch.object(launcher.subprocess, "call", side_effect=run):
            result = launcher.launch("ccache", [
                "prospero-clang", "-c", "main.c", "-o", str(self.output)])
        self.assertEqual(result, 0)
        self.assertTrue(all(c[:2] == ["ccache", "prospero-clang"] for c in commands))
        self.assertEqual(self.output.read_bytes(), b"cached")


if __name__ == "__main__":
    unittest.main()
