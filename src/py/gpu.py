"""The GPU profiler channel: the legacy per-frame batches, and the current per-queue timelines.

Unreal's *current* GPU channel emits one trace event per GPU work item. The channel the corpus
carries is the **legacy** one (removed from the engine in 5.6 as a writer, kept as a reader: UE
5.8.3 still ships `OldGpuProfilerTraceAnalysis.cpp` "for backward compatibility with old traces"),
which packs a whole rendered frame into one `GpuProfiler.Frame` event. That decoder is the
authority for the layout below, and this module follows it field for field:

* `GpuProfiler.EventSpec` (important): `uint32 EventType` plus a `WideString` **array** `Name` --
  the id-to-name map a frame's records resolve through. Ids are opaque (the writer that chose them
  no longer exists), so the map is the only way to name a GPU pass.
* `GpuProfiler.Frame` (MaybeHasAux): `uint64 CalibrationBias`, `uint64 TimestampBase`,
  `uint32 RenderingFrameNumber`, `uint8[] Data`.

`Data` is a varint-delta stream, not a struct array (`Utils.h`'s `Decode7bit`):

    packed = Decode7bit()                     # LEB128
    timestamp += packed >> 1                  # the delta is in microseconds
    if packed & 1:                            # a begin: its spec id follows as uint32 LE
        spec = uint32(4 bytes)
    else:                                     # an end: no payload
        ...

`busy_us` is the sum of the **outermost** event durations. Outermost intervals cannot overlap, so
that sum is the union: the microseconds the GPU spent executing traced work in that frame. A
nested pass is counted in `passes` as well (inclusive, like the CPU side), which is what makes
"which pass owns the GPU time" answerable. Nothing here interprets `CalibrationBias`: durations are
differences, so a constant bias cancels -- and the frame's `TimestampBase` (which the engine's
decoder uses to prime the delta chain) is kept as `base_us` for the alignment in `bottleneck.py`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from shapes import (
    GpuBreadcrumbSpecRow,
    GpuFenceRow,
    GpuFrameRow,
    GpuPassRow,
    GpuQueueRow,
    GpuSpecRow,
    GpuSpanRow,
)

#: The logger and event names the legacy channel declares (`OldGpuProfilerTraceAnalysis.cpp`).
LOGGER = "GpuProfiler"
SPEC_EVENT = "GpuProfiler.EventSpec"
FRAME_EVENT = "GpuProfiler.Frame"

#: The *current* channel's events (`Runtime/RHI/Private/GpuProfilerTrace.cpp`, REFERENCE §19). The
#: `Init`, `QueueSpec` and `EventBreadcrumbSpec` records are `NoSync|Important` -- they ride the
#: importants stream and the model's important loop reads them; the rest arrive on thread streams,
#: one event per GPU work item, and the per-thread walk collects them.
INIT_EVENT = "GpuProfiler.Init"
QUEUE_SPEC_EVENT = "GpuProfiler.QueueSpec"
BREADCRUMB_SPEC_EVENT = "GpuProfiler.EventBreadcrumbSpec"
FRAME_BOUNDARY_EVENT = "GpuProfiler.EventFrameBoundary"
BEGIN_WORK_EVENT = "GpuProfiler.EventBeginWork"
END_WORK_EVENT = "GpuProfiler.EventEndWork"
WAIT_EVENT = "GpuProfiler.EventWait"
STATS_EVENT = "GpuProfiler.EventStats"
BEGIN_BREADCRUMB_EVENT = "GpuProfiler.EventBeginBreadcrumb"
END_BREADCRUMB_EVENT = "GpuProfiler.EventEndBreadcrumb"
SIGNAL_FENCE_EVENT = "GpuProfiler.SignalFence"
WAIT_FENCE_EVENT = "GpuProfiler.WaitFence"

#: The thread-stream events of the current channel: the walk's dispatch test, one set membership
#: ahead of any value decoding (the same shape the batch events use).
QUEUE_EVENTS = frozenset((
    FRAME_BOUNDARY_EVENT,
    BEGIN_WORK_EVENT,
    END_WORK_EVENT,
    WAIT_EVENT,
    STATS_EVENT,
    BEGIN_BREADCRUMB_EVENT,
    END_BREADCRUMB_EVENT,
    SIGNAL_FENCE_EVENT,
    WAIT_FENCE_EVENT,
))

#: How many passes of a frame are kept for evidence (the biggest by inclusive microseconds).
PASS_KEEP = 6

#: Total microseconds a single frame's batch may claim before it is treated as corrupt. The
#: corpus's worst frame is 80 ms; a value that big means the deltas are being misread.
MAX_FRAME_US = 60 * 60 * 1000000

#: The named passes (breadcrumb specs) the model keeps for the current channel, by inclusive
#: time, after the merge. Distinct from the legacy frame's per-batch `PASS_KEEP` above.
QUEUE_PASS_KEEP = 24


class Frame(NamedTuple):
    """One decoded `GpuProfiler.Frame`: what the GPU did, and what could not be read."""

    busy_us: int
    events: int
    depth: int
    unbalanced: int
    truncated: int
    passes: List[Tuple[int, int]]


def spec(values: Mapping[str, Any]) -> Optional[GpuSpecRow]:
    """One `GpuProfiler.EventSpec` record: its id and its name.

    The name arrives as an array of UTF-16 code units (the engine reads
    `GetArray<UTF16CHAR>("Name")`), so the bytes are decoded as UTF-16LE. A name that is not valid
    UTF-16 decodes with replacement characters rather than raising: a malformed *name* must not cost
    the capture every GPU timing it has.
    """
    raw_id = values.get("EventType")
    if not isinstance(raw_id, (int, float)):
        return None
    blob = values.get("Name")
    name = ""
    if isinstance(blob, (bytes, bytearray)):
        name = bytes(blob).decode("utf-16-le", "replace").rstrip("\x00")
    elif isinstance(blob, str):
        name = blob
    return GpuSpecRow(id=int(raw_id), name=name)


def _varint(blob: bytes, offset: int) -> Optional[Tuple[int, int]]:
    """LEB128 at `offset`, or None when the varint runs off the end."""
    value = 0
    shift = 0
    while True:
        if offset >= len(blob):
            return None
        byte = blob[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            return None


def decode_frame(blob: bytes, base_us: int) -> Frame:
    """Decode one frame's `Data` array: the layout the engine's own legacy decoder reads.

    Never raises: a truncated varint, a begin without its 4-byte spec id, an end with no begin and
    events left open at the end are all counted (`truncated`, `unbalanced`) and the readable part is
    returned, because a GPU frame that half-decodes is still evidence and a capture must not be lost
    to one bad batch.
    """
    timestamp = base_us
    stack: List[Tuple[int, int]] = []
    passes: Dict[int, int] = {}
    busy = 0
    events = 0
    depth = 0
    unbalanced = 0
    truncated = 0
    offset = 0
    while offset < len(blob):
        read = _varint(blob, offset)
        if read is None:
            truncated += 1
            break
        packed, offset = read
        timestamp += packed >> 1
        if packed & 1:
            if offset + 4 > len(blob):
                truncated += 1
                break
            spec_id = int.from_bytes(blob[offset:offset + 4], "little")
            offset += 4
            events += 1
            stack.append((spec_id, timestamp))
            depth = max(depth, len(stack))
        elif stack:
            spec_id, begin = stack.pop()
            if not stack:
                busy += timestamp - begin
            passes[spec_id] = passes.get(spec_id, 0) + (timestamp - begin)
        else:
            unbalanced += 1
    unbalanced += len(stack)
    if busy > MAX_FRAME_US:
        return Frame(busy_us=0, events=0, depth=0, unbalanced=unbalanced + 1, truncated=truncated,
                     passes=[])
    ordered = sorted(passes.items(), key=lambda item: (-item[1], item[0]))[:PASS_KEEP]
    return Frame(busy_us=busy, events=events, depth=depth, unbalanced=unbalanced,
                 truncated=truncated, passes=[(int(spec_id), int(micro))
                                              for spec_id, micro in ordered])


def frame_row(values: Mapping[str, Any], tid: int, data: Any) -> Optional[GpuFrameRow]:
    """One `GpuProfiler.Frame` record turned into the row the model keeps (None if unreadable)."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    number = values.get("RenderingFrameNumber")
    base = values.get("TimestampBase")
    decoded = decode_frame(bytes(data), int(base) if isinstance(base, (int, float)) else 0)
    return GpuFrameRow(
        tid=tid,
        number=int(number) if isinstance(number, (int, float)) else 0,
        base_us=int(base) if isinstance(base, (int, float)) else 0,
        busy_us=decoded.busy_us,
        events=decoded.events,
        depth=decoded.depth,
        unbalanced=decoded.unbalanced,
        truncated=decoded.truncated,
        passes=decoded.passes,
    )


