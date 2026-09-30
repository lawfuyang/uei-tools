"""The GpuProfiler decoders: the legacy batch codec, the specs, and the current channel's queues.

Every batch below is built by hand, with the timestamps spelled out, so the expected busy times,
depths and pass lists are arithmetic rather than whatever the code happens to return. The legacy
layout is the engine's own backward-compatibility decoder's (`OldGpuProfilerTraceAnalysis.cpp:
143-204`): varint deltas, a begin carrying a uint32 spec id, an end carrying nothing. The current
channel's shapes are `RHI/Private/GpuProfilerTrace.cpp`'s, and the state machine mirrors the
engine reader's two stacks per queue (`GpuProfilerTraceAnalysis.cpp`).
"""

from __future__ import annotations

import unittest
from typing import Dict, List, Optional, Tuple

import gpu
from shapes import GpuFrameRow, GpuSpecRow


def varint(value: int) -> bytes:
    """LEB128, the encoding the GPU batch's timestamps use."""
    if value < 0:
        raise ValueError("a varint encodes an unsigned value; got %d (a delta that went backwards?)"
                         % (value,))
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def batch(records: List[Tuple[int, Optional[int], bool]]) -> bytes:
    """One `Data` array from `(delta microseconds, spec id or None, is_begin)` records."""
    out = bytearray()
    for delta, spec_id, is_begin in records:
        out += varint((delta << 1) | (1 if is_begin else 0))
        if is_begin:
            out += (spec_id or 0).to_bytes(4, "little")
    return bytes(out)


class TestTheBatchCodec(unittest.TestCase):
    """`decode_frame`: the timestamps accumulate, the outermost spans are the busy time."""

    def test_one_pair_is_its_own_span(self) -> None:
        frame = gpu.decode_frame(batch([(10, 7, True), (10, None, False)]), 1000)
        self.assertEqual((frame.busy_us, frame.events, frame.depth), (10, 1, 1))
        self.assertEqual(frame.passes, [(7, 10)])
        self.assertEqual((frame.unbalanced, frame.truncated), (0, 0))

    def test_a_nested_pair_is_counted_inclusive_and_the_outer_span_is_the_busy_time(self) -> None:
        frame = gpu.decode_frame(
            batch([(0, 1, True), (5, 2, True), (5, None, False), (5, None, False)]), 0,
        )
        self.assertEqual(frame.busy_us, 15, "the outer pair spans 0..15")
        self.assertEqual(frame.depth, 2)
        self.assertEqual(frame.passes, [(1, 15), (2, 5)], "inclusive, biggest first")

    def test_two_sequential_pairs_add_up(self) -> None:
        frame = gpu.decode_frame(
            batch([(0, 1, True), (10, None, False), (10, 2, True), (10, None, False)]), 500,
        )
        self.assertEqual(frame.busy_us, 20)
        self.assertEqual(frame.passes, [(1, 10), (2, 10)])
        self.assertEqual(frame.events, 2)

    def test_the_base_timestamp_only_primes_the_chain(self) -> None:
        early = gpu.decode_frame(batch([(5, 1, True), (5, None, False)]), 1000)
        late = gpu.decode_frame(batch([(5, 1, True), (5, None, False)]), 900000)
        self.assertEqual(early.busy_us, late.busy_us, "durations do not depend on the base")

    def test_a_truncated_varint_is_counted_and_the_readable_part_kept(self) -> None:
        good = batch([(10, 1, True), (10, None, False)])
        frame = gpu.decode_frame(good + b"\x80", 0)
        self.assertEqual(frame.busy_us, 10, "the pair before the rubbish is still evidence")
        self.assertEqual(frame.truncated, 1)

    def test_a_begin_without_its_four_bytes_is_counted(self) -> None:
        frame = gpu.decode_frame(batch([(10, 1, True)])[:-3], 0)
        self.assertEqual(frame.truncated, 1)
        self.assertEqual(frame.events, 0)

    def test_an_end_with_no_begin_is_unbalanced(self) -> None:
        frame = gpu.decode_frame(batch([(10, None, False)]), 0)
        self.assertEqual(frame.unbalanced, 1)
        self.assertEqual(frame.busy_us, 0)

    def test_an_event_left_open_is_unbalanced(self) -> None:
        frame = gpu.decode_frame(batch([(10, 1, True)]), 0)
        self.assertEqual(frame.unbalanced, 1, "one begin, no end")
        self.assertEqual(frame.depth, 1)

    def test_an_empty_batch_is_an_empty_frame(self) -> None:
        frame = gpu.decode_frame(b"", 1234)
        self.assertEqual((frame.busy_us, frame.events, frame.depth), (0, 0, 0))
        self.assertEqual((frame.unbalanced, frame.truncated, frame.passes), (0, 0, []))

    def test_an_implausible_span_is_refused_rather_than_believed(self) -> None:
        frame = gpu.decode_frame(batch([(0, 1, True), (gpu.MAX_FRAME_US + 1, None, False)]), 0)
        self.assertEqual(frame.busy_us, 0, "an hour-long GPU frame means misread bytes")
        self.assertEqual(frame.unbalanced, 1, "and the row says so, so nothing trusts it")
        self.assertEqual(frame.passes, [])

    def test_only_the_biggest_passes_are_kept(self) -> None:
        records: List[Tuple[int, Optional[int], bool]] = []
        for spec in range(1, 11):
            records.append((0, spec, True))
            records.append((spec, None, False))
        frame = gpu.decode_frame(batch(records), 0)
        self.assertEqual(len(frame.passes), gpu.PASS_KEEP)
        self.assertEqual([spec for spec, _us in frame.passes], [10, 9, 8, 7, 6, 5])


