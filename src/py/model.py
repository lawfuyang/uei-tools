"""The decoded session: threads, timers, frames, bookmarks -- and their provenance.

One pass over the capture's three kinds of stream builds everything the
commands ask for:

* the **Events** stream gives the vocabulary (schema);
* the **Importants** stream gives the specs -- thread names and groups, timer
  specs with file:line, bookmark specs, counter specs, the CSV-profiler
  definitions, the session's own description;
* every **thread** stream gives what happened: frame pairs, CPU batch records
  (decoded to cycles), bookmarks, log messages, region spans, counter values.

Every number in the model was decoded through the capture's own schema, and
whatever could not be decoded is counted rather than guessed at.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict

import decode
import events
import gpu
from concurrent.futures import ProcessPoolExecutor, wait

import schema
from container import Anomaly
from streams import StreamSet
from shapes import (
    BookmarkRow,
    CounterSpecRow,
    CsvStatRow,
    EventTypeRow,
    FrameRow,
    FrameWorkRow,
    GpuFrameRow,
    GpuSpecRow,
    RawEvent,
    TID_EVENTS,
    TID_IMPORTANTS,
    TOOL_VERSION,
    ThreadRow,
    TimerRow,
    UeiaError,
)

MAX_ANOMALY_SAMPLES = 20

#: Name fragments that mark a scope as a **wait** rather than work, for the occupancy split a
#: bottleneck verdict needs: a thread inside `WaitForTasks` is not a thread that is working. This is
#: a heuristic on the capture's own vocabulary -- the engine names its waits `WaitFor*` (`WaitForTasks`,
#: `WaitForGPU`, `WaitUntilTasksComplete`, `WaitForRHIThread`) -- and reports that use it say so.
WAIT_NAME_MARKERS = ("wait",)

#: How many of a thread's longest frames keep their work attribution (`FrameWorkRow`), and how many
#: timer specs each of those rows names. Sixteen frames is more than any report lists (the summary's
#: table caps at ten by default, and what it lists is the worst), and six timers is more than it
#: prints (three), so the cache holds a little room around what is asked for -- and nothing else.
_FRAME_WORK_KEEP = 16
_FRAME_WORK_TOP = 6

_COUNTS_KEYS = (
    "new_events",
    "redefined",
    "events",
    "sync_events",
    "scopes",
    "aux_blocks",
    "unknown_uid",
    "batches",
    "batch_records",
    "coroutine_records",
    "unpaired_frame_begin",
    "unpaired_frame_end",
    "bookmark_specs",
    "bookmarks",
    "unknown_bookmark_points",
    "log_categories",
    "log_specs",
    "log_messages",
    "stats_specs",
    "counter_values",
    "serial_carried",
    "serial_min",
    "serial_max",
    # the frame work attribution: pair-to-frame attributions and what could not be attributed
    "scope_pairs",
    "scope_pairs_spanning",
    "scope_pairs_no_spec",
    "scope_pairs_unframed",
    "scope_ends_unpaired",
    "scope_begins_unpaired",
    # the legacy GPU channel: frames, their events, and the batches that did not read cleanly
    "gpu_frames",
    "gpu_events",
    "gpu_unreadable",
    "gpu_specs",
)


class SessionInfo(TypedDict, total=False):
    """What the session says about itself ($Trace.NewTrace, Diagnostics.Session2)."""

    start_cycle: int
    cycle_frequency: int
    endian: int
    pointer_size: int
    start_date_time: float
    base_timestamp: int
    platform: str
    app: str
    project: str
    branch: str
    build_version: str
    changelist: int
    configuration: str
    target: str
    last_cycle: int
    duration_cycles: int


class ChannelRow(TypedDict):
    """One channel announce: a channel and whether this capture had it on."""

    id: int
    name: str
    is_enabled: bool
    read_only: bool


class SessionModel(TypedDict):
    """Everything the parser core knows about one capture. JSON-serialisable.

    `build_model` fills every key, and a cached model is the same document
    read back, so the type is total; a caller that deliberately wants a
    partial one (a test of `seconds_for_cycle`, say) casts it.
    """

    tool_version: str
    session: SessionInfo
    channels: List[ChannelRow]
    counts: Dict[str, int]
    schema: List[EventTypeRow]
    threads: List[ThreadRow]
    timers: List[TimerRow]
    frames: List[FrameRow]
    frame_work: List[FrameWorkRow]
    gpu_specs: List[GpuSpecRow]
    gpu_frames: List[GpuFrameRow]
    bookmarks: List[BookmarkRow]
    counters: List[CounterSpecRow]
    counter_values: Dict[str, int]
    csv_categories: Dict[str, str]
    csv_stats: List[CsvStatRow]
    region_counts: Dict[str, int]
    uid_counts: Dict[str, int]
    anomaly_counts: Dict[str, int]
    anomaly_samples: List[str]


def zero_counts() -> Dict[str, int]:
    """A fresh set of walk counters; serial_min starts at -1, meaning "unset"."""
    counts = {key: 0 for key in _COUNTS_KEYS}
    counts["serial_min"] = -1
    return counts


def _empty_thread_row(tid: int) -> ThreadRow:
    """A thread's row with nothing on it: what a worker starts from (it has no stream set to ask)."""
    return ThreadRow(
        tid=tid,
        name="",
        group="",
        system_id=0,
        packets=0,
        bytes=0,
        events=0,
        sync_events=0,
        batches=0,
        batch_records=0,
        first_cycle=0,
        last_cycle=0,
        end_cycle=0,
    )


