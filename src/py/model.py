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

import multiprocessing
import os
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypedDict, cast

import coverage
import decode
import events
import gpu
import tasks
from concurrent.futures import ProcessPoolExecutor, wait

import schema
from container import Anomaly
from streams import StreamSet
from shapes import (
    BookmarkRow,
    CounterSpecRow,
    CsvStatRow,
    EventTypeRow,
    FrameOccupancyRow,
    FrameRow,
    FrameWorkRow,
    GpuBreadcrumbSpecRow,
    GpuFenceRow,
    GpuFrameRow,
    GpuPassRow,
    GpuQueueRow,
    GpuSpecRow,
    GpuSpanRow,
    RawEvent,
    TID_EVENTS,
    TID_IMPORTANTS,
    TOOL_VERSION,
    TaskEventRow,
    TaskRow,
    TaskStepRow,
    TaskWaitRow,
    ThreadRow,
    ThreadSpanRow,
    TimerRow,
    UeiaError,
)

MAX_ANOMALY_SAMPLES = 20

#: Name fragments that mark a scope as a **wait** rather than work, for the occupancy split a
#: bottleneck verdict needs: a thread inside `WaitForTasks` is not a thread that is working. This is
#: a heuristic on the capture's own vocabulary -- the engine names its waits `WaitFor*` (`WaitForTasks`,
#: `WaitForGPU`, `WaitUntilTasksComplete`, `WaitForRHIThread`) -- and reports that use it say so.
WAIT_NAME_MARKERS = ("wait",)

#: Words that mark a scope as a **lock**: work spent acquiring something shared, where the question
#: is contention rather than duration (`FScopeLock`, `AcquireGCLock`, `FD3D12FastAllocator::Lock`,
#: `FCriticalSection`, `FMutex`). Matched against the name's camel-case words rather than as a
#: substring, because `AllocateHeapBlock` -- a real name in the corpus's timer table -- ends in
#: "Block", and a case-insensitive `"lock" in name` reads that as a lock. 97 of the corpus's 27,760
#: specs match (REFERENCE §6); like every name rule here, it is a heuristic and reports say so.
LOCK_NAME_WORDS = frozenset(("lock", "locks", "locked", "unlock", "mutex", "critical", "semaphore"))
_SPAN_WORD_RE = re.compile(r"[A-Z][a-z]+")


def span_kind(name: str) -> int:
    """How a scope's name reads: work, a wait, or a lock (see `WAIT_NAME_MARKERS`, `LOCK_NAME_WORDS`)."""
    lowered = name.lower()
    for marker in WAIT_NAME_MARKERS:
        if marker in lowered:
            return coverage.SPAN_WAIT
    for word in _SPAN_WORD_RE.findall(name):
        if word.lower() in LOCK_NAME_WORDS:
            return coverage.SPAN_LOCK
    return coverage.SPAN_WORK

#: How many of a thread's longest frames keep their work attribution (`FrameWorkRow`), and how many
#: timer specs each of those rows names. Sixteen frames is more than any report lists (the summary's
#: table caps at ten by default, and what it lists is the worst), and six timers is more than it
#: prints (three), so the cache holds a little room around what is asked for -- and nothing else.
#: The event names the walk decodes values for: everything else is counted (events, serials, uid
#: counts) and skipped, which is what keeps a million events the model has nothing to say about from
#: paying for a value dict each.
_BATCH_EVENTS = frozenset(("CpuProfiler.EventBatchV2", "CpuProfiler.EventBatchV3"))

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
    # the current GPU channel (REFERENCE §19): the queue timelines' events and what could not be
    # read cleanly -- counted, never dropped silently
    "gpu_queue_specs",
    "gpu_queue_events",
    "gpu_work_spans",
    "gpu_wait_spans",
    "gpu_work_unpaired",
    "gpu_negative_durations",
    "gpu_zero_timestamps",
    "gpu_out_of_order",
    "gpu_frame_boundaries",
    "gpu_stats_events",
    "gpu_fence_events",
    "gpu_breadcrumb_specs",
    "gpu_breadcrumb_no_spec",
    "gpu_spans_coarsened",
    "gpu_passes_dropped",
    # the task channel: the events themselves (the graph is built once, after the merge)
    "task_events",
    # the coverage timelines: the outermost spans kept per thread, and the spans a per-thread cap had
    # to coarsen (counted, never dropped silently -- REFERENCE §6)
    "scope_spans",
    "spans_coarsened",
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
    #: The current GPU channel's `GpuProfiler.Init.Version`, when the capture recorded one (0 when
    #: it did not -- the legacy channel has no Init event, and neither shape is a zero of the other).
    gpu_channel_version: int