def wall_seconds(base_us: int, anchor_us: int, scale: float, anchor_seconds: float) -> float:
    """A GPU timestamp placed on the session timeline: `anchor` plus the scaled difference.

    The two clocks are not the same clock: on the corpus the GPU timeline spans 215.175 s where the
    render thread's frames span 217.171 s, so a scale is measured rather than assumed (see
    `bottleneck.align_gpu`). The anchor is a single reference point -- a frame that is known to
    correspond -- and every other frame is placed relative to it.
    """
    return anchor_seconds + (base_us - anchor_us) * scale / 1000000.0


def specs_of(rows: Sequence[GpuSpecRow]) -> Dict[int, str]:
    """The id-to-name map, last declaration winning (the engine renames a spec in place)."""
    names: Dict[int, str] = {}
    for row in rows:
        names[int(row["id"])] = str(row["name"])
    return names


# ------------------------------------------------------------------------------------------
# The *current* channel: per-queue timelines (REFERENCE §19).
#
# The engine's own reader (`TraceServices/Private/Analyzers/GpuProfilerTraceAnalysis.cpp`) keeps
# two stacks per queue -- breadcrumbs (its stack 0) and work (its stack 1) -- and one queue state
# per `QueueId`; it treats the timestamps as *session cycles* (`FEventTime::AsSeconds(ts)`, the
# same conversion the CPU events get, because the platform RHI translates GPU timestamps into the
# CPU clock domain before they reach the profiler). This decoder follows it rule for rule, and
# where the engine warns (`NumInterleavedEvents`, `NumNegativeDurationEvents`, `NumMismatchedEvents`)
# this decoder counts, because a count is what a report can quote.
# ------------------------------------------------------------------------------------------

