"""Source mapping: the path rules, the anti-pattern names, the weights, and the corpus.

The hermetic half is `mapped_trace`, one 101 ms frame holding four scopes with real-world paths (two
engine files, one of them in a module that owns two of them, and one project plugin), so every share
the report prints is hand-checkable at 1 cycle = 1 microsecond. The corpus half asks the registered
captures and skips, loudly, when this machine has none of them.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, cast

from testcase import UeiaTestCase

import engine
import goldens
import shapes
import sources
import summary
from fixtures import (
    EVENT_FLAG_NOSYNC,
    build_trace,
    important_record,
    mapped_trace,
    new_event_record,
    pack,
)

BUDGET = summary.parse_budget("60", None)

ENGINE_FILE = ("D:\\Build\\UnrealEngine\\Engine\\Source\\Runtime\\Core\\Public\\Async\\"
               "ParallelFor.h")
PLUGIN_FILE = ("D:\\Build\\UnrealEngine\\Engine\\Plugins\\Experimental\\PythonScriptPlugin\\"
               "Source\\PythonScriptPlugin\\Private\\PythonScriptPlugin.cpp")
GAME_FILE = ("D:\\TikiStarMain_TMR\\TikiStar\\Plugins\\MinViablePluginSet\\Wwise\\Source\\"
             "WwiseConcurrency\\Private\\Wwise\\WwiseExecutionQueue.cpp")
PROJECT_FILE = "D:\\SomeGame\\Source\\SomeGame\\Private\\SomeGameModule.cpp"


class TestParseLocation(UeiaTestCase):
    """The path rules: who owns a file, and which module it belongs to, from the text alone."""

    def test_an_engine_source_file(self) -> None:
        location = sources.parse_location(ENGINE_FILE, 316)
        assert location is not None
        self.assertEqual(location.side, "engine")
        self.assertEqual(location.kind, "engine-source")
        self.assertEqual(location.group, "Runtime")
        self.assertEqual(location.module, "Core")
        self.assertEqual(location.label(), "Runtime/Core")
        self.assertEqual(location.relative, "Source/Runtime/Core/Public/Async/ParallelFor.h")
        self.assertEqual(location.where(), location.relative + ":316")

    def test_an_engine_plugin_file(self) -> None:
        location = sources.parse_location(PLUGIN_FILE, 1415)
        assert location is not None
        self.assertEqual((location.side, location.kind), ("engine", "engine-plugin"))
        self.assertEqual(location.group, "Experimental")
        self.assertEqual(location.module, "PythonScriptPlugin")
        self.assertEqual(location.label(), "Experimental/PythonScriptPlugin")

    def test_a_project_plugin_file(self) -> None:
        location = sources.parse_location(GAME_FILE, 473)
        assert location is not None
        self.assertEqual((location.side, location.kind), ("game", "project-plugin"))
        self.assertEqual(location.module, "WwiseConcurrency")
        self.assertEqual(location.group, "MinViablePluginSet/Wwise")
        self.assertEqual(location.relative,
                         "Plugins/MinViablePluginSet/Wwise/Source/WwiseConcurrency/Private/Wwise/"
                         "WwiseExecutionQueue.cpp")

    def test_a_project_file(self) -> None:
        location = sources.parse_location(PROJECT_FILE, 12)
        assert location is not None
        self.assertEqual((location.side, location.kind), ("game", "project"))
        self.assertEqual(location.module, "SomeGame")
        self.assertEqual(location.group, "")

    def test_a_bare_file_name_is_unknown_rather_than_guessed(self) -> None:
        """The corpus's own shape for a spec the recorder could not resolve: `Game.cpp`, no path."""
        location = sources.parse_location("Game.cpp", 91)
        assert location is not None
        self.assertEqual((location.side, location.kind), ("unknown", "unknown"))
        self.assertEqual(location.module, "")
        self.assertEqual(location.label(), "(unknown)")
        self.assertEqual(location.where(), "Game.cpp:91")

    def test_a_path_with_no_source_segment_is_unknown(self) -> None:
        location = sources.parse_location("D:\\Data\\Build\\Generated\\Things.h", 3)
        assert location is not None
        self.assertEqual(location.kind, "unknown")

    def test_an_engine_segment_not_followed_by_source_is_not_a_root(self) -> None:
        """`Engine\\Docs\\...` is not engine *source*: the root marker is the pair, not the word."""
        location = sources.parse_location("D:\\UE\\Engine\\Docs\\Build.md", 0)
        assert location is not None
        self.assertEqual(location.kind, "unknown")

    def test_the_root_is_the_first_pair_not_the_last(self) -> None:
        """A module called `Engine` inside the engine tree must not be mistaken for the root."""
        location = sources.parse_location(
            "D:\\UE\\Engine\\Source\\Runtime\\Engine\\Private\\Engine.cpp", 5)
        assert location is not None
        self.assertEqual(location.relative, "Source/Runtime/Engine/Private/Engine.cpp")
        self.assertEqual((location.group, location.module), ("Runtime", "Engine"))

    def test_separators_and_case_do_not_matter(self) -> None:
        forward = sources.parse_location(ENGINE_FILE.replace("\\", "/"), 1)
        assert forward is not None
        self.assertEqual(forward.kind, "engine-source")
        self.assertEqual(forward.module, "Core", "the module keeps the capture's own spelling")
        lowered = sources.parse_location(ENGINE_FILE.replace("\\", "/").lower(), 1)
        assert lowered is not None
        self.assertEqual((lowered.kind, lowered.module), ("engine-source", "core"),
                         "a path is matched case-insensitively and reported as it was written")

    def test_an_empty_path_is_none_and_a_line_of_zero_is_left_off(self) -> None:
        self.assertIsNone(sources.parse_location("", 0))
        self.assertIsNone(sources.parse_location("\\\\", 0))
        location = sources.parse_location(ENGINE_FILE, 0)
        assert location is not None
        self.assertNotIn(":0", location.where())


