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
from typing import List, Tuple

from testcase import UeiaTestCase

import engine
import toolrun
from shapes import UeiaError, UsageError


def _tree(root: Path, version: bool = True, tools: bool = True, complete: bool = False,
          numbers: Tuple[int, ...] = (5, 8, 3)) -> Path:
    """A fixture engine tree: the directories, a Build.version, and some executable names.

    `complete` writes every executable `engine.CSVTOOLS_EXES` names (with `version` and
    `Engine\\Source`, that is the whole inventory); the default writes two, which is a *partial*
    tree -- exactly the shape the search has to rank below a complete one.
    """
    binaries = root / "Engine" / "Binaries" / "DotNET" / "CsvTools"
    binaries.mkdir(parents=True, exist_ok=True)
    (root / "Engine" / "Source").mkdir(parents=True, exist_ok=True)
    if version:
        (root / "Engine" / "Build").mkdir(parents=True, exist_ok=True)
        padded = tuple(numbers) + (0,) * (3 - len(numbers))
        (root / "Engine" / "Build" / "Build.version").write_text(json.dumps({
            "MajorVersion": padded[0], "MinorVersion": padded[1], "PatchVersion": padded[2],
            "Changelist": 12345678,
        }), encoding="utf-8")
    if tools:
        names = engine.CSVTOOLS_EXES[:2] if not complete else engine.CSVTOOLS_EXES
        for name in names:
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
        """A wrong flag stays a mistake: the search is tried first, but finding nothing is fatal."""
        plain = self.dir / "not-an-engine"
        plain.mkdir()
        with self.assertRaises(UsageError) as caught:
            engine.resolve(str(plain), env={}, find=lambda: [])
        self.assertIn("is not an engine tree", str(caught.exception))
        self.assertIn("--engine-dir", str(caught.exception))
        self.assertIn("none was found on this machine", str(caught.exception))

    def test_the_variable_is_the_fallback_and_a_bad_one_is_reported_not_obeyed(self) -> None:
        root = _tree(self.dir / "engine")
        found = engine.resolve(None, env={engine.ENV_ENGINE_DIR: str(root)})
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.root, root.resolve())

        notes = []
        bad = self.dir / "elsewhere"
        bad.mkdir()
        self.assertIsNone(engine.resolve(None, env={engine.ENV_ENGINE_DIR: str(bad)}, notes=notes,
                                         find=lambda: []))
        self.assertEqual(len(notes), 1)
        self.assertIn("is not an engine tree: ignored", notes[0])

    def test_nothing_named_is_legal_and_the_hint_says_what_to_pass(self) -> None:
        self.assertIsNone(engine.resolve(None, env={}, find=lambda: []))
        self.assertIn("--engine-dir", engine.hint())
        self.assertIn("UEI_ENGINE_DIR", engine.hint())
        self.assertIn("searched automatically", engine.hint())

    def test_the_flag_beats_a_valid_variable(self) -> None:
        first = _tree(self.dir / "one")
        second = _tree(self.dir / "two")
        found = engine.resolve(str(first), env={engine.ENV_ENGINE_DIR: str(second)})
        assert found is not None
        self.assertEqual(found.root, first.resolve())