def _thread_row(tid: int, stream_set: StreamSet) -> ThreadRow:
    row = _empty_thread_row(tid)
    row["packets"] = stream_set.packets_per_tid.get(tid, 0)
    row["bytes"] = stream_set.bytes_per_tid.get(tid, 0)
    return row


def _important_values(
    row: EventTypeRow, stream: bytes, uid: int, offset: int, size: int
) -> Dict[str, object]:
    """Values of an important record: fixed part + the aux blocks inside it.

    `wide_as_bytes` is the important writer's own rule: a wide string there is
    one byte per character (`FImportantLogScope::FFieldSet<WIDECHAR>` truncates
    each character), not UTF-16.
    """
    end = offset + size
    fixed_end = min(offset + int(row["size"]), end)
    aux = events.walk_record_aux(stream, fixed_end, end)
    return decode.event_values(
        row, stream, RawEvent(uid, None, offset, size, aux, False), wide_as_bytes=True
    )


#: Below this many decoded stream bytes a build is one process's work: the pool's start-up costs
#: more than it saves. Measured on the corpus (49.9 MB decoded, 10.9 s serial cold) the ratio is
#: ~0.22 s per MB and the pool costs ~0.4 s to start, so the break-even is a couple of MB.
_PARALLEL_MIN_BYTES = 8 << 20

#: The most workers `--jobs 0` will start, for the reason spelled out in `_workers_for`.
_AUTO_WORKERS_MAX = 8

#: How long a single unit may produce no result before the pool is called stalled. A *no-progress*
#: budget, not a total one: measured, the corpus's biggest single unit (one thread's 14 MB, 5.5 M
#: batch records) walks in ~1.3 s in a worker, so two minutes of silence is a dead worker rather
#: than a busy one, and a capture ten times the corpus still has room.
_STALL_TIMEOUT = 120.0


class ThreadShare(TypedDict):
    """One thread's walk on its own: the part of the model no other thread can contribute to.

    Plain and small on purpose -- it is pickled between processes -- and the parent folds shares in
    ascending tid order, which is the order the serial walk visits them in.
    """

    tid: int
    row: ThreadRow
    counts: Dict[str, int]
    uid_counts: Dict[str, int]
    counter_values: Dict[str, int]
    region_counts: Dict[str, int]
    anomalies: List[Anomaly]
    frames: List[FrameRow]
    frame_work: List[FrameWorkRow]
    gpu_frames: List[GpuFrameRow]
    bookmarks: List[BookmarkRow]


class ModelAcc(TypedDict):
    """Where shares are folded together: the very structures the model is built from."""

    threads: Dict[int, ThreadRow]
    counts: Dict[str, int]
    uid_counts: Dict[str, int]
    counter_values: Dict[str, int]
    region_counts: Dict[str, int]
    anomalies: List[Anomaly]
    frames: List[FrameRow]
    frame_work: List[FrameWorkRow]
    gpu_frames: List[GpuFrameRow]
    bookmarks: List[BookmarkRow]


#: The counters a share's counts are *added* by; `serial_min`/`serial_max` are extremes instead
#: (`-1` meaning "nothing seen"), which is why they are not in this tuple.
_COUNTS_ADDITIVE = tuple(key for key in _COUNTS_KEYS if key not in ("serial_min", "serial_max"))

#: The context a worker rebuilds once per process rather than receiving once per unit.
_CONTEXT: Dict[str, Any] = {}


class _Window(object):
    """One frame of one thread: its window, and what the walk attributed to it.

    A plain object with slots, but touched once per *window* rather than once per scope pair: the hot
    loop keeps the running occupancy in locals and flushes them here when the cursor moves on
    (`_flush_window`). `totals` is None for a window that does not keep its work -- only a thread's
    longest frames do (`_FRAME_WORK_KEEP`) -- and a dict (spec id -> clipped cycles) for one that
    does.
    """

    __slots__ = ("type", "begin", "end", "covered", "waiting", "pairs", "totals")

    def __init__(self, frame_type: int, begin: int, end: int) -> None:
        self.type = frame_type
        self.begin = begin
        self.end = end
        self.covered = 0
        self.waiting = 0
        self.pairs = 0
        self.totals: Optional[Dict[int, int]] = None

    def work_row(self, tid: int, top: int) -> FrameWorkRow:
        """This frame as the model keeps it: its biggest specs by clipped cycles, then by id."""
        items: List[Tuple[int, int]] = []
        totals = self.totals
        if totals:
            ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
            items = [(int(spec), int(cycles)) for spec, cycles in ordered[:top]]
        return FrameWorkRow(
            tid=tid, type=self.type, begin_cycle=self.begin, end_cycle=self.end,
            cycles=self.end - self.begin, pairs=self.pairs, items=items,
        )

    def frame_row(self, tid: int, occupancy: bool) -> FrameRow:
        """This frame as the model keeps it, occupancy included when the capture could give it."""
        covered: Optional[int] = self.covered if occupancy else None
        waiting: Optional[int] = self.waiting if occupancy else None
        return FrameRow(
            index=0, type=self.type, tid=tid, begin_cycle=self.begin, end_cycle=self.end,
            covered_cycles=covered, wait_cycles=waiting,
        )


