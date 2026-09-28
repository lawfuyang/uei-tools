"""The CSV Profiler's data inside a trace, and the `.csv` the CsvTools executables read.

Both halves are the engine's, not ours:

* **The trace side.** The CSV Profiler's logger writes its definitions (`RegisterCategory`,
  `DefineInlineStat`, `DefineDeclaredStat`) to the important stream and its per-frame values to the
  thread streams, all on the `counters` channel (REFERENCE §5, §8). The definitions are in the model
  already (`csv_stats`, `csv_categories`); the values are collected here, with the same walk the
  model uses -- the cache does not hold them, so `csv from-trace` always reads the streams.
* **The file side.** `CsvStats.ReadCSVFromLines` (`CsvStats.cs`, read 2026-09-28) is the acceptance
  test this writer is built to pass: the first line is `EVENTS,<series names>`; every following line
  is one frame -- a `;`-separated events column (each `name##seconds`) and one value per series; the
  **last** line is `[Key],Value` metadata; numbers are `%.0f` when integral, `%.6f` below 0.1 and
  `%.4f` otherwise; and a series with no value in a frame is `0`, never blank.

Frames come from the trace's own `Misc.BeginFrame`/`EndFrame` pairs, because the CSV Profiler's
frame counter is not in the trace: the frames of the thread that emitted the values are taken as the
capture's frames, and the metadata line says so (`[FramesFrom]`). A value that falls before the
first frame is counted as dropped rather than attributed to a neighbour, and the command reports
that count rather than hiding it.
"""

from __future__ import annotations

import bisect
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, TypedDict

import decode
import events
import schema
from container import Anomaly
from model import zero_counts
from shapes import (
    CsvStatRow,
    FrameRow,
    TID_EVENTS,
    TID_IMPORTANTS,
    TOOL_VERSION,
)
from streams import StreamSet

#: The CSV Profiler's logger and events, as they appear in a capture's own vocabulary.
LOGGER = "CsvProfiler."
BEGIN_CAPTURE = LOGGER + "BeginCapture"
END_CAPTURE = LOGGER + "EndCapture"
BEGIN_STAT = LOGGER + "BeginStat"
END_STAT = LOGGER + "EndStat"
BEGIN_EXCLUSIVE = LOGGER + "BeginExclusiveStat"
END_EXCLUSIVE = LOGGER + "EndExclusiveStat"
CUSTOM_INT = LOGGER + "CustomStatInt"
CUSTOM_FLOAT = LOGGER + "CustomStatFloat"
EVENT = LOGGER + "Event"

#: `ECsvCustomStatOp` (`CsvProfiler.h`): how repeated custom values in one frame combine.
OP_SET, OP_MIN, OP_MAX, OP_ACCUMULATE = 0, 1, 2, 3

#: The category name used when the capture never registered one for a stat.
UNCATEGORISED = "Uncategorised"


class CsvValues(TypedDict):
    """What a trace holds for the CSV Profiler, and what it cost to find out."""

    frames: List[Tuple[int, int]]
    frames_from: str
    series: Dict[str, Dict[int, float]]
    events: Dict[int, List[Tuple[str, float]]]
    stat_count: int
    value_events: int
    dropped: int
    timers: int
    custom: int
    threads_walked: int


class _RawValue(TypedDict):
    cycle: int
    tid: int
    stat_id: str
    value: float
    op: int
    timer: bool


class _RawEvent(TypedDict):
    cycle: int
    text: str
    category: int


def _category(categories: Dict[str, str], index: int) -> str:
    return categories.get(str(index), UNCATEGORISED)


def _frame_of(cycle: int, begins: Sequence[int]) -> Optional[int]:
    """The frame a cycle falls in, by frame *begins*: None when it is before the first one."""
    if not begins:
        return None
    index = bisect.bisect_right(begins, cycle) - 1
    return index if index >= 0 else None