class TestTheSearch(UeiaTestCase):
    """Finding a tree when nobody named one: what is chosen, in what order, and what is said.

    The fixture trees are built in the scratch directory and handed to `discover` directly (roots,
    manifest folder and registry reader are all injectable), so nothing here depends on, or touches,
    what is installed on the machine running the suite.
    """

    def _search(self, *roots: Path) -> List[engine.EngineDir]:
        return engine.discover(roots=roots, manifest_dir=self.dir / "no-manifests",
                               registry=lambda: [])

    def test_a_valid_flag_means_no_search_at_all(self) -> None:
        """An explicit instruction is never second-guessed: the searcher must not even be called."""
        called: List[str] = []

        def find() -> List[engine.EngineDir]:
            called.append("searched")
            return []

        found = engine.resolve(str(_tree(self.dir / "engine")), env={}, find=find)
        self.assertIsNotNone(found)
        self.assertEqual(called, [], "a valid --engine-dir is the end of the question")

    def test_an_invalid_flag_is_reported_and_searched_past(self) -> None:
        """A typo is said out loud, and the machine's own tree is used rather than refusing work."""
        good = _tree(self.dir / "installed" / "UE_5.8", complete=True, numbers=(5, 8, 3))
        notes: List[str] = []
        found = engine.resolve(str(self.dir / "typo"), env={}, notes=notes,
                               find=lambda: self._search(good))
        assert found is not None
        self.assertEqual(found.root, good.resolve())
        self.assertTrue(any("is not an engine tree" in note and "searched this machine" in note
                            for note in notes), notes)
        self.assertTrue(any("UE_5.8" in note and "1 engine tree(s)" in note for note in notes), notes)

    def test_the_scan_can_be_switched_off(self) -> None:
        """`UEI_NO_ENGINE_SCAN` is what keeps the hermetic suite off the real machine."""
        def find() -> List[engine.EngineDir]:
            raise AssertionError("the search should not have run")

        self.assertIsNone(engine.resolve(None, env={engine.ENV_NO_ENGINE_SCAN: "1"}, find=find))
        with self.assertRaises(UsageError):
            engine.resolve("nowhere", env={engine.ENV_NO_ENGINE_SCAN: "1"}, find=find)

    def test_the_newest_complete_tree_wins(self) -> None:
        old = _tree(self.dir / "UE_5.6", complete=True, numbers=(5, 6, 4))
        new = _tree(self.dir / "UE_5.8", complete=True, numbers=(5, 8, 3))
        found = self._search(old, new)
        self.assertEqual([entry.root for entry in found], [new.resolve(), old.resolve()])
        notes: List[str] = []
        winner = engine.resolve(None, env={}, notes=notes, find=lambda: found)
        assert winner is not None
        self.assertEqual(winner.root, new.resolve())
        self.assertEqual([note for note in notes if note.startswith("scanned")],
                         ["scanned  : %s (%s) -- 2 engine tree(s) on this machine"
                          % (new.resolve(), winner.probe_line())])

    def test_versions_are_compared_as_numbers_not_text(self) -> None:
        """`UE_5.10` is newer than `UE_5.9`; a string comparison says the opposite."""
        nine = _tree(self.dir / "UE_5.9", version=False, complete=True)
        ten = _tree(self.dir / "UE_5.10", version=False, complete=True)
        self.assertEqual(
            [entry.root for entry in self._search(nine, ten)][0], ten.resolve(),
            "the version came from the directory name, and 10 > 9",
        )

    def test_a_complete_tree_beats_a_newer_incomplete_one(self) -> None:
        """The question is which tree gives *us* the most: 5.8 with no CsvTools cannot run `csv`."""
        thin = _tree(self.dir / "UE_5.8", tools=False, complete=False)
        whole = _tree(self.dir / "UE_5.6", complete=True, numbers=(5, 6, 4))
        found = self._search(thin, whole)
        self.assertEqual(found[0].root, whole.resolve())
        self.assertFalse(found[1].is_complete())

    def test_a_single_incomplete_tree_is_used_and_its_gaps_are_named(self) -> None:
        """Nothing better exists: use it, and say what it lacks rather than implying it is whole."""
        thin = _tree(self.dir / "UE_5.8", tools=False)
        notes: List[str] = []
        winner = engine.resolve(None, env={}, notes=notes, find=lambda: self._search(thin))
        assert winner is not None
        self.assertEqual(winner.root, thin.resolve())
        gaps = [note for note in notes if "it lacks" in note]
        self.assertEqual(len(gaps), 1, notes)
        self.assertIn("CsvTools", gaps[0])

    def test_discover_reads_the_launcher_manifests(self) -> None:
        """The launcher's own list is read, not guessed: `.item` JSON, and a broken one is skipped."""
        installed = _tree(self.dir / "installed" / "UE_5.8", complete=True)
        manifests = self.dir / "Manifests"
        manifests.mkdir()
        (manifests / "ue.item").write_text(json.dumps({
            "AppName": "UE_5.8", "InstallLocation": str(installed),
        }), encoding="utf-8")
        (manifests / "broken.item").write_text("{not json", encoding="utf-8")
        (manifests / "empty.item").write_text(json.dumps({"AppName": "UE_5.7"}), encoding="utf-8")
        found = engine.discover(roots=[], manifest_dir=manifests, registry=lambda: [])
        self.assertEqual([entry.root for entry in found], [installed.resolve()])

    def test_discover_reads_the_source_build_list(self) -> None:
        """A source build registers itself under `HKCU\\...\\Unreal Engine\\Builds`; read it too."""
        built = _tree(self.dir / "src" / "UnrealEngine", complete=True, numbers=(5, 8, 0))
        found = engine.discover(roots=[], manifest_dir=self.dir / "none",
                                registry=lambda: [built, self.dir / "gone", built])
        self.assertEqual([entry.root for entry in found], [built.resolve()],
                         "the same tree twice, and a path that is not one, give one entry")

    def test_the_search_looks_one_level_down_and_not_deeper(self) -> None:
        """Bounded on purpose: a tree in an unusual place is what `--engine-dir` is for."""
        shallow = _tree(self.dir / "Epic Games" / "UE_5.8", complete=True)
        deep = _tree(self.dir / "games" / "engines" / "UE_5.9", complete=True)
        roots = engine.search_roots(drives=[str(self.dir)])
        self.assertIn(shallow.resolve(), [path.resolve() for path in roots])
        self.assertNotIn(deep.resolve(), [path.resolve() for path in roots])

    def test_every_executable_the_csv_command_runs_is_in_the_inventory(self) -> None:
        """One list, checked: a subcommand that runs an exe the search never looks for would be a gap."""
        import commands

        missing = sorted(set(commands._CSV_EXES.values()) - set(engine.CSVTOOLS_EXES))
        self.assertEqual(missing, [], "the inventory and the subcommand table have to agree")


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