class TestTheSpecs(unittest.TestCase):
    """`spec`: the id-to-name map, whose names arrive as UTF-16 arrays."""

    def test_a_name_array_decodes_as_utf16(self) -> None:
        row = gpu.spec({"EventType": 82058, "Name": "SlateUI".encode("utf-16-le")})
        self.assertEqual(row, GpuSpecRow(id=82058, name="SlateUI"))

    def test_a_trailing_nul_is_dropped(self) -> None:
        row = gpu.spec({"EventType": 1, "Name": "Basepass\x00".encode("utf-16-le")})
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["name"], "Basepass")

    def test_a_name_that_is_not_utf16_does_not_cost_the_capture(self) -> None:
        row = gpu.spec({"EventType": 5, "Name": b"\xff\xfe\x41"})
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIn("\ufffd", row["name"])

    def test_a_spec_without_an_id_is_refused(self) -> None:
        self.assertIsNone(gpu.spec({"Name": "SlateUI".encode("utf-16-le")}))
        self.assertIsNone(gpu.spec({"EventType": "seventy"}))

    def test_a_spec_with_no_name_is_still_a_spec(self) -> None:
        row = gpu.spec({"EventType": 3})
        self.assertEqual(row, GpuSpecRow(id=3, name=""))

    def test_the_map_is_the_last_declaration_of_each_id(self) -> None:
        names = gpu.specs_of([
            GpuSpecRow(id=1, name="First"), GpuSpecRow(id=2, name="Other"),
            GpuSpecRow(id=1, name="Renamed"),
        ])
        self.assertEqual(names, {1: "Renamed", 2: "Other"})