class ChannelRow(TypedDict):
    """One channel announce: a channel and whether this capture had it on."""

    id: int
    name: str
    is_enabled: bool
    read_only: bool


class SessionModel(TypedDict):
    """Everything the parser core knows about one capture. JSON-serialisable.

    `build_model` fills every key, and a cached model is the same document
    read back, so the type is total -- with one exception, `self_pairing`:
    the call trees `ueia self` stores and the pass's own pairing counts for
    the threads it was asked about (`FrameRow.self_detail`) are added by that
    command, after the parse, and are absent until it runs. A caller that
    deliberately wants a partial model (a test of `seconds_for_cycle`, say)
    casts it.
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
    #: Per thread id, the pairing counts of the self pass that built the frames stored trees.
    #: Written by that command, not by build_model: absent until a run has read that thread.
    self_pairing: Dict[str, Any]
    thread_spans: List[ThreadSpanRow]
    frame_occupancy: List[FrameOccupancyRow]
    gpu_specs: List[GpuSpecRow]
    gpu_frames: List[GpuFrameRow]
    #: The current GPU channel (REFERENCE §19): queues and their union totals, the kept spans,
    #: the named passes and the spec table their names resolve through, and the fences.
    gpu_queues: List[GpuQueueRow]
    gpu_spans: List[GpuSpanRow]
    gpu_passes: List[GpuPassRow]
    gpu_breadcrumb_specs: List[GpuBreadcrumbSpecRow]
    gpu_fences: List[GpuFenceRow]
    tasks: List[TaskRow]
    task_edges: List[Tuple[int, int]]
    task_path: List[TaskStepRow]
    task_waits: List[TaskWaitRow]
    task_counts: Dict[str, int]
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

    Strings decode by their declared type (`decode.event_values`): a field the
    capture declared `WideString` arrives as UTF-16, one declared `AnsiString`
    as one byte per character -- which is what the CPU profiler's spec names
    (wide literals into AnsiString fields) actually are. Decoding every
    important string one byte per character mangled the genuinely wide ones
    (`Diagnostics.Session2`'s `BuildVersion`, `Misc.BookmarkSpec`'s format
    string), found 2026-09-30 against two real captures.
    """
    end = offset + size
    fixed_end = min(offset + int(row["size"]), end)
    aux = events.walk_record_aux(stream, fixed_end, end)
    return decode.event_values(row, stream, RawEvent(uid, None, offset, size, aux, False))


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
    # the coverage timelines, packed (`coverage.pack`): one `bytes` per set, so the share stays small
    # on the wire and the parent only expands the ones it measures
    spans: bytes
    wait_spans: bytes
    lock_spans: bytes
    #: how many spans of this thread's timeline the per-thread cap had to merge (`coverage.SPAN_KEEP`)
    spans_coarsened: int
    gpu_frames: List[GpuFrameRow]
    gpu_queues: List[GpuQueueRow]
    gpu_spans: List[GpuSpanRow]
    gpu_passes: List[GpuPassRow]
    gpu_fences: List[GpuFenceRow]
    task_events: List[TaskEventRow]
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
    gpu_queues: List[GpuQueueRow]
    gpu_spans: List[GpuSpanRow]
    gpu_passes: List[GpuPassRow]
    gpu_fences: List[GpuFenceRow]
    task_events: List[TaskEventRow]
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

    __slots__ = ("type", "begin", "end", "covered", "waiting", "pairs", "totals", "self_cycles")

    def __init__(self, frame_type: int, begin: int, end: int) -> None:
        self.type = frame_type
        self.begin = begin
        self.end = end
        self.covered = 0
        self.waiting = 0
        self.pairs = 0
        # this thread's self time inside the frame: the clips of the scopes attributed to it, minus
        # the clips of the pairs that closed inside them *here* (`FrameRow.self_cycles`). One integer
        # per frame, which is what lets `ueia self` rank a capture's frames out of the cache instead
        # of decoding every thread again.
        self.self_cycles = 0
        # one slot per spec id: a list, not a dict, because the hot loop writes it once per scope
        # pair (2.5 M of them on the corpus's game thread) and an index beats a hash there. Only a
        # thread's longest frames have one at all.
        self.totals: Optional[List[int]] = None

    def work_row(self, tid: int, top: int) -> FrameWorkRow:
        """This frame as the model keeps it: its biggest specs by clipped cycles, then by id."""
        items: List[Tuple[int, int]] = []
        totals = self.totals
        if totals:
            touched = [(cycles, spec) for spec, cycles in enumerate(totals) if cycles]
            touched.sort(key=lambda item: (-item[0], item[1]))
            items = [(spec, cycles) for cycles, spec in touched[:top]]
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
        self_cycles=self.self_cycles if occupancy else None,
        # the walk does not build trees: `ueia self` stores the few it reports on, into this very row
        self_detail=None,
        )


