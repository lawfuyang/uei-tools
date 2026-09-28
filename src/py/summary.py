"""The summary layer: the frame-time distribution, a budget verdict, and the frames that break it.

The practice this is built to (ROADMAP, "What professional performance work looks like") is blunt
about the shape of the answer: **the distribution, never the average** -- "a 16.6 ms average with
50 ms spikes feels terrible" -- measured against an explicit target, with a count of the frames that
miss it and the timers that own them. So one report answers:

* **the session** -- which frame series is being judged (a thread, a frame type), how long the
  capture ran, how many frames there were;
* **the distribution** -- mean, min, p50, p95, p99, max and a histogram, in milliseconds, from the
  capture's own cycle frequency;
* **the verdict** -- how many frames miss the budget, and how many are hitches (`HITCH_FACTOR`
  budgets or more; at 60 FPS that is the 33 ms the CI literature counts);
* **the tail** -- the frames that break the budget, worst first, each naming the timers that ran in
  it (`model.FrameWorkRow`, attributed by the walk).

Definitions this module owns, because a number without one is a guess:

* **A frame time is a `Misc.BeginFrame`/`EndFrame` pair's span**, in cycles, divided by the
  capture's `session.cycle_frequency`. The engine's own frame track draws the same pair, and no
  average of it is ever reported as the headline.
* **A percentile is the nearest-rank one**: p90 of n values is the value at rank ceil(0.9n) in
  sorted order. That is what a CI gate means by p99 ("10% worse than the baseline"), and it is a
  real frame's time, never an interpolation between two frames that never happened.
* **A hitch is `HITCH_FACTOR` budgets** -- 33.3 ms at 60 FPS, the threshold the practice names --
  which makes the count budget-relative: a 30 FPS capture's hitches are frames over 66.7 ms.
* **The histogram's bins double from half the budget** (`budget/2`, `budget`, `2 x budget`, ...), so
  the bin edges themselves say "half a budget", "on budget", "twice it" -- a linear axis would put
  every frame of a hitch-ridden capture in the first bin. The last bin is open-ended.

The frames' own work is the walk's (`model.FrameWorkRow`): **inclusive** cycles per timer, and only
for a thread's longest frames -- the tail is what the table asks about, and a capture with 100,000
frames must not put all of them in the cache to name its worst twenty.
"""

from __future__ import annotations

import math
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
    cast,
)

from shapes import FrameRow, FrameWorkRow, UsageError

#: The practice's target when the command line does not name one (30/60/120 are the usual targets).
DEFAULT_BUDGET_FPS = 60.0

#: A frame over this many budgets is a hitch: 33.3 ms at 60 FPS, the threshold the CI practice uses.
HITCH_FACTOR = 2.0

#: The histogram's bin ladder: at most this many bins, doubling from half the budget.
HISTOGRAM_MAX_BINS = 16

#: How wide a full histogram bar is, in characters.
BAR_WIDTH = 40

#: What to say about a frame the walk did not keep work for (see `model._FRAME_WORK_KEEP`).
NO_WORK = "-"


class Budget(NamedTuple):
    """An explicit frame-time target, in both spellings the command line accepts."""

    fps: float
    ms: float

    def label(self) -> str:
        return "%.4g FPS = %.3f ms" % (self.fps, self.ms)


def parse_budget(fps: Optional[str], ms: Optional[str]) -> Budget:
    """`--budget FPS` and `--budget-ms MS`, exactly one of them or neither (the default).

    Both are refused together rather than one silently winning: a command line that says 30 FPS and
    20 ms is a mistake, and picking either would be inventing a budget the user did not set.
    """
    if fps is not None and ms is not None:
        raise UsageError("--budget and --budget-ms are two ways to say one thing; give one")
    if fps is not None:
        try:
            target = float(fps)
        except ValueError:
            raise UsageError("--budget wants a frame rate, got %r" % (fps,))
        if not target > 0:
            raise UsageError("--budget wants a frame rate above 0, got %r" % (fps,))
        return Budget(fps=target, ms=1000.0 / target)
    if ms is not None:
        try:
            period = float(ms)
        except ValueError:
            raise UsageError("--budget-ms wants a time in milliseconds, got %r" % (ms,))
        if not period > 0:
            raise UsageError("--budget-ms wants a time above 0, got %r" % (ms,))
        return Budget(fps=1000.0 / period, ms=period)
    return Budget(fps=DEFAULT_BUDGET_FPS, ms=1000.0 / DEFAULT_BUDGET_FPS)


