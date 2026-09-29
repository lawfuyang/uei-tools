"""The parallelism report: the measures, the words printed for them, and the corpus they came from.

The hermetic half is `parallel_trace`, whose every number is hand-checkable at 1 cycle = 1
microsecond (see `fixtures.parallel_streams`): 65% of the frame thread's work is solo, one frame has
two threads working at once, and 1 ms of lock-named scope is held by two threads together. The
corpus half asks the registered captures the same questions and skips, loudly, when this machine has
none of them.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, cast

from testcase import UeiaTestCase

import goldens
import parallel
import summary
from fixtures import (
    EVENT_FLAG_NOSYNC,
    build_trace,
    demo_trace,
    important_record,
    new_event_record,
    pack,
    parallel_trace,
    work_schema,
)


class TestTheFixtureMeasures(UeiaTestCase):
    """`classify` on a capture where two threads worked at once."""

    def _model(self) -> Dict[str, Any]:
        import commands

        path = self.write_capture(parallel_trace())
        _view, model, _cached = commands.load_model(str(path))
        return cast(Dict[str, Any], model)

    def _report(self) -> parallel.Report:
        report, _notes = parallel.classify(self._model(), summary.parse_budget("60", None))
        assert report is not None
        return report

    def test_the_two_frames_are_the_series(self) -> None:
        report = self._report()
        self.assertEqual(len(report.series.rows), 2)
        self.assertEqual(report.series.tid, 2)
        self.assertEqual(report.span_cycles, 60000, "20 ms + 40 ms at 1 MHz")

    def test_the_frame_threads_own_numbers(self) -> None:
        report = self._report()
        self.assertEqual(report.own_busy, 24000, "16 ms in the first frame, 8 in the second")
        self.assertEqual(report.own_wait, 4000, "the WaitForTasks inside the first frame")
        self.assertEqual(report.own_work(), 20000)
        self.assertAlmostEqual(report.solo_share(), 0.65, places=6)
        self.assertEqual(report.solo, 13000, "9 ms of the first frame's work and all of the second")

    def test_the_other_threads_work_and_the_union(self) -> None:
        report = self._report()
        self.assertEqual(report.others_work, 9000, "7 ms of WorkerTask and 2 ms of FScopeLock")
        self.assertEqual(report.all_cycles, 24000, "[1,002,000, 1,018,000) and [1,022,000, 1,030,000)")

    def test_the_peak_is_two_threads_in_the_first_frame(self) -> None:
        report = self._report()
        self.assertEqual(report.peaks, {2: 1, 1: 1})
        self.assertEqual(report.workers(), 2, "the ceiling's floor: one thread is not a spread")
        self.assertEqual(report.threads_working(), 1, "the commonest frame's peak, printed as is")

    def test_contention_is_the_cycles_two_threads_held_a_lock(self) -> None:
        report = self._report()
        self.assertEqual(report.contended_frames, 1)
        self.assertEqual(report.contended_cycles, 1000, "1,015,000 to 1,016,000 at 1 MHz")

    def test_the_table_rows_are_per_thread(self) -> None:
        report = self._report()
        rows = {row.tid: row for row in report.threads}
        self.assertEqual(sorted(rows), [2, 4])
        self.assertEqual(rows[2].frames, 2)
        self.assertEqual(rows[2].busy_cycles, 24000)
        self.assertEqual(rows[2].wait_cycles, 4000)
        self.assertEqual(rows[2].lock_cycles, 2000)
        self.assertEqual(rows[2].work_cycles(), 20000)
        self.assertEqual(rows[4].frames, 1, "the worker only worked inside the first frame")
        self.assertEqual(rows[4].busy_cycles, 9000)
        self.assertEqual(rows[4].role, "", "its name says Worker, and only engine roles are matched")

    def test_the_frames_that_miss_the_budget_carry_their_own_share(self) -> None:
        report = self._report()
        self.assertEqual(report.over_frames, 2, "20 ms and 40 ms both miss 16.667 ms")
        self.assertEqual(report.over_work, 20000)
        self.assertEqual(report.over_solo, 13000)
        self.assertAlmostEqual(report.over_solo_share() or 0.0, 0.65, places=6)

    def test_the_candidates_are_the_kept_frames_biggest_timers(self) -> None:
        report = self._report()
        self.assertEqual([candidate.name for candidate in report.candidates], ["FrameTime"])
        self.assertAlmostEqual(report.candidates[0].share, 24.0 / 60.0, places=6)
        self.assertEqual(report.kept_frames, 2, "both of the thread's frames are kept")

    def test_the_words_say_what_was_measured_and_what_is_a_heuristic(self) -> None:
        report = self._report()
        lines = parallel.finding_lines(report, 1000000)
        text = "\n".join(lines)
        self.assertIn("65.0% of the frame thread's work", text)
        self.assertIn("measured overlap; a scope beside it is not a dependency", text)
        self.assertIn("Amdahl on the measured share: a ceiling, not a prediction", text)
        self.assertIn("no name among them says it is already parallel", text)
        self.assertIn("over-subscription cannot be judged here", text)
        self.assertEqual(parallel.verdict_text(report, 1000000).count("65.0%"), 1)
        self.assertIn("the commonest frame had 1 thread(s) working at once",
                      parallel.verdict_text(report, 1000000))
        self.assertIn("no thread inside a scope", parallel.occupancy_text(report))

    def test_a_capture_it_cannot_judge_says_why(self) -> None:
        model = self._model()
        model["frames"] = []
        model["frame_occupancy"] = []
        report, reasons = parallel.classify(model, summary.parse_budget(None, None))
        self.assertIsNone(report)
        self.assertIn("no Misc.BeginFrame/EndFrame pair", reasons[0])

        model = self._model()
        model["frame_occupancy"] = []
        report, reasons = parallel.classify(model, summary.parse_budget(None, None))
        self.assertIsNone(report)
        self.assertIn("no CpuProfiler timer specs", reasons[0])

        model = self._model()
        model["session"] = {key: value for key, value in model["session"].items()
                            if key != "cycle_frequency"}
        report, reasons = parallel.classify(model, summary.parse_budget(None, None))
        self.assertIsNone(report)
        self.assertIn("no cycle frequency", reasons[0])


class TestTheCommand(UeiaTestCase):
    """`ueia parallelism`: the table, the prose, the exit codes and the options it refuses."""

    def _capture(self) -> str:
        return str(self.write_capture(parallel_trace()))

    def test_the_table_lists_the_threads_by_work(self) -> None:
        """The default form keeps the prose and the table together, as every row command does."""
        code, out, _err = self.run_cli(["parallelism", self._capture(), "--jobs", "1"])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].startswith("capture   : "), out.splitlines()[:1])
        self.assertIn("verdict   : 65.0% of the frame thread's work", out)
        self.assertIn("locks     : 1 frame(s) had two or more threads", out)
        self.assertIn("tid  thread        role   frames   busy  wait  lock  note", out)
        self.assertIn("  2  GameThread    game        2  40.0%  6.7%  3.3%", out)
        self.assertIn("the frame thread: 65.0% of its work was solo", out)
        self.assertIn("  4  WorkerThread  other       1  15.0%  0.0%  3.3%", out)

    def test_the_csv_form_is_the_same_table_without_the_prose(self) -> None:
        code, out, err = self.run_cli(["parallelism", self._capture(), "--jobs", "1",
                                       "--format", "csv"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "tid,thread,role,frames,busy,wait,lock,note")
        self.assertIn("2,GameThread,game,2,40.0%,6.7%,3.3%", out)
        self.assertIn("verdict   :", err, "the prose still goes to stderr")

    def test_the_markdown_form_keeps_the_columns(self) -> None:
        code, out, _err = self.run_cli(["parallelism", self._capture(), "--jobs", "1",
                                        "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].startswith("| tid | thread | role |"))
        self.assertIn("| 2 | GameThread | game | 2 | 40.0% |", out)

    def test_limit_caps_the_rows_and_zero_lists_them_all(self) -> None:
        code, out, _err = self.run_cli(["parallelism", self._capture(), "--jobs", "1", "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertIn("GameThread", out)
        self.assertNotIn("WorkerThread", out)
        code, out, _err = self.run_cli(["parallelism", self._capture(), "--jobs", "1", "--limit", "0"])
        self.assertEqual(code, 0)
        self.assertIn("WorkerThread", out)

    def test_a_budget_given_twice_is_a_usage_error(self) -> None:
        code, _out, err = self.run_cli(["parallelism", self._capture(), "--budget", "30",
                                        "--budget-ms", "20"])
        self.assertEqual(code, 2)
        self.assertIn("--budget and --budget-ms are two ways to say one thing", err)

    def test_an_option_it_does_not_take_is_a_usage_error(self) -> None:
        """Unknown *and* known-elsewhere options are both refused before anything is read."""
        code, _out, err = self.run_cli(["parallelism", self._capture(), "--depth", "3"])
        self.assertEqual(code, 2)
        self.assertIn("unknown option", err)
        code, _out, err = self.run_cli(["parallelism", self._capture(), "--graph", "dot"])
        self.assertEqual(code, 2)
        self.assertIn("parallelism takes --budget, --budget-ms, --tid, --limit", err)

    def test_a_capture_without_frames_exits_two(self) -> None:
        capture = self.write_capture(demo_trace() if _has_no_frames(demo_trace()) else work_free())
        code, out, _err = self.run_cli(["parallelism", str(capture), "--jobs", "1"])
        self.assertEqual(code, 2)
        self.assertIn("cannot    :", out)
        self.assertIn("hint      :", out)

    def test_a_capture_without_timer_specs_exits_two(self) -> None:
        capture = self.write_capture(_frames_but_no_specs())
        code, out, _err = self.run_cli(["parallelism", str(capture), "--jobs", "1"])
        self.assertEqual(code, 2)
        self.assertIn("timer specs", out)

    def test_a_tid_without_frames_exits_two(self) -> None:
        code, out, _err = self.run_cli(["parallelism", self._capture(), "--jobs", "1", "--tid", "4"])
        self.assertEqual(code, 2)
        self.assertIn("no Misc.BeginFrame/EndFrame pair on tid 4", out)


def _has_no_frames(data: bytes) -> bool:
    """True when a fixture capture has no `Misc.BeginFrame` pair at all (checked, not assumed)."""
    import container
    import model as model_mod
    import streams

    rows, _anomalies = container.walk_packets(data, container.parse_header(data))
    built, _counts, _anomalies = model_mod.build_model(streams.assemble(data, rows))
    return not built["frames"]


def work_free() -> bytes:
    """A capture with events and specs but no frame pair: what a `frames`-less trace looks like."""
    return build_trace(
        events_stream=work_schema(),
        importants_stream=important_record(16, pack("u64", 1000000)),
        threads={2: b""},
    )


def _frames_but_no_specs() -> bytes:
    """A capture with frame pairs and no `CpuProfiler.EventSpec`: nothing could be attributed."""
    schema = (
        new_event_record(16, "$Trace", "NewTrace", [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                         flags=EVENT_FLAG_NOSYNC)
        + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
    )
    stream = (
        _frame_event(22, 1000000, 1) + _frame_event(23, 1020000, 2)
    )
    return build_trace(
        events_stream=schema,
        importants_stream=important_record(16, pack("u64", 1000000) + pack("u64", 1000000)),
        threads={2: stream},
    )


def _frame_event(uid: int, cycle: int, serial: int) -> bytes:
    from fixtures import event

    return event(uid, pack("u64", cycle) + pack("u8", 0), serial=serial)


class TestTheRealCaptures(UeiaTestCase):
    """The registered captures: what only a real `.utrace` can say about parallelism.

    Skipped, loudly, when this machine has none of them -- a skip is a skip, never a pass.
    """
    #: This class reads a registered capture: the cache beside it is content-keyed.
    use_cache = True


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

    def test_the_editor_session_worked_alone(self) -> None:
        """The corpus's shape: an editor thread busy 99.7% of its frames, its workers idle.

        93% of the game thread's work ran with no other thread working; the two threads that look
        busy (render and RHI) are inside wait-named scopes for 97-98.5% of that coverage, which is
        why the work split has to exist before this number can be read at all.
        """
        report, _notes = parallel.classify(self._model("editor-pie-1"),
                                           summary.parse_budget("60", None))
        assert report is not None
        self.assertAlmostEqual(report.solo_share(), 0.930, places=3)
        self.assertEqual(report.threads_working(), 5, "the commonest frame's working threads")
        waiters = [row for row in report.threads if row.tid in (97, 98)]
        self.assertEqual(len(waiters), 2)
        for row in waiters:
            self.assertGreater(row.wait_cycles * 0.95, row.work_cycles(),
                               "the render and RHI threads are parked, not working")

    def test_the_editor_named_its_locks_and_found_contention(self) -> None:
        report, _notes = parallel.classify(self._model("editor-pie-1"),
                                           summary.parse_budget("60", None))
        assert report is not None
        self.assertEqual(report.contended_frames, 42)
        self.assertGreater(report.contended_cycles, 0)
        locks = [row for row in report.threads if row.lock_cycles]
        self.assertTrue(locks, "the table carries the cycles spent inside lock-named scopes")

    def test_the_game_capture_spread_its_work(self) -> None:
        """The other shape: 22-23 threads working at once in the commonest frame of a console game.

        Its frame thread is inside a wait scope for 95.5% of the frames, so the interesting number is
        not the frame time but who else was working -- and 30% of its work still had nobody beside it.
        """
        report, _notes = parallel.classify(self._model("game-pc-2"),
                                           summary.parse_budget("60", None))
        assert report is not None
        self.assertAlmostEqual(report.solo_share(), 0.696, places=3)
        self.assertEqual(report.threads_working(), 23)
        self.assertGreater(report.others_work * 4, report.own_work(),
                           "the rest of the machine did more work than the frame thread")
        self.assertFalse([row for row in report.threads if row.elsewhere and row.coarsened == 0
                          and row.frames], "rows are either measured here or marked elsewhere")

    def test_the_coarsened_timeline_is_named_in_the_table(self) -> None:
        """`game-pc-2` hits `coverage.SPAN_KEEP` on one thread: the row says so, the model counts it."""
        model = self._model("game-pc-2")
        self.assertGreater(model["counts"]["spans_coarsened"], 0)
        report, _notes = parallel.classify(model, summary.parse_budget("60", None))
        assert report is not None
        coarsened = [row for row in report.threads if row.coarsened]
        self.assertTrue(coarsened, "the thread whose timeline was merged is in the table")

    def test_a_capture_with_no_frames_cannot_answer(self) -> None:
        model = self._model("viewer-pc-3")
        report, reasons = parallel.classify(model, summary.parse_budget(None, None))
        self.assertIsNone(report)
        self.assertIn("no Misc.BeginFrame/EndFrame pair", reasons[0])

    def test_the_model_carries_a_row_for_every_frame(self) -> None:
        """The measurement's alignment invariant, on a real capture rather than a fixture.

        And the one relationship the two occupancy measurements have: the frame row's own coverage
        (attributed by the pair's **end** cycle, clipped to the frame) can never exceed the
        timeline's (the union of every span, intersected with the window), because the attributed
        pairs are a subset of the spans. A frame where it is *less* would mean the timeline lost a
        span the attribution found.
        """
        model = self._model("editor-pie-1")
        occupancy = model["frame_occupancy"]
        self.assertEqual(len(occupancy), len(model["frames"]))
        for index, entry in enumerate(occupancy):
            self.assertEqual(int(entry["frame"]), index)
            self.assertEqual(int(entry["tid"]), int(model["frames"][index]["tid"]))
            own = [row for row in entry["threads"] if int(row["tid"]) == int(entry["tid"])]
            if own:
                self.assertGreaterEqual(
                    int(own[0]["busy_cycles"]),
                    int(model["frames"][index]["covered_cycles"] or 0),
                )


if __name__ == "__main__":  # pragma: no cover - unittest discovery runs it
    unittest.main()
