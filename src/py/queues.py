"""The GPU channel's queue timelines, and where they touch the CPU's frames (REFERENCE §19).

The *current* `GpuProfiler` channel is the per-event one the engine's modern RHIs write: queues
declared by `QueueSpec`, work bracketed by `EventBeginWork`/`EventEndWork`, waits self-contained in
`EventWait`, names carried by the breadcrumbs, fences and draw counts alongside. This module turns
the model's rows into the queue-side report:

* **per queue** -- the union of its work intervals (the time the queue was executing traced work),
  the union of its waits, the submit-to-start lag and the draw counts;
* **per pass** -- the named breadcrumbs by inclusive time, with the biggest single span and when it
  ran, which is the drill-down the work spans cannot name;
* **per frame** -- the GPU's busy time inside each of the capture's `Misc.BeginFrame` windows, which
  is the number the bottleneck verdict spends and the frame-time report is missing without it.

One clock, stated plainly: the current channel's timestamps are expected to be **in the CPU clock
domain already** -- the platform RHI translates GPU timestamps before they reach the profiler, and
the engine's own reader converts them with the session's clock (`Analysis/Engine.cpp`'s
`FEventTime::AsSeconds`, the same conversion the CPU cycles get). So unlike the legacy channel
(REFERENCE §11, which fits a scale between two clocks), placement here uses the same base and
frequency as the CPU events -- and it is **checked, not assumed**: too few spans landing inside a
frame window means the placement is refused and the GPU side reads as unknown, because an unplaced
GPU number is not evidence about a frame.

Absent is not zero, one level down: work without breadcrumb specs is a timeline no pass is named
for (breadcrumbs are conditionally compiled into the engine), and a capture can carry either
channel shape, both, or neither -- each of those is its own sentence in the report.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import gpu
import summary

#: A queue timeline is placed on a frame series only when at least this share of its work spans
#: lands inside a frame window (the same rule the legacy alignment keeps, REFERENCE §11).
PLACED_MIN_SHARE = 0.5


class Placement(NamedTuple):
    """How the queue spans sat against one frame series, and what each frame got."""

    #: id(frame row) -> the GPU's busy (resp. waiting) microseconds inside that frame's window.
    work_us: Dict[int, float]
    wait_us: Dict[int, float]
    placed: int
    total: int

    def describe(self) -> str:
        return ("%d of %d work span(s) land inside a frame window (one clock, the session's); the "
                "rest are outside every frame and are not evidence about one"
                % (self.placed, self.total))


class FrameGpu(NamedTuple):
    """One frame of the judged series with the GPU time the queues spent inside it.

    `frame` is the model's own frame index, so a reader can line the row up with `frames` or
    `summary`; it is not named `index` for the same reason `shapes.PacketRow` is a dataclass --
    a NamedTuple field may not shadow a tuple method.
    """

    frame: int
    at_s: Optional[float]
    ms: float
    gpu_ms: Optional[float]
    gpu_wait_ms: Optional[float]


class PassRow(NamedTuple):
    """One named pass, ready for the table: the name, the totals, and where the biggest one ran."""

    name: str
    calls: int
    inclusive_ms: float
    max_ms: float
    at_s: Optional[float]


class QueueReportRow(NamedTuple):
    """One queue of the capture, with the numbers the table prints."""

    id: int
    gpu: int
    queue_index: int
    type: int
    name: str
    busy_ms: float
    wait_ms: float
    work_spans: int
    wait_spans: int
    submits: int
    lag_mean_ms: Optional[float]
    lag_max_ms: Optional[float]
    lag_negative: int
    draws: int
    primitives: int
    boundaries: int
    last_frame: int


class Report(NamedTuple):
    """The whole queue-side answer for one capture."""

    version: int
    legacy_frames: int
    has_queue_data: bool
    queues: List[QueueReportRow]
    passes: List[PassRow]
    frames: Optional[List[FrameGpu]]
    placement: Optional[Placement]
    fences: List[Tuple[str, int, int, int]]
    notes: List[str]


def _session(model: Mapping[str, Any]) -> Mapping[str, Any]:
    session = model.get("session", {})
    return session if isinstance(session, dict) else {}


def frequency_of(model: Mapping[str, Any]) -> int:
    """The capture's cycle frequency (0 when it declares none): what both clocks are read in."""
    return int(_session(model).get("cycle_frequency", 0) or 0)


def _start_cycle(model: Mapping[str, Any]) -> int:
    return int(_session(model).get("start_cycle", 0) or 0)


