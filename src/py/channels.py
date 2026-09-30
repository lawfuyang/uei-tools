"""What a capture carries, and what it can therefore answer (REFERENCE §18).

Every trace declares its own channel registry, each row with an `is_enabled` flag: the *file* says
what was recorded, without decoding a single event. And an absent channel is **not** a zero value --
it is a question the capture cannot answer (REFERENCE §8) -- so this module turns that registry into
a per-analysis verdict, names what is missing, and quotes the `-trace=` line that would have
recorded it.

Two facts make it more than a table lookup:

* **Declared and recorded are different answers.** The registry lists every channel the engine
  knows, enabled or not, so the report keeps "this trace knows about `task`" apart from "this trace
  has task events".
* **The registry can be wrong.** It is written at capture time and can disagree with the events: the
  corpus was documented as GPU-less for a while because the *legacy* GPU channel is not the `gpu`
  channel (REFERENCE §8, §11). So wherever the tool models the channel, the model's own contents are
  the evidence, a disagreement is printed rather than resolved silently, and a channel that holds
  nothing is *not ready* however the registry reads.
"""

from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

import summary

#: The engine's `Default` preset: `-trace=Default` expands to these ids. Quoted whenever a capture is
#: missing something a CPU-side analysis needs.
DEFAULT_TRACE = "cpu,gpu,frame,log,bookmark,region,screenshot"
#: The memory preset is a *different recording*, not a longer default one (REFERENCE §8).
MEMORY_TRACE = "Memory"
#: Channels only the memory preset records: quoting the default preset for these would be wrong.
MEMORY_CHANNELS = ("memalloc", "memtag", "callstack")


class Analysis(NamedTuple):
    """One thing the tool reports, the channels it must have, and where a reader finds it."""

    title: str
    commands: str
    needs: Tuple[str, ...]


#: Every analysis, with the channels it cannot run without. This table *is* the item: it is what
#: turns "this capture has no `task` events" into "the task graph is not a question for this file".
ANALYSES: Tuple[Analysis, ...] = (
    Analysis("frame times: percentiles, hitches, and what ran in the worst frame",
             "summary, bottleneck, frames, advice", ("cpu", "frame")),
    Analysis("CPU root cause: the call tree, inclusive against self time",
             "self, sources", ("cpu",)),
    Analysis("each thread's occupancy, its waits, and parallelism",
             "threads, parallelism, coverage", ("cpu",)),
    Analysis("the task graph and its critical path", "tasks", ("task",)),
    Analysis("GPU passes, queue synchronisation, and a GPU-bound verdict",
             "gpu, bottleneck", ("gpu",)),
    Analysis("bookmarks: boot, level loads, markers in time", "summary, advice", ("bookmark",)),
    Analysis("regions: thread-agnostic timespans", "summary", ("region",)),
    Analysis("log messages and their specs", "summary", ("log",)),
    Analysis("the engine's counters and stat values per frame", "summary", ("counters",)),
    Analysis("CSV Profiler series (definitions here; values need `csv from-trace`)",
             "csv info, summary", ("counters",)),
    Analysis("memory: tags, allocations, allocation sites", "not implemented yet",
             ("memtag", "memalloc")),
    Analysis("loading: packages, request groups, IO waits", "not implemented yet",
             ("loadtime", "file")),
)

#: Where the tool itself holds evidence of a channel, and what that evidence is. A channel that is
#: not here is reported from the trace's registry alone and says so: claiming a count for something
#: we do not model would be the "zero value" this module exists to avoid.
EVIDENCE: Dict[str, Tuple[str, str]] = {
    "cpu": ("timers", "timer spec(s)"),
    "frame": ("frames", "frame pair(s)"),
    "gpu": ("gpu_frames", "decoded GPU frame(s)"),
    "task": ("tasks", "task(s)"),
    "counters": ("counters", "counter spec(s)"),
    "stats": ("counters", "counter spec(s)"),
    "bookmark": ("bookmarks", "bookmark(s)"),
}


class ChannelState(NamedTuple):
    """One channel of one capture: what the file says, and what the model actually holds."""

    name: str
    enabled: bool
    held: Optional[int]
    what: str
    note: str