class TestPatterns(UeiaTestCase):
    """The anti-pattern names: loose on purpose, and honest about being a name rule."""

    def test_the_engine_names_it_is_built_for(self) -> None:
        self.assertTrue(sources.matches("UWorld::Tick", ("tick",)))
        self.assertTrue(sources.matches("FTickTaskLevel::Tick", ("tick",)))
        self.assertTrue(sources.matches("StaticLoadObjectInternal",
                                        ("staticloadobject", "loadobject")))
        self.assertTrue(sources.matches("FlushAsyncLoading", ("flushasyncloading",)))
        self.assertTrue(sources.matches("SpawnActor", ("spawnactor",)))
        self.assertTrue(sources.matches("CollectGarbageFull", ("collectgarbage",)))
        self.assertTrue(sources.matches("WaitForTasks", ("waitfortasks", "waitfor")))

    def test_names_that_are_not_patterns(self) -> None:
        self.assertFalse(sources.matches("RenderingFrame", ("tick", "spawnactor")))
        self.assertFalse(sources.matches("FSceneRenderer::Render", ("tick",)))
        self.assertFalse(sources.matches("", ("tick",)))
        self.assertTrue(sources.matches("TickingClock::Read", ("tick",)),
                        "loose is the point: a name carrying `tick` anywhere does match, and the "
                        "report calls the match a heuristic")

    def test_every_pattern_has_words_and_a_label(self) -> None:
        for key, label, words in sources.PATTERNS:
            self.assertTrue(words, "%s has no words" % (key,))
            self.assertTrue(label, "%s has no label" % (key,))
            self.assertTrue(all(word == word.lower() for word in words),
                            "%s's words are matched against a lower-cased name" % (key,))


