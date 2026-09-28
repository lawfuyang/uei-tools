"""The bottleneck verdict: the rules, the numbers behind them, and the corpus they were measured on.

Two halves again. The hermetic half builds captures whose frames, scopes and GPU frames are known
arithmetic -- and it builds them the awkward way on purpose: the frame markers come **first** in the
stream and the scope batches after them, which is what the game capture (`game-pc-2`) does and what "the frame
that was open when the pair was read" got wrong. The corpus half asks the registered captures the
questions only they can answer (does the GPU timeline align? is the editor capped at 3 FPS?), and
skips, loudly, on a machine that has none of them.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from testcase import UeiaTestCase

import bottleneck
import container
import goldens
import model as model_mod
import streams
import summary
from fixtures import (
    EVENT_FLAG_IMPORTANT,
    EVENT_FLAG_MAYBE_HAS_AUX,
    EVENT_FLAG_NOSYNC,
    build_trace,
    event,
    important_aux_block,
    important_record,
    new_event_record,
    pack,
)

IMPORTANT_AUX = EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX | EVENT_FLAG_NOSYNC
FREQUENCY = 1000000

#: The timer specs the fixtures declare: ordinary work, a wait (which must not count as work), and
#: an undeclared-but-referenced id is *not* here on purpose (the no-spec path is tested elsewhere).
SPEC_TICK, SPEC_WAIT, SPEC_OTHER = 7, 8, 9
GPU_SPEC_UI, GPU_SPEC_OPAQUE = 82058, 112898


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _blob(records: Sequence[Tuple[int, int, bool, Optional[int]]]) -> bytes:
    """One CPU batch: `(cycle, spec id, is_begin, ignored)` records, deltas accumulated."""
    out = bytearray()
    previous = 0
    for cycle, spec_id, is_begin, _unused in records:
        delta = cycle - previous if previous else cycle
        previous = cycle
        out += _varint((delta << 2) | (1 if is_begin else 0))
        if is_begin:
            out += _varint(spec_id)
    return bytes(out)


def pairs(*items: Tuple[int, int, int]) -> List[Tuple[int, int, bool, Optional[int]]]:
    """`(spec, begin, end)` triples as the batch records a writer would emit, in nesting order."""
    records: List[Tuple[int, int, bool, Optional[int]]] = []
    for spec, begin, end in items:
        records.append((begin, spec, True, None))
        records.append((end, 0, False, None))
    return records


def gpu_blob(records: Sequence[Tuple[int, int, bool]]) -> bytes:
    """One legacy GPU frame batch: `(delta microseconds, spec id, is_begin)`."""
    out = bytearray()
    for delta, spec_id, is_begin in records:
        out += _varint((delta << 1) | (1 if is_begin else 0))
        if is_begin:
            out += spec_id.to_bytes(4, "little")
    return bytes(out)


def frame_events(tid: int, frame_type: int, frames: Sequence[Tuple[int, int]]) -> bytes:
    """The BeginFrame/EndFrame pairs of one thread, one frame type."""
    out = b""
    serial = 1
    for begin, end in frames:
        out += event(22, pack("u64", begin) + pack("u8", frame_type), serial=serial)
        out += event(23, pack("u64", end) + pack("u8", frame_type), serial=serial + 1)
        serial += 2
    return out


def trace(frames: Sequence[Tuple[int, int]], game: Sequence[Tuple[int, int, int]],
          render: Sequence[Tuple[int, int, int]] = (),
          gpu: Sequence[Tuple[int, int, Sequence[Tuple[int, int, bool]]]] = (),
          frequency: int = FREQUENCY, game_name: str = "GameThread",
          render_name: str = "RenderThread 0") -> bytes:
    """A capture with known frames, known scopes and (optionally) known GPU frames.

    The scope batches are written **after** the frame markers, which is the shape that broke the
    first implementation: nothing here may depend on the batches arriving while a frame is open.
    """
    schema = (
        new_event_record(16, "$Trace", "NewTrace",
                         [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                         flags=EVENT_FLAG_NOSYNC)
        + new_event_record(17, "$Trace", "ThreadInfo", [("ThreadId", "u32"), ("Name", "s")],
                           flags=IMPORTANT_AUX)
        + new_event_record(20, "CpuProfiler", "EventSpec",
                           [("Id", "u32"), ("Name", "s"), ("File", "s"), ("Line", "u32")],
                           flags=IMPORTANT_AUX)
        + new_event_record(21, "CpuProfiler", "EventBatchV2", [("Data", "arr")],
                           flags=EVENT_FLAG_NOSYNC | EVENT_FLAG_MAYBE_HAS_AUX)
        + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(61, "GpuProfiler", "EventSpec",
                           [("EventType", "u32"), ("Name", "arr")], flags=IMPORTANT_AUX)
        + new_event_record(62, "GpuProfiler", "Frame",
                           [("CalibrationBias", "u64"), ("TimestampBase", "u64"),
                            ("RenderingFrameNumber", "u32"), ("Data", "arr")],
                           flags=EVENT_FLAG_NOSYNC | EVENT_FLAG_MAYBE_HAS_AUX)
    )
    importants = important_record(
        16, pack("u64", frames[0][0] if frames else 0) + pack("u64", frequency),
    )
    for tid, name in ((2, game_name), (98, render_name)):
        importants += important_record(17, pack("u32", tid)
                                       + important_aux_block(1, name.encode("utf-8")))
    for spec_id, spec_name, spec_file in (
            (SPEC_TICK, "Tick", "Game.cpp"), (SPEC_WAIT, "WaitForTasks", "TaskGraph.cpp"),
            (SPEC_OTHER, "Slate::DrawWindows", "Slate.cpp")):
        importants += important_record(
            20, pack("u32", spec_id) + pack("u32", 10)
            + important_aux_block(1, spec_name.encode("utf-8"))
            + important_aux_block(2, spec_file.encode("utf-8")),
        )
    for spec_id, spec_name in ((GPU_SPEC_UI, "SlateUI"), (GPU_SPEC_OPAQUE, "Basepass")):
        importants += important_record(
            61, pack("u32", spec_id)
            + important_aux_block(1, spec_name.encode("utf-16-le")),
        )
    game_stream = frame_events(2, 0, frames)
    if game:
        game_stream += event(21, b"", aux=[(0, _blob(pairs(*game)))], maybe_aux=True)
    render_stream = frame_events(98, 1, frames)
    if render:
        render_stream += event(21, b"", aux=[(0, _blob(pairs(*render)))], maybe_aux=True)
    for number, base_us, records in gpu:
        render_stream += event(
            62, pack("u64", 0) + pack("u64", base_us) + pack("u32", number),
            aux=[(3, gpu_blob(records))], maybe_aux=True,
        )
    return build_trace(events_stream=schema, importants_stream=importants,
                       threads={2: game_stream, 98: render_stream})


def decode(data: bytes) -> Dict[str, Any]:
    """A fixture capture decoded end to end -- what a command sees after `load_model`."""
    rows, _anomalies = container.walk_packets(data, container.parse_header(data))
    built, _counts, _all = model_mod.build_model(streams.assemble(data, rows))
    return dict(built)


#: Three 20 ms frames. 20 ms is over a 60 FPS budget by 3.3 ms, which makes every verdict below
#: readable: "the thread worked 18 ms of it" is the bound, "1 ms" is not.
FRAMES = ((1000000, 1020000), (1020000, 1040000), (1040000, 1060000))
BUDGET = summary.parse_budget(None, None)


class TestTheRoles(unittest.TestCase):
    def test_the_engines_own_thread_names_are_read(self) -> None:
        self.assertEqual(bottleneck.role_of("GameThread"), bottleneck.GAME_ROLE)
        self.assertEqual(bottleneck.role_of("RenderThread 0"), bottleneck.RENDER_ROLE)
        self.assertEqual(bottleneck.role_of("RHIThread"), bottleneck.RHI_ROLE)

    def test_a_name_that_says_nothing_gets_no_role(self) -> None:
        self.assertEqual(bottleneck.role_of("AsyncLoadingThread"), "")
        self.assertEqual(bottleneck.role_of(""), "")


class TestTheSeriesChoice(unittest.TestCase):
    def test_the_named_game_thread_is_judged(self) -> None:
        model = decode(trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)]))
        series = bottleneck.pick_series(model)
        self.assertIsNotNone(series)
        assert series is not None
        self.assertEqual(series.tid, 2)

    def test_a_capture_that_names_no_game_thread_falls_back_to_the_busiest(self) -> None:
        model = decode(trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)],
                             game_name="MainThread", render_name="DrawThread"))
        series = bottleneck.pick_series(model)
        self.assertIsNotNone(series)
        assert series is not None
        self.assertEqual(series.tid, 2, "both threads have three frames; the lower id wins")

    def test_a_thread_can_be_asked_for(self) -> None:
        model = decode(trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)]))
        series = bottleneck.pick_series(model, 98)
        self.assertIsNotNone(series)
        assert series is not None
        self.assertEqual(series.tid, 98)

    def test_a_capture_without_frames_has_no_series(self) -> None:
        self.assertIsNone(bottleneck.pick_series({"frames": []}))
        self.assertIsNone(bottleneck.pick_series({"frames": [{"tid": 2}]}, 98))


class TestTheVerdicts(unittest.TestCase):
    """The engine's decision tree, one fixture per branch, with hand-computed numbers."""

    def _report(self, data: bytes) -> bottleneck.Report:
        model = decode(data)
        report, notes = bottleneck.classify(model, BUDGET)
        self.assertIsNotNone(report, notes)
        assert report is not None
        return report

    def test_a_busy_game_thread_bounds_the_frame(self) -> None:
        """The first frame gets 18 ms of work; the other two get none, so they are unexplained."""
        report = self._report(trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)]))
        self.assertEqual([item.verdict for item in report.verdicts],
                         ["game", "unexplained", "unexplained"])
        self.assertEqual(report.counts["game"], 1)
        self.assertAlmostEqual(report.verdicts[0].work_ms or 0.0, 18.0, places=3)
        self.assertIn("worked 18.000 ms", report.verdicts[0].why)

    def test_a_wait_is_not_work(self) -> None:
        """The same 18 ms, all of it inside `WaitForTasks`: nothing is bound by it."""
        report = self._report(trace(FRAMES, [(SPEC_WAIT, 1000000, 1018000)]))
        self.assertEqual(report.verdicts[0].verdict, "unexplained")
        self.assertAlmostEqual(report.verdicts[0].covered_ms or 0.0, 18.0, places=3)
        self.assertAlmostEqual(report.verdicts[0].work_ms or 0.0, 0.0, places=3)

    def test_a_busy_render_thread_bounds_the_frame(self) -> None:
        report = self._report(trace(FRAMES, [(SPEC_TICK, 1000000, 1001000)],
                                    [(SPEC_TICK, 1000000, 1018000)]))
        self.assertEqual(report.verdicts[0].verdict, "render")
        self.assertIn("tid 98 worked 18.000 ms", report.verdicts[0].why)
        self.assertEqual([role.role for role in report.roles],
                         [bottleneck.GAME_ROLE, bottleneck.RENDER_ROLE])

    def test_a_busy_gpu_bounds_the_frame_when_no_thread_does(self) -> None:
        # the two GPU frames span the same 60 ms the three frames do, so the clock scale is 1: the
        # alignment is measured, and a timeline that does not measure up is refused (see below)
        gpu = ((1, 1000000, [(0, GPU_SPEC_UI, True), (25000, 0, False)]),
               (2, 1060000, [(0, GPU_SPEC_OPAQUE, True), (1000, 0, False)]))
        report = self._report(trace(FRAMES, [(SPEC_TICK, 1000000, 1001000)],
                                    [(SPEC_TICK, 1000000, 1001000)], gpu=gpu))
        self.assertEqual(report.verdicts[0].verdict, "gpu")
        self.assertAlmostEqual(report.verdicts[0].gpu_ms or 0.0, 25.0, places=3)
        self.assertIsNotNone(report.alignment)

    def test_nothing_measured_is_reported_as_unexplained(self) -> None:
        report = self._report(trace(FRAMES, [(SPEC_TICK, 1000000, 1001000)]))
        self.assertEqual(report.verdicts[0].verdict, "unexplained")
        self.assertIn("nothing measured accounts for it", report.verdicts[0].why)

    def test_a_frame_inside_the_budget_is_not_bound(self) -> None:
        quick = ((1000000, 1010000),)
        report = self._report(trace(quick, [(SPEC_TICK, 1000000, 1009000)]))
        self.assertEqual(report.verdicts[0].verdict, "within")
        self.assertEqual(report.bound(), 0)

    def test_the_budget_moves_the_verdict(self) -> None:
        data = trace(FRAMES, [(SPEC_TICK, 1000000, 1009000)])
        model = decode(data)
        tight, _notes = bottleneck.classify(model, summary.parse_budget("240", None))
        loose, _notes = bottleneck.classify(model, summary.parse_budget("5", None))
        assert tight is not None and loose is not None
        self.assertEqual(tight.verdicts[0].verdict, "game", "9 ms of work misses 4.2 ms")
        self.assertEqual(loose.verdicts[0].verdict, "within", "and meets 200 ms")

    def test_every_number_in_a_verdict_comes_from_the_file(self) -> None:
        report = self._report(trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)]))
        first = report.verdicts[0]
        self.assertEqual((first.frame["begin_cycle"], first.frame["end_cycle"]),
                         (1000000, 1020000))
        self.assertAlmostEqual(first.milliseconds, 20.0, places=3)
        self.assertAlmostEqual(first.covered_ms or 0.0, 18.0, places=3)