#: The intervals kept per queue per kind (`work`, `wait`) once they have been merged into a union.
#: Past the cap the tail merges into the last kept interval -- the union is then *overstated*,
#: which `gpu_spans_coarsened` counts, the same rule the coverage timelines keep.
QUEUE_SPAN_KEEP = 2048

#: (The kept-passes cap for the current channel is `QUEUE_PASS_KEEP`, up beside the legacy one.)

#: How many raw intervals a queue's list may hold before it is merged in place (a memory bound,
#: not a report bound: the merge is exact, the cap above is where the overstatement starts).
_MERGE_AT = 4 * QUEUE_SPAN_KEEP


def queue_id_parts(queue_id: int) -> Tuple[int, int, int]:
    """`QueueId` unpacked the engine reader's way: `(GPU, Index, Type)` bytes."""
    return (queue_id >> 8) & 0xFF, (queue_id >> 16) & 0xFF, queue_id & 0xFF


def queue_spec(values: Mapping[str, Any]) -> Optional[GpuQueueRow]:
    """One `GpuProfiler.QueueSpec` record as a model row, with the id unpacked and the name."""
    raw_id = values.get("QueueId")
    if not isinstance(raw_id, (int, float)):
        return None
    name = values.get("TypeString")
    gpu_byte, index, kind = queue_id_parts(int(raw_id))
    return GpuQueueRow(
        id=int(raw_id), gpu=gpu_byte, index=index, type=kind,
        name=name if isinstance(name, str) else "",
        boundaries=0, last_frame=0, work_spans=0, wait_spans=0, busy_us=0, wait_us=0,
        submits=0, lag_total_us=0, lag_negative=0, lag_max_us=0, draws=0, primitives=0,
    )


def breadcrumb_spec(values: Mapping[str, Any]) -> Optional[GpuBreadcrumbSpecRow]:
    """One `GpuProfiler.EventBreadcrumbSpec` record: its id, names and the field-name blob's size."""
    raw_id = values.get("SpecId")
    if not isinstance(raw_id, (int, float)):
        return None
    static_name = values.get("StaticName")
    name_format = values.get("NameFormat")
    fields = values.get("FieldNames")
    return GpuBreadcrumbSpecRow(
        spec=int(raw_id),
        static_name=static_name if isinstance(static_name, str) else "",
        name_format=name_format if isinstance(name_format, str) else "",
        fields=len(fields) if isinstance(fields, (bytes, bytearray)) else 0,
    )


