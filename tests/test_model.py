"""The session model: a whole fixture capture decoded end to end."""

from __future__ import annotations

import json
import pickle
import time
import unittest
from typing import Dict, List, Optional, Tuple, cast

from testcase import UeiaTestCase

import container
from model import (
    SessionModel,
    ThreadShare,
    _AUTO_WORKERS_MAX,
    _CONTEXT,
    _FRAME_WORK_KEEP,
    _FRAME_WORK_TOP,
    _PARALLEL_MIN_BYTES,
    _parallel_shares,
    _walk_tid,
    _worker_init,
    _worker_walk,
    _workers_for,
    build_model,
    seconds_for_cycle,
    zero_counts,
)
import schema
from shapes import UeiaError
import streams
from fixtures import (
    EVENT_FLAG_IMPORTANT,
    EVENT_FLAG_MAYBE_HAS_AUX,
    EVENT_FLAG_NOSYNC,
    build_trace,
    demo_trace,
    event,
    important_aux_block,
    important_record,
    new_event_record,
    pack,
    work_importants,
    work_schema,
    work_stream,
    work_trace,
)

IMPORTANT = EVENT_FLAG_IMPORTANT | EVENT_FLAG_NOSYNC
IMPORTANT_AUX = EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX | EVENT_FLAG_NOSYNC

#: What the walk's tests hand in as the capture's spec count: one past the highest id `_importants`
#: declares (7), with room for the test that uses spec 8 as well.
_SPEC_COUNT = 9


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


def _schema() -> bytes:
    return (
        new_event_record(16, "$Trace", "NewTrace",
                         [("StartCycle", "u64"), ("CycleFrequency", "u64"),
                          ("Endian", "u16"), ("PointerSize", "u8")], flags=EVENT_FLAG_NOSYNC)
        + new_event_record(17, "$Trace", "ThreadInfo",
                           [("ThreadId", "u32"), ("SystemId", "u32"), ("SortHint", "i32"),
                            ("Name", "s")], flags=IMPORTANT_AUX)
        + new_event_record(18, "$Trace", "ThreadGroupBegin", [("Name", "s")], flags=IMPORTANT_AUX)
        + new_event_record(19, "$Trace", "ThreadTiming", [("BaseTimestamp", "u64")],
                           flags=EVENT_FLAG_NOSYNC)
        + new_event_record(20, "CpuProfiler", "EventSpec",
                           [("Id", "u32"), ("Name", "s"), ("File", "s"), ("Line", "u32")],
                           flags=IMPORTANT_AUX)
        + new_event_record(21, "CpuProfiler", "EventBatchV2", [("Data", "arr")],
                           flags=EVENT_FLAG_NOSYNC | EVENT_FLAG_MAYBE_HAS_AUX)
        + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(24, "Misc", "BookmarkSpec",
                           [("BookmarkPoint", "u64"), ("Line", "i32"), ("FormatString", "ws"),
                            ("FileName", "s")], flags=IMPORTANT_AUX)
        + new_event_record(25, "Misc", "Bookmark", [("Cycle", "u64"), ("BookmarkPoint", "u64")])
        + new_event_record(26, "Counters", "Spec",
                           [("Id", "u16"), ("Type", "u8"), ("DisplayHint", "u8"), ("Name", "s")],
                           flags=IMPORTANT_AUX)
        + new_event_record(27, "CsvProfiler", "RegisterCategory", [("Index", "u32"), ("Name", "s")],
                           flags=IMPORTANT_AUX)
        + new_event_record(28, "CsvProfiler", "DefineDeclaredStat",
                           [("StatId", "u32"), ("CategoryIndex", "u32"), ("Name", "s")],
                           flags=IMPORTANT_AUX)
        + new_event_record(29, "Stats", "Spec", [("Id", "u32"), ("Name", "s")], flags=IMPORTANT_AUX)
        + new_event_record(30, "Logging", "LogMessage", [("LogPoint", "u64"), ("Cycle", "u64")])
    )