class Verdict(NamedTuple):
    """One analysis, and whether this capture can answer it."""

    title: str
    commands: str
    ready: bool
    missing: Tuple[str, ...]


class Series(NamedTuple):
    """One thread's frames of one type: how many, how long, and how the lengths are distributed."""

    label: str
    frames: int
    seconds: float
    median: float
    p95: float
    longest: float


class Report(NamedTuple):
    """A capture's coverage: what it carries, what it can answer, and how it was recorded."""

    names: List[str]
    metadata: List[Tuple[str, str]]
    channels: List[ChannelState]
    verdicts: List[Verdict]
    missing: List[str]
    redo: str
    series: List[Series]
    warm_up: str
    variance: List[str]


def _counts(model: Dict[str, Any]) -> Dict[str, Any]:
    value = model.get("counts")
    return value if isinstance(value, dict) else {}


def _session(model: Dict[str, Any]) -> Dict[str, Any]:
    value = model.get("session")
    return value if isinstance(value, dict) else {}


def _held(model: Dict[str, Any], channel: str) -> Optional[Tuple[int, str]]:
    """`(how many, of what)` the model holds for a channel, or None when we do not model it."""
    if channel == "log":
        return int(_counts(model).get("log_messages", 0)), "log message(s)"
    if channel == "gpu":
        # either channel shape is evidence: the legacy per-frame rows, or the current channel's
        # queue events (REFERENCE §19). A modern capture can carry only the latter.
        frames = len(model.get("gpu_frames") or [])
        events = int(_counts(model).get("gpu_queue_events", 0))
        return frames + events, "GPU frame(s)/queue event(s)"
    entry = EVIDENCE.get(channel)
    if entry is None:
        return None
    key, what = entry
    value = model.get(key)
    return (len(value) if isinstance(value, list) else 0), what


def _ready(state: ChannelState) -> bool:
    """Can this capture answer questions that need this channel?

    **Where the tool models a channel, its events decide** -- a registry row claiming `cpu` is not
    enough when the schema holds no timer specs, and a capture whose registry is silent (or absent)
    is still answerable when the model is full of the events. For a channel the tool does not model,
    the trace's own registry is the only word there is.
    """
    if state.held is not None:
        return state.held > 0
    return state.enabled


def channel_states(model: Dict[str, Any]) -> List[ChannelState]:
    """The trace's registry, each channel against the model's own evidence for it.

    Recorded channels come first, in the registry's order; a channel the registry says was off but
    the model holds events for is kept apart (that is the interesting one), and a channel the
    registry does not mention at all but the model has events for is added rather than dropped --
    the events are the evidence, and a fixture, a synthesised capture or a partial registry would
    otherwise read as a capture that cannot answer anything.
    """
    states: List[ChannelState] = []
    seen = set()
    for row in model.get("channels") or []:
        name = str(row.get("name", "")).lower()
        if not name:
            continue
        seen.add(name)
        held = _held(model, name)
        enabled = bool(row.get("is_enabled"))
        note = ""
        if held is not None and enabled and held[0] == 0:
            note = "recorded, but the model holds nothing for it: the registry and the events disagree"
        elif held is not None and not enabled and held[0] > 0:
            note = "the registry says it was off, but the model holds events for it"
        states.append(ChannelState(name=name, enabled=enabled, held=held[0] if held else None,
                                   what=held[1] if held else "", note=note))
    for name in sorted(list(EVIDENCE) + ["log"]):
        if name in seen:
            continue
        held = _held(model, name)
        if held is None or held[0] <= 0:
            continue
        states.append(ChannelState(name=name, enabled=False, held=held[0], what=held[1],
                                   note="not in the trace's registry: its events are the evidence"))
    return sorted(states, key=lambda state: (not _ready(state), state.name))


def verdicts(model: Dict[str, Any]) -> Tuple[List[Verdict], List[str]]:
    """Every analysis, ready or not, and the channels that are missing for at least one of them."""
    states = {state.name: state for state in channel_states(model)}
    out: List[Verdict] = []
    missing: List[str] = []
    for analysis in ANALYSES:
        short: List[str] = []
        for channel in analysis.needs:
            state = states.get(channel)
            if state is None or not _ready(state):
                short.append(channel)
                if channel not in missing:
                    missing.append(channel)
        out.append(Verdict(title=analysis.title, commands=analysis.commands,
                           ready=not short, missing=tuple(short)))
    return out, sorted(missing)