def _flush_window(window: _Window, occupancy: List[int], pairs: int) -> None:
    """Write the hot loop's running occupancy and pair count into the window they belong to."""
    window.covered = occupancy[0]
    window.waiting = occupancy[1]
    window.pairs = pairs


def _pair_windows(stream: bytes, tid: int, registry: schema.SchemaRegistry,
                  counts: Dict[str, int]) -> List[_Window]:
    """Pass 1: this thread's frame windows, in **end** order -- the order the attribution searches.

    Frames are paired (first begin with first end, per frame type) exactly as they always were, and
    nothing else in the stream is looked at, which is what makes a second pass affordable: the
    batches' payloads -- where the ten million records live -- are skipped, so this costs one header
    parse per event.

    A separate pass is not an optimisation, it is a correctness fix. The frame markers and the scope
    batches do **not** interleave in the stream: on the game capture (`game-pc-2`) the writer flushed all 771
    game frames in the first 8% of the thread's stream and the scope batches after them, and on
    `editor-pie-1` whole ranges of the stream carry one and not the other -- so "the frame that was
    open when the pair was read" is no frame at all. Cycles are what a pair belongs by, which is also
    what the engine's own analyser does (`FrameStatsHelper` clips an event to the frame interval it
    overlaps).
    """
    scratch = zero_counts()
    open_frames: Dict[int, List[int]] = {}
    windows: List[_Window] = []
    for event in events.iter_thread_events(stream, tid, registry, [], scratch):
        if event.b_scope:
            continue
        row = registry.get(event.uid)
        if row is None:
            continue
        full_name = str(row["full_name"])
        if full_name not in ("Misc.BeginFrame", "Misc.EndFrame"):
            continue
        values = decode.event_values(row, stream, event)
        cycle = decode.value_int(values, "Cycle")
        if cycle is None:
            continue
        frame_type = decode.value_int(values, "FrameType") or 0
        if full_name == "Misc.BeginFrame":
            open_frames.setdefault(frame_type, []).append(cycle)
        else:
            pending = open_frames.get(frame_type)
            if pending:
                windows.append(_Window(frame_type, pending.pop(0), cycle))
            else:
                counts["unpaired_frame_end"] += 1
    for pending in open_frames.values():
        counts["unpaired_frame_begin"] += len(pending)
    windows.sort(key=lambda window: (window.end, window.begin, window.type))
    return windows


