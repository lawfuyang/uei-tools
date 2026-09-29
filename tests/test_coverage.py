"""The interval arithmetic, and the frame measurement built on it.

Everything here is hermetic: intervals are handed in as `coverage.pack` writes them, so no capture is
needed to pin what the walk's timelines mean or what the model's per-frame occupancy rows are made
of -- including the two things the corpus cannot show quickly (a coarsened timeline, and a frame no
thread ever entered).
"""

from __future__ import annotations

import unittest
from typing import Dict, List, Optional, Sequence, Tuple

from testcase import UeiaTestCase

import coverage
from shapes import FrameRow


def frame(index: int, tid: int, begin: int, end: int, kind: int = 0) -> FrameRow:
    """A frame row as `build_model` makes one (occupancy columns are the walk's own business)."""
    return FrameRow(index=index, type=kind, tid=tid, begin_cycle=begin, end_cycle=end,
                    covered_cycles=None, wait_cycles=None)


def spans(*pairs: Tuple[int, int]) -> bytes:
    """A packed interval list from `(begin, end)` pairs."""
    flat: List[int] = []
    for begin, end in pairs:
        flat.append(begin)
        flat.append(end)
    return coverage.pack(flat)


class TestPacking(UeiaTestCase):
    """`pack`/`unpack`: the shape that crosses the walker boundary and lands in the model."""

    def test_a_list_survives_the_round_trip(self) -> None:
        self.assertEqual(coverage.unpack(coverage.pack([1, 2, 3, 1 << 40])), [1, 2, 3, 1 << 40])

    def test_nothing_packs_to_nothing(self) -> None:
        self.assertEqual(coverage.pack([]), b"")
        self.assertEqual(coverage.unpack(b""), [])

    def test_it_is_byte_exact_and_little_endian(self) -> None:
        self.assertEqual(coverage.pack([1]), b"\x01\x00\x00\x00\x00\x00\x00\x00")
        self.assertEqual(coverage.pack([0x0102030405060708]), bytes(range(8, 0, -1)))

    def test_pairs_and_flat_are_inverses(self) -> None:
        flat = [10, 20, 30, 40]
        self.assertEqual(coverage.pairs(flat), [(10, 20), (30, 40)])
        self.assertEqual(coverage.flat(coverage.pairs(flat)), flat)
        self.assertEqual(coverage.pairs([1]), [])


class TestClipAndTotal(UeiaTestCase):
    """`clip`/`total`: what a window sees of a timeline."""

    def test_a_span_wholly_inside_is_untouched(self) -> None:
        self.assertEqual(coverage.clip([(10, 20)], 0, 100), [(10, 20)])

    def test_a_span_wholly_outside_is_dropped(self) -> None:
        self.assertEqual(coverage.clip([(10, 20)], 20, 30), [])
        self.assertEqual(coverage.clip([(10, 20)], 0, 10), [])

    def test_a_span_crossing_an_edge_is_clipped_to_it(self) -> None:
        self.assertEqual(coverage.clip([(10, 20)], 15, 25), [(15, 20)])
        self.assertEqual(coverage.clip([(10, 20)], 5, 15), [(10, 15)])
        self.assertEqual(coverage.clip([(0, 100)], 40, 60), [(40, 60)])

    def test_only_the_overlapping_spans_are_returned(self) -> None:
        self.assertEqual(coverage.clip([(0, 5), (10, 20), (30, 40)], 8, 25), [(10, 20)])

    def test_total_is_the_cycles_covered(self) -> None:
        self.assertEqual(coverage.total([(0, 10), (20, 25)]), 15)
        self.assertEqual(coverage.total([]), 0)


class TestUnion(UeiaTestCase):
    """`merged`: the union of pieces, which is what "the threads that worked" is made of."""

    def test_overlapping_pieces_collapse(self) -> None:
        self.assertEqual(coverage.merged([(0, 10), (5, 15)]), [(0, 15)])

    def test_touching_pieces_collapse_too(self) -> None:
        self.assertEqual(coverage.merged([(0, 10), (10, 20)]), [(0, 20)])

    def test_pieces_are_sorted_even_when_handed_in_backwards(self) -> None:
        self.assertEqual(coverage.merged([(30, 40), (0, 10)]), [(0, 10), (30, 40)])

    def test_a_contained_piece_changes_nothing(self) -> None:
        self.assertEqual(coverage.merged([(0, 100), (20, 30)]), [(0, 100)])

    def test_empty_and_backwards_pieces_are_dropped(self) -> None:
        self.assertEqual(coverage.merged([(10, 10), (20, 15), (30, 40)]), [(30, 40)])
        self.assertEqual(coverage.merged([]), [])