class TestTheFixtureMeasures(UeiaTestCase):
    """`build` on `mapped_trace`: every share hand-checkable at 1 cycle = 1 microsecond."""

    def _model(self) -> Dict[str, Any]:
        import commands

        path = self.write_capture(mapped_trace())
        _view, model, _cached = commands.load_model(str(path))
        return cast(Dict[str, Any], model)

    def _report(self, engine_dir: "engine.EngineDir | None" = None) -> sources.Report:
        report, reasons = sources.build(self._model(), BUDGET, engine_dir)
        self.assertEqual(reasons, [])
        assert report is not None
        return report

    def test_the_specs_are_counted_and_split(self) -> None:
        report = self._report()
        self.assertEqual(report.specs, 5, "four located specs and one with no file at all")
        self.assertEqual(report.located, 4)
        self.assertEqual(report.side_counts, {"engine": 3, "game": 1, "unknown": 0})
        self.assertEqual(report.kinds, {"engine-source": 3, "project-plugin": 1})

    def test_the_kept_frame_and_its_budget_verdict(self) -> None:
        report = self._report()
        self.assertEqual(report.frames, 1)
        self.assertEqual(report.over_budget, 1, "101 ms misses 60 FPS")
        self.assertEqual(report.owned_total, 101000, "the frame's own span")
        self.assertAlmostEqual(report.located_share(), 28000 / 101000.0, places=9)

    def test_the_files_carry_the_biggest_timer_they_hold(self) -> None:
        report = self._report()
        rows = {row.location.basename: row for row in report.files}
        self.assertEqual(sorted(rows), ["TimerManager.cpp", "UObjectGlobals.cpp", "Waits.cpp",
                                        "World.cpp"])
        world = rows["World.cpp"]
        self.assertEqual(world.specs, 1)
        self.assertEqual(world.owned_cycles, 28000)
        self.assertEqual(world.biggest, (8, "UWorld::Tick"))
        self.assertEqual(world.location.where(),
                         "Source/Runtime/Engine/Private/World.cpp:100")
        self.assertEqual(world.over_frames, 1)

    def test_a_module_holding_two_of_them_counts_the_frame_once(self) -> None:
        """The nesting rule: `Runtime/Engine` holds 28 ms and 10 ms *in the same frame*, and the
        report says 28 -- the sum would be a number no frame ever contained."""
        report = self._report()
        modules = {label: (side, specs, cycles, frames)
                   for label, side, specs, cycles, frames in report.modules}
        self.assertEqual(modules["Runtime/Engine"][1:], (2, 28000, 1))
        self.assertEqual(modules["Runtime/Engine"][0], "engine")
        self.assertEqual(modules["Perf/PerfCore"][1:], (1, 20000, 1))
        self.assertEqual(modules["Perf/PerfCore"][0], "game")
        self.assertEqual(sorted(modules), ["Perf/PerfCore", "Runtime/CoreUObject", "Runtime/Engine"])

    def test_the_patterns_need_more_than_one_spec_and_take_the_biggest_per_frame(self) -> None:
        report = self._report()
        findings = {finding.key: finding for finding in report.findings}
        self.assertEqual(sorted(findings), ["tick"], "one sync-load and one wait is not a pattern")
        tick = findings["tick"]
        self.assertEqual(tick.specs, 2)
        self.assertEqual(tick.owned_cycles, 28000, "the biggest of the two in the frame, not both")
        self.assertEqual(tick.over_frames, 1)
        assert tick.biggest is not None
        self.assertEqual(tick.biggest[1], "UWorld::Tick")
        self.assertEqual(tick.biggest[2].where(), "Source/Runtime/Engine/Private/World.cpp:100")

    def test_nothing_to_map_is_a_reason_rather_than_a_table(self) -> None:
        model = {"timers": [], "frame_work": [], "session": {"cycle_frequency": 1000000}}
        report, reasons = sources.build(model, BUDGET)
        self.assertIsNone(report)
        self.assertIn("no CpuProfiler timer specs", reasons[0])

        model = {"timers": [{"id": 1, "name": "Tick", "file": "", "line": 0}], "frame_work": []}
        report, reasons = sources.build(model, BUDGET)
        self.assertIsNone(report)
        self.assertIn("no timer spec in this capture carries a file:line", reasons[0])

    def test_without_frames_the_specs_are_mapped_with_no_weight(self) -> None:
        model = {"timers": [{"id": 1, "name": "Tick", "file": ENGINE_FILE, "line": 1}],
                 "frame_work": [], "session": {"cycle_frequency": 1000000}}
        report, _reasons = sources.build(model, BUDGET)
        assert report is not None
        self.assertEqual(report.frames, 0)
        self.assertEqual(report.files[0].owned_cycles, 0)
        self.assertEqual(report.located_share(), 0.0)

    def test_the_engine_tree_is_checked_and_never_required(self) -> None:
        """The mapping needs no tree; a tree only answers "is this capture from this revision?"."""
        report = self._report()
        self.assertIsNone(report.engine_dir)
        self.assertEqual((report.checked, report.found), (0, 0))

        tree = self.dir / "tree"
        (tree / "Engine" / "Source" / "Runtime" / "Engine" / "Private").mkdir(parents=True)
        (tree / "Engine" / "Source" / "Runtime" / "Engine" / "Private" / "World.cpp").write_text("")
        checked = self._report(engine.EngineDir(tree))
        self.assertEqual(checked.checked, 3, "every distinct engine path is looked up once")
        self.assertEqual(checked.found, 1, "and only the one this tree actually has")

    def test_a_bare_file_name_is_counted_as_unknown_not_as_the_project(self) -> None:
        """`work_trace`'s specs carry `Game.cpp` and `TaskGraph.cpp` with no path at all."""
        import commands

        path = self.write_capture(_bare_names())
        _view, model, _cached = commands.load_model(str(path))
        report, _reasons = sources.build(cast(Dict[str, Any], model), BUDGET)
        assert report is not None
        self.assertEqual(report.side_counts["unknown"], report.located)
        self.assertEqual(report.side_counts["game"], 0)
        self.assertEqual(report.side_counts["engine"], 0)