def _walk_tid(
    tid: int,
    stream: bytes,
    registry: schema.SchemaRegistry,
    bookmark_specs: Dict[int, Tuple[str, str, int]],
    specs: Optional[bytes] = None,
) -> ThreadShare:
    """Walk one thread's stream: everything about that thread, and nothing about any other.

    The walk is per-thread by construction -- a thread's bytes are its own, its cycle fold is its
    own, and `events.iter_thread_events` never consults another thread -- which is what makes
    `--jobs` possible at all: this function *is* the unit a worker process runs, and what it returns
    is small enough to send back. It keeps the counters it wrote, so `_merge_share` is the only
    place where two threads meet.

    `specs` (one flag per timer spec the capture declared, `b"\x01"` when the spec's name reads as a
    wait like `WaitForTasks`; None when it declared none) turns on the frame attribution and
    occupancy. Two passes: `_pair_windows` collects this thread's frame windows, then this loop
    attributes every scope pair of every batch to the window whose span contains the pair's **end**
    cycle, clipped to that window, and merges it into the window's occupancy -- cycles inside any
    scope, and cycles inside a wait-named one, which is the difference between a thread that is
    working and a thread that is waiting. A pair that began before the frame it ended in is counted
    (`scope_pairs_spanning`); one that ended in no window at all is counted too
    (`scope_pairs_unframed` -- which is also every pair of a thread that has no frames at all),
    never guessed into the nearest frame.

    The **work** (the big specs by clipped cycles) is kept for the thread's longest frames only
    (`_FRAME_WORK_KEEP`), which pass 1 makes possible: their lengths are known before a single
    record is read. With `specs` None none of this runs, the frames are still paired and reported,
    and the walk costs what it always did.
    """
    trow = _empty_thread_row(tid)
    counts = zero_counts()
    uid_counts: Dict[str, int] = {}
    counter_values: Dict[str, int] = {}
    region_counts: Dict[str, int] = {"begins": 0, "ends": 0}
    anomalies: List[Anomaly] = []
    bookmarks: List[BookmarkRow] = []
    gpu_frames: List[GpuFrameRow] = []
    windows = _pair_windows(stream, tid, registry, counts)
    occupancy_on = specs is not None
    spec_count = len(specs) if specs is not None else 0
    # the work windows: the thread's longest frames, chosen now that their lengths are known
    if occupancy_on:
        for window in sorted(windows, key=lambda item: (
                -(item.end - item.begin), item.begin, item.type))[:_FRAME_WORK_KEEP]:
            if window.end > window.begin:
                window.totals = {}
    window_count = len(windows)
    last_cycle = 0
    stack: List[Tuple[Optional[int], int]] = []
    # the hot loop's own copies of the running occupancy and of the counters it writes: an attribute
    # lookup or a dict update per scope pair is what makes this expensive (`_flush_window` is what
    # hands them back, once per window)
    cursor = 0
    active = -1
    covered = 0
    waiting = 0
    cover_start = 0
    cover_end = -1
    wait_start = 0
    wait_end = -1
    pairs = 0
    attributed = 0
    spanning = 0
    no_spec = 0
    unframed = 0
    ends_unpaired = 0
    for event in events.iter_thread_events(stream, tid, registry, anomalies, counts):
        if event.b_scope:
            continue
        row = registry.get(event.uid)
        if row is None:
            continue
        # the walker already counted this event in counts["events"]
        trow["events"] = int(trow["events"]) + 1
        if event.serial is not None:
            trow["sync_events"] = int(trow["sync_events"]) + 1
            counts["serial_carried"] += 1
            serial_min = counts["serial_min"]
            if serial_min < 0 or event.serial < serial_min:
                counts["serial_min"] = event.serial
            if event.serial > counts["serial_max"]:
                counts["serial_max"] = event.serial
        uid_key = str(event.uid)
        uid_counts[uid_key] = uid_counts.get(uid_key, 0) + 1
        values = decode.event_values(row, stream, event)
        full_name = str(row["full_name"])

        if full_name in ("CpuProfiler.EventBatchV2", "CpuProfiler.EventBatchV3"):
            blob = values.get("Data")
            if not isinstance(blob, (bytes, bytearray)):
                anomalies.append((
                    "bad-batch", event.offset, event.size,
                    "a %s event carries no Data array" % (full_name,),
                ))
                continue
            records, coroutine_records, error = decode.decode_batch(bytes(blob))
            if error:
                anomalies.append((
                    "bad-batch", event.offset, len(blob),
                    "batch does not decode: %s" % (error,),
                ))
            if coroutine_records:
                counts["coroutine_records"] += coroutine_records
            counts["batches"] += 1
            counts["batch_records"] += len(records)
            trow["batches"] = int(trow["batches"]) + 1
            trow["batch_records"] = int(trow["batch_records"]) + len(records)
            first_cycle = int(trow["first_cycle"])
            for delta, spec_id, is_begin in records:
                cycle = delta
                if cycle < last_cycle:
                    cycle += last_cycle
                last_cycle = cycle
                if first_cycle == 0:
                    first_cycle = cycle
                if not occupancy_on:
                    continue
                if is_begin:
                    stack.append((spec_id, cycle))
                    continue
                if not stack:
                    ends_unpaired += 1
                    continue
                spec, begin = stack.pop()
                # the window whose span holds this pair's end: the cursor only ever moves forward,
                # because pairs are popped in end order
                while cursor < window_count and windows[cursor].end < cycle:
                    if active == cursor:
                        _flush_window(windows[cursor], [covered, waiting], pairs)
                        active = -1
                    cursor += 1
                if cursor >= window_count or cycle < windows[cursor].begin:
                    unframed += 1
                    continue
                window = windows[cursor]
                if active != cursor:
                    active = cursor
                    covered, waiting, pairs = 0, 0, 0
                    cover_start, cover_end = 0, -1
                    wait_start, wait_end = 0, -1
                if begin < window.begin:
                    begin = window.begin
                    spanning += 1
                span = cycle - begin
                if span > 0:
                    # the streaming union, inlined because it runs once per scope pair: an interval
                    # adds its prefix when it starts before the region and its tail when it ends
                    # after it. The region is empty while `end < start`.
                    if cover_end < cover_start:
                        covered = span
                        cover_start = begin
                        cover_end = cycle
                    else:
                        if begin < cover_start:
                            covered += cover_start - begin
                            cover_start = begin
                        if cycle > cover_end:
                            covered += cycle - (begin if begin > cover_end else cover_end)
                            cover_end = cycle
                    if (spec is not None and spec < spec_count and specs[spec]
                            and cycle > wait_end):
                        if wait_end < wait_start:
                            waiting = span
                            wait_start = begin
                            wait_end = cycle
                        else:
                            if begin < wait_start:
                                waiting += wait_start - begin
                                wait_start = begin
                            if cycle > wait_end:
                                waiting += cycle - (begin if begin > wait_end else wait_end)
                                wait_end = cycle
                if spec is None or spec >= spec_count:
                    no_spec += 1
                    continue
                attributed += 1
                totals = window.totals
                if totals is not None and span > 0:
                    totals[spec] = totals.get(spec, 0) + span
                    pairs += 1
            if last_cycle:
                trow["first_cycle"] = first_cycle
                trow["last_cycle"] = last_cycle
        elif full_name == "Misc.Bookmark":
            cycle = decode.value_int(values, "Cycle")
            point = decode.value_int(values, "BookmarkPoint")
            if cycle is None or point is None:
                continue
            spec = bookmark_specs.get(point)
            if spec is None:
                counts["unknown_bookmark_points"] += 1
                continue
            counts["bookmarks"] += 1
            bookmarks.append(BookmarkRow(
                cycle=cycle, point=point, name=spec[0], file=spec[1], line=spec[2],
            ))
        elif full_name == "CpuProfiler.EndThread":
            cycle = decode.value_int(values, "Cycle")
            if cycle is not None:
                trow["end_cycle"] = cycle
        elif full_name == "Logging.LogMessage":
            counts["log_messages"] += 1
        elif full_name == "Misc.RegionBegin":
            region_counts["begins"] += 1
        elif full_name == "Misc.RegionEnd":
            region_counts["ends"] += 1
        elif full_name in ("Counters.SetValueInt", "Counters.SetValueFloat"):
            counts["counter_values"] += 1
            counter_id = decode.value_int(values, "CounterId")
            if counter_id is not None:
                key = str(counter_id)
                counter_values[key] = counter_values.get(key, 0) + 1
        elif full_name == gpu.FRAME_EVENT:
            gpu_row = gpu.frame_row(values, tid, values.get("Data"))
            if gpu_row is None:
                anomalies.append((
                    "bad-batch", event.offset, event.size,
                    "a %s event carries no Data array" % (full_name,),
                ))
                continue
            counts["gpu_frames"] += 1
            counts["gpu_events"] += int(gpu_row["events"])
            if int(gpu_row["unbalanced"]) or int(gpu_row["truncated"]):
                counts["gpu_unreadable"] += 1
            gpu_frames.append(gpu_row)

    while cursor < window_count:
        if active == cursor:
            _flush_window(windows[cursor], [covered, waiting], pairs)
            active = -1
        cursor += 1
    if occupancy_on:
        counts["scope_pairs"] += attributed
        counts["scope_pairs_spanning"] += spanning
        counts["scope_pairs_no_spec"] += no_spec
        counts["scope_pairs_unframed"] += unframed
        counts["scope_ends_unpaired"] += ends_unpaired
        counts["scope_begins_unpaired"] += len(stack)
    frame_rows = [window.frame_row(tid, occupancy_on) for window in windows]
    frame_work = [window.work_row(tid, _FRAME_WORK_TOP) for window in windows
                  if window.totals is not None]

    return ThreadShare(
        tid=tid, row=trow, counts=counts, uid_counts=uid_counts, counter_values=counter_values,
        region_counts=region_counts, anomalies=anomalies, frames=frame_rows, frame_work=frame_work,
        gpu_frames=gpu_frames, bookmarks=bookmarks,
    )