def redo_line(missing: Sequence[str]) -> str:
    """The `-trace=` line that would have recorded `missing`.

    Two presets, because they are two recordings: the CPU-side channels come from the engine's
    `Default` preset, and allocations/tags/callstacks come from `-trace=Memory`, which a capture has
    to be recorded with on purpose (REFERENCE §8).
    """
    if not missing:
        return ""
    lines: List[str] = []
    if any(name in MEMORY_CHANNELS for name in missing):
        lines.append("-trace=%s" % (MEMORY_TRACE,))
    if any(name not in MEMORY_CHANNELS for name in missing):
        lines.append("-trace=%s" % (DEFAULT_TRACE,))
    return " and ".join(lines)


def _clean(value: Any) -> str:
    """A trace string without its UTF-16 NULs, or `-` when the capture did not say."""
    text = str(value or "").replace("\x00", "").strip()
    return text or "-"


def series_of(model: Dict[str, Any]) -> List[Series]:
    """Every thread's frames of every type, longest series first.

    The length of a frame is what the engine recorded (`EndFrame - BeginFrame`), so this says what
    the capture *is*, not what a report made of it: a median here is the shape to expect, and the
    longest frame is the hitch the reader is about to chase.
    """
    frequency = _frequency(model)
    if not frequency:
        return []
    grouped: Dict[Tuple[int, int], List[float]] = {}
    for row in model.get("frames") or []:
        key = (int(row.get("tid", 0)), int(row.get("type", 0)))
        beginning = int(row.get("begin_cycle", 0))
        length = (int(row.get("end_cycle", 0)) - beginning) * 1000.0 / frequency
        grouped.setdefault(key, []).append(length)
    series: List[Series] = []
    for (tid, kind), values in grouped.items():
        ordered = sorted(values)
        series.append(Series(
            label="tid %d, type %d" % (tid, kind), frames=len(values),
            seconds=sum(values) / 1000.0, median=summary.percentile(ordered, 0.5),
            p95=summary.percentile(ordered, 0.95), longest=ordered[-1]))
    return sorted(series, key=lambda entry: (-entry.frames, entry.label))


def _frequency(model: Dict[str, Any]) -> int:
    return int(_session(model).get("cycle_frequency", 0) or 0)


def _length_seconds(model: Dict[str, Any]) -> float:
    """How long the capture is: the trace's own duration when it carries one, else its cycle span."""
    session = _session(model)
    frequency = _frequency(model)
    if not frequency:
        return 0.0
    duration = int(session.get("duration_cycles", 0) or 0)
    if not duration:
        duration = max(0, int(session.get("last_cycle", 0) or 0)
                       - int(session.get("start_cycle", 0) or 0))
    return duration / float(frequency)


def _warm_up(series: Sequence[Series], model: Dict[str, Any]) -> str:
    """Whether the first frame of the longest series is slow enough to be worth trimming.

    The comparison is against that series' own median, so a capture whose frames are all slow does
    not read as one whose first frame is a warm-up -- and it is a hint, not a verdict: the line says
    what was seen, never what to do about it.
    """
    if not series:
        return "no frame pairs, so no frame-time distribution to judge a warm-up window by"
    frequency = _frequency(model)
    if not frequency:
        return "the trace declares no cycle frequency, so a frame length cannot be read"
    longest = series[0]
    windows = sorted((int(row.get("begin_cycle", 0)), int(row.get("end_cycle", 0)))
                     for row in model.get("frames") or []
                     if "tid %d, type %d" % (int(row.get("tid", 0)), int(row.get("type", 0)))
                     == longest.label)
    if not windows:
        return "no frame pairs to judge a warm-up window by"
    first = (windows[0][1] - windows[0][0]) * 1000.0 / frequency
    if first > 2.0 * longest.median and longest.frames > 4:
        return ("the first frame is %.3f ms against a %.3f ms median: that is a warm-up frame, and "
                "a hitch that happened before the scene settled should be read as one"
                % (first, longest.median))
    return ("the first frame is %.3f ms against a %.3f ms median: no warm-up window looks needed"
            % (first, longest.median))


