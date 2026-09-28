"""The summary layer: the budget, the distribution, the histogram, and the frames that break it.

Two halves, like the module: the *definitions* (`percentile`, `histogram`, `distribute`) tested
against numbers worked out by hand on a series whose answer is obvious, and the *command* run over
a fixture capture whose four frames are 20, 60, 8 and 32 ms wide with known scopes inside them.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional, cast

from testcase import UeiaTestCase

import container
import model as model_mod
import streams
import summary
from shapes import FrameRow, FrameWorkRow, UsageError
from fixtures import (
    EVENT_FLAG_NOSYNC,
    build_trace,
    event,
    important_record,
    new_event_record,
    pack,
    work_trace,
)


def _model(data: bytes) -> Dict[str, Any]:
    """A fixture capture decoded end to end -- what every command sees after `load_model`."""
    rows, _anomalies = container.walk_packets(data, container.parse_header(data))
    built, _counts, _all = model_mod.build_model(streams.assemble(data, rows))
    return cast(Dict[str, Any], dict(built))


def _frame(index: int, tid: int, begin: int, end: int, frame_type: int = 0,
           covered: Optional[int] = None, wait: Optional[int] = None) -> FrameRow:
    return FrameRow(index=index, type=frame_type, tid=tid, begin_cycle=begin, end_cycle=end,
                    covered_cycles=covered, wait_cycles=wait)


def _no_frames_trace() -> bytes:
    """A capture with a schema but no frame pairs at all: nothing to summarise."""
    schema = new_event_record(
        30, "Logging", "LogMessage", [("LogPoint", "u64"), ("Cycle", "u64")],
    )
    return build_trace(
        events_stream=schema,
        threads={2: event(30, pack("u64", 1) + pack("u64", 1250000))},
    )


def _no_frequency_trace() -> bytes:
    """Frames, but no cycle frequency: the spans are knowable in cycles and not in milliseconds."""
    schema = (
        new_event_record(16, "$Trace", "NewTrace", [("StartCycle", "u64")],
                         flags=EVENT_FLAG_NOSYNC)
        + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
    )
    stream = (
        event(22, pack("u64", 1100000) + pack("u8", 0), serial=1)
        + event(23, pack("u64", 1300000) + pack("u8", 0), serial=2)
    )
    return build_trace(
        events_stream=schema,
        importants_stream=important_record(16, pack("u64", 1000000)),
        threads={2: stream},
    )


class TestTheBudget(unittest.TestCase):
    def test_the_default_is_the_practices_sixty(self) -> None:
        budget = summary.parse_budget(None, None)
        self.assertEqual(budget.fps, 60.0)
        self.assertAlmostEqual(budget.ms, 16.6666667, places=6)
        self.assertEqual(budget.label(), "60 FPS = 16.667 ms")

    def test_a_frame_rate_and_a_period_are_two_spellings_of_one_budget(self) -> None:
        self.assertAlmostEqual(summary.parse_budget("30", None).ms, 1000.0 / 30.0)
        self.assertAlmostEqual(summary.parse_budget(None, "33.333333").fps, 30.0, places=5)
        self.assertEqual(summary.parse_budget("30", None).label(),
                         summary.parse_budget(None, "33.333").label())

    def test_both_spellings_at_once_is_a_usage_error(self) -> None:
        with self.assertRaises(UsageError) as caught:
            summary.parse_budget("30", "20")
        self.assertIn("give one", str(caught.exception))

    def test_a_budget_that_is_not_a_number_or_not_positive_is_refused(self) -> None:
        for fps, ms in (("sixty", None), (None, "fast"), ("0", None), ("-1", None),
                        (None, "0"), (None, "-16.6")):
            with self.assertRaises(UsageError):
                summary.parse_budget(fps, ms)


class TestThePercentile(unittest.TestCase):
    """Nearest rank, defined by hand: the value at rank ceil(q*n), never an interpolation."""

    def test_the_values_the_practice_asks_for(self) -> None:
        values = [float(number) for number in range(1, 101)]
        self.assertEqual(summary.percentile(values, 0.50), 50.0)
        self.assertEqual(summary.percentile(values, 0.95), 95.0)
        self.assertEqual(summary.percentile(values, 0.99), 99.0)
        self.assertEqual(summary.percentile(values, 1.0), 100.0)

    def test_an_even_series_takes_the_upper_middle_not_the_mean_of_two_frames(self) -> None:
        values = [8.0, 20.0, 32.0, 60.0]
        self.assertEqual(summary.percentile(values, 0.50), 20.0, "rank 2 of 4")
        self.assertEqual(summary.percentile(values, 0.95), 60.0, "rank 4 of 4 -- a real frame")
        self.assertEqual(summary.percentile(values, 0.25), 8.0, "rank 1 of 4")

    def test_one_value_is_its_own_percentile(self) -> None:
        self.assertEqual(summary.percentile([4.5], 0.99), 4.5)

    def test_nothing_has_no_percentile(self) -> None:
        with self.assertRaises(ValueError):
            summary.percentile([], 0.99)


class TestTheDistribution(unittest.TestCase):
    """The fixture's four frames: 8, 20, 32 and 60 ms, against 60 FPS."""

    VALUES = [20.0, 60.0, 8.0, 32.0]

    def test_the_shape_of_a_series_with_one_hitch(self) -> None:
        result = summary.distribute(self.VALUES, summary.parse_budget(None, None))
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["mean_ms"], 30.0)
        self.assertEqual(result["min_ms"], 8.0)
        self.assertEqual(result["p50_ms"], 20.0)
        self.assertEqual(result["p95_ms"], 60.0)
        self.assertEqual(result["p99_ms"], 60.0)
        self.assertEqual(result["max_ms"], 60.0)
        self.assertEqual(result["over"], 3, "20, 32 and 60 ms all miss 16.667 ms")
        self.assertEqual(result["hitches"], 1, "only 60 ms is over two budgets")

    def test_the_verdict_is_budget_relative(self) -> None:
        at_30 = summary.distribute(self.VALUES, summary.parse_budget("30", None))
        self.assertEqual(at_30["over"], 1, "only 60 ms misses a 33.333 ms budget")
        self.assertEqual(at_30["hitches"], 0, "and nothing is over 66.667 ms")
        at_120 = summary.distribute(self.VALUES, summary.parse_budget("120", None))
        self.assertEqual(at_120["over"], 3, "a 8.333 ms budget is missed by 20, 32 and 60 ms")
        self.assertEqual(at_120["hitches"], 3, "and its hitch line is 16.667 ms")

    def test_the_mean_is_reported_but_never_the_answer(self) -> None:
        text = summary.verdict_text(
            summary.distribute(self.VALUES, summary.parse_budget(None, None)),
            summary.parse_budget(None, None),
        )
        self.assertIn("over budget", text)
        self.assertIn("3 of 4 frame(s) (75.0%) miss it", text)
        self.assertIn("1 hitch(es) (25.0%) over 33.333 ms", text)

    def test_a_series_inside_its_budget_says_so(self) -> None:
        quick = [4.0, 8.0, 12.0, 16.0]
        text = summary.verdict_text(
            summary.distribute(quick, summary.parse_budget(None, None)),
            summary.parse_budget(None, None),
        )
        self.assertIn("within budget", text)
        self.assertIn("slowest 16.000 ms", text)

    def test_no_frames_have_no_distribution(self) -> None:
        with self.assertRaises(ValueError):
            summary.distribute([], summary.parse_budget(None, None))