def _importants() -> bytes:
    return (
        important_record(16, pack("u64", 1000000) + pack("u64", 1000000) + pack("u16", 0x524D)
                         + pack("u8", 8))
        + important_record(18, important_aux_block(0, b"GameThread"))
        + important_record(17, pack("u32", 2) + pack("u32", 4242) + pack("i32", 1)
                           + important_aux_block(3, b"Game"))
        + important_record(19, pack("u64", 500))
        # the fixed fields first (Id, Line), then the strings as aux blocks; an
        # important record writes each field's bytes one per character
        + important_record(20, pack("u32", 7) + pack("u32", 91)
                           + important_aux_block(1, b"Tick")
                           + important_aux_block(2, b"Game.cpp"))
        + important_record(24, pack("u64", 77) + pack("i32", 12)
                           + important_aux_block(2, b"LoadMap") + important_aux_block(3, b"Maps.cpp"))
        + important_record(26, pack("u16", 1) + pack("u8", 0) + pack("u8", 0)
                           + important_aux_block(3, b"FPS"))
        + important_record(27, pack("u32", 0) + important_aux_block(1, b"Game"))
        + important_record(28, pack("u32", 3) + pack("u32", 0) + important_aux_block(2, b"FrameTime"))
        + important_record(29, pack("u32", 5) + important_aux_block(1, b"Stat"))
    )


def _thread_stream() -> bytes:
    """Frames, a bookmark, one CPU batch and a log message, all on thread 2."""
    batch = (
        _varint((1400000 << 2) | 1) + _varint(7)     # begin at cycle 1,400,000
        + _varint((400000 << 2) | 0) + _varint(7)   # +400,000 below the last -> 1,800,000
    )
    return (
        event(22, pack("u64", 1100000) + pack("u8", 0), serial=5)
        + event(25, pack("u64", 1200000) + pack("u64", 77), serial=7)
        + event(23, pack("u64", 1300000) + pack("u8", 0), serial=8)
        + event(21, b"", aux=[(0, batch)], maybe_aux=True)
        + event(30, pack("u64", 1) + pack("u64", 1250000), serial=10)
    )


