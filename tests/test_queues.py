"""The current GPU channel: the decode, the queue report, and the command that prints it.

Every number in the fixtures is a hand-checkable microsecond (the session's frequency is
1,000,000 and its start cycle 1,000,000, so a timestamp is its own offset), and the expected
queues, passes, placements and verdicts are arithmetic rather than whatever the code happens to
return -- the same discipline `test_gpu` keeps for the legacy channel, one channel over.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, Optional

import bottleneck
import fixtures
import model as model_module
import queues
import summary
from testcase import UeiaTestCase


def decode_trace(data: bytes) -> Dict[str, Any]:
    """The model of fixture bytes, built in one process (no pool)."""
    import container
    import streams

    header = container.parse_header(data)
    packets, _anomalies = container.walk_packets(data, header)
    stream_set = streams.assemble(data, packets)
    built, _counts, _all = model_module.build_model(stream_set, 1)
    return dict(built)


class TestTheDecode(unittest.TestCase):
    """What `build_model` holds for a capture that carries the current channel."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.model = decode_trace(fixtures.gpu_queue_trace())

    def counts(self, key: str) -> int:
        return int(self.model["counts"].get(key, 0))

    def queue(self, queue_id: int) -> Optional[Dict[str, Any]]:
        for row in self.model["gpu_queues"]:
            if int(row["id"]) == queue_id:
                return dict(row)
        return None

    def test_the_session_records_the_channel_version(self) -> None:
        self.assertEqual(self.model["session"].get("gpu_channel_version"), 2)

    def test_the_queue_specs_are_decoded_with_their_names(self) -> None:
        direct = self.queue(0)
        copy = self.queue(2)
        self.assertIsNotNone(direct)
        self.assertIsNotNone(copy)
        assert direct is not None and copy is not None
        # "Direct" arrives as a WideString on an important record: full UTF-16, decoded as such.
        self.assertEqual(direct["name"], "Direct")
        self.assertEqual(copy["name"], "Copy")
        self.assertEqual((direct["gpu"], direct["index"], direct["type"]), (0, 0, 0))
        self.assertEqual((copy["gpu"], copy["index"], copy["type"]), (0, 0, 2))

    def test_the_unions_are_the_work_and_wait_time(self) -> None:
        direct = self.queue(0)
        copy = self.queue(2)
        assert direct is not None and copy is not None
        # 8 ms (1,002,000-1,010,000) + 6 ms (1,020,000-1,026,000); the nested span counts once.
        self.assertEqual(direct["busy_us"], 14000)
        self.assertEqual(direct["wait_us"], 2000, "one 2 ms wait; the negative one is dropped")
        self.assertEqual(copy["busy_us"], 500)

    def test_the_span_counts_say_what_was_completed(self) -> None:
        direct = self.queue(0)
        copy = self.queue(2)
        assert direct is not None and copy is not None
        self.assertEqual(direct["work_spans"], 3, "7 ms + 8 ms + 6 ms, nested included")
        self.assertEqual(direct["wait_spans"], 1)
        self.assertEqual(copy["work_spans"], 1)
        self.assertEqual(direct["boundaries"], 3)
        self.assertEqual(direct["last_frame"], 3)

    def test_the_lag_is_the_submit_to_start_distance(self) -> None:
        direct = self.queue(0)
        assert direct is not None
        self.assertEqual(direct["submits"], 4, "the zero-timestamp submit is not one of them")
        self.assertEqual(direct["lag_total_us"], 3000)
        self.assertEqual(direct["lag_max_us"], 1000)
        self.assertEqual(direct["lag_negative"], 0)
        copy = self.queue(2)
        assert copy is not None
        self.assertEqual(copy["lag_total_us"], 100)

    def test_the_draw_stats_accumulate(self) -> None:
        direct = self.queue(0)
        assert direct is not None
        self.assertEqual(direct["draws"], 120)
        self.assertEqual(direct["primitives"], 2400)

    def test_the_passes_are_the_named_breadcrumbs(self) -> None:
        by_spec = {int(row["spec"]): dict(row) for row in self.model["gpu_passes"]}
        self.assertEqual(set(by_spec), {1, 2})
        self.assertEqual(by_spec[1]["inclusive_us"], 7000, "ShadowPass, 1,002,000-1,009,000")
        self.assertEqual(by_spec[1]["calls"], 1)
        self.assertEqual(by_spec[1]["max_begin_us"], 1002000)
        self.assertEqual(by_spec[2]["inclusive_us"], 3000, "BasePass, nested inside ShadowPass")

    def test_the_spec_table_names_the_passes(self) -> None:
        specs = {int(row["spec"]): dict(row) for row in self.model["gpu_breadcrumb_specs"]}
        self.assertEqual(specs[1]["static_name"], "ShadowPass")
        self.assertEqual(specs[1]["fields"], 1, "the FieldNames blob is counted, not decoded")

    def test_the_kept_spans_are_the_union_in_order(self) -> None:
        spans = self.model["gpu_spans"]
        work = [(int(row["begin_us"]), int(row["end_us"])) for row in spans
                if row["kind"] == "work" and int(row["queue"]) == 0]
        self.assertEqual(work, [(1002000, 1010000), (1020000, 1026000)])
        wait = [(int(row["begin_us"]), int(row["end_us"])) for row in spans
                if row["kind"] == "wait" and int(row["queue"]) == 0]
        self.assertEqual(wait, [(1010000, 1012000)])

    def test_the_fences_name_who_waited_on_whom(self) -> None:
        fences = {(str(row["kind"]), int(row["queue"]), int(row["other"])): int(row["count"])
                  for row in self.model["gpu_fences"]}
        self.assertEqual(fences[("signal", 0, 0)], 1)
        self.assertEqual(fences[("wait", 2, 0)], 1)

    def test_every_refusal_is_counted(self) -> None:
        self.assertEqual(self.counts("gpu_queue_specs"), 2)
        self.assertEqual(self.counts("gpu_breadcrumb_specs"), 2)
        self.assertEqual(self.counts("gpu_queue_events"), 23)
        self.assertEqual(self.counts("gpu_frame_boundaries"), 3)
        self.assertEqual(self.counts("gpu_stats_events"), 1)
        self.assertEqual(self.counts("gpu_fence_events"), 2)
        self.assertEqual(self.counts("gpu_zero_timestamps"), 1)
        self.assertEqual(self.counts("gpu_work_unpaired"), 2, "an end with no begin, a begin left open")
        self.assertEqual(self.counts("gpu_negative_durations"), 1, "the backwards wait")
        self.assertEqual(self.counts("gpu_out_of_order"), 0)
        self.assertEqual(self.counts("gpu_work_spans"), 4)
        self.assertEqual(self.counts("gpu_wait_spans"), 1)
        self.assertEqual(self.counts("gpu_spans_coarsened"), 0)
        self.assertEqual(self.counts("gpu_passes_dropped"), 0)