class TestTheHistogram(unittest.TestCase):
    def test_the_bins_double_from_half_a_budget_and_the_last_one_is_open(self) -> None:
        weekend = summary.histogram([20.0, 60.0, 8.0, 32.0], 16.6666667)
        self.assertEqual(
            [(bin.low_ms, bin.high_ms) for bin in weekend][:3],
            [(0.0, 16.6666667 / 2), (16.6666667 / 2, 16.6666667),
             (16.6666667, 16.6666667 * 2)],
        )
        self.assertIsNone(weekend[-1].high_ms, "the last bin catches everything above it")
        self.assertEqual([bin.frames for bin in weekend], [1, 0, 2, 1])
        self.assertEqual(sum(bin.frames for bin in weekend), 4)

    def test_a_value_above_every_edge_lands_in_the_open_bin(self) -> None:
        bins = summary.histogram([10.0, 100000.0], 16.6666667)
        self.assertEqual(bins[-1].frames, 1)
        self.assertIn("and over", bins[-1].label())

    def test_the_ladder_is_capped_however_large_the_outlier(self) -> None:
        bins = summary.histogram([1e12], 16.6666667)
        self.assertEqual(len(bins), summary.HISTOGRAM_MAX_BINS)
        self.assertEqual(bins[-1].frames, 1)

    def test_no_values_land_in_any_bin(self) -> None:
        bins = summary.histogram([], 16.6666667)
        self.assertEqual([bin.frames for bin in bins], [0, 0])
        self.assertIsNone(bins[-1].high_ms, "the last bin stays open even with nothing to count")

    def test_the_bars_scale_to_the_busiest_bin_and_empty_bins_have_none(self) -> None:
        lines = summary.histogram_lines(
            summary.histogram([20.0, 60.0, 8.0, 32.0], 16.6666667)
        )
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].rstrip().endswith("#"), lines[0])
        self.assertNotIn("#", lines[1], "an empty bin has no bar")
        self.assertTrue(lines[2].rstrip().endswith("#" * summary.BAR_WIDTH), lines[2])
        self.assertEqual(lines[2].count("#"), summary.BAR_WIDTH)