class TestTheEvidence(unittest.TestCase):
    def test_the_evidence_names_the_timers_of_the_frame(self) -> None:
        data = trace(FRAMES, [(SPEC_TICK, 1000000, 1018000), (SPEC_OTHER, 1018000, 1019000)])
        model = decode(data)
        report, _notes = bottleneck.classify(model, BUDGET)
        assert report is not None
        text = bottleneck.evidence_text(model, report.verdicts[0])
        self.assertIn("Tick 18.000 ms (90%)", text)
        self.assertIn("Slate::DrawWindows 1.000 ms (5%)", text)

    def test_the_evidence_names_the_gpu_passes_of_the_frame(self) -> None:
        gpu = ((1, 1000000, [(0, GPU_SPEC_UI, True), (25000, 0, False)]),
               (2, 1060000, [(0, GPU_SPEC_OPAQUE, True), (1000, 0, False)]))
        data = trace(FRAMES, [(SPEC_TICK, 1000000, 1001000)], gpu=gpu)
        model = decode(data)
        report, _notes = bottleneck.classify(model, BUDGET)
        assert report is not None
        text = bottleneck.evidence_text(model, report.verdicts[0])
        self.assertIn("GPU: SlateUI 25.00 ms", text)


class TestTheGpuAlignment(unittest.TestCase):
    """A GPU timeline is placed by a *measured* scale, and refused when it does not fit."""

    def _aligned(self, bases: Sequence[int]) -> Tuple[Any, Dict[int, Dict[int, float]]]:
        gpu = tuple((index + 1, base, [(0, GPU_SPEC_UI, True), (100, 0, False)])
                    for index, base in enumerate(bases))
        model = decode(trace(FRAMES, [(SPEC_TICK, 1000000, 1001000)], gpu=gpu))
        series = bottleneck.pick_series(model)
        assert series is not None
        return bottleneck.align_gpu(model, series, FREQUENCY)

    def test_a_matching_clock_places_every_frame(self) -> None:
        alignment, per_frame = self._aligned([5000000, 5030000, 5060000])
        self.assertIsNotNone(alignment)
        assert alignment is not None
        self.assertAlmostEqual(alignment.scale, 1.0, places=6)
        self.assertEqual((alignment.contained, alignment.total), (3, 3))
        self.assertEqual(len(per_frame), 3, "one GPU frame per window")

    def test_a_clock_that_does_not_fit_is_refused(self) -> None:
        alignment, per_frame = self._aligned([5000000, 5000001])
        self.assertIsNone(alignment, "0.0002 s of GPU timeline cannot span 60 ms of frames")
        self.assertEqual(per_frame, {})

    def test_one_gpu_frame_is_not_a_timeline(self) -> None:
        alignment, _per_frame = self._aligned([5000000])
        self.assertIsNone(alignment)

    def test_an_unreadable_gpu_frame_is_not_evidence(self) -> None:
        gpu = ((1, 1000000, [(0, GPU_SPEC_UI, True)]),)  # a begin with no end
        model = decode(trace(FRAMES, [(SPEC_TICK, 1000000, 1001000)], gpu=gpu))
        self.assertEqual(model["counts"]["gpu_unreadable"], 1)
        series = bottleneck.pick_series(model)
        assert series is not None
        alignment, per_frame = bottleneck.align_gpu(model, series, FREQUENCY)
        self.assertIsNone(alignment)
        self.assertEqual(per_frame, {})