class Series(NamedTuple):
    """The frame series a report judges: one thread's frames of one frame type."""

    tid: int
    type: int
    label: str
    rows: List[FrameRow]
    span_s: Optional[float]

    def describe(self) -> str:
        return "%d frame(s) on %s, frame type %d" % (len(self.rows), self.label, self.type)


def series_of(model: Mapping[str, Any], tid: Optional[int] = None) -> Optional[Series]:
    """The capture's frame series: the busiest (thread, frame type), or a named thread's.

    One series, never a pool of them: a capture's game and render threads are different clocks
    (the corpus's run 1413 frames each with different spans), and averaging them into one
    distribution would describe no thread at all. The busiest thread is the default because that is
    the thread a frame-time question is about; `--tid` asks for another on purpose.
    """
    frames = [row for row in model.get("frames", []) if isinstance(row, dict)]
    if tid is not None:
        frames = [row for row in frames if int(row.get("tid", 0)) == tid]
    counts: Dict[Tuple[int, int], int] = {}
    for row in frames:
        key = (int(row.get("tid", 0)), int(row.get("type", 0)))
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    chosen = min(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    rows: List[FrameRow] = [
        cast(FrameRow, row) for row in frames
        if (int(row.get("tid", 0)), int(row.get("type", 0))) == chosen
    ]
    rows.sort(key=lambda row: (int(row.get("begin_cycle", 0)), int(row.get("end_cycle", 0))))
    names = {
        int(row.get("tid", 0)): str(row.get("name", ""))
        for row in model.get("threads", []) if isinstance(row, dict)
    }
    thread = names.get(chosen[0], "")
    label = "tid %d%s" % (chosen[0], " (%s)" % (thread,) if thread else "")
    span: Optional[float] = None
    if rows:
        first = int(rows[0].get("begin_cycle", 0))
        last = int(rows[-1].get("end_cycle", 0))
        frequency = _frequency(model)
        if frequency and last > first:
            span = (last - first) / frequency
    return Series(tid=chosen[0], type=chosen[1], label=label, rows=rows, span_s=span)


def _frequency(model: Mapping[str, Any]) -> int:
    session = model.get("session", {})
    frequency = session.get("cycle_frequency", 0) if isinstance(session, dict) else 0
    return int(frequency) if isinstance(frequency, (int, float)) else 0


def frame_work_of(model: Mapping[str, Any], tid: int, frame_type: int,
                  begin_cycle: int) -> Optional[FrameWorkRow]:
    """The work the walk kept for one frame, or None when this frame was not long enough to keep."""
    for row in model.get("frame_work", []):
        if (int(row.get("tid", 0)) == tid and int(row.get("type", 0)) == frame_type
                and int(row.get("begin_cycle", 0)) == begin_cycle):
            return row
    return None


def times_ms(model: Mapping[str, Any], rows: Sequence[FrameRow]) -> Optional[List[float]]:
    """One frame time per frame, in milliseconds, in capture order.

    None when the capture carries no cycle frequency: cycles are not milliseconds without it, and
    a budget is stated in milliseconds, so a report would be a guess either way.
    """
    frequency = _frequency(model)
    if not frequency:
        return None
    return [
        (int(row.get("end_cycle", 0)) - int(row.get("begin_cycle", 0))) / frequency * 1000.0
        for row in rows
    ]


class Distribution(TypedDict):
    """What a frame series looks like: the shape first, the tail named, and the budget's verdict."""

    count: int
    mean_ms: float
    min_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    over: int
    hitches: int


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """The nearest-rank percentile: the value at rank ceil(q*n), not an interpolation.

    q is a fraction (0.99, not 99). An empty series has no percentile and raises: a caller that has
    no frames must say so, not paint a zero.
    """
    if not sorted_values:
        raise ValueError("no values have a percentile")
    rank = int(math.ceil(q * len(sorted_values)))
    return sorted_values[min(max(rank, 1), len(sorted_values)) - 1]


def distribute(values: Sequence[float], budget: Budget) -> Distribution:
    """The practice's numbers over one series: percentiles, the extremes, and the budget's verdict."""
    if not values:
        raise ValueError("no frames have a distribution")
    ordered = sorted(values)
    return Distribution(
        count=len(values),
        mean_ms=sum(values) / len(values),
        min_ms=ordered[0],
        p50_ms=percentile(ordered, 0.50),
        p95_ms=percentile(ordered, 0.95),
        p99_ms=percentile(ordered, 0.99),
        max_ms=ordered[-1],
        over=len([value for value in values if value > budget.ms]),
        hitches=len([value for value in values if value > budget.ms * HITCH_FACTOR]),
    )


class Bin(NamedTuple):
    """One histogram bin: `low_ms <= time < high_ms`, high None for the open-ended last bin.

    `frames` is the count, and not `count`, because a NamedTuple's field may not shadow a `tuple`
    method -- the same reason `shapes.PacketRow` is a dataclass (pyright's
    `reportIncompatibleMethodOverride` is right to refuse it).
    """

    low_ms: float
    high_ms: Optional[float]
    frames: int

    def label(self) -> str:
        if self.high_ms is None:
            return "%10.3f ms and over" % (self.low_ms,)
        return "%10.3f -  %9.3f ms" % (self.low_ms, self.high_ms)


def histogram(values: Sequence[float], budget_ms: float,
              max_bins: int = HISTOGRAM_MAX_BINS) -> List[Bin]:
    """The bins the practice's targets imply: doubling from half a budget, last one open.

    `histogram_lines` renders them; nothing here depends on the number of values, so an empty
    series gives empty bins rather than an error -- a caller with no frames has to say that itself.
    """
    edges = [budget_ms / 2.0]
    maximum = max(values) if values else 0.0
    # one edge fewer than the bin cap, because the last bin is the open one
    while edges[-1] * 2.0 <= maximum and len(edges) < max_bins - 1:
        edges.append(edges[-1] * 2.0)
    tallies = [0] * (len(edges) + 1)
    for value in values:
        index = 0
        while index < len(edges) and value >= edges[index]:
            index += 1
        tallies[index] += 1
    bins: List[Bin] = []
    low = 0.0
    for index, edge in enumerate(edges):
        bins.append(Bin(low_ms=low, high_ms=edge, frames=tallies[index]))
        low = edge
    bins.append(Bin(low_ms=low, high_ms=None, frames=tallies[len(edges)]))
    return bins


def histogram_lines(bins: Sequence[Bin]) -> List[str]:
    """The bars, one line per bin, scaled to the busiest bin (deterministic by construction)."""
    widest = max([bin.frames for bin in bins] or [0])
    lines = []
    for bin in bins:
        bar = ""
        if bin.frames and widest:
            bar = "#" * max(1, int(bin.frames * BAR_WIDTH / widest + 0.5))
        lines.append("  %s : %6d  %s" % (bin.label(), bin.frames, bar))
    return lines


def verdict_text(distribution: Distribution, budget: Budget) -> str:
    """One line: whether the capture meets the budget, and by how much it misses if it does not.

    The budget itself is stated once, on its own line, so this says "the budget" rather than
    repeating the number -- and the p99 is named when the average hides the tail, because that is
    the finding a reader has to see ("16.6 ms average with 50 ms spikes").
    """
    over = distribution["over"]
    share = over * 100.0 / distribution["count"] if distribution["count"] else 0.0
    if not over:
        return "within budget -- every frame meets it (slowest %.3f ms), and no hitch" % (
            distribution["max_ms"],
        )
    return "over budget -- %d of %d frame(s) (%.1f%%) miss it; %d hitch(es) (%.1f%%) over %.3f ms" % (
        over, distribution["count"], share,
        distribution["hitches"], distribution["hitches"] * 100.0 / distribution["count"],
        budget.ms * HITCH_FACTOR,
    )


def work_text(row: Optional[FrameWorkRow], names: Dict[int, str], frequency: int,
              frame_cycles: int, top: int = 3) -> str:
    """The biggest timers of one frame: `Name 12.345 ms (63% of the frame); ...`.

    Inclusive cycles (`model.FrameWorkRow`), so two timers on one line of the output can overlap:
    the percent is the share of the frame's own span, which is what makes "this timer owns the
    frame" a sentence with a number in it.
    """
    if row is None or not frequency:
        return NO_WORK
    items = row.get("items", [])
    if not items:
        return NO_WORK
    parts = []
    for spec, cycles in list(items)[:top]:
        milliseconds = cycles / frequency * 1000.0
        share = cycles * 100.0 / frame_cycles if frame_cycles else 0.0
        parts.append("%s %.3f ms (%.0f%%)" % (names.get(int(spec), "spec %d" % (spec,)),
                                              milliseconds, share))
    return "; ".join(parts)