class TestTheSeries(unittest.TestCase):
    def test_the_fixtures_series_is_the_thread_its_frames_belong_to(self) -> None:
        model = _model(work_trace())
        series = summary.series_of(model)
        self.assertIsNotNone(series)
        assert series is not None
        self.assertEqual((series.tid, series.type), (2, 0))
        self.assertEqual(series.label, "tid 2 (GameThread)")
        self.assertEqual(len(series.rows), 4)
        self.assertAlmostEqual(series.span_s or 0.0, 0.12, places=6)
        self.assertEqual(series.describe(), "4 frame(s) on tid 2 (GameThread), frame type 0")

    def test_the_busiest_thread_wins_and_tid_asks_for_another(self) -> None:
        model: Dict[str, Any] = {
            "frames": [
                _frame(0, 2, 100, 200), _frame(1, 2, 200, 300), _frame(2, 2, 300, 400),
                _frame(3, 98, 100, 250),
            ],
            "threads": [{"tid": 2, "name": "GameThread"}, {"tid": 98, "name": "RenderThread 0"}],
            "session": {"start_cycle": 0, "cycle_frequency": 1000},
        }
        chosen = summary.series_of(model)
        assert chosen is not None
        self.assertEqual(chosen.tid, 2)
        other = summary.series_of(model, 98)
        assert other is not None
        self.assertEqual((other.tid, other.type), (98, 0))
        self.assertEqual(len(other.rows), 1)
        self.assertAlmostEqual(other.span_s or 0.0, 0.15, places=6)

    def test_a_frame_type_is_not_mixed_into_another_series(self) -> None:
        model: Dict[str, Any] = {
            "frames": [_frame(0, 2, 100, 200, frame_type=0), _frame(1, 2, 100, 200, frame_type=1)],
            "threads": [],
        }
        chosen = summary.series_of(model)
        assert chosen is not None
        self.assertEqual(chosen.type, 0, "the tie goes to the lower type, deterministically")

    def test_a_capture_without_frames_has_no_series(self) -> None:
        self.assertIsNone(summary.series_of({"frames": []}))
        self.assertIsNone(summary.series_of({"frames": [_frame(0, 2, 1, 2)]}, tid=98))

    def test_a_thread_without_a_name_is_still_named(self) -> None:
        model: Dict[str, Any] = {
            "frames": [_frame(0, 44, 0, 10)], "threads": [{"tid": 44, "name": ""}],
        }
        series = summary.series_of(model)
        assert series is not None
        self.assertEqual(series.label, "tid 44")

    def test_a_frame_time_is_a_cycle_span_over_the_captures_frequency(self) -> None:
        model: Dict[str, Any] = {"session": {"cycle_frequency": 1000000}}
        rows = [_frame(0, 2, 1000, 21000), _frame(1, 2, 21000, 22000)]
        self.assertEqual(summary.times_ms(model, rows), [20.0, 1.0])

    def test_cycles_are_not_milliseconds_without_a_frequency(self) -> None:
        self.assertIsNone(summary.times_ms({"session": {}}, [_frame(0, 2, 1000, 2000)]))
        self.assertIsNone(summary.times_ms(
            {"session": {"cycle_frequency": 0}}, [_frame(0, 2, 1000, 2000)]
        ))

    def test_the_work_of_a_frame_is_found_by_thread_type_and_begin(self) -> None:
        model = _model(work_trace())
        work = summary.frame_work_of(model, 2, 0, 1020000)
        self.assertIsNotNone(work)
        assert work is not None
        self.assertEqual(work["cycles"], 60000)
        self.assertEqual(work["pairs"], 2)
        self.assertEqual(work["items"], [(7, 45000), (8, 20000)])
        self.assertIsNone(summary.frame_work_of(model, 2, 0, 999999))
        self.assertIsNone(summary.frame_work_of(model, 98, 0, 1020000))