class TestTheCapNote(unittest.TestCase):
    def test_frames_pinned_to_a_display_period_are_noticed(self) -> None:
        note = bottleneck.cap_note([33.334, 33.334, 16.667, 19.0], BUDGET)
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("3 of 4 unexplained frame(s)", note)
        self.assertIn("heuristic", note)

    def test_frames_scattered_off_the_periods_are_not(self) -> None:
        self.assertIsNone(bottleneck.cap_note([7.0, 12.5, 19.4, 27.0], BUDGET))
        self.assertIsNone(bottleneck.cap_note([], BUDGET))


class TestWhatItCannotDo(unittest.TestCase):
    """Absent is not zero: each unknown comes back as its own reason, and none of them is a zero."""

    def test_no_frames_at_all(self) -> None:
        model = decode(trace(FRAMES, []) if False else build_trace(
            events_stream=new_event_record(30, "Logging", "LogMessage", [("Cycle", "u64")]),
            threads={2: event(30, pack("u64", 1000000))},
        ))
        report, reasons = bottleneck.classify(model, BUDGET)
        self.assertIsNone(report)
        self.assertIn("no Misc.BeginFrame/EndFrame pair", reasons[0])

    def test_frames_but_no_cycle_frequency(self) -> None:
        model = decode(trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)]))
        model["session"] = {key: value for key, value in model["session"].items()
                            if key != "cycle_frequency"}
        report, reasons = bottleneck.classify(model, BUDGET)
        self.assertIsNone(report)
        self.assertIn("no cycle frequency", reasons[0])

    def test_frames_but_no_timer_specs(self) -> None:
        """A capture whose schema declares no CpuProfiler specs: the occupancy is None, not zero."""
        model = decode(trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)]))
        for row in model["frames"]:
            row["covered_cycles"] = None
            row["wait_cycles"] = None
        report, reasons = bottleneck.classify(model, BUDGET)
        self.assertIsNone(report)
        self.assertIn("no CpuProfiler timer specs", reasons[0])

    def test_no_gpu_frames_is_a_note_not_a_verdict(self) -> None:
        model = decode(trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)]))
        report, _notes = bottleneck.classify(model, BUDGET)
        assert report is not None
        self.assertEqual(report.verdicts[0].verdict, "game")
        self.assertFalse(report.gpu_present)
        self.assertIn("GPU side is unknown", report.notes[0])
        self.assertIn("re-record", report.notes[0])


