"""The legacy GpuProfiler decoder: the batch codec, the specs, and what it does with rubbish.

Every batch below is built by hand, with the timestamps spelled out, so the expected busy times,
depths and pass lists are arithmetic rather than whatever the code happens to return. The layout is
the engine's own backward-compatibility decoder's (`OldGpuProfilerTraceAnalysis.cpp:143-204`):
varint deltas, a begin carrying a uint32 spec id, an end carrying nothing.
"""

from __future__ import annotations

import unittest
from typing import List, Optional, Tuple

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


if __name__ == "__main__":
    unittest.main()