class TestTheReport(unittest.TestCase):
    """`queues.report_of`: the per-frame placement, the passes, and what the notes say."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.model = decode_trace(fixtures.gpu_queue_trace())
        cls.series = summary.series_of(cls.model)

    def report(self, series: Optional[summary.Series] = None) -> queues.Report:
        return queues.report_of(self.model, self.series if series is None else series)

    def test_the_frames_carry_the_gpu_time_that_landed_in_them(self) -> None:
        report = self.report()
        self.assertIsNotNone(report.frames)
        assert report.frames is not None
        by_frame = {frame.frame: frame for frame in report.frames}
        # worst first: the frame with the most GPU busy time leads
        self.assertEqual([frame.frame for frame in report.frames], [0, 1, 2])
        self.assertAlmostEqual(by_frame[0].gpu_ms or 0.0, 8.5, places=6,
                               msg="8 ms of Direct work + 0.5 ms of Copy work")
        self.assertAlmostEqual(by_frame[0].gpu_wait_ms or 0.0, 2.0, places=6)
        self.assertAlmostEqual(by_frame[1].gpu_ms or 0.0, 6.0, places=6)
        self.assertIsNone(by_frame[2].gpu_ms, "no kept span lands in the last frame")

    def test_the_frames_carry_their_own_times(self) -> None:
        report = self.report()
        assert report.frames is not None
        first = report.frames[0]
        self.assertAlmostEqual(first.ms, 16.0, places=6)
        self.assertIsNotNone(first.at_s)
        assert first.at_s is not None
        self.assertAlmostEqual(first.at_s, 0.0, places=6)

    def test_the_placement_describes_its_own_fit(self) -> None:
        report = self.report()
        self.assertIsNotNone(report.placement)
        assert report.placement is not None
        # the kept spans are unions: Direct's two disjoint intervals plus Copy's one
        self.assertEqual((report.placement.placed, report.placement.total), (3, 3))
        self.assertIn("3 of 3 work span(s)", report.placement.describe())

    def test_the_passes_carry_when_their_biggest_span_ran(self) -> None:
        report = self.report()
        self.assertEqual([row.name for row in report.passes], ["ShadowPass", "BasePass"])
        self.assertAlmostEqual(report.passes[0].inclusive_ms, 7.0, places=6)
        self.assertAlmostEqual(report.passes[0].at_s or -1.0, 0.002, places=6)

    def test_a_timeline_that_lands_nowhere_is_refused(self) -> None:
        # shift every span an hour into the fake future: none of them can sit on a frame
        model = dict(self.model)
        model["gpu_spans"] = [
            dict(row, begin_us=int(row["begin_us"]) + 3_600_000_000,
                 end_us=int(row["end_us"]) + 3_600_000_000)
            for row in self.model["gpu_spans"]
        ]
        report = queues.report_of(model, self.series)
        self.assertIsNone(report.placement)
        self.assertIsNone(report.frames)
        self.assertTrue(any("could not be placed" in note for note in report.notes))

    def test_a_series_with_no_frequency_cannot_be_placed(self) -> None:
        model = json.loads(json.dumps(self.model))
        model["session"]["cycle_frequency"] = 0
        report = queues.report_of(model, self.series)
        self.assertIsNone(report.placement)
        self.assertTrue(any("no cycle frequency" in note for note in report.notes))

    def test_the_report_says_the_channel_version_and_counts_the_refusals(self) -> None:
        report = self.report()
        joined = "\n".join(report.notes)
        self.assertIn("version 2", joined)
        self.assertIn("unpaired work bracket: 2", joined)
        self.assertIn("negative duration: 1", joined)
        self.assertIn("zero-timestamp event: 1", joined)

    def test_no_breadcrumbs_means_no_pass_names(self) -> None:
        model = dict(self.model)
        model["gpu_passes"] = []
        model["gpu_breadcrumb_specs"] = []
        report = queues.report_of(model, None)
        self.assertEqual(report.passes, [])
        self.assertTrue(any("no breadcrumb specs" in note for note in report.notes))


class TestTheTables(unittest.TestCase):
    """The three tables, their caps, and their machine forms."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.model = decode_trace(fixtures.gpu_queue_trace())
        cls.report = queues.report_of(cls.model, summary.series_of(cls.model))

    def test_the_queues_table_has_one_row_per_queue(self) -> None:
        rows = queues.table_rows(self.report, "queues", 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][4], "Direct")
        self.assertEqual(rows[0][5], "14.000")
        self.assertEqual(rows[1][4], "Copy")

    def test_the_limit_caps_every_table(self) -> None:
        self.assertEqual(len(queues.table_rows(self.report, "queues", 1)), 1)
        self.assertEqual(len(queues.table_rows(self.report, "passes", 1)), 1)
        self.assertEqual(len(queues.table_rows(self.report, "frames", 2)), 2)
        self.assertEqual(len(queues.table_rows(self.report, "frames", 0)), 3)

    def test_the_passes_table_names_and_ranks(self) -> None:
        rows = queues.table_rows(self.report, "passes", 0)
        self.assertEqual([row[0] for row in rows], ["ShadowPass", "BasePass"])
        self.assertEqual(rows[0][1], "1")
        self.assertEqual(rows[0][2], "7.000")

    def test_the_frames_table_prints_a_dash_where_nothing_landed(self) -> None:
        rows = queues.table_rows(self.report, "frames", 0)
        self.assertEqual(rows[0][:4], ("0", "0.000", "16.000", "8.500"))
        self.assertEqual(rows[2][3], "-", "the last frame has no kept GPU span inside it")


