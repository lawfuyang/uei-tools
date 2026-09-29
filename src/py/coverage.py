"""Cycle-interval arithmetic, and the per-frame occupancy it measures.

The walk records each thread's **coverage** -- the cycles it spent inside a `CpuProfiler` scope --
as merged intervals, in three sets: every scope (busy), the wait-named ones, and the lock-named
ones. This module is what those intervals are made of: pack/unpack for the walker boundary, the set
arithmetic a report needs (`clip`, `merged`, `subtract`, `sweep`), and `measure_frames`, which
turns every thread's timeline into the one measurement the parallelism report reads -- **what each
thread did inside each frame**.

Why the measurement is taken at build time rather than at report time: the model *is* the cache,
the cache is JSON (`cache.py`), and a capture's raw timelines -- the corpus's editor capture has
769,875 busy intervals, plus 240,201 wait and 36,509 lock ones -- have no business in it. What belongs is the answer: per frame, every thread's
busy/wait/lock cycles, the frame thread's **solo** cycles (work while no other thread worked), the
union of the rest, and the concurrency peaks. Those are bounded by the capture's frames, not by its
scope count, and a report that reads them cannot re-derive them wrong.

Three rules this module keeps, all from AGENTS.md:

* **Absent is not zero.** A capture that declared no timer specs has no timelines; `measure_frames`
  then returns two empty lists, and a report says "nothing was measured" rather than claiming an
  idle machine.
* **A sample says it is a sample.** Nothing here is sampled: a thread's whole timeline is folded in.
  What *is* bounded is the timeline itself (`SPAN_KEEP`), and the walk counts what it coarsened.
* **A wait is not work.** Every "working" measure here is `busy - wait`: a thread inside
  `WaitForTasks` is not a thread doing work beside the frame thread, which is the difference between
  a capture that is serial and one that merely has a second thread parked.

Intervals are half-open `[begin, end)` in cycles, sorted by begin and merged -- the invariants every
function here assumes, and what the corpus's probes confirmed the walk emits (the engine's scopes
are well-nested, so one interval per *outermost* span is the union of all of them).
"""

from __future__ import annotations

import array
import sys
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from shapes import FrameOccupancyRow, FrameRow, FrameThreadRow, ThreadSpanRow

#: How many intervals one thread's coverage timeline may keep, per set (busy, wait, lock). Beyond it
#: the walk stops opening intervals and merges what it sees into the one before, which *overstates*
#: coverage instead of dropping it -- the safe direction for a parallelism claim -- and counts the
#: spans it swallowed (`spans_coarsened`). A capture whose thread takes a scope per spin must not put
#: an unbounded list in the cache; the corpus's worst uncoarsened thread needs 120,846 (REFERENCE §6).
SPAN_KEEP = 262144

#: The three kinds a scope's name can read as, as the walk flags each spec (`model.span_kind`).
SPAN_WORK = 0
SPAN_WAIT = 1
SPAN_LOCK = 2


def pack(intervals: Sequence[int]) -> bytes:
    """A flat interval list as little-endian u64 bytes -- what crosses the walker boundary.

    Bytes rather than a list because the share is pickled between processes and held until the
    measurement runs: the editor capture's 1.05 M intervals are ~17 MB this way, and well over 70 MB
    as Python ints in lists.
    """
    if not intervals:
        return b""
    values = array.array("Q", intervals)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def unpack(data: bytes) -> List[int]:
    """The inverse of `pack`: a flat `[begin, end, begin, end, ...]` list."""
    if not data:
        return []
    values = array.array("Q")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tolist()


def pairs(flat: Sequence[int]) -> List[Tuple[int, int]]:
    """A flat list as `(begin, end)` tuples (the shape the arithmetic below works in)."""
    return [(flat[index], flat[index + 1]) for index in range(0, len(flat) - 1, 2)]


def flat(spans: Sequence[Tuple[int, int]]) -> List[int]:
    """`(begin, end)` tuples back as a flat list."""
    out: List[int] = []
    for begin, end in spans:
        out.append(begin)
        out.append(end)
    return out


def total(spans: Sequence[Tuple[int, int]]) -> int:
    """The cycles a merged span list covers."""
    return sum(end - begin for begin, end in spans)


def clip(spans: Sequence[Tuple[int, int]], low: int, high: int) -> List[Tuple[int, int]]:
    """The parts of a sorted, merged span list inside `[low, high)`."""
    out: List[Tuple[int, int]] = []
    for begin, end in spans:
        if end <= low:
            continue
        if begin >= high:
            break
        out.append((max(begin, low), min(end, high)))
    return out