def _merge_share(acc: "ModelAcc", share: ThreadShare) -> None:
    """Fold one thread's share into the build in progress: serial and parallel share this code.

    That is the whole point of extracting it: the serial walk and the pool produce the same shares
    and meet here, in ascending tid order (`executor.map` keeps its input's order), so `--jobs`
    cannot change a byte -- the anomalies and the frame and bookmark lists are extended in exactly
    the order the single-process walk would have produced them.
    """
    row = acc["threads"][share["tid"]]
    # the seven keys `_walk_tid` owns; the row already carries the important stream's names, packets
    # and bytes, which this walk never touches
    row["events"] = share["row"]["events"]
    row["sync_events"] = share["row"]["sync_events"]
    row["batches"] = share["row"]["batches"]
    row["batch_records"] = share["row"]["batch_records"]
    row["first_cycle"] = share["row"]["first_cycle"]
    row["last_cycle"] = share["row"]["last_cycle"]
    row["end_cycle"] = share["row"]["end_cycle"]
    for key in _COUNTS_ADDITIVE:
        acc["counts"][key] += share["counts"][key]
    seen_min = share["counts"]["serial_min"]
    if seen_min >= 0 and (acc["counts"]["serial_min"] < 0 or seen_min < acc["counts"]["serial_min"]):
        acc["counts"]["serial_min"] = seen_min
    if share["counts"]["serial_max"] > acc["counts"]["serial_max"]:
        acc["counts"]["serial_max"] = share["counts"]["serial_max"]
    for key, value in share["uid_counts"].items():
        acc["uid_counts"][key] = acc["uid_counts"].get(key, 0) + value
    for key, value in share["counter_values"].items():
        acc["counter_values"][key] = acc["counter_values"].get(key, 0) + value
    for key, value in share["region_counts"].items():
        acc["region_counts"][key] = acc["region_counts"].get(key, 0) + value
    acc["anomalies"].extend(share["anomalies"])
    acc["frames"].extend(share["frames"])
    acc["frame_work"].extend(share["frame_work"])
    acc["gpu_frames"].extend(share["gpu_frames"])
    acc["bookmarks"].extend(share["bookmarks"])


def _spec_flags(timers: Dict[int, TimerRow]) -> Optional[bytes]:
    """One byte per timer spec: 1 when the spec's name reads as a wait, 0 when it does not.

    The array doubles as the attribution switch -- its length is one past the highest spec id the
    capture declared, and None (no specs at all) turns the frame attribution and the occupancy off,
    because a capture that declared no timer specs has nothing to attribute.
    """
    if not timers:
        return None
    flags = bytearray(max(timers) + 1)
    for spec_id, row in timers.items():
        name = str(row["name"]).lower()
        if any(marker in name for marker in WAIT_NAME_MARKERS):
            flags[spec_id] = 1
    return bytes(flags)