def _rows(out: str) -> List[str]:
    """The table's own rows: the prose above it names timers and files too, so a substring check
    over the whole output cannot say what the table holds."""
    lines = out.splitlines()
    start = [index for index, line in enumerate(lines) if line.startswith("file:line")]
    if not start:
        return []
    body = lines[start[0] + 2:]
    return [line.split("  ")[0] for line in body if line.strip()]


def _bare_names() -> bytes:
    """A capture whose specs carry a file name and no path -- what a stripped build records."""
    from fixtures import work_importants, work_schema, work_stream

    return build_trace(
        events_stream=work_schema(), importants_stream=work_importants(),
        threads={2: work_stream()},
    )


class TestTheCommand(UeiaTestCase):
    """`ueia sources`: the table, the prose, the tree flag, and what it refuses."""

    def _capture(self, data: "bytes | None" = None) -> str:
        return str(self.write_capture(data if data is not None else mapped_trace()))

    def test_the_table_lists_the_files_by_what_they_held(self) -> None:
        code, out, _err = self.run_cli(["sources", self._capture(), "--jobs", "1"])
        self.assertEqual(code, 0)
        self.assertIn("sources   : 5 timer spec(s), 4 with a file:line (80.0%); of those 3 engine, "
                      "1 game, 0 unknown", out)
        self.assertIn("shapes    : engine-source 3, project-plugin 1", out)
        self.assertIn("file:line", out)
        self.assertIn("Source/Runtime/Engine/Private/World.cpp:100", out)
        self.assertIn("engine  Engine", out)
        self.assertIn("27.7%", out, "28 ms of the 101 ms the frames span")
        self.assertIn("tick         per-frame work in Tick: 2 spec(s), 27.7% of the kept frames, "
                      "1 over budget", out)
        self.assertIn("UWorld::Tick", out)
        self.assertIn("the *recording* machine's", out, "no tree given: said out loud, not hidden")

    def test_the_csv_form_is_the_same_table(self) -> None:
        code, out, _err = self.run_cli(["sources", self._capture(), "--jobs", "1",
                                        "--format", "csv"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0],
                         "file:line,side,module,specs,share,over,top timer")
        self.assertIn("Source/Runtime/Engine/Private/World.cpp:100,engine,Engine,1,27.7%,1,"
                      "UWorld::Tick", out)

    def test_limit_and_filter_narrow_the_table(self) -> None:
        code, out, _err = self.run_cli(["sources", self._capture(), "--jobs", "1", "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(_rows(out), ["Source/Runtime/Engine/Private/World.cpp:100"])
        code, out, _err = self.run_cli(["sources", self._capture(), "--jobs", "1",
                                        "--filter", "CoreUObject"])
        self.assertEqual(code, 0)
        self.assertEqual(_rows(out),
                         ["Source/Runtime/CoreUObject/Private/UObject/UObjectGlobals.cpp:200"])

    def test_a_filter_that_matches_nothing_says_so(self) -> None:
        code, out, _err = self.run_cli(["sources", self._capture(), "--jobs", "1",
                                        "--filter", "NothingHere"])
        self.assertEqual(code, 0)
        self.assertIn("note      : no file matched --filter NothingHere", out)

    def test_the_engine_tree_is_reported_when_it_is_given(self) -> None:
        tree = self.dir / "tree"
        (tree / "Engine" / "Source" / "Runtime" / "Engine" / "Private").mkdir(parents=True)
        (tree / "Engine" / "Source" / "Runtime" / "Engine" / "Private" / "World.cpp").write_text("")
        code, out, _err = self.run_cli(["sources", self._capture(), "--jobs", "1",
                                        "--engine-dir", str(tree)])
        self.assertEqual(code, 0)
        # the scratch directory's own path is printed as the tree resolved it (a short name on
        # Windows), so the assertion is about the check and not about the spelling of the path
        self.assertIn("tree      : ", out)
        self.assertIn("1 of 3 engine path(s) exist in it", out)

    def test_a_directory_that_is_not_an_engine_tree_is_a_usage_error(self) -> None:
        code, _out, err = self.run_cli(["sources", self._capture(), "--engine-dir",
                                        str(self.dir)])
        self.assertEqual(code, 2)
        self.assertIn("is not an engine tree", err)

    def test_a_capture_with_no_specs_exits_two(self) -> None:
        code, out, _err = self.run_cli(["sources", str(self.write_capture(_no_specs())),
                                        "--jobs", "1"])
        self.assertEqual(code, 2)
        self.assertIn("cannot    :", out)
        self.assertIn("hint      :", out)

    def test_an_option_it_does_not_take_is_a_usage_error(self) -> None:
        code, _out, err = self.run_cli(["sources", self._capture(), "--graph", "dot"])
        self.assertEqual(code, 2)
        self.assertIn("sources takes --engine-dir, --filter, --limit", err)


def _no_specs() -> bytes:
    """A capture with frames and no `CpuProfiler.EventSpec`: nothing to map."""
    schema = (
        new_event_record(16, "$Trace", "NewTrace", [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                         flags=EVENT_FLAG_NOSYNC)
        + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
    )
    stream = (
        _frame(22, 1000000, 1) + _frame(23, 1020000, 2)
    )
    return build_trace(
        events_stream=schema,
        importants_stream=important_record(16, pack("u64", 1000000) + pack("u64", 1000000)),
        threads={2: stream},
    )


def _frame(uid: int, cycle: int, serial: int) -> bytes:
    from fixtures import event

    return event(uid, pack("u64", cycle) + pack("u8", 0), serial=serial)


class TestTheRealCaptures(UeiaTestCase):
    """The registered captures: what only a real `.utrace` says about where its timers live."""
    #: This class reads a registered capture: the cache beside it is content-keyed.
    use_cache = True


    def _tree(self) -> "engine.EngineDir | None":
        """The engine tree this machine names, if it names one: `$UEI_ENGINE_DIR`, never a literal.

        The path of an engine tree is a property of the machine, not of this repository, and the
        variable `engine.py` already defines is where a machine says where its tree is. A test that
        hard-coded one would fail on every other checkout (and commit a machine path).
        """
        named = os.environ.get(shapes.ENV_ENGINE_DIR, "")
        if named and engine.is_engine_dir(Path(named)):
            return engine.EngineDir(Path(named))
        return None

    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = goldens.capture_paths()
        cls._models: Dict[str, Dict[str, Any]] = {}

    def _path(self, key: str) -> str:
        path = self.paths.get(key)
        if path is None or not path.is_file():
            self.skipTest("the %s capture is not on this machine" % (key,))
        return str(path)

    def _model(self, key: str) -> Dict[str, Any]:
        model = self._models.get(key)
        if model is None:
            import commands

            _view, loaded, _cached = commands.load_model(self._path(key))
            model = cast(Dict[str, Any], loaded)
            self._models[key] = model
        return model

    def _report(self, key: str, tree: "engine.EngineDir | None" = None) -> sources.Report:
        report, reasons = sources.build(self._model(key), BUDGET, tree)
        self.assertEqual(reasons, [], "%s: %s" % (key, reasons))
        assert report is not None
        return report

    def test_the_editor_session_maps_its_engine_code(self) -> None:
        """The corpus: 4,879 of 27,760 specs carry a location, and two thirds of them are engine."""
        report = self._report("editor-pie-1")
        self.assertEqual(report.specs, 27760)
        self.assertEqual(report.located, 4879)
        self.assertEqual(report.side_counts, {"engine": 3605, "game": 1274, "unknown": 0})
        self.assertEqual(report.kinds, {"engine-source": 2052, "engine-plugin": 1553,
                                        "project-plugin": 1274})
        self.assertEqual(len(report.files), 362)
        self.assertEqual(report.files[0].location.basename, "LaunchEngineLoop.cpp")
        biggest = report.files[0].biggest
        assert biggest is not None
        self.assertIn("RenderingFrame", biggest[1])

    def test_the_editor_session_names_its_synchronous_loads(self) -> None:
        report = self._report("editor-pie-1")
        findings = {finding.key: finding for finding in report.findings}
        self.assertIn("sync-load", findings)
        self.assertIn("wait", findings)
        self.assertIn("tick", findings)
        assert findings["sync-load"].biggest is not None
        self.assertEqual(findings["sync-load"].biggest[1], "StaticLoadObjectInternal")
        self.assertTrue(findings["tick"].specs > 100, "the engine's own Tick timers are many")
        for finding in report.findings:
            self.assertGreaterEqual(finding.specs, sources.PATTERN_MIN_SPECS)
            self.assertLessEqual(finding.owned_cycles, report.owned_total,
                                 "a share of the frames, never more than the frames")

    def test_the_located_specs_hold_almost_all_of_the_kept_frames(self) -> None:
        report = self._report("editor-pie-1")
        self.assertGreater(report.located_share(), 0.9)
        self.assertEqual(report.over_budget, report.frames, "every kept frame is a long one")

    def test_the_game_capture_is_all_engine_code_and_parks_in_WaitForTask(self) -> None:
        """A shipped build's timers come from engine code -- and its frames are waiting.

        The corpus's game capture has 1,352 located specs and **not one** of them is project source,
        which is what a packaged build looks like: the game's own scopes are the engine's (the
        sample's `TRACE_CPUPROFILER_EVENT_SCOPE`s live in engine headers). What the mapping then
        shows is the finding the bottleneck report reached from the other side: 83% of the kept
        frames are inside `WaitForTask`-style scopes, with 31 of 32 over budget.
        """
        report = self._report("game-pc-2")
        self.assertEqual(report.located, 1352)
        self.assertEqual(report.side_counts, {"engine": 1352, "game": 0, "unknown": 0})
        self.assertEqual(report.kinds, {"engine-source": 1321, "engine-plugin": 31})
        findings = {finding.key: finding for finding in report.findings}
        self.assertIn("wait", findings)
        self.assertGreater(report.share_of(findings["wait"].owned_cycles), 0.8)
        self.assertGreater(findings["wait"].over_frames, 25)

    def test_a_capture_with_no_frames_maps_its_specs_without_a_weight(self) -> None:
        """`viewer-pc-3` is a `UnrealPak` run: 244 located specs and no frame pair at all."""
        report = self._report("viewer-pc-3")
        self.assertEqual(report.located, 244)
        self.assertEqual(report.kinds, {"engine-source": 244})
        self.assertEqual(report.frames, 0)
        self.assertEqual(report.owned_total, 0)
        self.assertTrue(report.files, "the mapping stands on its own, without frames")
        lines = sources.summarize_lines(report, 1000000, 4)[0]
        self.assertIn("no frame work in this capture", "\n".join(lines))
        self.assertIn("unweighted, because this capture has no frame work", "\n".join(lines))

    def test_the_engine_tree_checks_the_recorded_paths(self) -> None:
        """With an engine tree named on this machine: most engine paths resolve, the rest do not.

        Skipped when no tree is named (`$UEI_ENGINE_DIR`), because the mapping does not need one --
        the count is a version-consistency signal, and a capture recorded from another revision is
        expected to miss some of its files.
        """
        tree = self._tree()
        if tree is None:
            self.skipTest("no engine tree named by $%s" % (shapes.ENV_ENGINE_DIR,))
        report, _reasons = sources.build(self._model("editor-pie-1"), BUDGET, tree)
        assert report is not None
        self.assertEqual(report.checked, report.side_counts["engine"])
        self.assertGreater(report.found, 0)
        self.assertLess(report.found, report.checked, "another revision is missing files")


if __name__ == "__main__":  # pragma: no cover - unittest discovery runs it
    unittest.main()