class TestBuildModel(UeiaTestCase):
    def _model(self) -> Tuple[SessionModel, Dict[str, int]]:
        data = build_trace(
            events_stream=_schema(), importants_stream=_importants(), threads={2: _thread_stream()}
        )
        header = container.parse_header(data)
        rows, _ = container.walk_packets(data, header)
        stream_set = streams.assemble(data, rows)
        model, counts, _anomalies = build_model(stream_set)
        return model, counts

    def test_session_comes_from_new_trace(self) -> None:
        model, _counts = self._model()
        session = model["session"]
        self.assertEqual(session.get("start_cycle"), 1000000)
        self.assertEqual(session.get("cycle_frequency"), 1000000)
        self.assertEqual(session.get("pointer_size"), 8)
        self.assertEqual(session.get("base_timestamp"), 500)
        self.assertEqual(session.get("last_cycle"), 1800000)

    def test_threads_get_names_and_groups_from_the_importants_stream(self) -> None:
        model, _counts = self._model()
        threads = {row["tid"]: row for row in model["threads"]}
        self.assertIn(2, threads)
        self.assertEqual(threads[2]["name"], "Game")
        self.assertEqual(threads[2]["group"], "GameThread")
        self.assertEqual(threads[2]["system_id"], 4242)
        self.assertEqual(threads[2]["packets"], 1)

    def test_timer_specs_carry_file_and_line(self) -> None:
        model, _counts = self._model()
        timers = model["timers"]
        self.assertEqual(len(timers), 1)
        self.assertEqual(
            (timers[0]["id"], timers[0]["name"], timers[0]["file"], timers[0]["line"]),
            (7, "Tick", "Game.cpp", 91),
        )

    def test_frames_pair_per_thread_and_type(self) -> None:
        model, counts = self._model()
        frames = model["frames"]
        self.assertEqual(len(frames), 1)
        self.assertEqual(
            (frames[0]["begin_cycle"], frames[0]["end_cycle"], frames[0]["type"]),
            (1100000, 1300000, 0),
        )
        self.assertEqual(counts["unpaired_frame_begin"], 0)
        self.assertEqual(counts["unpaired_frame_end"], 0)

    def test_bookmarks_join_their_spec_by_point(self) -> None:
        model, counts = self._model()
        bookmarks = model["bookmarks"]
        self.assertEqual(len(bookmarks), 1)
        self.assertEqual(
            (bookmarks[0]["name"], bookmarks[0]["file"], bookmarks[0]["line"]),
            ("LoadMap", "Maps.cpp", 12),
        )
        self.assertEqual(counts["unknown_bookmark_points"], 0)

    def test_batches_accumulate_cycles_per_thread(self) -> None:
        model, counts = self._model()
        threads = {row["tid"]: row for row in model["threads"]}
        self.assertEqual(threads[2]["batches"], 1)
        self.assertEqual(threads[2]["batch_records"], 2)
        self.assertEqual(threads[2]["first_cycle"], 1400000)
        self.assertEqual(threads[2]["last_cycle"], 1800000)
        self.assertEqual(counts["batch_records"], 2)

    def test_csv_definitions_and_counter_specs_are_kept(self) -> None:
        model, counts = self._model()
        self.assertEqual(model["csv_categories"], {"0": "Game"})
        self.assertEqual(len(model["csv_stats"]), 1)
        self.assertEqual(model["csv_stats"][0]["name"], "FrameTime")
        self.assertEqual(len(model["counters"]), 1)
        self.assertEqual(counts["stats_specs"], 1)
        self.assertEqual(len(model["schema"]), 15)

    def test_seconds_for_cycle_uses_the_frequency(self) -> None:
        model, _counts = self._model()
        self.assertEqual(seconds_for_cycle(model, 1000000), 0.0)
        self.assertEqual(seconds_for_cycle(model, 2000000), 1.0)
        self.assertEqual(seconds_for_cycle(model, 1100000), 0.1)

    def test_seconds_are_absent_without_a_frequency(self) -> None:
        model = cast(SessionModel, {"session": {}})
        self.assertIsNone(seconds_for_cycle(model, 5))

    def test_log_messages_and_uid_counts(self) -> None:
        model, counts = self._model()
        self.assertEqual(counts["log_messages"], 1)
        self.assertEqual(model["uid_counts"]["30"], 1)
        self.assertEqual(model["counts"]["events"], 5)

    def test_an_unknown_bookmark_point_is_counted_not_joined(self) -> None:
        schema = new_event_record(25, "Misc", "Bookmark", [("Cycle", "u64"), ("BookmarkPoint", "u64")])
        data = build_trace(
            events_stream=schema, threads={2: event(25, pack("u64", 5) + pack("u64", 999), serial=1)}
        )
        header = container.parse_header(data)
        rows, _ = container.walk_packets(data, header)
        model, counts, _ = build_model(streams.assemble(data, rows))
        self.assertEqual(model["bookmarks"], [])
        self.assertEqual(counts["unknown_bookmark_points"], 1)


def _batch(records: List[Tuple[int, Optional[int], bool]]) -> bytes:
    """One EventBatch blob from (absolute cycle, spec id or None, is-begin) records.

    The walker reads deltas and re-accumulates them (`cycle += last_cycle`), so the encoder emits
    differences: the first record is absolute, and every later one is behind the last by less than
    it -- which is what a real writer's deltas look like too.
    """
    blob = bytearray()
    previous = 0
    for cycle, spec_id, is_begin in records:
        delta = cycle - previous if previous else cycle
        previous = cycle
        blob += _varint((delta << 2) | (1 if is_begin else 0))
        if is_begin and spec_id is not None:
            blob += _varint(spec_id)
    return bytes(blob)


def _frame_pair(begin: int, end: int, serial: int, body: bytes = b"") -> bytes:
    """A frame pair with whatever ran inside it between the two events."""
    return (
        event(22, pack("u64", begin) + pack("u8", 0), serial=serial)
        + body
        + event(23, pack("u64", end) + pack("u8", 0), serial=serial + 1)
    )


def _batch_event(records: List[Tuple[int, Optional[int], bool]]) -> bytes:
    """One batch event carrying `records` -- what the walk decodes and pairs."""
    return event(21, b"", aux=[(0, _batch(records))], maybe_aux=True)