def _combine(series: Dict[int, float], frame: int, value: float, op: int) -> None:
    if op == OP_MIN:
        series[frame] = min(series[frame], value) if frame in series else value
    elif op == OP_MAX:
        series[frame] = max(series[frame], value) if frame in series else value
    elif op == OP_ACCUMULATE:
        series[frame] = series.get(frame, 0.0) + value
    else:
        series[frame] = value


def _frames_of(frame_rows: Sequence[FrameRow], tid: int) -> List[Tuple[int, int]]:
    """One thread's frame pairs, ordered: the capture's own frame boundaries."""
    pairs = [
        (int(row["begin_cycle"]), int(row["end_cycle"]))
        for row in frame_rows
        if int(row["tid"]) == tid and int(row["type"]) == 0
    ]
    pairs.sort()
    return pairs


def collect(
    stream_set: StreamSet,
    registry: schema.SchemaRegistry,
    csv_stats: Sequence[CsvStatRow],
    categories: Dict[str, str],
    frame_rows: Sequence[FrameRow],
    session: Dict[str, Any],
    thread_labels: Dict[int, str],
) -> CsvValues:
    """Walk every thread stream for the CSV Profiler's values, attributed to the capture's frames.

    One pass, serial: the values live in the thread streams, so this costs what the model's own walk
    costs. The stats' definitions decide the series names: `<Thread>/<Category>/<Stat>` for a timed
    stat (the thread it was recorded on) and `<Category>/<Stat>` for a custom one, which is the
    naming the engine's own writer uses.
    """
    stats = {int(row["id"]): row for row in csv_stats}
    values_found: List[_RawValue] = []
    events_found: List[_RawEvent] = []
    pending: Dict[Tuple[int, int], int] = {}
    anomalies: List[Anomaly] = []
    threads_walked = 0
    timers = 0
    custom = 0

    for tid in sorted(stream_set.streams):
        if tid in (TID_EVENTS, TID_IMPORTANTS):
            continue
        threads_walked += 1
        stream = stream_set.streams[tid]
        for event in events.iter_thread_events(stream, tid, registry, anomalies, zero_counts()):
            if event.b_scope:
                continue
            row = registry.get(event.uid)
            if row is None:
                continue
            full_name = str(row["full_name"])
            if not full_name.startswith(LOGGER):
                continue
            decoded = decode.event_values(row, stream, event)
            cycle = decode.value_int(decoded, "Cycle")
            if cycle is None:
                continue
            if full_name == EVENT:
                text = decode.value_str(decoded, "Text")
                category = decode.value_int(decoded, "CategoryIndex")
                events_found.append(_RawEvent(
                    cycle=cycle, text=text, category=0 if category is None else category,
                ))
                continue
            stat_id = decode.value_int(decoded, "StatId")
            if stat_id is None:
                continue
            key = (tid, stat_id)
            if full_name in (BEGIN_STAT, BEGIN_EXCLUSIVE):
                pending[key] = cycle
                continue
            if full_name in (END_STAT, END_EXCLUSIVE):
                started = pending.pop(key, None)
                if started is None:
                    continue
                values_found.append(_RawValue(
                    cycle=cycle, tid=tid, stat_id=str(stat_id), value=float(cycle - started),
                    op=0, timer=True,
                ))
                timers += 1
                continue
            if full_name in (CUSTOM_INT, CUSTOM_FLOAT):
                raw_value = decoded.get("Value")
                if not isinstance(raw_value, (int, float)):
                    continue
                op = decode.value_int(decoded, "OpType")
                values_found.append(_RawValue(
                    cycle=cycle, tid=tid, stat_id=str(stat_id), value=float(raw_value),
                    op=0 if op is None else op, timer=False,
                ))
                custom += 1

    # the thread that emitted the values is the thread whose frames the capture ran on
    emits = Counter(item["tid"] for item in values_found)
    dominant = emits.most_common(1)[0][0] if emits else min(stream_set.streams, default=0)
    frames = _frames_of(frame_rows, dominant)
    begins = [begin for begin, _end in frames]

    frequency = session.get("cycle_frequency")
    start_cycle = session.get("start_cycle")
    per_second = float(frequency) if isinstance(frequency, (int, float)) and frequency else 0.0
    base = int(start_cycle) if isinstance(start_cycle, int) else (begins[0] if begins else 0)

    series: Dict[str, Dict[int, float]] = {}
    dropped = 0
    for item in values_found:
        frame = _frame_of(item["cycle"], begins)
        if frame is None:
            dropped += 1
            continue
        row = stats.get(int(item["stat_id"]))
        stat_name = str(row["name"]) if row is not None else "Stat%s" % (item["stat_id"],)
        category = _category(categories, int(row["category"])) if row is not None else UNCATEGORISED
        if item["timer"]:
            label = thread_labels.get(item["tid"], "tid %d" % (item["tid"],))
            name = "%s/%s/%s" % (label, category, stat_name)
            milliseconds = item["value"] / per_second * 1000.0 if per_second else 0.0
            bucket = series.setdefault(name, {})
            bucket[frame] = bucket.get(frame, 0.0) + milliseconds
        else:
            name = "%s/%s" % (category, stat_name)
            _combine(series.setdefault(name, {}), frame, item["value"], item["op"])

    csv_events: Dict[int, List[Tuple[str, float]]] = {}
    for item in events_found:
        frame = _frame_of(item["cycle"], begins)
        if frame is None:
            dropped += 1
            continue
        category = _category(categories, item["category"])
        label = "%s/%s" % (category, item["text"]) if item["text"] else category
        seconds = (item["cycle"] - base) / per_second if per_second else 0.0
        csv_events.setdefault(frame, []).append((label, seconds))

    frames_from = (
        "tid %d (Misc.BeginFrame/Misc.EndFrame pairs)" % (dominant,)
        if frames else "none (this capture has no Misc.BeginFrame events)"
    )
    return CsvValues(
        frames=frames,
        frames_from=frames_from,
        series=series,
        events=csv_events,
        stat_count=len(stats),
        value_events=timers + custom + len(events_found),
        dropped=dropped,
        timers=timers,
        custom=custom,
        threads_walked=threads_walked,
    )