def _worker_init(registry: schema.SchemaRegistry,
                 bookmark_specs: Dict[int, Tuple[str, str, int]],
                 specs: Optional[bytes]) -> None:
    """Hand a worker the read-only context (spawn keeps no memory of the parent's).

    It *receives* the registry rather than rebuilding it, and that is a scar, not a style choice.
    Rebuilding it needed the Events stream plus a full counts dict, and handing it `{}` made
    `build_registry` raise in every worker's initializer -- which `ProcessPoolExecutor` answers by
    replacing the dead worker and retrying the same task, for ever, with nothing in the parent's
    output to say so. Passing picklable data means the only way this can fail is in the parent, when
    the pool is constructed (a real hour-long hang, 2026-09-28; see also `_parallel_shares`).
    """
    _CONTEXT["registry"] = registry
    _CONTEXT["bookmark_specs"] = bookmark_specs
    _CONTEXT["specs"] = specs


def _worker_walk(unit: Tuple[int, bytes]) -> ThreadShare:
    """One work unit -- a thread id and its bytes -- walked in a worker process."""
    tid, stream = unit
    return _walk_tid(
        tid, stream, _CONTEXT["registry"], _CONTEXT["bookmark_specs"], _CONTEXT["specs"]
    )


def _parallel_shares(
    registry: schema.SchemaRegistry,
    units: List[Tuple[int, bytes]],
    bookmark_specs: Dict[int, Tuple[str, str, int]],
    workers: int,
    specs: Optional[bytes] = None,
    worker: Callable[[Tuple[int, bytes]], ThreadShare] = _worker_walk,
    stall_timeout: float = _STALL_TIMEOUT,
) -> List[ThreadShare]:
    """Walk every unit in a worker process; returns the shares in the units' order.

    Two guards, both from the same incident (an hour-long hang, 2026-09-28). The context is
    *pickled in* rather than rebuilt, so a mistake in it fails when the pool is built -- in this
    process, with a traceback -- instead of in a worker initializer the executor would silently
    replace and retry. And a unit that produces no result for `stall_timeout` seconds is reported as
    an error: `ProcessPoolExecutor` has no failing-fast mode, so "no progress" has to be *noticed*,
    and the timeout is a no-progress budget rather than a total one, so a big capture is never
    failed for taking a long time.

    `worker` and `stall_timeout` are parameters so the tests can drive both guards: a worker that
    raises, and a worker that never answers.
    """
    if not units:
        return []
    pool = ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(registry, bookmark_specs, specs),
    )
    futures: List[Any] = []
    try:
        futures = [pool.submit(worker, unit) for unit in units]
        shares: List[ThreadShare] = []
        for index, future in enumerate(futures):
            done, _pending = wait([future], timeout=stall_timeout)
            if not done:
                raise UeiaError(
                    "a worker stopped making progress: unit %d of %d produced no result in %.0f s "
                    "(%d done)" % (index + 1, len(units), stall_timeout, len(shares))
                )
            shares.append(future.result())
        return shares
    finally:
        # a raise must not wait for the units behind it: cancel what has not started and stop
        for pending in futures:
            pending.cancel()
        pool.shutdown(wait=False)


def _workers_for(units: List[Tuple[int, bytes]], jobs: int) -> int:
    """How many processes to walk with: `--jobs N` verbatim, `0` (the default) chosen here.

    Auto is the box's cores, capped by the work and by `_AUTO_WORKERS_MAX`. The cap is measured
    rather than guessed: per-thread work is only as parallel as its biggest thread, and on the
    corpus one thread holds 53.4% of it (REFERENCE §6) -- so past a handful of workers nothing gets
    faster on *that* capture, while one whose work is spread evenly gets the whole box.
    """
    if jobs > 1:
        return max(1, min(jobs, len(units)))
    if jobs == 1 or not units:
        return 1
    if sum(len(stream) for _tid, stream in units) < _PARALLEL_MIN_BYTES:
        return 1
    return max(1, min(os.cpu_count() or 1, len(units), _AUTO_WORKERS_MAX))