class TestFrameWork(UeiaTestCase):
    """The frame work attribution: what a frame was spent on, and what could not be attributed.

    The walk pairs batch records into scopes and lands each pair's inclusive cycles on the frame
    that was open when it ended; everything it cannot attribute is counted, never guessed:
    a pair that began before the frame, one with no spec id or an undeclared one, an end with no
    begin. What is kept is bounded (`_FRAME_WORK_KEEP`, `_FRAME_WORK_TOP`) because the model is a
    cache and the report asks about the tail.
    """

    def _model(self, data: bytes) -> Tuple[SessionModel, Dict[str, int]]:
        rows, _anomalies = container.walk_packets(data, container.parse_header(data))
        model, counts, _all = build_model(streams.assemble(data, rows))
        return model, counts

    def test_the_fixtures_frames_carry_their_work(self) -> None:
        model, counts = self._model(work_trace())
        work = {row["begin_cycle"]: row for row in model["frame_work"]}
        self.assertEqual(sorted(work), [1000000, 1020000, 1080000, 1088000])
        self.assertEqual(
            (work[1020000]["cycles"], work[1020000]["pairs"], work[1020000]["items"]),
            (60000, 2, [(7, 45000), (8, 20000)]),
            "45 ms of Tick containing 20 ms of FrameTime, inclusive both",
        )
        self.assertEqual((work[1000000]["cycles"], work[1000000]["items"]), (20000, [(8, 8000)]))
        self.assertEqual((work[1080000]["items"], work[1080000]["pairs"]), ([], 0))
        self.assertEqual(
            (counts["scope_pairs"], counts["scope_pairs_spanning"], counts["scope_pairs_no_spec"],
             counts["scope_ends_unpaired"], counts["scope_begins_unpaired"]),
            (3, 0, 0, 0, 0),
        )

    def test_a_pair_that_began_before_the_frame_is_counted_not_attributed(self) -> None:
        stream = (
            _batch_event([(900000, 7, True)])
            + _frame_pair(1000000, 1020000, 1, body=_batch_event([(1010000, None, False)]))
        )
        model, counts = self._model(self._trace({2: stream}))
        self.assertEqual(counts["scope_pairs_spanning"], 1)
        self.assertEqual(counts["scope_pairs"], 0, "the frame is not credited with it")
        self.assertEqual(model["frame_work"][0]["items"], [])

    def test_a_pair_with_an_undeclared_spec_is_counted(self) -> None:
        stream = _frame_pair(
            1000000, 1020000, 1,
            body=_batch_event([(1010000, 99, True), (1015000, None, False)]),
        )
        model, counts = self._model(self._trace({2: stream}))
        self.assertEqual(counts["scope_pairs_no_spec"], 1)
        self.assertEqual(counts["scope_pairs"], 0, "a spec with no name is not credited either")
        self.assertEqual(model["frame_work"][0]["items"], [])

    def test_an_end_with_no_begin_is_counted(self) -> None:
        stream = _frame_pair(1000000, 1020000, 1, body=_batch_event([(1010000, None, False)]))
        _model, counts = self._model(self._trace({2: stream}))
        self.assertEqual(counts["scope_ends_unpaired"], 1)

    def test_a_begin_that_never_ends_is_counted_at_the_end_of_the_stream(self) -> None:
        _model, counts = self._model(demo_trace())
        self.assertEqual(counts["scope_begins_unpaired"], 1, "demo_trace opens a scope and ends")
        self.assertEqual(counts["scope_pairs"], 0)

    def test_only_the_longest_frames_of_a_thread_keep_their_work(self) -> None:
        frames = bytearray()
        for index in range(20):
            begin = 1000000 + index * 20000
            frames += _frame_pair(begin, begin + (index + 1) * 10000, index * 2 + 1)
        model, _counts = self._model(self._trace({2: bytes(frames)}))
        kept = model["frame_work"]
        self.assertEqual(len(kept), _FRAME_WORK_KEEP)
        self.assertEqual(
            sorted(int(row["cycles"]) for row in kept),
            [50000 + 10000 * step for step in range(_FRAME_WORK_KEEP)],
            "the 20 frames are 10..200 ms wide; the 16 kept are 50..200",
        )
        self.assertEqual([int(row["begin_cycle"]) for row in kept],
                         sorted(int(row["begin_cycle"]) for row in kept))

    def test_a_frame_names_at_most_the_configured_number_of_specs(self) -> None:
        specs = [important_record(
            20, pack("u32", spec) + pack("u32", spec) + important_aux_block(1, b"Stat%d" % spec)
            + important_aux_block(2, b"S.cpp"),
        ) for spec in range(1, 9)]
        schema = (
            new_event_record(16, "$Trace", "NewTrace",
                             [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                             flags=EVENT_FLAG_NOSYNC)
            + new_event_record(20, "CpuProfiler", "EventSpec",
                               [("Id", "u32"), ("Name", "s"), ("File", "s"), ("Line", "u32")],
                               flags=EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX
                               | EVENT_FLAG_NOSYNC)
            + new_event_record(21, "CpuProfiler", "EventBatchV2", [("Data", "arr")],
                               flags=EVENT_FLAG_NOSYNC | EVENT_FLAG_MAYBE_HAS_AUX)
            + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
            + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        )
        records: List[Tuple[int, Optional[int], bool]] = []
        cycle = 1000000
        for spec in range(1, 9):
            records.append((cycle, spec, True))
            records.append((cycle + spec * 100, None, False))
            cycle += 1000
        stream = _frame_pair(1000000, 2000000, 1, body=_batch_event(records))
        data = build_trace(
            events_stream=schema,
            importants_stream=important_record(16, pack("u64", 1000000) + pack("u64", 1000000))
            + b"".join(specs),
            threads={2: stream},
        )
        model, counts = self._model(data)
        work = model["frame_work"][0]
        self.assertEqual(work["pairs"], 8)
        self.assertEqual(len(work["items"]), _FRAME_WORK_TOP)
        self.assertEqual([spec for spec, _cycles in work["items"]], [8, 7, 6, 5, 4, 3],
                         "the biggest by inclusive cycles, then by spec id")
        self.assertEqual(counts["scope_pairs"], 8)

    def test_frames_of_two_types_at_once_keep_their_own_totals(self) -> None:
        """The loop carries the current frame's totals in locals; nesting must not mix them.

        A thread with one frame type never hits this, but nothing in the format forbids a second
        type opening while the first is still open -- so the walk hands the live totals back to the
        frame that owns them when the current one changes, and takes them up again when it returns.
        """
        stream = (
            event(22, pack("u64", 1000000) + pack("u8", 0), serial=1)
            + _batch_event([(1005000, 7, True)])
            + event(22, pack("u64", 1010000) + pack("u8", 1), serial=2)
            + _batch_event([(1012000, 8, True), (1016000, None, False)])
            + event(23, pack("u64", 1020000) + pack("u8", 1), serial=3)
            + _batch_event([(1030000, None, False)])
            + event(23, pack("u64", 1040000) + pack("u8", 0), serial=4)
        )
        model, counts = self._model(self._trace({2: stream}))
        work = {(row["type"], row["begin_cycle"]): row for row in model["frame_work"]}
        self.assertEqual(work[(1, 1010000)]["items"], [(8, 4000)])
        self.assertEqual(work[(0, 1000000)]["items"], [(7, 25000)])
        self.assertEqual(counts["scope_pairs"], 2)

    def test_without_a_spec_count_the_walk_attributes_nothing(self) -> None:
        data = work_trace()
        rows, _anomalies = container.walk_packets(data, container.parse_header(data))
        stream_set = streams.assemble(data, rows)
        registry = schema.build_registry(stream_set.streams[0], [], zero_counts())
        share = _walk_tid(2, stream_set.streams[2], registry, {}, 0)
        self.assertEqual(share["frame_work"], [], "no timer specs, no attribution")
        self.assertEqual(share["counts"]["scope_pairs"], 0)
        self.assertEqual(len(share["frames"]), 4, "the frames themselves are still paired")
        with_specs = _walk_tid(2, stream_set.streams[2], registry, {}, 9)
        self.assertEqual(len(with_specs["frame_work"]), 4)

    def _trace(self, threads: Dict[int, bytes]) -> bytes:
        return build_trace(
            events_stream=work_schema(), importants_stream=work_importants(), threads=threads,
        )


def _worker_that_raises(unit: Tuple[int, bytes]) -> ThreadShare:
    """A worker that dies, for the pool's failure contract.

    Module-level on purpose: a `spawn`ed process receives the function *by name*, so a lambda or a
    closure would fail in the pool rather than in the assertion.
    """
    raise RuntimeError("worker %d exploded" % (unit[0],))


def _worker_that_never_answers(unit: Tuple[int, bytes]) -> ThreadShare:
    """A worker that takes longer than the stall budget, for the no-progress guard."""
    time.sleep(3.0)
    return _worker_walk(unit)


class TestParallelWalk(UeiaTestCase):
    """`--jobs`: the same walk spread over worker processes.

    Three things have to hold, and each of them is a way the pool can be wrong: a unit walked by a
    worker must equal the unit walked here, `--jobs` must not change a byte of the answer, and a pool
    that fails or stalls must be an error rather than a wait (the 2026-09-28 hang was neither
    reported nor finished: a worker initializer raised, and `ProcessPoolExecutor` respawned it).
    """

    def _stream_set(self) -> streams.StreamSet:
        data = build_trace(
            events_stream=_schema(),
            importants_stream=_importants(),
            # tid 3 carries frames and scopes as well, so the equality pin covers `frame_work`:
            # what a worker attributes must be what this process attributes, byte for byte
            threads={2: _thread_stream(), 3: _thread_stream() + work_stream(),
                     5: _thread_stream()},
        )
        rows, _anomalies = container.walk_packets(data, container.parse_header(data))
        return streams.assemble(data, rows)

    def _registry(self, stream_set: streams.StreamSet) -> schema.SchemaRegistry:
        return schema.build_registry(stream_set.streams[0], [], zero_counts())

    def _units(self, stream_set: streams.StreamSet) -> List[Tuple[int, bytes]]:
        return [
            (tid, stream_set.streams[tid])
            for tid in sorted(stream_set.streams)
            if tid not in (0, 1)
        ]

    def test_the_worker_walks_one_unit_exactly_as_the_serial_walk_does(self) -> None:
        stream_set = self._stream_set()
        registry = self._registry(stream_set)
        _worker_init(registry, {}, _SPEC_COUNT)
        self.addCleanup(_CONTEXT.clear)
        share = _worker_walk((2, stream_set.streams[2]))
        here = _walk_tid(2, stream_set.streams[2], registry, {}, _SPEC_COUNT)
        self.assertEqual(share, here)
        self.assertGreater(share["row"]["events"], 0)
        # the initargs have to cross a process boundary, so unpicklable is a build failure
        pickle.dumps((registry, {}, _SPEC_COUNT))

    def test_jobs_one_and_jobs_two_write_the_same_bytes(self) -> None:
        stream_set = self._stream_set()
        serial, serial_counts, _a = build_model(stream_set, 1)
        parallel, parallel_counts, _b = build_model(stream_set, 2)
        self.assertEqual(json.dumps(serial, sort_keys=True), json.dumps(parallel, sort_keys=True))
        self.assertEqual(serial_counts, parallel_counts)

    def test_a_worker_that_dies_fails_the_build_rather_than_losing_a_thread(self) -> None:
        stream_set = self._stream_set()
        with self.assertRaises(RuntimeError) as caught:
            _parallel_shares(self._registry(stream_set), self._units(stream_set), {}, 2,
                             worker=_worker_that_raises)
        self.assertIn("exploded", str(caught.exception))

    def test_a_worker_that_never_answers_is_reported_as_a_stall(self) -> None:
        stream_set = self._stream_set()
        with self.assertRaises(UeiaError) as caught:
            _parallel_shares(self._registry(stream_set), self._units(stream_set), {}, 1,
                             worker=_worker_that_never_answers, stall_timeout=0.5)
        self.assertIn("stopped making progress", str(caught.exception))

    def test_no_units_means_no_pool_at_all(self) -> None:
        self.assertEqual(_parallel_shares(self._registry(self._stream_set()), [], {}, 4), [])

    def test_the_auto_choice_is_serial_below_the_threshold_and_a_handful_above_it(self) -> None:
        small = [(2, b"x" * 1024)]
        self.assertEqual(_workers_for(small, 0), 1)

        big = [(tid, b"x" * _PARALLEL_MIN_BYTES) for tid in (2, 3, 4)]
        chosen = _workers_for(big, 0)
        self.assertGreater(chosen, 1)
        self.assertLessEqual(chosen, _AUTO_WORKERS_MAX)
        self.assertLessEqual(chosen, len(big))

        self.assertEqual(_workers_for(big, 1), 1, "an explicit 1 is serial")
        self.assertEqual(_workers_for(big, 99), 3, "more workers than units is pointless")
        self.assertEqual(_workers_for(big, 2), 2)


if __name__ == "__main__":
    unittest.main()