def format_number(value: float) -> str:
    """The engine's own number formatting (`FCsvWriterHelper::WriteValue`), so files look alike."""
    if value == int(value):
        return "%.0f" % (value,)
    if abs(value) < 0.1:
        return "%.6f" % (value,)
    return "%.4f" % (value,)


def _sanitise(text: str) -> str:
    """The engine replaces the separators inside event text rather than quoting it."""
    return text.replace(",", ".").replace(";", ".")


def default_metadata(values: CsvValues, source: str) -> List[Tuple[str, str]]:
    """What the synthesized file says about itself -- honest, and never a machine path."""
    return [
        ("EventTimestamps", "1"),
        ("FramesFrom", values["frames_from"]),
        ("SynthesizedBy", "ueia %s (csv from-trace)" % (TOOL_VERSION,)),
        ("Source", source),
    ]


def write_csv(values: CsvValues, path: Path, metadata: Sequence[Tuple[str, str]]) -> int:
    """Write the values as the `.csv` the CsvTools executables parse; returns the bytes written."""
    names = sorted(values["series"])
    lines: List[str] = [",".join(["EVENTS"] + names)]
    for index in range(len(values["frames"])):
        stamps = values["events"].get(index, [])
        column = ";".join("%s##%.6f" % (_sanitise(text), seconds) for text, seconds in stamps)
        row = [column]
        for name in names:
            row.append(format_number(values["series"][name].get(index, 0.0)))
        lines.append(",".join(row))
    entries: List[str] = []
    for key, value in metadata:
        entries.extend(["[%s]" % (key,), _sanitise(value)])
    lines.append(",".join(entries))
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8", newline="")
    return len(text.encode("utf-8"))