class TestSubtract(UeiaTestCase):
    """`subtract`: "work that happened while nobody else worked" is exactly this."""

    def test_nothing_covering_leaves_own_whole(self) -> None:
        self.assertEqual(coverage.subtract([(0, 10)], []), [(0, 10)])

    def test_a_covered_head_leaves_the_tail(self) -> None:
        self.assertEqual(coverage.subtract([(0, 10)], [(0, 4)]), [(4, 10)])

    def test_a_covered_middle_splits_own(self) -> None:
        self.assertEqual(coverage.subtract([(0, 10)], [(4, 6)]), [(0, 4), (6, 10)])

    def test_own_wholly_covered_leaves_nothing(self) -> None:
        self.assertEqual(coverage.subtract([(0, 10)], [(-5, 15)]), [])

    def test_several_others_over_several_of_own(self) -> None:
        self.assertEqual(
            coverage.subtract([(0, 10), (20, 30)], [(2, 4), (22, 40)]),
            [(0, 2), (4, 10), (20, 22)],
        )

    def test_the_result_is_the_cycles_that_are_own_alone(self) -> None:
        remaining = coverage.subtract([(100, 200), (300, 400)], [(150, 320)])
        self.assertEqual(remaining, [(100, 150), (320, 400)])
        self.assertEqual(coverage.total(remaining), 130)


class TestSweep(UeiaTestCase):
    """`sweep`: covered cycles, peak concurrency, and the cycles two or more spans shared."""

    def test_two_overlapping_spans(self) -> None:
        self.assertEqual(coverage.sweep([(10, 20), (15, 25)]), (15, 2, 5))

    def test_two_spans_that_only_touch_do_not_overlap(self) -> None:
        self.assertEqual(coverage.sweep([(10, 20), (20, 30)]), (20, 1, 0))

    def test_a_nested_span_is_concurrency_two_for_its_own_cycles(self) -> None:
        self.assertEqual(coverage.sweep([(10, 30), (12, 14), (20, 22)]), (20, 2, 4))

    def test_three_spans_peak_at_three_and_share_what_they_share(self) -> None:
        coverage_cycles, peak, together = coverage.sweep([(0, 10), (2, 8), (4, 6)])
        self.assertEqual((coverage_cycles, peak, together), (10, 3, 6))

    def test_a_shared_edge_is_not_shared_time(self) -> None:
        self.assertEqual(coverage.sweep([(0, 5), (5, 5), (5, 10)]), (10, 1, 0))

    def test_nothing_at_all(self) -> None:
        self.assertEqual(coverage.sweep([]), (0, 0, 0))
        self.assertEqual(coverage.sweep([(10, 10)]), (0, 0, 0))


class TestFoldIntoFrames(UeiaTestCase):
    """`fold_into_frames`: which frames a thread's span landed in, and how much of it."""

    def _fold(self, pieces: Sequence[Tuple[int, int]],
              frames: Sequence[FrameRow]) -> Dict[int, List[Tuple[int, int]]]:
        return coverage.fold_into_frames(
            list(pieces), [int(row["begin_cycle"]) for row in frames],
            [int(row["end_cycle"]) for row in frames],
        )

    def test_a_span_inside_one_frame(self) -> None:
        frames = [frame(0, 2, 0, 100), frame(1, 2, 100, 200)]
        self.assertEqual(self._fold([(10, 20)], frames), {0: [(10, 20)]})

    def test_a_span_across_two_frames_is_clipped_into_both(self) -> None:
        frames = [frame(0, 2, 0, 100), frame(1, 2, 100, 200)]
        self.assertEqual(self._fold([(90, 110)], frames), {0: [(90, 100)], 1: [(100, 110)]})

    def test_a_span_outside_every_frame_lands_nowhere(self) -> None:
        frames = [frame(0, 2, 0, 100)]
        self.assertEqual(self._fold([(200, 300)], frames), {})

    def test_nested_frames_each_get_the_part_they_contain(self) -> None:
        """Two frame types on one thread overlap, and both are windows: neither is preferred."""
        frames = [frame(0, 2, 0, 100), frame(1, 2, 50, 150)]
        self.assertEqual(self._fold([(40, 60)], frames), {0: [(40, 60)], 1: [(50, 60)]})

    def test_pieces_stay_in_cycle_order_within_a_frame(self) -> None:
        frames = [frame(0, 2, 0, 100)]
        folded = self._fold([(10, 20), (30, 40), (50, 60)], frames)
        self.assertEqual(folded[0], [(10, 20), (30, 40), (50, 60)])