def _variance(names: Sequence[str], series: Sequence[Sequence[Series]]) -> List[str]:
    """Run-to-run noise, when more than one capture was given: the medians, and how far apart.

    The comparison is between each capture's **biggest** series only, because two captures of the
    same scene are two recordings of the same thread, and comparing a frame thread against a render
    thread would answer a different question.
    """
    if len(names) < 2:
        return []
    lines: List[str] = []
    for name, one in zip(names, series):
        if not one:
            lines.append("%s: no frames at all" % (name,))
            continue
        lines.append("%s: %s -- %d frame(s), %.3f ms median, %.3f ms p95, %.3f ms longest"
                     % (name, one[0].label, one[0].frames, one[0].median, one[0].p95, one[0].longest))
    medians = [one[0].median for one in series if one]
    if len(medians) > 1:
        low, high = min(medians), max(medians)
        spread = (high - low) / low * 100.0 if low else 0.0
        lines.append("medians %.3f and %.3f ms: %.1f%% apart -- the scene, not the code, unless the "
                     "captures were taken the same way" % (low, high, spread))
    return lines


def report_of(models: Sequence[Dict[str, Any]], names: Sequence[str]) -> Report:
    """The whole coverage report over one capture, or over several to compare against each other."""
    first = models[0] if models else {}
    states = channel_states(first)
    judged, missing = verdicts(first)
    session = _session(first)
    counts = _counts(first)
    metadata = [
        ("build", _clean(session.get("build_version"))),
        ("changelist", str(session.get("changelist", 0))),
        ("configuration", _clean(session.get("configuration"))),
        ("platform", _clean(session.get("platform"))),
        ("project", _clean(session.get("project"))),
        ("app (trace)", _clean(session.get("app"))),
        ("length", "%.2f s" % (_length_seconds(first),)),
        ("threads", str(len(first.get("threads") or []))),
        ("events", str(int(counts.get("events", 0)))),
    ]
    all_series = [series_of(model) for model in models]
    return Report(names=list(names), metadata=metadata, channels=states, verdicts=judged,
                  missing=missing, redo=redo_line(missing), series=all_series[0] if all_series else [],
                  warm_up=_warm_up(all_series[0] if all_series else [], first),
                  variance=_variance(names, all_series))


def render_lines(report: Report, limit: int = 12) -> Tuple[List[str], List[Tuple[str, ...]]]:
    """`(prose, rows)`: what the capture carries, and every analysis against it."""
    lines: List[str] = []
    recorded = [state for state in report.channels if _ready(state)]
    known = len(report.channels)
    lines.append("channels  : %d of %d declared channel(s) recorded: %s" % (
        len(recorded), known,
        ", ".join("%s (%s)" % (state.name, state.what or "not modelled")
                  for state in recorded[:8]) or "none"))
    if len(recorded) > 8:
        lines.append("            ... and %d more" % (len(recorded) - 8,))
    quiet = [state for state in report.channels if not state.enabled and state.held is None]
    if quiet:
        lines.append("not in it : %s -- a question this capture cannot answer, not a zero"
                     % ", ".join(state.name for state in quiet[:14]))
    for state in report.channels:
        if state.note:
            lines.append("registry  : %s: %s" % (state.name, state.note))
    lines.append("metadata  : %s" % ", ".join("%s %s" % (label, value)
                                              for label, value in report.metadata[:5]))
    lines.append("            %s" % ", ".join("%s %s" % (label, value)
                                              for label, value in report.metadata[5:]))
    for entry in report.series[:3]:
        lines.append("frames    : %s -- %d frame(s), %.3f ms median, %.3f ms p95, %.3f ms longest"
                     % (entry.label, entry.frames, entry.median, entry.p95, entry.longest))
    lines.append("warm-up   : %s" % (report.warm_up,))
    for line in report.variance:
        lines.append("variance  : %s" % (line,))
    if report.redo:
        lines.append("to record : %s would have carried %s"
                     % (report.redo, ", ".join(report.missing)))
    else:
        lines.append("to record : nothing is missing: every analysis above can run on this capture")
    rows = [(verdict.title, verdict.commands,
             "ready" if verdict.ready else "skipped",
             "" if verdict.ready else ", ".join(verdict.missing)) for verdict in report.verdicts]
    return lines, rows
