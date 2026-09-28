"""The `--engine-dir` contract, and the one place an engine executable is run.

Two halves of the same promise: a directory is validated once (`engine.resolve`) and every call to
something inside it is recorded (`toolrun.run_tool`). The subprocess tests use `sys.executable` as
the "tool" -- a real process, real exit codes, real streams -- so the wrapper is exercised for real
without needing the engine's executables on the machine.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from testcase import UeiaTestCase

import engine
import toolrun
from shapes import UeiaError, UsageError


def _tree(root: Path, version: bool = True, tools: bool = True) -> Path:
    """A fixture engine tree: the directories, a Build.version, and some executable names."""
    binaries = root / "Engine" / "Binaries" / "DotNET" / "CsvTools"
    binaries.mkdir(parents=True, exist_ok=True)
    (root / "Engine" / "Source").mkdir(parents=True, exist_ok=True)
    if version:
        (root / "Engine" / "Build").mkdir(parents=True, exist_ok=True)
        (root / "Engine" / "Build" / "Build.version").write_text(json.dumps({
            "MajorVersion": 5, "MinorVersion": 8, "PatchVersion": 3, "Changelist": 12345678,
        }), encoding="utf-8")
    if tools:
        for name in ("csvinfo.exe", "CSVToSVG.exe"):
            (binaries / name).write_bytes(b"MZ fake")
    return root


class TestResolve(UeiaTestCase):
    """Where the engine tree comes from, and what a bad answer to that question does."""

    def test_the_flag_wins_and_is_validated(self) -> None:
        root = _tree(self.dir / "engine")
        found = engine.resolve(str(root), env={})
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.root, root.resolve())

    def test_a_flag_that_is_not_an_engine_tree_is_a_usage_error(self) -> None:
        plain = self.dir / "not-an-engine"
        plain.mkdir()
        with self.assertRaises(UsageError) as caught:
            engine.resolve(str(plain), env={})
        self.assertIn("is not an engine tree", str(caught.exception))
        self.assertIn("--engine-dir", str(caught.exception))

    def test_the_variable_is_the_fallback_and_a_bad_one_is_reported_not_obeyed(self) -> None:
        root = _tree(self.dir / "engine")
        found = engine.resolve(None, env={engine.ENV_ENGINE_DIR: str(root)})
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.root, root.resolve())

        notes = []
        bad = self.dir / "elsewhere"
        bad.mkdir()
        self.assertIsNone(engine.resolve(None, env={engine.ENV_ENGINE_DIR: str(bad)}, notes=notes))
        self.assertEqual(len(notes), 1)
        self.assertIn("is not an engine tree: ignored", notes[0])

    def test_nothing_named_is_legal_and_the_hint_says_what_to_pass(self) -> None:
        self.assertIsNone(engine.resolve(None, env={}))
        self.assertIn("--engine-dir", engine.hint())
        self.assertIn("UEI_ENGINE_DIR", engine.hint())

    def test_the_flag_beats_a_valid_variable(self) -> None:
        first = _tree(self.dir / "one")
        second = _tree(self.dir / "two")
        found = engine.resolve(str(first), env={engine.ENV_ENGINE_DIR: str(second)})
        assert found is not None
        self.assertEqual(found.root, first.resolve())


class TestTheTree(UeiaTestCase):
    """What an engine tree answers about itself: tools, version, source, and an honest description."""

    def test_tools_are_found_by_name_and_a_missing_one_is_none(self) -> None:
        root = _tree(self.dir / "engine")
        found = engine.resolve(str(root), env={})
        assert found is not None
        self.assertTrue(found.csvtools("csvinfo.exe") is not None)
        self.assertIsNone(found.csvtools("NoSuchTool.exe"))

    def test_the_version_string_comes_from_build_version(self) -> None:
        root = _tree(self.dir / "engine")
        found = engine.resolve(str(root), env={})
        assert found is not None
        self.assertEqual(found.version_string(), "5.8.3 (CL 12345678)")
        document = found.version()
        assert document is not None
        self.assertEqual(document["MajorVersion"], 5)

    def test_a_tree_without_build_version_says_nothing_rather_than_guessing(self) -> None:
        root = _tree(self.dir / "engine", version=False)
        found = engine.resolve(str(root), env={})
        assert found is not None
        self.assertIsNone(found.version())
        self.assertIsNone(found.version_string())

    def test_the_description_names_the_tools_and_the_missing_source(self) -> None:
        root = _tree(self.dir / "engine")
        (root / "Engine" / "Source").rmdir()
        found = engine.resolve(str(root), env={})
        assert found is not None
        lines = "\n".join(found.describe())
        self.assertIn("version  : 5.8.3", lines)
        self.assertIn("csvtools : 2 executable(s)", lines)
        self.assertIn("source   : none", lines)


class TestRunTool(UeiaTestCase):
    """The wrapper: a real process, its streams, its exit code, and the call's identity."""

    def test_a_successful_call_records_the_argv_streams_and_identity(self) -> None:
        result = toolrun.run_tool(
            Path(sys.executable), ["-c", "print('answer')"], cwd=self.dir, timeout=60,
        )
        self.assertEqual(result.exit, 0)
        self.assertEqual(result.stdout.strip(), "answer")
        self.assertEqual(result.argv, [sys.executable, "-c", "print('answer')"])
        self.assertEqual(result.cwd, str(self.dir))
        self.assertGreater(result.size, 0)
        self.assertEqual(len(result.sha256), 64)
        self.assertGreaterEqual(result.seconds, 0.0)
        described = "\n".join(result.describe())
        self.assertIn("sha256", described)
        self.assertIn("exit     : 0", described)

    def test_a_non_zero_exit_is_data_not_an_error(self) -> None:
        result = toolrun.run_tool(
            Path(sys.executable), ["-c", "import sys; sys.stderr.write('nope'); sys.exit(3)"],
            cwd=self.dir, timeout=60,
        )
        self.assertEqual(result.exit, 3)
        self.assertEqual(result.stderr.strip(), "nope")

    def test_a_timeout_is_an_error_naming_the_budget(self) -> None:
        with self.assertRaises(UeiaError) as caught:
            toolrun.run_tool(
                Path(sys.executable), ["-c", "import time; time.sleep(30)"], cwd=self.dir,
                timeout=0.5,
            )
        message = str(caught.exception)
        self.assertIn("did not finish within", message)
        self.assertIn("0 s", message)

    def test_a_missing_executable_is_an_error(self) -> None:
        with self.assertRaises(UeiaError) as caught:
            toolrun.run_tool(self.dir / "absent.exe", [], cwd=self.dir)
        self.assertIn("no such tool", str(caught.exception))

    def test_the_identity_is_the_same_object_for_the_same_file(self) -> None:
        first = toolrun.identity(Path(sys.executable))
        second = toolrun.identity(Path(sys.executable))
        self.assertIs(first, second, "hashing an executable twice must not happen twice")
        self.assertGreater(first["size"], 0)

    def test_the_json_form_is_what_a_report_quotes(self) -> None:
        result = toolrun.run_tool(Path(sys.executable), ["-c", "pass"], cwd=self.dir, timeout=60)
        document = result.to_json()
        self.assertEqual(document["exit"], 0)
        self.assertEqual(document["argv"][0], sys.executable)
        self.assertIn("sha256", document)


if __name__ == "__main__":
    unittest.main()