def breadcrumb_name(spec: int, specs: Mapping[int, Mapping[str, Any]]) -> str:
    """A breadcrumb's display name: the format when it has one, the static name otherwise."""
    row = specs.get(spec)
    if row is None:
        return "spec %d" % (spec,)
    if row.get("name_format"):
        return str(row["name_format"])
    return str(row.get("static_name") or "") or "spec %d" % (spec,)


def _union(intervals: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Sorted, merged intervals: the union, so nested or touching spans count once."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [[ordered[0][0], ordered[0][1]]]
    for begin, end in ordered[1:]:
        if begin <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([begin, end])
    return [(begin, end) for begin, end in merged]


def _capped(intervals: List[Tuple[int, int]], keep: int) -> Tuple[List[Tuple[int, int]], int]:
    """The union, and how many intervals the `keep` cap had to fold away (coarsening)."""
    merged = _union(intervals)
    if len(merged) <= keep:
        return merged, 0
    tail = merged[keep - 1:]
    kept = merged[:keep - 1]
    kept.append((tail[0][0], max(end for _begin, end in tail)))
    return kept, len(merged) - keep


def union_total(spans: Sequence[Mapping[str, Any]], queue_id: int, kind: str) -> int:
    """The union microseconds of one queue's kept spans of one kind (`work` or `wait`).

    The model recomputes each queue row's totals from the spans it keeps, so a row's `busy_us` and
    the spans a frame placement reads can never disagree.
    """
    return sum(
        end - begin for begin, end in _union([
            (int(row["begin_us"]), int(row["end_us"])) for row in spans
            if int(row["queue"]) == queue_id and str(row["kind"]) == kind
        ])
    )


class _QueueState(object):
    """One queue's timeline as the walk builds it: open stacks, raw intervals, counters."""

    __slots__ = (
        "work_open", "crumb_open", "work", "wait", "last_ts", "coarsened",
    )

    def __init__(self) -> None:
        self.work_open: List[Tuple[int, int]] = []       # (GPUTimestampTOP, CPUTimestamp)
        self.crumb_open: List[Tuple[int, int]] = []      # (GPUTimestampTOP, SpecId)
        self.work: List[Tuple[int, int]] = []
        self.wait: List[Tuple[int, int]] = []
        self.last_ts = 0
        self.coarsened = 0

    def _trim(self, intervals: List[Tuple[int, int]]) -> None:
        if len(intervals) >= _MERGE_AT:
            merged = _union(intervals)
            intervals[:] = merged
            if len(intervals) > QUEUE_SPAN_KEEP:
                capped, dropped = _capped(intervals, QUEUE_SPAN_KEEP)
                intervals[:] = capped
                self.coarsened += dropped


class QueueWalk(object):
    """The current channel's decode for one thread's stream.

    The walk hands over every event whose name is in `QUEUE_EVENTS`, values already decoded
    through the capture's own schema; this class keeps the per-queue state and returns nothing --
    the rows come out of `flush` at the end of the stream, shaped for the thread share. Every
    anomaly the engine's reader warns about is *counted* in the walk's `counts` dict, never
    dropped silently and never allowed to cost the rest of the channel.
    """

    def __init__(self, counts: Dict[str, int]) -> None:
        self.counts = counts
        self.queues: Dict[int, _QueueState] = {}
        self.rows: Dict[int, GpuQueueRow] = {}
        self.passes: Dict[int, GpuPassRow] = {}
        self.fences: Dict[Tuple[str, int, int], int] = {}

    def _state(self, queue_id: int) -> _QueueState:
        state = self.queues.get(queue_id)
        if state is None:
            state = _QueueState()
            self.queues[queue_id] = state
        return state

    def _row(self, queue_id: int) -> GpuQueueRow:
        row = self.rows.get(queue_id)
        if row is None:
            gpu_byte, index, kind = queue_id_parts(queue_id)
            row = GpuQueueRow(
                id=queue_id, gpu=gpu_byte, index=index, type=kind, name="",
                boundaries=0, last_frame=0, work_spans=0, wait_spans=0, busy_us=0, wait_us=0,
                submits=0, lag_total_us=0, lag_negative=0, lag_max_us=0, draws=0, primitives=0,
            )
            self.rows[queue_id] = row
        return row

    def event(self, full_name: str, values: Mapping[str, Any]) -> None:
        """One current-channel event, dispatched by its name; absent fields are refused, not guessed."""
        self.counts["gpu_queue_events"] += 1
        handler = getattr(self, _HANDLERS.get(full_name, ""), None)
        if handler is not None:
            handler(values)

    def _ts(self, values: Mapping[str, Any], name: str) -> Optional[int]:
        value = values.get(name)
        return value if isinstance(value, int) else None

    def _work(self, values: Mapping[str, Any]) -> None:
        queue_id = self._ts(values, "QueueId")
        top = self._ts(values, "GPUTimestampTOP")
        if queue_id is None or top is None:
            return
        if top == 0:
            # the engine reader's own rule: a timestamp of 0 is "could not be determined", skipped
            self.counts["gpu_zero_timestamps"] += 1
            return
        state = self._state(queue_id)
        if top < state.last_ts:
            self.counts["gpu_out_of_order"] += 1
        state.last_ts = max(state.last_ts, top)
        cpu = self._ts(values, "CPUTimestamp")
        row = self._row(queue_id)
        row["submits"] += 1
        # a CPUTimestamp of 0 is "not determined", the same convention the GPU timestamps follow:
        # it measures no lag and would poison the mean with the whole timeline's length
        if cpu is not None and cpu > 0:
            if cpu > top:
                row["lag_negative"] += 1
            else:
                row["lag_total_us"] += top - cpu
                if top - cpu > row["lag_max_us"]:
                    row["lag_max_us"] = top - cpu
        state.work_open.append((top, cpu or 0))

    def _end_work(self, values: Mapping[str, Any]) -> None:
        queue_id = self._ts(values, "QueueId")
        bop = self._ts(values, "GPUTimestampBOP")
        if queue_id is None or bop is None:
            return
        if bop == 0:
            self.counts["gpu_zero_timestamps"] += 1
            return
        state = self._state(queue_id)
        if bop > state.last_ts:
            state.last_ts = bop
        if not state.work_open:
            self.counts["gpu_work_unpaired"] += 1
            return
        top, _cpu = state.work_open.pop()
        if bop < top:
            self.counts["gpu_negative_durations"] += 1
            return
        self._row(queue_id)["work_spans"] += 1
        self.counts["gpu_work_spans"] += 1
        state.work.append((top, bop))
        state._trim(state.work)

    def _wait(self, values: Mapping[str, Any]) -> None:
        queue_id = self._ts(values, "QueueId")
        start = self._ts(values, "StartTime")
        end = self._ts(values, "EndTime")
        if queue_id is None or start is None or end is None:
            return
        state = self._state(queue_id)
        row = self._row(queue_id)
        if start == 0 and end == 0:
            self.counts["gpu_zero_timestamps"] += 1
            return
        if end < start:
            self.counts["gpu_negative_durations"] += 1
            return
        row["wait_spans"] += 1
        self.counts["gpu_wait_spans"] += 1
        state.wait.append((start, end))
        state._trim(state.wait)

    def _boundary(self, values: Mapping[str, Any]) -> None:
        queue_id = self._ts(values, "QueueId")
        number = self._ts(values, "FrameNumber")
        if queue_id is None or number is None:
            return
        row = self._row(queue_id)
        row["boundaries"] += 1
        row["last_frame"] = number
        self.counts["gpu_frame_boundaries"] += 1

    def _stats(self, values: Mapping[str, Any]) -> None:
        queue_id = self._ts(values, "QueueId")
        if queue_id is None:
            return
        row = self._row(queue_id)
        row["draws"] += self._ts(values, "NumDraws") or 0
        row["primitives"] += self._ts(values, "NumPrimitives") or 0
        self.counts["gpu_stats_events"] += 1

    def _begin_crumb(self, values: Mapping[str, Any]) -> None:
        queue_id = self._ts(values, "QueueId")
        top = self._ts(values, "GPUTimestampTOP")
        spec = self._ts(values, "SpecId")
        if queue_id is None or top is None:
            return
        if top == 0:
            self.counts["gpu_zero_timestamps"] += 1
            return
        if spec is None:
            self.counts["gpu_breadcrumb_no_spec"] += 1
            spec = 0
        state = self._state(queue_id)
        if top < state.last_ts:
            self.counts["gpu_out_of_order"] += 1
        state.last_ts = max(state.last_ts, top)
        state.crumb_open.append((top, spec))

    def _end_crumb(self, values: Mapping[str, Any]) -> None:
        queue_id = self._ts(values, "QueueId")
        bop = self._ts(values, "GPUTimestampBOP")
        if queue_id is None or bop is None:
            return
        if bop == 0:
            self.counts["gpu_zero_timestamps"] += 1
            return
        state = self._state(queue_id)
        if bop > state.last_ts:
            state.last_ts = bop
        if not state.crumb_open:
            self.counts["gpu_work_unpaired"] += 1
            return
        top, spec = state.crumb_open.pop()
        if bop < top:
            self.counts["gpu_negative_durations"] += 1
            return
        row = self.passes.get(spec)
        if row is None:
            row = GpuPassRow(spec=spec, calls=0, inclusive_us=0, max_us=0,
                             max_begin_us=top, max_end_us=bop)
            self.passes[spec] = row
        span = bop - top
        row["calls"] += 1
        row["inclusive_us"] += span
        if span > row["max_us"]:
            row["max_us"] = span
            row["max_begin_us"] = top
            row["max_end_us"] = bop

    def _fence(self, values: Mapping[str, Any], kind: str, other: int) -> None:
        queue_id = self._ts(values, "QueueId")
        if queue_id is None:
            return
        key = (kind, queue_id, other)
        self.fences[key] = self.fences.get(key, 0) + 1
        self.counts["gpu_fence_events"] += 1

    def _signal(self, values: Mapping[str, Any]) -> None:
        self._fence(values, "signal", 0)

    def _wait_fence(self, values: Mapping[str, Any]) -> None:
        other = self._ts(values, "QueueToWaitForId")
        if other is None:
            return  # a fence wait that does not name its queue names nothing, so it counts nothing
        self._fence(values, "wait", other)

    def flush(self) -> Tuple[List[GpuQueueRow], List[GpuSpanRow], List[GpuPassRow],
                             List[GpuFenceRow]]:
        """The walk's rows: queues (with their union totals), kept spans, passes and fences.

        Called once, at the end of the thread's stream. Open work and breadcrumb brackets are
        counted (`gpu_work_unpaired`) rather than paired with an invented end; the intervals are
        merged into their union and capped at `QUEUE_SPAN_KEEP` per queue and kind, the fold counted
        in `gpu_spans_coarsened`.
        """
        spans: List[GpuSpanRow] = []
        for queue_id, state in self.queues.items():
            row = self._row(queue_id)
            for kind, raw in (("work", state.work), ("wait", state.wait)):
                merged, dropped = _capped(raw, QUEUE_SPAN_KEEP)
                state.coarsened += dropped
                total = 0
                for begin, end in merged:
                    total += end - begin
                    if kind == "work":
                        spans.append(GpuSpanRow(queue=queue_id, kind="work",
                                                begin_us=begin, end_us=end))
                    else:
                        spans.append(GpuSpanRow(queue=queue_id, kind="wait",
                                                begin_us=begin, end_us=end))
                if kind == "work":
                    row["busy_us"] = total
                else:
                    row["wait_us"] = total
            self.counts["gpu_spans_coarsened"] += state.coarsened
            self.counts["gpu_work_unpaired"] += len(state.work_open)
            self.counts["gpu_work_unpaired"] += len(state.crumb_open)
        fences = [
            GpuFenceRow(kind=kind, queue=queue_id, other=other, count=count)
            for (kind, queue_id, other), count in sorted(self.fences.items())
        ]
        ordered_rows = [self.rows[queue_id] for queue_id in sorted(self.rows)]
        ordered_passes = sorted(
            self.passes.values(), key=lambda row: (-row["inclusive_us"], row["spec"]),
        )
        return ordered_rows, spans, ordered_passes, fences


#: Event name -> the `QueueWalk` method that answers it (`event` dispatches through this).
_HANDLERS: Dict[str, str] = {
    BEGIN_WORK_EVENT: "_work",
    END_WORK_EVENT: "_end_work",
    WAIT_EVENT: "_wait",
    FRAME_BOUNDARY_EVENT: "_boundary",
    STATS_EVENT: "_stats",
    BEGIN_BREADCRUMB_EVENT: "_begin_crumb",
    END_BREADCRUMB_EVENT: "_end_crumb",
    SIGNAL_FENCE_EVENT: "_signal",
    WAIT_FENCE_EVENT: "_wait_fence",
}