def _flush_window(window: _Window, occupancy: List[int], pairs: int) -> None:
    """Write the hot loop's running occupancy, self time and pair count into the window they are."""
    window.self_cycles = occupancy[2]
    window.covered = occupancy[0]
    window.waiting = occupancy[1]
    window.pairs = pairs


def _place_chain(steps: List[TaskStepRow], frames: Sequence[FrameRow]) -> None:
    """Tag every step of a chain with the frame its thread was in when the task began.

    The chain belongs to the capture, not to one frame -- it can cross frames -- and this is what
    lets a report say which frames it touched. A task that ran on a thread with no frames, or began
    in a gap between two of them, gets None: the honest answer, not the nearest frame.
    """
    by_thread: Dict[int, List[FrameRow]] = {}
    for row in frames:
        by_thread.setdefault(int(row["tid"]), []).append(row)
    for rows in by_thread.values():
        rows.sort(key=lambda row: int(row["begin_cycle"]))
    for step in steps:
        rows = by_thread.get(int(step["started_tid"]))
        if not rows:
            continue
        cycle = int(step["begin_cycle"])
        low, high = 0, len(rows)
        while low < high:                     # the first frame that ends at or after the task began
            middle = (low + high) // 2
            if int(rows[middle]["end_cycle"]) < cycle:
                low = middle + 1
            else:
                high = middle
        if low < len(rows) and int(rows[low]["begin_cycle"]) <= cycle:
            step["frame_index"] = int(rows[low]["index"])


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

    `specs` (one kind per timer spec the capture declared -- work, wait like `WaitForTasks`, or lock;
    None when it declared none) turns on the frame attribution, the occupancy and the **coverage
    timeline**. Two passes: `_pair_windows` collects this thread's frame windows, then this loop
    attributes every scope pair of every batch to the window whose span contains the pair's **end**
    cycle, clipped to that window, and merges it into the window's occupancy -- cycles inside any
    scope, and cycles inside a wait-named one, which is the difference between a thread that is
    working and a thread that is waiting. A pair that began before the frame it ended in is counted
    (`scope_pairs_spanning`); one that ended in no window at all is counted too
    (`scope_pairs_unframed` -- which is also every pair of a thread that has no frames at all),
    never guessed into the nearest frame.

    The same records also build the thread's **coverage timeline**: the union of every span it held
    (one interval per *outermost* span, because a nested scope closes inside its parent), and the
    same for the wait-named and lock-named ones. Frames are not involved -- a worker pool has no
    frames of its own, and its occupancy only means something against another thread's windows -- so
    the timeline is folded into the frames later, once every thread has been walked
    (`coverage.measure_frames`). The lists are packed (`coverage.pack`) because they cross a process
    boundary and outlive the walk.

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
    gpu_walk = gpu.QueueWalk(counts)
    task_events: List[TaskEventRow] = []
    windows = _pair_windows(stream, tid, registry, counts)
    occupancy_on = specs is not None
    spec_count = len(specs) if specs is not None else 0
    # the work windows: the thread's longest frames, chosen now that their lengths are known
    if occupancy_on:
        for window in sorted(windows, key=lambda item: (
                -(item.end - item.begin), item.begin, item.type))[:_FRAME_WORK_KEEP]:
            if window.end > window.begin:
                window.totals = [0] * spec_count
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
    #: the running self time of the window being filled, and -- in lockstep with `stack` -- how much
    #: of each open pair's span is already its children's. Both are reset when the window changes: a
    #: child that closed in the previous window is that window's, and must not be subtracted from its
    #: parent's self time here.
    window_self = 0
    child_stack: List[int] = []
    #: the own time of each open pair's children in this window, held until the frame is
    #: flushed: if the pair is still open then, it belongs to a later frame and those children
    #: are this frame's roots (the promotion `calltree.weigh` does for the pass)
    child_own: List[int] = []
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
    # the coverage timeline, in the loop's own locals for the same reason: flat [begin, end, ...]
    # lists, one per kind, plus the depths that say when a wait or a lock is open at all. `busy`
    # opens on the span that finds the stack empty and closes when it empties again -- which *is* the
    # union of every span, since a nested one lives inside its parent.
    busy: List[int] = []
    waits: List[int] = []
    locks: List[int] = []
    busy_begin = 0
    wait_begin = 0
    lock_begin = 0
    wait_depth = 0
    lock_depth = 0
    kinds = specs if specs is not None else b""
    kind_count = len(kinds)
    coarsened = 0
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
        # the name decides, and only then are the values decoded: `event_values` builds a dict and
        # walks the aux blocks per event, which on the corpus is ~1 M events this walk has nothing
        # to say about (it is what the "most of the profile" line was about)
        full_name = str(row["full_name"])

        if full_name in _BATCH_EVENTS:
            values = decode.event_values(row, stream, event)
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
                # The window whose span holds this record's cycle: the cursor only ever moves forward,
                # because the records arrive in cycle order. **This runs before the pair is popped**,
                # and that ordering is the whole point: a pair that closes beyond a window -- in the
                # gap between two frames, after the last frame, or in a later frame -- is not in that
                # window, but the *children* it collected there are. Its slot holds their own time, and
                # popping it would take that time out of the frame it belongs to; flushing first
                # credits it (the pass calls the same thing a promotion, `calltree.weigh`). Found by
                # instrumenting this loop: `covered=8980608` at the flush of the corpus's frame 0 with
                # `sum(child_own)=0`, because the scope that held the frame's 3,108 pairs closed in the
                # gap after it first (2026-09-30).
                while cursor < window_count and windows[cursor].end < cycle:
                    if active == cursor:
                        _flush_window(windows[cursor], [covered, waiting, window_self + sum(child_own)], pairs)
                        active = -1
                    cursor += 1
                if is_begin:
                    if not stack:
                        busy_begin = cycle
                    stack.append((spec_id, cycle))
                    child_stack.append(0)
                    child_own.append(0)
                    kind = kinds[spec_id] if spec_id is not None and spec_id < kind_count else 0
                    if kind == coverage.SPAN_WAIT:
                        if not wait_depth:
                            wait_begin = cycle
                        wait_depth += 1
                    elif kind == coverage.SPAN_LOCK:
                        if not lock_depth:
                            lock_begin = cycle
                        lock_depth += 1
                    continue
                if not stack:
                    ends_unpaired += 1
                    continue
                spec, begin = stack.pop()
                # popped in lockstep with `stack`, and before any `continue` below: a pair that is
                # not attributed to a frame is still its parent's child while the parent is open.
                # `is_root` is read here because the window switch rebuilds `child_stack` from the
                # pairs still open -- which is empty for a root pair, and only for one.
                child = child_stack.pop() if child_stack else 0
                if child_own:
                    child_own.pop()
                is_root = not child_stack
                kind = kinds[spec] if spec is not None and spec < kind_count else 0
                if kind == coverage.SPAN_WAIT:
                    wait_depth -= 1
                    if wait_depth <= 0:
                        if cycle > wait_begin:
                            if len(waits) < coverage.SPAN_KEEP * 2:
                                waits.append(wait_begin)
                                waits.append(cycle)
                            else:
                                waits[-1] = cycle
                                coarsened += 1
                        wait_depth = 0
                elif kind == coverage.SPAN_LOCK:
                    lock_depth -= 1
                    if lock_depth <= 0:
                        if cycle > lock_begin:
                            if len(locks) < coverage.SPAN_KEEP * 2:
                                locks.append(lock_begin)
                                locks.append(cycle)
                            else:
                                locks[-1] = cycle
                                coarsened += 1
                        lock_depth = 0
                if not stack and cycle > busy_begin:
                    # the outermost span closed: one interval of coverage, or -- past the cap -- the
                    # one before it, extended (coverage overstated rather than dropped, and counted)
                    if len(busy) < coverage.SPAN_KEEP * 2:
                        busy.append(busy_begin)
                        busy.append(cycle)
                    else:
                        busy[-1] = cycle
                        coarsened += 1
                if cursor >= window_count or cycle < windows[cursor].begin:
                    unframed += 1
                    continue
                window = windows[cursor]
                if active != cursor:
                    active = cursor
                    covered, waiting, pairs = 0, 0, 0
                    window_self = 0
                    child_stack = [0] * len(stack)
                    child_own = [0] * len(stack)
                    cover_start, cover_end = 0, -1
                    wait_start, wait_end = 0, -1
                if begin < window.begin:
                    begin = window.begin
                    spanning += 1
                span = cycle - begin
                if span > 0 and not is_root:
                    # inside its parent *in this frame*, so the parent's own time excludes it
                    child_stack[-1] += span
                if span > 0:
                    # the streaming union, inlined because it runs once per scope pair. The
                    # commonest shape by far is the *disjoint* one -- the next scope starts where the
                    # last one ended or later (`RenderingFrame`, then the work inside it) -- so that
                    # case is tested first and answered without touching the general arithmetic. An
                    # interval otherwise adds its prefix when it starts before the region and its
                    # tail when it ends after it; the region is empty while `end < start`.
                    if cover_end < cover_start:      # nothing covered yet in this window
                        covered = span
                        cover_start = begin
                        cover_end = cycle
                    elif begin >= cover_end:         # the commonest shape: disjoint, to the right
                        covered += span
                        cover_end = cycle            # the left edge of the region does not move
                    else:                            # overlapping, or containing the region
                        if begin < cover_start:
                            covered += cover_start - begin
                            cover_start = begin
                        if cycle > cover_end:
                            covered += cycle - (begin if begin > cover_end else cover_end)
                            cover_end = cycle
                    if (spec is not None and spec < spec_count
                            and kinds[spec] == coverage.SPAN_WAIT and cycle > wait_end):
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
                if span > 0 and not is_root:
                    child_own[-1] += span - child
                elif span > 0:
                    # Self time, and **roots only**. Every pair's own time is already inside its
                    # parent's own time, so summing over all of them telescopes into the roots'
                    # *inclusive* total -- 816.21 ms against the pass's 815.38 for the corpus's frame
                    # 52, which is how the arithmetic was caught (2026-09-30). A root's own time is
                    # the frame's self time, and it is measured for *every* frame, not only the ones
                    # that keep their work: one integer per frame, which is what a report ranks by.
                    window_self += span - child
                totals = window.totals
                if totals is not None and span > 0:
                    totals[spec] += span
                    pairs += 1
            if last_cycle:
                trow["first_cycle"] = first_cycle
                trow["last_cycle"] = last_cycle
        elif full_name == "Misc.Bookmark":
            values = decode.event_values(row, stream, event)
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
            values = decode.event_values(row, stream, event)
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
            values = decode.event_values(row, stream, event)
            counter_id = decode.value_int(values, "CounterId")
            if counter_id is not None:
                key = str(counter_id)
                counter_values[key] = counter_values.get(key, 0) + 1
        elif full_name.startswith(tasks.PREFIX):
            values = decode.event_values(row, stream, event)
            kind = full_name[len(tasks.PREFIX):]
            if kind == "Init":
                continue
            stamp = decode.value_int(values, "Timestamp")
            if stamp is None:
                continue
            counts["task_events"] += 1
            if kind in ("WaitingStarted", "WaitingFinished"):
                # the wait events carry no task id: the *recording* thread is what is waiting, and
                # `Tasks` (an aux array) names what it waits for -- the count is what the report uses
                waited = values.get("Tasks")
                other = (len(waited) // 8) if isinstance(waited, (bytes, bytearray)) else 0
                task_events.append(TaskEventRow(
                    kind=kind, task=0, stamp=stamp, other=other, size=0, flags=0, text="", tid=tid,
                ))
                continue
            task_id = decode.value_int(values, "TaskId")
            if task_id is None:
                continue
            other = 0
            size = 0
            flags = 0
            text = ""
            if kind == "Created":
                size = decode.value_int(values, "TaskSize") or 0
            elif kind == "Launched":
                size = decode.value_int(values, "TaskSize") or 0
                to_execute = decode.value_int(values, "ThreadToExecuteOn") or 0
                tracked = 1 if values.get("Tracked") else 0
                flags = (to_execute << 1) | tracked
                text = decode.value_str(values, "DebugName")
            elif kind == "SubsequentAdded":
                other = decode.value_int(values, "SubsequentId") or 0
            task_events.append(TaskEventRow(
                kind=kind, task=task_id, stamp=stamp, other=other, size=size, flags=flags,
                text=text, tid=tid,
            ))
        elif full_name == gpu.FRAME_EVENT:
            values = decode.event_values(row, stream, event)
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
        elif full_name in gpu.QUEUE_EVENTS:
            # the current GPU channel: one event per GPU work item, dispatched by name (REFERENCE
            # §19). Everything it counts lands in `counts` and the share's rows, so `--jobs` folds
            # it in the same ascending-tid order the serial walk produced.
            gpu_walk.event(full_name, decode.event_values(row, stream, event))

    while cursor < window_count:
        if active == cursor:
            _flush_window(windows[cursor], [covered, waiting, window_self + sum(child_own)], pairs)
            active = -1
        cursor += 1
    if occupancy_on:
        counts["scope_pairs"] += attributed
        counts["scope_pairs_spanning"] += spanning
        counts["scope_pairs_no_spec"] += no_spec
        counts["scope_pairs_unframed"] += unframed
        counts["scope_ends_unpaired"] += ends_unpaired
        counts["scope_begins_unpaired"] += len(stack)
        counts["scope_spans"] += len(busy) // 2
        counts["spans_coarsened"] += coarsened
    frame_rows = [window.frame_row(tid, occupancy_on) for window in windows]
    frame_work = [window.work_row(tid, _FRAME_WORK_TOP) for window in windows
                  if window.totals is not None]
    gpu_queues, gpu_spans, gpu_passes, gpu_fences = gpu_walk.flush()

    return ThreadShare(
        tid=tid, row=trow, counts=counts, uid_counts=uid_counts, counter_values=counter_values,
        region_counts=region_counts, anomalies=anomalies, frames=frame_rows, frame_work=frame_work,
        spans=coverage.pack(busy), wait_spans=coverage.pack(waits), lock_spans=coverage.pack(locks),
        spans_coarsened=coarsened,
        gpu_frames=gpu_frames, gpu_queues=gpu_queues, gpu_spans=gpu_spans, gpu_passes=gpu_passes,
        gpu_fences=gpu_fences, task_events=task_events, bookmarks=bookmarks,
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
    acc["gpu_queues"].extend(share["gpu_queues"])
    acc["gpu_spans"].extend(share["gpu_spans"])
    acc["gpu_passes"].extend(share["gpu_passes"])
    acc["gpu_fences"].extend(share["gpu_fences"])
    acc["task_events"].extend(share["task_events"])
    acc["bookmarks"].extend(share["bookmarks"])


def _merge_gpu_queues(rows: Sequence[GpuQueueRow], spans: Sequence[GpuSpanRow],
                      specs: Dict[int, GpuQueueRow]) -> List[GpuQueueRow]:
    """One row per queue id: additive counters summed, union totals recomputed from the spans.

    The queue's declaration (`GpuProfiler.QueueSpec`, an important record) seeds the row -- it is
    where the capture's own name for the queue lives -- and a queue's events can in principle
    arrive on more than one thread, so the shares' rows are re-merged here rather than trusted.
    The union totals are always recomputed from the kept spans (`gpu.union_total`), which is what
    keeps a row's `busy_us` and the spans a frame placement reads from ever disagreeing.
    """
    merged: Dict[int, GpuQueueRow] = {
        queue_id: cast(GpuQueueRow, dict(row)) for queue_id, row in specs.items()
    }
    for row in rows:
        existing = merged.get(int(row["id"]))
        if existing is None:
            merged[int(row["id"])] = cast(GpuQueueRow, dict(row))
            continue
        existing["boundaries"] += row["boundaries"]
        existing["work_spans"] += row["work_spans"]
        existing["wait_spans"] += row["wait_spans"]
        existing["submits"] += row["submits"]
        existing["lag_total_us"] += row["lag_total_us"]
        existing["lag_negative"] += row["lag_negative"]
        existing["lag_max_us"] = max(int(existing["lag_max_us"]), int(row["lag_max_us"]))
        existing["draws"] += row["draws"]
        existing["primitives"] += row["primitives"]
        existing["last_frame"] = max(int(existing["last_frame"]), int(row["last_frame"]))
        if not existing["name"] and row["name"]:
            existing["name"] = row["name"]
    for queue_id, row in merged.items():
        row["busy_us"] = gpu.union_total(spans, queue_id, "work")
        row["wait_us"] = gpu.union_total(spans, queue_id, "wait")
    return [merged[queue_id] for queue_id in sorted(merged)]


def _merge_gpu_passes(rows: Sequence[GpuPassRow], counts: Dict[str, int]) -> List[GpuPassRow]:
    """Pass aggregates re-merged across shares, then capped (`gpu.QUEUE_PASS_KEEP`) by inclusive time.

    The cap counts what it folded away (`gpu_passes_dropped`): the passes kept are the report's
    table, and the dropped ones are named as a number, never as a silence.
    """
    merged: Dict[int, GpuPassRow] = {}
    for row in rows:
        spec = int(row["spec"])
        existing = merged.get(spec)
        if existing is None:
            merged[spec] = cast(GpuPassRow, dict(row))
            continue
        existing["calls"] += row["calls"]
        existing["inclusive_us"] += row["inclusive_us"]
        if int(row["max_us"]) > int(existing["max_us"]):
            existing["max_us"] = row["max_us"]
            existing["max_begin_us"] = row["max_begin_us"]
            existing["max_end_us"] = row["max_end_us"]
    ordered = sorted(merged.values(), key=lambda row: (-int(row["inclusive_us"]), int(row["spec"])))
    if len(ordered) > gpu.QUEUE_PASS_KEEP:
        counts["gpu_passes_dropped"] += len(ordered) - gpu.QUEUE_PASS_KEEP
        ordered = ordered[:gpu.QUEUE_PASS_KEEP]
    return ordered


def _merge_gpu_fences(rows: Sequence[GpuFenceRow]) -> List[GpuFenceRow]:
    """Fence lines merged by (kind, queue, other): the same aggregation the counters take."""
    merged: Dict[Tuple[str, int, int], GpuFenceRow] = {}
    for row in rows:
        key = (str(row["kind"]), int(row["queue"]), int(row["other"]))
        existing = merged.get(key)
        if existing is None:
            merged[key] = cast(GpuFenceRow, dict(row))
        else:
            existing["count"] += row["count"]
    return [merged[key] for key in sorted(merged)]


def _spec_kinds(timers: Dict[int, TimerRow]) -> Optional[bytes]:
    """One byte per timer spec: how its name reads (`coverage.SPAN_WORK/WAIT/LOCK`).

    The array doubles as the attribution switch -- its length is one past the highest spec id the
    capture declared, and None (no specs at all) turns the frame attribution, the occupancy and the
    coverage timelines off, because a capture that declared no timer specs has nothing to attribute.
    """
    if not timers:
        return None
    kinds = bytearray(max(timers) + 1)
    for spec_id, row in timers.items():
        kinds[spec_id] = span_kind(str(row["name"]))
    return bytes(kinds)


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
    before = {child.pid for child in multiprocessing.active_children()}
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
        # ... and the executor's own `atexit` handler joins its workers, so a worker that never
        # comes back -- a stall, a death, or work it never received -- keeps the *interpreter* alive
        # for as long as it survives. Measured 2026-09-29: six pool tests cost 1.39 s of test time
        # and 7.3 s of wall time, all of the difference spent after the tests, joining workers.
        # Only this pool's own children are terminated, by pid, and only after every result this
        # call was going to get has been collected (or its unit has failed) -- so nothing is cut
        # short that the caller was still waiting for.
        for child in multiprocessing.active_children():
            if child.pid not in before:
                child.terminate()
        for child in multiprocessing.active_children():
            if child.pid not in before:
                child.join(timeout=1.0)


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
    gpu_queues: Dict[int, GpuQueueRow] = {}
    breadcrumb_specs: Dict[int, GpuBreadcrumbSpecRow] = {}
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
        elif full_name == gpu.QUEUE_SPEC_EVENT:
            # the current channel's queue declarations: last declaration of an id wins, like the
            # legacy spec table above
            queue_spec = gpu.queue_spec(values)
            if queue_spec is not None:
                gpu_queues[queue_spec["id"]] = queue_spec
                counts["gpu_queue_specs"] += 1
        elif full_name == gpu.BREADCRUMB_SPEC_EVENT:
            crumb_spec = gpu.breadcrumb_spec(values)
            if crumb_spec is not None:
                breadcrumb_specs[crumb_spec["spec"]] = crumb_spec
                counts["gpu_breadcrumb_specs"] += 1
        elif full_name == gpu.INIT_EVENT:
            version = decode.value_int(values, "Version")
            if version is not None:
                session["gpu_channel_version"] = version
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
    gpu_queue_rows: List[GpuQueueRow] = []
    gpu_span_rows: List[GpuSpanRow] = []
    gpu_pass_rows: List[GpuPassRow] = []
    gpu_fence_rows: List[GpuFenceRow] = []
    task_events: List[TaskEventRow] = []
    bookmarks: List[BookmarkRow] = []
    units = [
        (tid, stream_set.streams[tid])
        for tid in sorted(stream_set.streams)
        if tid not in (TID_EVENTS, TID_IMPORTANTS)
    ]
    # one kind per timer spec, one past the highest id the capture declared: the array a frame's
    # totals are counted in, and which of the specs read as a wait or a lock. None (no specs at all)
    # is what turns the frame attribution and the coverage timelines off, so an unattributable
    # capture still walks its frames.
    specs = _spec_kinds(timers)
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
        gpu_queues=gpu_queue_rows,
        gpu_spans=gpu_span_rows,
        gpu_passes=gpu_pass_rows,
        gpu_fences=gpu_fence_rows,
        task_events=task_events,
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

    # -- the current GPU channel: one row per queue (a queue's events can in principle arrive on
    # more than one thread, so its totals are re-merged here from the rows and the kept spans),
    # passes re-aggregated and capped, spans in placement order
    gpu_queue_merged = _merge_gpu_queues(gpu_queue_rows, gpu_span_rows, gpu_queues)
    gpu_pass_merged = _merge_gpu_passes(gpu_pass_rows, counts)
    gpu_span_rows.sort(key=lambda row: (int(row["queue"]), int(row["begin_us"]), str(row["kind"])))
    gpu_fence_merged = _merge_gpu_fences(gpu_fence_rows)

    # -- what every thread did inside every frame: the timelines the walks measured, folded into the
    # capture's own frame windows. Here, once, because a worker pool has no frames of its own and a
    # per-thread walk cannot see another thread's -- and the timelines are *not* kept past this point
    # (a capture's 1.05 M intervals have no business in a JSON cache, REFERENCE §6 and §13).
    thread_spans, frame_occupancy = coverage.measure_frames(
        frame_rows,
        {share["tid"]: (share["spans"], share["wait_spans"], share["lock_spans"])
         for share in shares},
        {share["tid"]: int(share["spans_coarsened"]) for share in shares},
    )
    del shares

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

    # -- the task graph: built once, here, because a task's events can come from any thread
    frequency = int(session.get("cycle_frequency", 0) or 0)
    tasks_table, task_edges, chain, task_waits, task_counts = tasks.build_graph(
        task_events, frequency,
    )
    kept_tasks = tasks.top_tasks(tasks_table, tasks.TASK_KEEP)
    _place_chain(chain.steps, frame_rows)
    task_counts["dropped"] = max(0, len(tasks_table) - len(kept_tasks))
    task_counts["path_steps"] = len(chain.steps)
    task_counts["path_cycles"] = chain.total_cycles
    task_counts["path_ms"] = int(chain.total_cycles * 1000.0 / frequency) if frequency else 0

    model = SessionModel(
        tool_version=TOOL_VERSION,
        self_pairing={},
        session=session,
        channels=channels,
        counts=counts,
        schema=registry.rows(),
        threads=[threads[tid] for tid in sorted(threads)],
        timers=[timers[spec_id] for spec_id in sorted(timers)],
        frames=frame_rows,
        frame_work=frame_work,
        gpu_specs=[gpu_specs[spec_id] for spec_id in sorted(gpu_specs)],
        thread_spans=thread_spans,
        frame_occupancy=frame_occupancy,
        gpu_frames=sorted(gpu_frames, key=lambda row: (
            int(row["base_us"]), int(row["number"]), int(row["tid"]),
        )),
        gpu_queues=gpu_queue_merged,
        gpu_spans=gpu_span_rows,
        gpu_passes=gpu_pass_merged,
        gpu_breadcrumb_specs=[breadcrumb_specs[spec] for spec in sorted(breadcrumb_specs)],
        gpu_fences=gpu_fence_merged,
        tasks=kept_tasks,
        task_edges=tasks.edges_between(task_edges, [task["id"] for task in kept_tasks],
                                       tasks.EDGE_KEEP),
        task_path=chain.steps,
        task_waits=sorted(task_waits, key=lambda row: (int(row["tid"]), int(row["begin_cycle"]))),
        task_counts=task_counts,
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