class TestTheCommand(UeiaTestCase):
    """`ueia gpu`: the exit codes, the forms, and determinism."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.write_capture(fixtures.gpu_queue_trace(), "gpuq.utrace")

    def test_the_default_table_is_the_queues(self) -> None:
        code, out, err = self.run_cli(["gpu", str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn("queue 0    : Direct", out)
        self.assertIn("busy 14.000 ms", out)
        self.assertIn("queue  gpu  index", out)
        self.assertEqual(err, "")

    def test_the_frames_and_passes_tables_answer(self) -> None:
        code, out, _err = self.run_cli(["gpu", str(self.path), "--table", "frames"])
        self.assertEqual(code, 0)
        self.assertIn("8.500", out)
        code, out, _err = self.run_cli(["gpu", str(self.path), "--table", "passes"])
        self.assertEqual(code, 0)
        self.assertIn("ShadowPass", out)

    def test_the_csv_form_puts_the_prose_on_stderr(self) -> None:
        code, out, err = self.run_cli(["gpu", str(self.path), "--format", "csv"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("queue,gpu,index,type,name,busy ms"))
        self.assertIn("channel   :", err)

    def test_the_markdown_form(self) -> None:
        code, out, _err = self.run_cli(["gpu", str(self.path), "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertIn("| queue | gpu | index |", out)

    def test_the_limit_caps_the_table(self) -> None:
        code, out, _err = self.run_cli(["gpu", str(self.path), "--limit", "1"])
        self.assertEqual(code, 0)
        # the prose names both queues; only the table row is capped, so "Copy" appears once
        self.assertEqual(out.count("Copy"), 1)

    def test_an_unknown_table_is_a_usage_error(self) -> None:
        code, _out, err = self.run_cli(["gpu", str(self.path), "--table", "spans"])
        self.assertEqual(code, 2)
        self.assertIn("--table", err)

    def test_determinism_the_same_input_twice_is_byte_identical(self) -> None:
        first = self.run_cli(["gpu", str(self.path)])
        second = self.run_cli(["gpu", str(self.path)])
        self.assertEqual(first, second)

    def test_the_cache_never_changes_the_answer(self) -> None:
        import os

        import shapes

        cold = self.run_cli(["gpu", str(self.path)])
        self.assertEqual(cold[0], 0)
        # let the cache back in: one run stores, the next loads, and both must match the cold one
        saved = os.environ.pop(shapes.ENV_NO_CACHE)
        try:
            self.run_cli(["gpu", str(self.path)])
            warm = self.run_cli(["gpu", str(self.path)])
        finally:
            if saved is not None:
                os.environ[shapes.ENV_NO_CACHE] = saved
        self.assertEqual(cold, warm)


class TestTheAbsentChannel(UeiaTestCase):
    """No GPU data in either shape: exit 2 and the re-record line, never an empty answer."""

    def test_a_capture_without_any_gpu_events_is_skipped(self) -> None:
        path = self.write_capture(fixtures.demo_trace(), "plain.utrace")
        code, out, _err = self.run_cli(["gpu", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("no GpuProfiler events", out)
        self.assertIn("-trace=", out)

    def test_a_declared_channel_with_no_events_is_still_skipped(self) -> None:
        # the schema and the QueueSpecs arrive, but no work, no waits, no frames: nothing to say
        data = fixtures.build_trace(
            events_stream=(
                fixtures.new_event_record(16, "$Trace", "NewTrace",
                                          [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                                          flags=fixtures.EVENT_FLAG_NOSYNC)
                + fixtures.gpu_channel_schema()
            ),
            importants_stream=(
                fixtures.important_record(16, fixtures.pack("u64", 1000000)
                                          + fixtures.pack("u64", 1000000))
                + fixtures.important_record(
                    fixtures.gpu_queue_uid("QueueSpec"),
                    fixtures.pack("u32", 0) + fixtures.important_aux_block(
                        1, "Direct".encode("utf-16-le")))
            ),
        )
        path = self.write_capture(data, "declared.utrace")
        code, out, _err = self.run_cli(["gpu", str(path)])
        self.assertEqual(code, 2)
        self.assertIn("no GpuProfiler events", out)


class TestBottleneckOnTheQueueTimeline(UeiaTestCase):
    """The verdict reaches its GPU answer through the queue spans when no legacy frames exist."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.write_capture(fixtures.gpu_bound_trace(), "bound.utrace")

    def test_a_frame_over_budget_with_an_idle_thread_reads_gpu_bound(self) -> None:
        code, out, _err = self.run_cli(
            ["bottleneck", str(self.path), "--budget", "60"])
        self.assertEqual(code, 0)
        self.assertIn("gpu", out)
        self.assertIn("GPU timeline: 1 of 1 work span(s)", out)

    def test_the_verdict_carries_the_gpu_time(self) -> None:
        model = decode_trace(fixtures.gpu_bound_trace())
        report, _notes = bottleneck.classify(
            model, summary.parse_budget("60", None))
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.verdicts[0].verdict, "gpu")
        self.assertAlmostEqual(report.verdicts[0].gpu_ms or 0.0, 33.295, places=3)
        self.assertTrue(report.gpu_present)

    def test_the_gpu_command_reports_the_queues(self) -> None:
        code, out, _err = self.run_cli(["gpu", str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn("Direct", out)
        self.assertIn("busy 33.295 ms", out)
        self.assertIn("legacy frames 0", out)

    def test_verify_says_what_the_queue_walk_counted(self) -> None:
        code, out, _err = self.run_cli(["verify", str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn("gpu queues: 3 event(s) over 1 queue(s)", out)
        self.assertIn("1 work / 0 wait span(s)", out)


class TestSerialEqualsParallel(UeiaTestCase):
    """The queue rows cross the process boundary like every other share: `--jobs` cannot change a byte."""

    def test_the_model_is_identical_walked_in_one_process_or_two(self) -> None:
        data = fixtures.gpu_queue_trace()
        import container
        import streams

        header = container.parse_header(data)
        packets, _anomalies = container.walk_packets(data, header)
        stream_set = streams.assemble(data, packets)
        serial, _counts, _anomalies = model_module.build_model(stream_set, 1)
        parallel, _counts, _anomalies = model_module.build_model(stream_set, 2)
        self.assertEqual(json.dumps(serial, sort_keys=True), json.dumps(parallel, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