class TestTheWorkText(unittest.TestCase):
    NAMES = {7: "Tick", 8: "FrameTime"}

    def test_a_frame_with_work_names_its_biggest_timers_with_their_share(self) -> None:
        row = FrameWorkRow(
            tid=2, type=0, begin_cycle=1020000, end_cycle=1080000, cycles=60000, pairs=2,
            items=[(7, 45000), (8, 20000)],
        )
        self.assertEqual(
            summary.work_text(row, self.NAMES, 1000000, 60000),
            "Tick 45.000 ms (75%); FrameTime 20.000 ms (33%)",
        )

    def test_an_inclusive_share_may_pass_a_hundred_percent(self) -> None:
        row = FrameWorkRow(
            tid=2, type=0, begin_cycle=0, end_cycle=1000, cycles=1000, pairs=2,
            items=[(7, 2000), (8, 500)],
        )
        self.assertIn("Tick 2.000 ms (200%)", summary.work_text(row, self.NAMES, 1000000, 1000))

    def test_a_spec_the_capture_never_declared_is_still_named(self) -> None:
        row = FrameWorkRow(
            tid=2, type=0, begin_cycle=0, end_cycle=1000, cycles=1000, pairs=1,
            items=[(99, 500)],
        )
        self.assertIn("spec 99 0.500 ms (50%)", summary.work_text(row, self.NAMES, 1000000, 1000))

    def test_only_the_biggest_three_are_named(self) -> None:
        row = FrameWorkRow(
            tid=2, type=0, begin_cycle=0, end_cycle=1000, cycles=1000, pairs=4,
            items=[(7, 400), (8, 300), (7, 200), (8, 100)],
        )
        self.assertEqual(len(summary.work_text(row, self.NAMES, 1000000, 1000).split("; ")), 3)

    def test_a_frame_whose_work_was_not_kept_says_so(self) -> None:
        empty = FrameWorkRow(
            tid=2, type=0, begin_cycle=0, end_cycle=1000, cycles=1000, pairs=0, items=[],
        )
        self.assertEqual(summary.work_text(None, self.NAMES, 1000000, 1000), summary.NO_WORK)
        self.assertEqual(summary.work_text(empty, self.NAMES, 1000000, 1000), summary.NO_WORK)
        self.assertEqual(summary.work_text(empty, self.NAMES, 0, 1000), summary.NO_WORK)