class TestTheCommand(UeiaTestCase):
    """`ueia bottleneck`: the report, the exits, and the three output forms."""

    def _capture(self, data: Optional[bytes] = None) -> str:
        return str(self.write_capture(data if data is not None else
                                      trace(FRAMES, [(SPEC_TICK, 1000000, 1018000)])))

    def test_the_report_of_the_fixture_by_hand(self) -> None:
        code, out, err = self.run_cli(["bottleneck", self._capture()])
        self.assertEqual(code, 0)
        self.assertEqual(err, "", "table form keeps everything on stdout")
        self.assertIn("frames    : 3 frame(s) on tid 2 (GameThread), frame type 0", out)
        self.assertIn("budget    : 60 FPS = 16.667 ms", out)
        self.assertIn("verdict   : 1 of 3 frame(s) bound; game 1; unexplained 2", out)
        self.assertIn("thread    : tid 2 GameThread (game, heuristic) -- 3 frame(s), 1 over budget",
                      out)
        self.assertIn("thread    : tid 98 RenderThread 0 (render, heuristic)", out)
        self.assertIn("note      : no GPU frames in this capture", out)

    def test_the_table_names_the_bound_frames_worst_first(self) -> None:
        code, out, _err = self.run_cli(["bottleneck", self._capture()])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("frame "))
        rows = [line for line in lines[start + 2:] if line.strip()]
        self.assertEqual(len(rows), 3, "--limit 10 lists all three")
        self.assertEqual(rows[0].split()[:5], ["0", "0.000", "20.000", "game", "18.000"])
        self.assertIn("Tick 18.000 ms (90%)", rows[0])

    def test_the_limit_caps_the_table(self) -> None:
        one = self.run_cli(["bottleneck", self._capture(), "--limit", "1"])[1]
        lines = one.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("frame "))
        self.assertEqual(len([line for line in lines[start + 2:] if line.strip()]), 1)

    def test_the_csv_form_is_the_table_alone(self) -> None:
        code, out, err = self.run_cli(["bottleneck", self._capture(), "--format", "csv"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0],
                         "frame,at s,ms,verdict,work ms,gpu ms,why / what ran in it")
        self.assertIn("Tick 18.000 ms (90%)", out)
        self.assertIn("verdict   : 1 of 3 frame(s) bound", err)

    def test_the_markdown_form_carries_the_same_table(self) -> None:
        code, out, _err = self.run_cli(["bottleneck", self._capture(), "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0],
                         "| frame | at s | ms | verdict | work ms | gpu ms | why / what ran in it |")

    def test_a_capture_without_frames_exits_two_with_the_reason(self) -> None:
        capture = self.write_capture(build_trace(
            events_stream=new_event_record(30, "Logging", "LogMessage", [("Cycle", "u64")]),
            threads={2: event(30, pack("u64", 1000000))},
        ), name="plain.utrace")
        code, out, _err = self.run_cli(["bottleneck", str(capture)])
        self.assertEqual(code, 2)
        self.assertIn("cannot    : this capture carries no Misc.BeginFrame/EndFrame pair", out)
        self.assertIn("hint      :", out)

    def test_a_thread_without_frames_exits_two(self) -> None:
        code, out, _err = self.run_cli(["bottleneck", self._capture(), "--tid", "77"])
        self.assertEqual(code, 2)
        self.assertIn("no Misc.BeginFrame/EndFrame pair on tid 77", out)

    def test_bad_options_are_usage_errors(self) -> None:
        capture = self._capture()
        self.assertEqual(self.run_cli(["bottleneck", capture, "--budget"])[0], 2)
        self.assertEqual(self.run_cli(["bottleneck", capture, "--order", "worst"])[0], 2)
        self.assertEqual(
            self.run_cli(["bottleneck", capture, "--budget", "60", "--budget-ms", "16"])[0], 2)

    def test_the_same_capture_gives_the_same_report_twice(self) -> None:
        capture = self._capture()
        self.assertEqual(self.run_cli(["bottleneck", capture]),
                         self.run_cli(["bottleneck", capture]))

    def test_summary_carries_the_verdict_it_can_see(self) -> None:
        code, out, _err = self.run_cli(["summary", self._capture()])
        self.assertEqual(code, 0)
        self.assertIn("bottleneck: 1 of 3 frame(s) bound; game 1", out)

    def test_summary_says_when_the_verdict_cannot_be_decided(self) -> None:
        capture = self.write_capture(build_trace(
            events_stream=new_event_record(30, "Logging", "LogMessage", [("Cycle", "u64")]),
            threads={2: event(30, pack("u64", 1000000))},
        ), name="plain.utrace")
        code, out, _err = self.run_cli(["summary", str(capture)])
        self.assertEqual(code, 2, "summary exits 2 for its own reason, before any verdict line")
        self.assertIn("frames    : none", out)
        self.assertNotIn("bottleneck:", out)


class TestTheRealCaptures(UeiaTestCase):
    """The registered captures: the questions only a real `.utrace` can answer.

    Skipped, loudly, when this machine has none of them -- a skip is a skip, never a pass.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = goldens.capture_paths()

    def _path(self, key: str) -> str:
        path = self.paths.get(key)
        if path is None or not path.is_file():
            self.skipTest("the %s capture is not on this machine" % (key,))
        return str(path)

    def test_the_editor_session_is_capped_rather_than_bound(self) -> None:
        """The corpus: 1413 frames at ~333 ms, nothing on the CPU or GPU busy, GPU decoded.

        The two findings that make this a real classification rather than a shrug: its GPU timeline
        lines up with the frame series (so the GPU really was idle), and most frames sit on a
        multiple of a display period -- an editor throttled to 3 FPS, not a game with a slow frame.
        """
        code, _out, err = self.run_cli(["bottleneck", self._path("editor-pie-1"),
                                        "--format", "csv"])
        self.assertEqual(code, 0)
        self.assertIn("unexplained 1320", err)
        self.assertIn("frame-rate cap", err)
        self.assertIn("GPU timeline: 1409 of 1410", err)
        self.assertIn("game,", err.splitlines()[4] if len(err.splitlines()) > 4 else err)

    def test_the_editor_frames_are_the_three_fps_the_capture_shows(self) -> None:
        model = self._model("editor-pie-1")
        report, _notes = bottleneck.classify(model, summary.parse_budget(None, None))
        assert report is not None
        self.assertEqual(len(report.verdicts), 1413)
        worst = max(report.verdicts, key=lambda item: item.milliseconds)
        self.assertEqual(worst.verdict, "game")
        self.assertGreater(worst.milliseconds, 120000.0, "the 121-second PIE stall")
        self.assertEqual(report.counts["unexplained"], 1320)

    def test_the_editor_gpu_timeline_reads_its_real_passes(self) -> None:
        model = self._model("editor-pie-1")
        names = {row["name"] for row in model["gpu_specs"]}
        self.assertIn("SlateUI", names)
        self.assertIn("Basepass", names)
        self.assertEqual(model["counts"]["gpu_unreadable"], 0)
        busiest = max(model["gpu_frames"], key=lambda row: int(row["busy_us"]))
        self.assertLess(int(busiest["busy_us"]) / 1000.0, 200.0, "an editor's GPU is not busy")

    def test_the_game_capture_is_waiting_and_says_so(self) -> None:
        """The game capture: 905 ms frames, both threads inside wait scopes, and no GPU channel.

        This is the case the roadmap's rule exists for. The game thread's frame is mostly
        `FlushRenderingCommands`/`GameThreadWaitForTask` -- wait-shaped scopes, so not work -- and
        the render thread's own work is under the budget in 763 of 771 frames. What is left is a
        frame waiting on something this capture cannot show, and the report says exactly that
        instead of calling it CPU-bound.
        """
        model = self._model("game-pc-2")
        report, _notes = bottleneck.classify(model, summary.parse_budget(None, None))
        assert report is not None
        self.assertEqual(report.counts["unexplained"], 761)
        self.assertEqual(report.counts["game"], 1)
        self.assertEqual(report.counts["render"], 8)
        self.assertLess(report.bound(), 20, "nine frames out of 771 have a measured bound")
        self.assertFalse(report.gpu_present)
        self.assertIn("GPU side is unknown", report.notes[0])
        self.assertEqual(model["counts"]["gpu_frames"], 0)
        game = next(role for role in report.roles if role.role == bottleneck.GAME_ROLE)
        self.assertEqual((game.frames, game.over_budget), (771, 1))

    def test_the_viewer_capture_has_no_frames_to_judge(self) -> None:
        code, out, _err = self.run_cli(["bottleneck", self._path("viewer-pc-3")])
        self.assertEqual(code, 2)
        self.assertIn("no Misc.BeginFrame/EndFrame pair", out)

    def _model(self, key: str) -> Dict[str, Any]:
        import commands

        _view, model, _cached = commands.load_model(self._path(key))
        return dict(model)


if __name__ == "__main__":
    unittest.main()