def merged(pieces: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """A sorted, merged span list -- the union of whatever pieces were handed in."""
    out: List[Tuple[int, int]] = []
    for begin, end in sorted(pieces):
        if end <= begin:
            continue
        if out and begin <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((begin, end))
    return out


def subtract(own: Sequence[Tuple[int, int]],
             others: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """The parts of `own` no part of `others` covers (both sorted and merged).

    The measure "work that happened while nobody else worked" is this, and it is why a `WaitForTasks`
    span on another thread must not be counted as that thread working: with waits counted, the
    corpus's render thread parks through every frame and no work is ever solo.
    """
    out: List[Tuple[int, int]] = []
    index = 0
    count = len(others)
    for begin, end in own:
        cursor = begin
        while index < count and others[index][1] <= cursor:
            index += 1
        scan = index
        while scan < count and others[scan][0] < end:
            other_begin, other_end = others[scan]
            if other_begin > cursor:
                out.append((cursor, min(other_begin, end)))
            if other_end > cursor:
                cursor = min(other_end, end)
            if cursor >= end:
                break
            scan += 1
        if cursor < end:
            out.append((cursor, end))
    return out


def sweep(pieces: Sequence[Tuple[int, int]]) -> Tuple[int, int, int]:
    """`(cycles covered, peak concurrency, cycles at concurrency >= 2)` of a set of spans.

    Each event is one integer -- `cycle * 2 + 1` for a span beginning, `cycle * 2` for one ending --
    so one sort orders them by cycle *and* puts an end before a begin at the same cycle: two spans
    that merely touch are not an overlap. That distinction is what a contention claim is made of
    (one thread handing over to another is not two threads inside a scope together), and a signed
    cycle cannot express it: sorting signed values puts every end before every begin.
    """
    events: List[int] = []
    for begin, end in pieces:
        if end > begin:
            events.append((begin << 1) | 1)
            events.append(end << 1)
    events.sort()
    covered = 0
    together = 0
    peak = 0
    current = 0
    previous: Optional[int] = None
    for event in events:
        cycle = event >> 1
        if previous is not None and current > 0:
            covered += cycle - previous
            if current >= 2:
                together += cycle - previous
        if event & 1:
            current += 1
        else:
            current -= 1
        if current > peak:
            peak = current
        previous = cycle
    return covered, peak, together


def fold_into_frames(
    spans: Sequence[Tuple[int, int]],
    frame_begins: Sequence[int],
    frame_ends: Sequence[int],
) -> Dict[int, List[Tuple[int, int]]]:
    """Which frames each span overlaps, as `{frame index: [clipped spans]}`.

    A moving cursor rather than a binary search per span: the span list is sorted by begin, so the
    first frame that ends after it only ever moves forward. Frames may nest (two frame types, two
    threads), which is why every candidate is filtered by `end > begin` as well.

    The callers below fold three lists per thread through this and then build a thread's **work**
    clips from its busy and wait clips *per frame* rather than as one more pass over the thread:
    subtracting two long lists once and then walking the result costs a second full pass over the
    corpus's 770 k intervals, and subtracting the small per-frame lists costs nothing (measured on
    the corpus: 3.91 s for the whole measurement with the extra pass, 2.40 s without, REFERENCE §6).
    """
    count = len(frame_begins)
    out: Dict[int, List[Tuple[int, int]]] = {}
    cursor = 0
    for begin, end in spans:
        while cursor < count and frame_ends[cursor] <= begin:
            cursor += 1
        scan = cursor
        while scan < count and frame_begins[scan] < end:
            if frame_ends[scan] > begin:
                low = frame_begins[scan] if frame_begins[scan] > begin else begin
                high = frame_ends[scan] if frame_ends[scan] < end else end
                if high > low:
                    pieces = out.get(scan)
                    if pieces is None:
                        out[scan] = [(low, high)]
                    else:
                        pieces.append((low, high))
            scan += 1
    return out


def measure_frames(
    frames: Sequence[FrameRow],
    spans: Mapping[int, Tuple[bytes, bytes, bytes]],
    coarsened: Optional[Mapping[int, int]] = None,
) -> Tuple[List[ThreadSpanRow], List[FrameOccupancyRow]]:
    """Fold every thread's timeline into every frame it overlaps: the parallelism report's numbers.

    `spans` maps a tid to its packed `(busy, wait, lock)` interval lists. `frames` is the model's own
    frame list, already sorted and indexed -- the windows are the *capture's* frames, every thread's,
    because a worker pool has no frames of its own and its occupancy is only meaningful against
    somebody else's. `coarsened` (from the walk) says which threads hit `SPAN_KEEP`, so a report can
    name the rows whose coverage is an overstatement.

    Returns `(thread rows, frame rows)`. Both are empty when nothing was measured (no timer specs),
    and `thread rows` includes every thread that had any coverage -- a thread that is busy outside
    every frame is a fact a report needs, not a hole in it.
    """
    coarsened = coarsened or {}
    thread_rows: List[ThreadSpanRow] = []
    # every thread hands in a (possibly empty) triple, so "nothing was measured" is a mapping whose
    # every list is empty -- a capture with no timer specs, whose rows must be *absent*, not zero
    if not frames or not any(any(ranges) for ranges in spans.values()):
        return thread_rows, []
    frame_begins = [int(row["begin_cycle"]) for row in frames]
    frame_ends = [int(row["end_cycle"]) for row in frames]
    frame_tids = [int(row["tid"]) for row in frames]
    threads: Dict[int, Dict[int, List[int]]] = {}
    own_work: Dict[int, List[Tuple[int, int]]] = {}
    others_work: Dict[int, List[Tuple[int, int]]] = {}
    work_events: Dict[int, List[Tuple[int, int]]] = {}
    cover_events: Dict[int, List[Tuple[int, int]]] = {}
    lock_events: Dict[int, List[Tuple[int, int]]] = {}

    for tid in sorted(spans):
        busy_data, wait_data, lock_data = spans[tid]
        busy = pairs(unpack(busy_data))
        wait = pairs(unpack(wait_data))
        lock = pairs(unpack(lock_data))
        thread_rows.append(ThreadSpanRow(
            tid=tid, spans=len(busy), coarsened=int(coarsened.get(tid, 0)),
            busy_cycles=total(busy), wait_cycles=total(wait), lock_cycles=total(lock),
        ))
        busy_at = fold_into_frames(busy, frame_begins, frame_ends)
        wait_at = fold_into_frames(wait, frame_begins, frame_ends)
        lock_at = fold_into_frames(lock, frame_begins, frame_ends)
        for index, pieces in busy_at.items():
            entry = threads.get(index)
            if entry is None:
                entry = {}
                threads[index] = entry
            row = entry.get(tid)
            if row is None:
                row = [0, 0, 0]
                entry[tid] = row
            row[SPAN_WORK] += total(pieces)
            cover_events.setdefault(index, []).extend(pieces)
            others = wait_at.get(index)
            work = subtract(pieces, others) if others else pieces
            if work:
                work_events.setdefault(index, []).extend(work)
                target = own_work if frame_tids[index] == tid else others_work
                target.setdefault(index, []).extend(work)
        for index, pieces in wait_at.items():
            row = threads.get(index, {}).get(tid)
            if row is not None:
                row[SPAN_WAIT] += total(pieces)
        for index, pieces in lock_at.items():
            entry = threads.get(index)
            if entry is None:
                entry = {}
                threads[index] = entry
            row = entry.get(tid)
            if row is None:
                row = [0, 0, 0]
                entry[tid] = row
            row[SPAN_LOCK] += total(pieces)
            lock_events.setdefault(index, []).extend(pieces)

    occupancy: List[FrameOccupancyRow] = []
    for index in range(len(frames)):
        tid = frame_tids[index]
        table = threads.get(index, {})
        others_union = merged(others_work.get(index, []))
        own_union = merged(own_work.get(index, []))
        solo = total(subtract(own_union, others_union))
        # the union of everyone's coverage is a merge, not a sweep: no concurrency is asked of it
        all_cycles = total(merged(cover_events.get(index, [])))
        _work_cycles, peak_workers, _work_together = sweep(work_events.get(index, []))
        _lock_cycles, _lock_peak, contended = sweep(lock_events.get(index, []))
        occupancy.append(FrameOccupancyRow(
            frame=index, tid=tid,
            threads=[FrameThreadRow(tid=other, busy_cycles=entry[SPAN_WORK],
                                    wait_cycles=entry[SPAN_WAIT], lock_cycles=entry[SPAN_LOCK])
                     for other, entry in sorted(table.items())],
            solo_cycles=solo, others_work_cycles=total(others_union), all_cycles=all_cycles,
            peak_workers=peak_workers, contended_cycles=contended,
        ))
    return thread_rows, occupancy