class TestTheCommand(UeiaTestCase):
    """`ueia summary`: the report over the fixture, and the exits a pipeline reads."""

    def _capture(self) -> str:
        return str(self.write_capture(work_trace()))

    def _table_rows(self, out: str) -> List[str]:
        """The rendered rows of the breakers table: header and rule dropped, blanks ignored."""
        lines = out.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("frame "))
        return [line for line in lines[start + 2:] if line.strip()]

    def test_the_report_of_the_fixture_by_hand(self) -> None:
        code, out, err = self.run_cli(["summary", self._capture()])
        self.assertEqual(code, 0)
        self.assertEqual(err, "", "table form keeps everything on stdout")
        self.assertIn("capture   : fixture.utrace, 0.120 s", out)
        self.assertIn(
            "frames    : 4 frame(s) on tid 2 (GameThread), frame type 0, spanning 0.120 s", out,
        )
        self.assertIn("budget    : 60 FPS = 16.667 ms; a hitch is a frame over 33.333 ms", out)
        self.assertIn(
            "time      : mean 30.000, min 8.000, p50 20.000, p95 60.000, p99 60.000, max 60.000 ms",
            out,
        )
        self.assertIn("verdict   : over budget -- 3 of 4 frame(s) (75.0%) miss it", out)
        self.assertIn("1 hitch(es) (25.0%) over 33.333 ms", out)
        self.assertIn("histogram :", out)
        self.assertRegex(out, r"0\.000\s+-\s+8\.333 ms\s+:\s+1\s+#")
        self.assertRegex(out, r"16\.667\s+-\s+33\.333 ms\s+:\s+2\s+#")
        self.assertRegex(out, r"33\.333 ms and over\s+:\s+1\s+#")
        self.assertIn("work      : 4 scope pair(s) attributed", out)

    def test_the_table_names_the_frames_that_break_the_budget_worst_first(self) -> None:
        code, out, _err = self.run_cli(["summary", self._capture()])
        self.assertEqual(code, 0)
        rows = self._table_rows(out)
        self.assertEqual(len(rows), 3, "three frames are over budget; the 8 ms one is not")
        self.assertEqual(rows[0].split()[:4], ["1", "0.020", "60.000", "3.60"])
        self.assertIn("Tick 45.000 ms (75%); FrameTime 20.000 ms (33%)", rows[0])
        self.assertEqual(rows[1].split()[:4], ["3", "0.088", "32.000", "1.92"])
        self.assertTrue(rows[1].rstrip().endswith("-"), "frame 3 has no work to name")
        self.assertEqual(rows[2].split()[:4], ["0", "0.000", "20.000", "1.20"])
        self.assertIn("FrameTime 8.000 ms (40%)", rows[2])

    def test_the_limit_caps_the_table_and_zero_lists_everything(self) -> None:
        one = self.run_cli(["summary", self._capture(), "--limit", "1"])[1]
        self.assertEqual(len(self._table_rows(one)), 1)
        every = self.run_cli(["summary", self._capture(), "--limit", "0"])[1]
        self.assertEqual(len(self._table_rows(every)), 3)

    def test_another_budget_moves_the_verdict_and_the_hitches(self) -> None:
        code, out, _err = self.run_cli(["summary", self._capture(), "--budget", "30"])
        self.assertEqual(code, 0)
        self.assertIn("budget    : 30 FPS = 33.333 ms; a hitch is a frame over 66.667 ms", out)
        self.assertIn("verdict   : over budget -- 1 of 4 frame(s) (25.0%) miss it", out)
        self.assertIn("0 hitch(es)", out)

    def test_a_budget_in_milliseconds_is_the_same_budget(self) -> None:
        _, out, _err = self.run_cli(["summary", self._capture(), "--budget-ms", "33.333"])
        self.assertIn("budget    : 30 FPS = 33.333 ms", out)
        self.assertIn("1 of 4 frame(s) (25.0%) miss it", out)

    def test_the_csv_form_is_the_table_alone_and_the_prose_moves_to_stderr(self) -> None:
        code, out, err = self.run_cli(["summary", self._capture(), "--format", "csv"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "frame,at s,ms,x budget,top work")
        self.assertIn("Tick 45.000 ms (75%)", out)
        self.assertNotIn("verdict", out, "the verdict is prose, and prose goes to stderr")
        self.assertIn("verdict   : over budget", err)
        self.assertIn("histogram :", err)

    def test_the_markdown_form_carries_the_same_table(self) -> None:
        code, out, err = self.run_cli(["summary", self._capture(), "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "| frame | at s | ms | x budget | top work |")
        self.assertIn("| 1 | 0.020 | 60.000 | 3.60 | Tick 45.000 ms (75%)", out)
        self.assertIn("verdict   : over budget", err)
        self.assertEqual(out.splitlines()[0].count("|"), 6, "five columns, one pipe each side")

    def test_a_tid_without_frames_cannot_be_summarised(self) -> None:
        code, out, _err = self.run_cli(["summary", self._capture(), "--tid", "98"])
        self.assertEqual(code, 2)
        self.assertIn(
            "frames    : none -- this capture carries no Misc.BeginFrame/EndFrame pair on tid 98",
            out,
        )
        self.assertIn(
            "hint      : a frame-time report needs a capture recorded with -trace=cpu,frame", out,
        )

    def test_a_capture_without_frames_cannot_be_summarised(self) -> None:
        capture = self.write_capture(_no_frames_trace(), name="plain.utrace")
        code, out, _err = self.run_cli(["summary", str(capture)])
        self.assertEqual(code, 2)
        self.assertIn("frames    : none -- this capture carries no Misc.BeginFrame/EndFrame pair", out)

    def test_frames_without_a_cycle_frequency_cannot_be_timed(self) -> None:
        capture = self.write_capture(_no_frequency_trace(), name="nofreq.utrace")
        code, out, _err = self.run_cli(["summary", str(capture)])
        self.assertEqual(code, 2)
        self.assertIn("frames    : 1 frame(s) on tid 2, frame type 0", out)
        self.assertIn("frequency : 0 -- the capture declares no cycle frequency", out)

    def test_bad_options_are_usage_errors(self) -> None:
        capture = self._capture()
        self.assertEqual(self.run_cli(["summary", capture, "--budget"])[0], 2)
        self.assertEqual(self.run_cli(["summary", capture, "--frequency", "60"])[0], 2)
        self.assertEqual(
            self.run_cli(["summary", capture, "--budget", "30", "--budget-ms", "33"])[0], 2,
        )
        self.assertEqual(self.run_cli(["summary", capture, "--budget", "0"])[0], 2)

    def test_the_same_capture_gives_the_same_report_twice(self) -> None:
        capture = self._capture()
        self.assertEqual(self.run_cli(["summary", capture]), self.run_cli(["summary", capture]))


if __name__ == "__main__":
    unittest.main()