class TestTheFrameRow(unittest.TestCase):
    """`frame_row`: the shape the model keeps, and the fields that mark a bad one."""

    def _row(self, data: bytes, base: int = 1000, number: int = 7) -> GpuFrameRow:
        row = gpu.frame_row(
            {"RenderingFrameNumber": number, "TimestampBase": base, "Data": data}, 98, data,
        )
        self.assertIsNotNone(row)
        assert row is not None
        return row

    def test_a_good_frame_carries_its_numbers(self) -> None:
        row = self._row(batch([(10, 3, True), (10, None, False)]))
        self.assertEqual(
            (row["tid"], row["number"], row["base_us"], row["busy_us"], row["events"],
             row["depth"], row["unbalanced"], row["truncated"], row["passes"]),
            (98, 7, 1000, 10, 1, 1, 0, 0, [(3, 10)]),
        )

    def test_a_frame_with_no_data_is_refused(self) -> None:
        self.assertIsNone(gpu.frame_row({"Data": None}, 2, None))
        self.assertIsNone(gpu.frame_row({}, 2, "not bytes"))

    def test_missing_scalars_read_as_zero_rather_than_failing(self) -> None:
        row = self._row(batch([(10, 1, True), (10, None, False)]), number=0)
        self.assertEqual(row["base_us"], 1000)
        bare = gpu.frame_row({"Data": b""}, 2, b"")
        self.assertIsNotNone(bare)
        assert bare is not None
        self.assertEqual(bare["number"], 0)


class TestTheClock(unittest.TestCase):
    def test_wall_seconds_places_a_timestamp_relative_to_the_anchor(self) -> None:
        self.assertAlmostEqual(gpu.wall_seconds(1000000, 1000000, 1.0, 5.0), 5.0)
        self.assertAlmostEqual(gpu.wall_seconds(3000000, 1000000, 1.0, 5.0), 7.0)
        self.assertAlmostEqual(gpu.wall_seconds(3000000, 1000000, 0.5, 5.0), 6.0)


def counts() -> Dict[str, int]:
    """A fresh walk-counter dict, the keys the queue decode writes included."""
    from model import zero_counts

    return zero_counts()


class TestTheQueueIds(unittest.TestCase):
    """`QueueId` unpacks into the GPU, the per-GPU index and the queue type."""

    def test_the_engine_reader_s_unpacking(self) -> None:
        # GPU 1, index 2, type 3 -- the bytes the reader reads out of `(id >> 8)`, `(id >> 16)`, `id`
        self.assertEqual(gpu.queue_id_parts(0x020103), (1, 2, 3))

    def test_the_zero_queue_is_gpu_zero_direct(self) -> None:
        self.assertEqual(gpu.queue_id_parts(0), (0, 0, 0))


class TestTheQueueSpecs(unittest.TestCase):
    """`queue_spec` and `breadcrumb_spec`: the id -> name maps the current channel resolves through."""

    def test_a_queue_spec_carries_its_name_and_its_id_parts(self) -> None:
        row = gpu.queue_spec({"QueueId": 2, "TypeString": "Copy"})
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual((row["id"], row["gpu"], row["index"], row["type"], row["name"]),
                         (2, 0, 0, 2, "Copy"))

    def test_a_queue_spec_without_an_id_is_refused(self) -> None:
        self.assertIsNone(gpu.queue_spec({"TypeString": "Direct"}))

    def test_a_breadcrumb_spec_keeps_both_names_and_the_field_blob_s_size(self) -> None:
        row = gpu.breadcrumb_spec({"SpecId": 7, "StaticName": "Pass", "NameFormat": "Pass %s",
                                   "FieldNames": b"\x00\x01"})
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["spec"], 7)
        self.assertEqual(row["fields"], 2)

    def test_a_breadcrumb_spec_without_an_id_is_refused(self) -> None:
        self.assertIsNone(gpu.breadcrumb_spec({"StaticName": "Pass"}))

    def test_breadcrumb_name_prefers_the_format_and_falls_back(self) -> None:
        specs = {1: {"spec": 1, "static_name": "Pass", "name_format": "Pass %s", "fields": 0}}
        self.assertEqual(gpu.breadcrumb_name(1, specs), "Pass %s")
        bare = {2: {"spec": 2, "static_name": "Pass", "name_format": "", "fields": 0}}
        self.assertEqual(gpu.breadcrumb_name(2, bare), "Pass")
        self.assertEqual(gpu.breadcrumb_name(3, bare), "spec 3")