def _seconds_at(model: Mapping[str, Any], value: int) -> Optional[float]:
    """A session-clock value (a CPU cycle or a channel timestamp) as seconds since the start."""
    frequency = frequency_of(model)
    start = _start_cycle(model)
    if not frequency or not start:
        return None
    return (value - start) / float(frequency)


def _row_list(model: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    value = model.get(key, [])
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _sweep(spans: Sequence[Mapping[str, Any]], windows: Sequence[Mapping[str, Any]],
           scale_us: float, into: Dict[int, float]) -> int:
    """Clip sorted spans into sorted, disjoint frame windows; returns how many landed in one.

    Both sides are one-directional sweeps (the same shape as the frame attribution's cursor): a
    window that ends before a span begins ends before every later span begins too, so the cursor
    never moves back, and a window a long span crosses is read by the span's inner scan.
    """
    cursor = 0
    landed = 0
    for row in spans:
        begin, end = int(row["begin_us"]), int(row["end_us"])
        if end <= begin:
            continue
        while cursor < len(windows) and int(windows[cursor]["end_cycle"]) * scale_us <= begin:
            cursor += 1
        scan = cursor
        hit = False
        while scan < len(windows) and int(windows[scan]["begin_cycle"]) * scale_us < end:
            window = windows[scan]
            inside = min(end, int(window["end_cycle"]) * scale_us) \
                - max(begin, int(window["begin_cycle"]) * scale_us)
            if inside > 0:
                key = id(window)
                into[key] = into.get(key, 0) + inside
                hit = True
            scan += 1
        if hit:
            landed += 1
    return landed


def align(model: Mapping[str, Any], series: summary.Series,
          frequency: int) -> Optional[Placement]:
    """The GPU's per-frame time on `series`, or None when the spans refuse to sit on the windows.

    Windows are in cycles, spans in the channel's timestamps -- the same domain, per the engine
    reader's own conversion -- so the windows are converted once and compared directly. A timeline
    that does not overlap the windows at all, or that puts fewer than `PLACED_MIN_SHARE` of its
    work spans inside one, is refused: the queue totals stand, the per-frame numbers do not.
    """
    work_spans = [row for row in _row_list(model, "gpu_spans") if str(row.get("kind")) == "work"]
    if not work_spans or not series.rows or not frequency:
        return None
    windows = sorted(series.rows, key=lambda row: int(row["begin_cycle"]))
    scale_us = 1000000.0 / frequency
    low = int(windows[0]["begin_cycle"]) * scale_us
    high = int(windows[-1]["end_cycle"]) * scale_us
    timeline_lo = min(int(row["begin_us"]) for row in work_spans)
    timeline_hi = max(int(row["end_us"]) for row in work_spans)
    if timeline_hi <= low or timeline_lo >= high:
        return None
    work_us: Dict[int, float] = {}
    wait_us: Dict[int, float] = {}
    ordered = sorted(work_spans, key=lambda row: (int(row["begin_us"]), int(row["end_us"])))
    placed = _sweep(ordered, windows, scale_us, work_us)
    waits = [row for row in _row_list(model, "gpu_spans") if str(row.get("kind")) == "wait"]
    if waits:
        ordered_waits = sorted(waits, key=lambda row: (int(row["begin_us"]), int(row["end_us"])))
        _sweep(ordered_waits, windows, scale_us, wait_us)
    if placed < len(ordered) * PLACED_MIN_SHARE:
        return None
    return Placement(work_us=work_us, wait_us=wait_us, placed=placed, total=len(ordered))


def _lag_mean(row: Mapping[str, Any]) -> Optional[float]:
    """A queue's mean submit-to-start lag, or None when nothing positive was measured."""
    negative = int(row.get("lag_negative", 0))
    positive = int(row.get("submits", 0)) - negative
    if positive <= 0:
        return None
    return int(row.get("lag_total_us", 0)) / positive / 1000.0


def report_of(model: Mapping[str, Any], series: Optional[summary.Series]) -> Report:
    """The whole queue-side report; `series` is what the frames are placed on (None allowed)."""
    frequency = frequency_of(model)
    queue_rows = _row_list(model, "gpu_queues")
    specs = {int(row["spec"]): row for row in _row_list(model, "gpu_breadcrumb_specs")}
    counts = model.get("counts", {})
    counts = counts if isinstance(counts, dict) else {}
    notes: List[str] = []

    rows: List[QueueReportRow] = []
    for row in sorted(queue_rows, key=lambda item: int(item.get("id", 0))):
        lag_max_us = int(row.get("lag_max_us", 0))
        rows.append(QueueReportRow(
            id=int(row.get("id", 0)), gpu=int(row.get("gpu", 0)),
            queue_index=int(row.get("index", 0)),
            type=int(row.get("type", 0)), name=str(row.get("name", "")),
            busy_ms=int(row.get("busy_us", 0)) / 1000.0,
            wait_ms=int(row.get("wait_us", 0)) / 1000.0,
            work_spans=int(row.get("work_spans", 0)), wait_spans=int(row.get("wait_spans", 0)),
            submits=int(row.get("submits", 0)), lag_mean_ms=_lag_mean(row),
            lag_max_ms=lag_max_us / 1000.0 if lag_max_us else None,
            lag_negative=int(row.get("lag_negative", 0)), draws=int(row.get("draws", 0)),
            primitives=int(row.get("primitives", 0)), boundaries=int(row.get("boundaries", 0)),
            last_frame=int(row.get("last_frame", 0)),
        ))

    passes: List[PassRow] = []
    for row in sorted(_row_list(model, "gpu_passes"),
                      key=lambda item: (-int(item.get("inclusive_us", 0)), int(item.get("spec", 0)))):
        begin_us = int(row.get("max_begin_us", 0))
        passes.append(PassRow(
            name=gpu.breadcrumb_name(int(row.get("spec", 0)), specs),
            calls=int(row.get("calls", 0)), inclusive_ms=int(row.get("inclusive_us", 0)) / 1000.0,
            max_ms=int(row.get("max_us", 0)) / 1000.0,
            at_s=_seconds_at(model, begin_us),
        ))

    placement: Optional[Placement] = None
    frames: Optional[List[FrameGpu]] = None
    # events, not declarations: a bare QueueSpec names a queue but is no evidence about its work
    has_queue_data = int(counts.get("gpu_queue_events", 0)) > 0
    if series is not None and frequency:
        placement = align(model, series, frequency)
        if placement is None and has_queue_data:
            # a refusal only means something when there was a timeline to place; a capture whose
            # queue half is absent gets its own note below
            notes.append("the queue timeline could not be placed on this frame series: the "
                         "per-frame GPU numbers here are unknown (the queue totals above stand)")
        elif placement is not None:
            notes.append("queue timeline: %s" % (placement.describe(),))
            frames = []
            for row in series.rows:
                work = placement.work_us.get(id(row))
                wait = placement.wait_us.get(id(row))
                frames.append(FrameGpu(
                    frame=int(row["index"]),
                    at_s=_seconds_at(model, int(row["begin_cycle"])),
                    ms=(int(row["end_cycle"]) - int(row["begin_cycle"])) * 1000.0 / frequency,
                    gpu_ms=work / 1000.0 if work is not None else None,
                    gpu_wait_ms=wait / 1000.0 if wait is not None else None,
                ))
            frames.sort(key=lambda item: (-(item.gpu_ms or 0.0), item.frame))
    elif series is not None:
        notes.append("the capture declares no cycle frequency, so the queue spans cannot be placed "
                     "on the frame windows")

    version = int(_session(model).get("gpu_channel_version", 0) or 0)
    if version:
        notes.append("channel version: the capture's GpuProfiler.Init says version %d" % (version,))
    if not has_queue_data and not rows:
        notes.append("the current GpuProfiler channel is absent from this capture: no QueueSpec, no "
                     "work, no waits (the queue half of this report is skipped, not zero)")
    elif not has_queue_data:
        notes.append("the channel is declared but silent: queue spec(s) without a single work, "
                     "wait or boundary event, so there is nothing here to time")
    elif not passes:
        notes.append("the capture carries queue work but no breadcrumb specs: no pass is named "
                     "(breadcrumbs may be compiled out; the work and wait totals stand)")
    for label, key in (("unpaired work bracket", "gpu_work_unpaired"),
                       ("negative duration", "gpu_negative_durations"),
                       ("zero-timestamp event", "gpu_zero_timestamps"),
                       ("out-of-order timestamp", "gpu_out_of_order"),
                       ("breadcrumb with no spec", "gpu_breadcrumb_no_spec")):
        value = int(counts.get(key, 0))
        if value:
            notes.append("%s: %d (counted, never guessed away)" % (label, value))
    dropped = int(counts.get("gpu_passes_dropped", 0))
    if dropped:
        notes.append("%d lesser pass(es) dropped by the kept-passes cap (%d)" % (
            dropped, gpu.QUEUE_PASS_KEEP))
    coarsened = int(counts.get("gpu_spans_coarsened", 0))
    if coarsened:
        notes.append("%d interval(s) folded by the per-queue span cap: the unions are overstated "
                     "where it happened, never the answer silently cut" % (coarsened,))

    fences: List[Tuple[str, int, int, int]] = [
        (str(row.get("kind")), int(row.get("queue", 0)), int(row.get("other", 0)),
         int(row.get("count", 0)))
        for row in _row_list(model, "gpu_fences")
    ]
    legacy = len(_row_list(model, "gpu_frames"))
    if legacy:
        notes.append("the legacy channel is present too: %d rendered frame(s); `bottleneck` judges "
                     "the frames and its GPU evidence names those passes" % (legacy,))
    return Report(version=version, legacy_frames=legacy, has_queue_data=has_queue_data,
                  queues=rows, passes=passes, frames=frames, placement=placement, fences=fences,
                  notes=notes)


QUEUE_TABLE_HEADERS = (
    "queue", "gpu", "index", "type", "name", "busy ms", "wait ms", "work", "waits",
    "lag mean ms", "lag max ms", "draws", "prims", "frame ends",
)
PASS_TABLE_HEADERS = ("pass", "calls", "inclusive ms", "biggest ms", "at s")
FRAME_TABLE_HEADERS = ("frame", "at s", "ms", "gpu ms", "gpu wait ms")

#: The tables the command can print, and what each needs (the exit-2 rules are the command's).
TABLES = ("queues", "passes", "frames")


def table_rows(report: Report, table: str, limit: int) -> List[Tuple[str, ...]]:
    """The chosen table's rows, capped (`limit` 0 = all)."""
    if table == "queues":
        source: Sequence[Any] = report.queues
    elif table == "passes":
        source = report.passes
    else:
        source = report.frames or []
    if limit > 0:
        source = list(source)[:limit]
    rows: List[Tuple[str, ...]] = []
    for item in source:
        if isinstance(item, QueueReportRow):
            rows.append((
                str(item.id), str(item.gpu), str(item.queue_index), str(item.type),
                item.name or "-", "%.3f" % (item.busy_ms,), "%.3f" % (item.wait_ms,),
                str(item.work_spans), str(item.wait_spans),
                "-" if item.lag_mean_ms is None else "%.3f" % (item.lag_mean_ms,),
                "-" if item.lag_max_ms is None else "%.3f" % (item.lag_max_ms,),
                str(item.draws), str(item.primitives), str(item.boundaries),
            ))
        elif isinstance(item, PassRow):
            rows.append((
                item.name, str(item.calls), "%.3f" % (item.inclusive_ms,),
                "%.3f" % (item.max_ms,), "-" if item.at_s is None else "%.3f" % (item.at_s,),
            ))
        else:
            rows.append((
                str(item.frame), "-" if item.at_s is None else "%.3f" % (item.at_s,),
                "%.3f" % (item.ms,),
                "-" if item.gpu_ms is None else "%.3f" % (item.gpu_ms,),
                "-" if item.gpu_wait_ms is None else "%.3f" % (item.gpu_wait_ms,),
            ))
    return rows


def prose_lines(report: Report) -> List[str]:
    """The prose that surrounds the table: what the capture carries, and what the queues say."""
    lines: List[str] = []
    lines.append("channel   : legacy frames %d, current-channel events %s" % (
        report.legacy_frames, "present" if report.has_queue_data else "absent"))
    for queue in report.queues:
        lines.append("queue %d    : %s (gpu %d, index %d, type %d) -- busy %.3f ms, wait %.3f ms "
                     "over %d work / %d wait span(s)"
                     % (queue.id, queue.name or "-", queue.gpu, queue.queue_index, queue.type,
                        queue.busy_ms, queue.wait_ms, queue.work_spans, queue.wait_spans))
        lag = ""
        if queue.lag_mean_ms is not None:
            lag = "submit->start lag mean %.3f ms, max %.3f ms" % (
                queue.lag_mean_ms, queue.lag_max_ms or 0.0)
        if queue.lag_negative:
            lag = "%s, %d submit(s) with the CPU timestamp after the GPU start" % (
                lag, queue.lag_negative) if lag else \
                "%d submit(s) with the CPU timestamp after the GPU start" % (queue.lag_negative,)
        if lag:
            lines.append("            %s" % (lag,))
        if queue.draws or queue.primitives:
            lines.append("            %d draw call(s), %d primitive(s) over %d frame end(s)"
                         % (queue.draws, queue.primitives, queue.boundaries))
        elif queue.boundaries:
            lines.append("            last frame boundary says frame %d" % (queue.last_frame,))
    for kind, queue_id, other, count in report.fences:
        if kind == "wait":
            lines.append("fence      : queue %d waited on queue %d, %d time(s)" % (
                queue_id, other, count))
        else:
            lines.append("fence      : queue %d signalled a fence, %d time(s)" % (queue_id, count))
    lines.extend("note      : %s" % (note,) for note in report.notes)
    return lines