class TestMeasureFrames(UeiaTestCase):
    """`measure_frames`: the rows a parallelism report reads, from timelines and frames alone."""

    def _measure(self, frames: Sequence[FrameRow],
                 spans_by_tid: Dict[int, Tuple[bytes, bytes, bytes]],
                 coarsened: Optional[Dict[int, int]] = None
                 ) -> Tuple[List[dict], List[dict]]:
        rows, occupancy = coverage.measure_frames(list(frames), spans_by_tid, coarsened)
        return [dict(row) for row in rows], [dict(row) for row in occupancy]

    def test_nothing_measured_is_no_rows_at_all(self) -> None:
        frames = [frame(0, 2, 0, 100)]
        self.assertEqual(coverage.measure_frames(frames, {}), ([], []))
        self.assertEqual(coverage.measure_frames([], {2: (b"", b"", b"")}), ([], []))

    def test_a_thread_row_counts_the_whole_timeline(self) -> None:
        rows, _occupancy = self._measure(
            [frame(0, 2, 0, 100)],
            {2: (spans((10, 20), (30, 50)), spans((12, 15)), spans((14, 16)))},
            {2: 3},
        )
        self.assertEqual(rows[0]["tid"], 2)
        self.assertEqual(rows[0]["spans"], 2)
        self.assertEqual(rows[0]["busy_cycles"], 30)
        self.assertEqual(rows[0]["wait_cycles"], 3)
        self.assertEqual(rows[0]["lock_cycles"], 2)
        self.assertEqual(rows[0]["coarsened"], 3, "what the walk had to merge is passed through")

    def test_the_frame_rows_carry_the_measurements_of_the_whole_machine(self) -> None:
        """Two threads, one frame: busy/wait/lock rows, solo work, union, peak and contention.

        tid 2 works 0-100 with a wait 20-30 and a lock 40-50; tid 4 works 50-80 and takes a lock
        45-55. So: tid 2's work is [0,20) + [30,100) = 90 cycles, and 60 of it is solo -- [0,20),
        [30,50) and [80,100); the union of every thread's coverage is [0,100) = 100; the peak is two
        threads at 50..80; the lock overlap is 45..50 = 5 cycles.
        """
        frames = [frame(0, 2, 0, 100)]
        rows, occupancy = self._measure(frames, {
            2: (spans((0, 100)), spans((20, 30)), spans((40, 50))),
            4: (spans((50, 80)), b"", spans((45, 55))),
        })
        entry = occupancy[0]
        self.assertEqual(entry["frame"], 0)
        self.assertEqual(entry["tid"], 2)
        self.assertEqual([item["tid"] for item in entry["threads"]], [2, 4])
        self.assertEqual(entry["solo_cycles"], 60)
        self.assertEqual(entry["others_work_cycles"], 30, "tid 4's work, and it never waits")
        self.assertEqual(entry["all_cycles"], 100)
        self.assertEqual(entry["peak_workers"], 2)
        self.assertEqual(entry["contended_cycles"], 5)
        self.assertEqual([row["tid"] for row in rows], [2, 4])
        self.assertEqual(rows[1]["busy_cycles"], 30)

    def test_a_wait_on_another_thread_is_not_work_beside_the_frame_thread(self) -> None:
        """The corpus's shape: a render thread inside `WaitForTasks` must not hide solo work."""
        frames = [frame(0, 2, 0, 100)]
        _rows, occupancy = self._measure(frames, {
            2: (spans((0, 100)), b"", b""),
            98: (spans((0, 100)), spans((0, 100)), b""),
        })
        self.assertEqual(occupancy[0]["solo_cycles"], 100, "the other thread only waited")
        self.assertEqual(occupancy[0]["others_work_cycles"], 0)
        self.assertEqual(occupancy[0]["peak_workers"], 1)

    def test_a_frame_two_others_work_in_reports_the_union_not_the_sum(self) -> None:
        frames = [frame(0, 2, 0, 100)]
        _rows, occupancy = self._measure(frames, {
            2: (spans((0, 10)), b"", b""),
            4: (spans((20, 60)), b"", b""),
            5: (spans((40, 80)), b"", b""),
        })
        self.assertEqual(occupancy[0]["others_work_cycles"], 60, "[20,60) and [40,80) overlap")
        self.assertEqual(occupancy[0]["all_cycles"], 70, "[0,10) and [20,80)")
        self.assertEqual(occupancy[0]["solo_cycles"], 10, "no other thread worked in [0,10)")
        self.assertEqual(occupancy[0]["peak_workers"], 2)

    def test_a_thread_busy_outside_every_frame_keeps_its_row_and_no_frame_rows(self) -> None:
        frames = [frame(0, 2, 0, 100)]
        rows, occupancy = self._measure(frames, {
            2: (spans((0, 100)), b"", b""),
            9: (spans((500, 600)), b"", b""),
        })
        self.assertEqual([row["tid"] for row in rows], [2, 9])
        self.assertEqual(rows[1]["busy_cycles"], 100, "a fact about the capture, not a frame")
        self.assertEqual([item["tid"] for item in occupancy[0]["threads"]], [2])

    def test_every_frame_gets_a_row_in_the_model_s_order(self) -> None:
        frames = [frame(0, 2, 0, 100), frame(1, 98, 100, 200), frame(2, 2, 200, 300)]
        _rows, occupancy = self._measure(frames, {
            2: (spans((0, 50), (200, 250)), b"", b""),
            98: (spans((120, 180)), b"", b""),
        })
        self.assertEqual([entry["frame"] for entry in occupancy], [0, 1, 2])
        self.assertEqual([entry["tid"] for entry in occupancy], [2, 98, 2])
        self.assertEqual(occupancy[0]["solo_cycles"], 50)
        self.assertEqual(occupancy[1]["solo_cycles"], 60, "a frame of its own thread")
        self.assertEqual(occupancy[2]["solo_cycles"], 50)


if __name__ == "__main__":  # pragma: no cover - unittest discovery runs it
    unittest.main()