def build_model(
    stream_set: StreamSet, jobs: int = 0
) -> Tuple[SessionModel, Dict[str, int], List[Anomaly]]:
    """Decode the capture's streams into the session model.

    Returns (model, counts, anomalies): the counters are the walk's own account
    of what it saw, and every anomaly is kept, not just counted -- `verify`
    reports them and the model only carries their summary.

    `jobs` says how to walk the per-thread half: 0 chooses (see `_workers_for`), 1 walks here, N
    walks in N worker processes. The answer does not depend on it -- shares meet in one
    `_merge_share`, in ascending tid order -- and the tests pin that equivalence, because a
    speed-up that changed the model would be a bug with a nicer story.
    """
    """Decode the capture's streams into the session model.

    Returns (model, counts, anomalies): the counters are the walk's own account
    of what it saw, and every anomaly is kept, not just counted -- `verify`
    reports them and the model only carries their summary.
    """
    anomalies: List[Anomaly] = list(stream_set.anomalies)
    counts = zero_counts()

    events_stream = stream_set.streams.get(TID_EVENTS, b"")
    importants_stream = stream_set.streams.get(TID_IMPORTANTS, b"")
    registry = schema.build_registry(events_stream, anomalies, counts)

    session: SessionInfo = {}
    channels: List[ChannelRow] = []
    threads: Dict[int, ThreadRow] = {}
    timers: Dict[int, TimerRow] = {}
    gpu_specs: Dict[int, GpuSpecRow] = {}
    bookmark_specs: Dict[int, Tuple[str, str, int]] = {}
    counters: Dict[int, CounterSpecRow] = {}
    csv_categories: Dict[str, str] = {}
    csv_stats: List[CsvStatRow] = []
    counter_values: Dict[str, int] = {}
    uid_counts: Dict[str, int] = {}
    region_counts: Dict[str, int] = {"begins": 0, "ends": 0}
    current_group = [""]

    def thread(tid: int) -> ThreadRow:
        row = threads.get(tid)
        if row is None:
            row = _thread_row(tid, stream_set)
            threads[tid] = row
        return row

    # -- the important stream: specs, thread names, the session's description
    for uid, size, offset in events.iter_important_records(importants_stream, anomalies):
        row = registry.get(uid)
        if row is None:
            anomalies.append((
                "unknown-important-uid", offset, uid,
                "important record uid %d is not declared by the capture" % (uid,),
            ))
            continue
        values = _important_values(row, importants_stream, uid, offset, size)
        full_name = str(row["full_name"])
        if full_name == "$Trace.NewTrace":
            session["start_cycle"] = decode.value_int(values, "StartCycle") or 0
            session["cycle_frequency"] = decode.value_int(values, "CycleFrequency") or 0
            endian = decode.value_int(values, "Endian")
            session["endian"] = 0 if endian is None else endian
            pointer_size = decode.value_int(values, "PointerSize")
            session["pointer_size"] = 0 if pointer_size is None else pointer_size
            date_time = values.get("StartDateTime")
            if isinstance(date_time, float):
                session["start_date_time"] = date_time
        elif full_name == "$Trace.ThreadInfo":
            tid = decode.value_int(values, "ThreadId")
            if tid is not None:
                trow = thread(tid)
                trow["name"] = decode.value_str(values, "Name")
                system_id = decode.value_int(values, "SystemId")
                if system_id is not None:
                    trow["system_id"] = system_id
                trow["group"] = current_group[0]
        elif full_name == "$Trace.ThreadGroupBegin":
            current_group[0] = decode.value_str(values, "Name")
        elif full_name == "$Trace.ThreadGroupEnd":
            current_group[0] = ""
        elif full_name == "$Trace.ThreadTiming":
            base = decode.value_int(values, "BaseTimestamp")
            if base is not None:
                session["base_timestamp"] = base
        elif full_name == "CpuProfiler.EventSpec":
            spec_id = decode.value_int(values, "Id")
            if spec_id is not None:
                timers[spec_id] = TimerRow(
                    id=spec_id,
                    name=decode.value_str(values, "Name"),
                    file=decode.value_str(values, "File"),
                    line=decode.value_int(values, "Line") or 0,
                )
        elif full_name == gpu.SPEC_EVENT:
            gpu_spec = gpu.spec(values)
            if gpu_spec is not None:
                gpu_specs[gpu_spec["id"]] = gpu_spec
                counts["gpu_specs"] += 1
        elif full_name == "Misc.BookmarkSpec":
            point = decode.value_int(values, "BookmarkPoint")
            if point is not None:
                bookmark_specs[point] = (
                    decode.value_str(values, "FormatString"),
                    decode.value_str(values, "FileName"),
                    decode.value_int(values, "Line") or 0,
                )
                counts["bookmark_specs"] += 1
        elif full_name == "Counters.Spec":
            spec_id = decode.value_int(values, "Id")
            if spec_id is not None:
                counters[spec_id] = CounterSpecRow(
                    id=spec_id,
                    type=decode.value_int(values, "Type") or 0,
                    display_hint=decode.value_int(values, "DisplayHint") or 0,
                    name=decode.value_str(values, "Name"),
                )
        elif full_name == "CsvProfiler.RegisterCategory":
            index = decode.value_int(values, "Index")
            if index is not None:
                csv_categories[str(index)] = decode.value_str(values, "Name")
        elif full_name in ("CsvProfiler.DefineDeclaredStat", "CsvProfiler.DefineInlineStat"):
            spec_id = decode.value_int(values, "StatId")
            if spec_id is not None:
                csv_stats.append(CsvStatRow(
                    id=spec_id,
                    category=decode.value_int(values, "CategoryIndex") or 0,
                    name=decode.value_str(values, "Name"),
                    kind="declared" if full_name.endswith("DeclaredStat") else "inline",
                ))
        elif full_name == "Trace.ChannelAnnounce":
            channel_id = decode.value_int(values, "Id")
            if channel_id is not None:
                channels.append(ChannelRow(
                    id=channel_id,
                    name=decode.value_str(values, "Name"),
                    is_enabled=bool(decode.value_int(values, "IsEnabled") or 0),
                    read_only=bool(decode.value_int(values, "ReadOnly") or 0),
                ))
        elif full_name == "Logging.LogCategory":
            counts["log_categories"] += 1
        elif full_name == "Logging.LogMessageSpec":
            counts["log_specs"] += 1
        elif full_name == "Stats.Spec":
            counts["stats_specs"] += 1
        elif full_name == "Diagnostics.Session2":
            session["platform"] = decode.value_str(values, "Platform")
            session["app"] = decode.value_str(values, "AppName")
            session["project"] = decode.value_str(values, "ProjectName")
            session["branch"] = decode.value_str(values, "Branch")
            session["build_version"] = decode.value_str(values, "BuildVersion")
            changelist = decode.value_int(values, "Changelist")
            if changelist is not None:
                session["changelist"] = changelist
            session["configuration"] = decode.value_str(values, "ConfigurationType")
            session["target"] = decode.value_str(values, "TargetType")

    # -- every thread stream: what happened, one thread at a time (`--jobs` parallelises this bit)
    frame_rows: List[FrameRow] = []
    frame_work: List[FrameWorkRow] = []
    gpu_frames: List[GpuFrameRow] = []
    bookmarks: List[BookmarkRow] = []
    units = [
        (tid, stream_set.streams[tid])
        for tid in sorted(stream_set.streams)
        if tid not in (TID_EVENTS, TID_IMPORTANTS)
    ]
    # one flag per timer spec, one past the highest id the capture declared: the array a frame's
    # totals are counted in, and which of the specs read as a wait. None (no specs at all) is what
    # turns the frame attribution off, so an unattributable capture still walks its frames.
    specs = _spec_flags(timers)
    workers = _workers_for(units, jobs)
    if workers > 1:
        shares = _parallel_shares(registry, units, bookmark_specs, workers, specs)
    else:
        shares = [
            _walk_tid(tid, stream, registry, bookmark_specs, specs)
            for tid, stream in units
        ]
    acc = ModelAcc(
        threads=threads,
        counts=counts,
        uid_counts=uid_counts,
        counter_values=counter_values,
        region_counts=region_counts,
        anomalies=anomalies,
        frames=frame_rows,
        frame_work=frame_work,
        gpu_frames=gpu_frames,
        bookmarks=bookmarks,
    )
    for share in shares:
        thread(share["tid"])
        _merge_share(acc, share)


    frame_rows.sort(key=lambda row: (int(row["begin_cycle"]), int(row["type"]), int(row["tid"])))
    for index, row in enumerate(frame_rows):
        row["index"] = index
    frame_work.sort(key=lambda row: (
        int(row["begin_cycle"]), int(row["tid"]), int(row["type"]),
    ))
    bookmarks.sort(key=lambda row: (int(row["cycle"]), int(row["point"])))

    last_cycle = 0
    for trow in threads.values():
        last_cycle = max(
            last_cycle,
            int(trow.get("last_cycle", 0)),
            int(trow.get("end_cycle", 0)),
        )
    for frame in frame_rows:
        last_cycle = max(last_cycle, int(frame["end_cycle"]))
    if last_cycle:
        session["last_cycle"] = last_cycle
        start_cycle = int(session.get("start_cycle", 0))
        if start_cycle:
            session["duration_cycles"] = last_cycle - start_cycle

    anomaly_counts: Dict[str, int] = {}
    anomaly_samples: List[str] = []
    for kind, offset, value, message in anomalies:
        anomaly_counts[kind] = anomaly_counts.get(kind, 0) + 1
        if len(anomaly_samples) < MAX_ANOMALY_SAMPLES:
            anomaly_samples.append(
                "%s at offset %d (value %d): %s" % (kind, offset, value, message)
            )

    model = SessionModel(
        tool_version=TOOL_VERSION,
        session=session,
        channels=channels,
        counts=counts,
        schema=registry.rows(),
        threads=[threads[tid] for tid in sorted(threads)],
        timers=[timers[spec_id] for spec_id in sorted(timers)],
        frames=frame_rows,
        frame_work=frame_work,
        gpu_specs=[gpu_specs[spec_id] for spec_id in sorted(gpu_specs)],
        gpu_frames=sorted(gpu_frames, key=lambda row: (
            int(row["base_us"]), int(row["number"]), int(row["tid"]),
        )),
        bookmarks=bookmarks,
        counters=[counters[spec_id] for spec_id in sorted(counters)],
        counter_values=counter_values,
        csv_categories=csv_categories,
        csv_stats=csv_stats,
        region_counts=region_counts,
        uid_counts=uid_counts,
        anomaly_counts=anomaly_counts,
        anomaly_samples=anomaly_samples,
    )
    return model, counts, anomalies


def seconds_for_cycle(model: SessionModel, cycle: int) -> Optional[float]:
    """A cycle count as seconds since the trace's start, when both are known."""
    session = model.get("session", SessionInfo())
    start = int(session.get("start_cycle", 0))
    frequency = int(session.get("cycle_frequency", 0))
    if not start or not frequency:
        return None
    return (cycle - start) / float(frequency)