class TestTheQueueWalk(unittest.TestCase):
    """`QueueWalk`: begin/end pairing, waits, breadcrumbs, and every refusal counted."""

    def _walk(self) -> gpu.QueueWalk:
        return gpu.QueueWalk(counts())

    def _flush_queues(self, walk: gpu.QueueWalk) -> Dict[int, Tuple[int, int]]:
        queues, _spans, _passes, _fences = walk.flush()
        return {int(row["id"]): (int(row["busy_us"]), int(row["wait_us"])) for row in queues}

    def _work(self, walk: gpu.QueueWalk, queue: int, top: int, cpu: int = 0) -> None:
        walk.event(gpu.BEGIN_WORK_EVENT, {"QueueId": queue, "GPUTimestampTOP": top,
                                          "CPUTimestamp": cpu})

    def _end(self, walk: gpu.QueueWalk, queue: int, bop: int) -> None:
        walk.event(gpu.END_WORK_EVENT, {"QueueId": queue, "GPUTimestampBOP": bop})

    def test_a_work_pair_is_its_own_busy_time(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 1000, 900)
        self._end(walk, 0, 2000)
        self.assertEqual(self._flush_queues(walk)[0], (1000, 0))

    def test_a_nested_span_counts_once_in_the_union(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 1000)
        self._work(walk, 0, 1500)
        self._end(walk, 0, 2500)
        self._end(walk, 0, 3000)
        self.assertEqual(self._flush_queues(walk)[0], (2000, 0), "the union, not the 2500 us sum")

    def test_two_queues_are_independent(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 1000)
        self._end(walk, 0, 2000)
        self._work(walk, 2, 1500)
        self._end(walk, 2, 2000)
        totals = self._flush_queues(walk)
        self.assertEqual(totals[0], (1000, 0))
        self.assertEqual(totals[2], (500, 0))

    def test_a_wait_is_its_own_interval(self) -> None:
        walk = self._walk()
        walk.event(gpu.WAIT_EVENT, {"QueueId": 0, "StartTime": 1000, "EndTime": 4000})
        self.assertEqual(self._flush_queues(walk)[0], (0, 3000))

    def test_a_backwards_wait_is_counted_and_dropped(self) -> None:
        walk = self._walk()
        walk.event(gpu.WAIT_EVENT, {"QueueId": 0, "StartTime": 4000, "EndTime": 1000})
        queues, _spans, _passes, _fences = walk.flush()
        self.assertEqual(int(queues[0]["wait_us"]), 0)
        self.assertEqual(walk.counts["gpu_negative_durations"], 1)

    def test_an_end_with_no_begin_is_counted(self) -> None:
        walk = self._walk()
        self._end(walk, 0, 2000)
        self.assertEqual(self._flush_queues(walk)[0], (0, 0))
        self.assertEqual(walk.counts["gpu_work_unpaired"], 1)

    def test_a_begin_left_open_is_counted_at_flush(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 1000)
        self.assertEqual(self._flush_queues(walk)[0], (0, 0))
        self.assertEqual(walk.counts["gpu_work_unpaired"], 1)

    def test_a_zero_timestamp_is_skipped_the_way_the_engine_skips_it(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 0, 900)
        self._end(walk, 0, 2000)
        self.assertEqual(walk.counts["gpu_zero_timestamps"], 1)
        self.assertEqual(walk.counts["gpu_work_unpaired"], 1, "the end now has no begin")

    def test_a_zero_cpu_submit_timestamp_measures_no_lag(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 2000, 0)
        self._end(walk, 0, 3000)
        queues, _spans, _passes, _fences = walk.flush()
        self.assertEqual(int(queues[0]["submits"]), 1)
        self.assertEqual(int(queues[0]["lag_total_us"]), 0, "0 is not determined, not zero lag")

    def test_a_negative_duration_is_counted_and_dropped(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 2000)
        self._end(walk, 0, 1000)
        self.assertEqual(self._flush_queues(walk)[0], (0, 0))
        self.assertEqual(walk.counts["gpu_negative_durations"], 1)

    def test_a_backwards_begin_is_out_of_order(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 2000)
        self._end(walk, 0, 3000)
        self._work(walk, 0, 1000)
        self.assertEqual(walk.counts["gpu_out_of_order"], 1)

    def test_the_lag_aggregates_over_the_submits(self) -> None:
        walk = self._walk()
        self._work(walk, 0, 2000, 1500)
        self._end(walk, 0, 3000)
        self._work(walk, 0, 5000, 5600)   # the CPU timestamp after the GPU start: negative
        self._end(walk, 0, 6000)
        queues, _spans, _passes, _fences = walk.flush()
        self.assertEqual(int(queues[0]["submits"]), 2)
        self.assertEqual(int(queues[0]["lag_total_us"]), 500)
        self.assertEqual(int(queues[0]["lag_max_us"]), 500)
        self.assertEqual(int(queues[0]["lag_negative"]), 1)

    def test_stats_and_boundaries_accumulate_on_the_row(self) -> None:
        walk = self._walk()
        walk.event(gpu.STATS_EVENT, {"QueueId": 0, "NumDraws": 10, "NumPrimitives": 100})
        walk.event(gpu.STATS_EVENT, {"QueueId": 0, "NumDraws": 5, "NumPrimitives": 50})
        walk.event(gpu.FRAME_BOUNDARY_EVENT, {"QueueId": 0, "FrameNumber": 4})
        queues, _spans, _passes, _fences = walk.flush()
        self.assertEqual(int(queues[0]["draws"]), 15)
        self.assertEqual(int(queues[0]["primitives"]), 150)
        self.assertEqual(int(queues[0]["boundaries"]), 1)
        self.assertEqual(int(queues[0]["last_frame"]), 4)

    def test_breadcrumbs_aggregate_per_spec(self) -> None:
        walk = self._walk()
        walk.event(gpu.BEGIN_BREADCRUMB_EVENT,
                   {"SpecId": 1, "QueueId": 0, "GPUTimestampTOP": 1000, "Metadata": b""})
        walk.event(gpu.END_BREADCRUMB_EVENT, {"QueueId": 0, "GPUTimestampBOP": 4000})
        walk.event(gpu.BEGIN_BREADCRUMB_EVENT,
                   {"SpecId": 1, "QueueId": 0, "GPUTimestampTOP": 5000, "Metadata": b""})
        walk.event(gpu.END_BREADCRUMB_EVENT, {"QueueId": 0, "GPUTimestampBOP": 7000})
        _queues, _spans, passes, _fences = walk.flush()
        self.assertEqual(len(passes), 1)
        self.assertEqual(int(passes[0]["calls"]), 2)
        self.assertEqual(int(passes[0]["inclusive_us"]), 5000)
        self.assertEqual(int(passes[0]["max_us"]), 3000)
        self.assertEqual(int(passes[0]["max_begin_us"]), 1000)

    def test_a_breadcrumb_end_with_no_begin_is_counted(self) -> None:
        walk = self._walk()
        walk.event(gpu.END_BREADCRUMB_EVENT, {"QueueId": 0, "GPUTimestampBOP": 4000})
        self.assertEqual(walk.counts["gpu_work_unpaired"], 1)

    def test_a_breadcrumb_without_a_spec_id_is_counted_and_still_aggregated(self) -> None:
        walk = self._walk()
        walk.event(gpu.BEGIN_BREADCRUMB_EVENT,
                   {"QueueId": 0, "GPUTimestampTOP": 1000, "Metadata": b""})
        walk.event(gpu.END_BREADCRUMB_EVENT, {"QueueId": 0, "GPUTimestampBOP": 2000})
        self.assertEqual(walk.counts["gpu_breadcrumb_no_spec"], 1)
        _queues, _spans, passes, _fences = walk.flush()
        self.assertEqual(len(passes), 1)

    def test_fences_aggregate_by_kind_and_queue(self) -> None:
        walk = self._walk()
        walk.event(gpu.SIGNAL_FENCE_EVENT, {"QueueId": 0, "CPUTimestamp": 1000, "Value": 1})
        walk.event(gpu.WAIT_FENCE_EVENT,
                   {"QueueId": 2, "CPUTimestamp": 1500, "QueueToWaitForId": 0, "Value": 1})
        walk.event(gpu.WAIT_FENCE_EVENT,
                   {"QueueId": 2, "CPUTimestamp": 2500, "QueueToWaitForId": 0, "Value": 2})
        _queues, _spans, _passes, fences = walk.flush()
        by_key = {(row["kind"], row["queue"], row["other"]): row["count"] for row in fences}
        self.assertEqual(by_key[("signal", 0, 0)], 1)
        self.assertEqual(by_key[("wait", 2, 0)], 2)

    def test_past_the_span_cap_the_union_is_overstated_and_counted(self) -> None:
        walk = self._walk()
        for index in range(gpu.QUEUE_SPAN_KEEP + 50):
            begin = index * 10
            self._work(walk, 0, begin)
            self._end(walk, 0, begin + 5)
        queues, spans, _passes, _fences = walk.flush()
        self.assertLessEqual(len(spans), gpu.QUEUE_SPAN_KEEP)
        self.assertGreater(walk.counts["gpu_spans_coarsened"], 0)
        # the union total is overstated by the folded tail: every 5 us span kept is disjoint, so
        # the kept intervals' sum is at least the cap's worth
        self.assertGreater(int(queues[0]["busy_us"]), 0)

    def test_absent_fields_are_refused_not_guessed(self) -> None:
        walk = self._walk()
        walk.event(gpu.BEGIN_WORK_EVENT, {"QueueId": 0})
        walk.event(gpu.END_WORK_EVENT, {"QueueId": 0})
        walk.event(gpu.WAIT_EVENT, {"QueueId": 0, "StartTime": 1})
        walk.event(gpu.FRAME_BOUNDARY_EVENT, {"QueueId": 0})
        walk.event(gpu.STATS_EVENT, {"QueueId": 0})
        walk.event(gpu.BEGIN_BREADCRUMB_EVENT, {"QueueId": 0, "GPUTimestampTOP": 5})
        walk.event(gpu.END_BREADCRUMB_EVENT, {"QueueId": 0})
        walk.event(gpu.SIGNAL_FENCE_EVENT, {})
        walk.event(gpu.WAIT_FENCE_EVENT, {"QueueId": 0})
        self.assertEqual(walk.counts["gpu_queue_events"], 9, "counted, every one")
        queues, _spans, _passes, fences = walk.flush()
        self.assertEqual(len(queues), 1, "the queue exists -- a stats event addressed it")
        row = queues[0]
        self.assertEqual((row["busy_us"], row["wait_us"], row["submits"], row["draws"],
                          row["boundaries"], row["last_frame"], row["work_spans"],
                          row["wait_spans"]), (0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(fences, [], "a fence wait that names no queue is not invented")


class TestTheUnion(unittest.TestCase):
    """`_union` and `union_total`: the interval arithmetic the queue totals stand on."""

    def test_touching_and_nested_intervals_merge(self) -> None:
        merged = gpu._union([(0, 10), (10, 20), (5, 8), (30, 40)])
        self.assertEqual(merged, [(0, 20), (30, 40)])

    def test_an_empty_list_is_an_empty_union(self) -> None:
        self.assertEqual(gpu._union([]), [])

    def test_union_total_filters_by_queue_and_kind(self) -> None:
        spans = [
            {"queue": 0, "kind": "work", "begin_us": 0, "end_us": 10},
            {"queue": 0, "kind": "wait", "begin_us": 0, "end_us": 10},
            {"queue": 2, "kind": "work", "begin_us": 0, "end_us": 10},
        ]
        self.assertEqual(gpu.union_total(spans, 0, "work"), 10)
        self.assertEqual(gpu.union_total(spans, 0, "wait"), 10)
        self.assertEqual(gpu.union_total(spans, 2, "work"), 10)
        self.assertEqual(gpu.union_total(spans, 2, "wait"), 0)


if __name__ == "__main__":
    unittest.main()
