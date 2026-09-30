"""The bottleneck verdict: is a frame bound by the game thread, the render thread, the GPU, or none?

The question the practice decides *before* drilling anywhere, answered from what the model measured:

* **the frame's own thread** -- the cycles of the frame's window its thread spent inside a scope,
  split into work and wait (`FrameRow.covered_cycles`/`wait_cycles`);
* **the capture's other frame series**, matched by the windows they overlap (a game frame and the
  render frame that runs beside it are one frame of the pipeline, and their frames overlap);
* **the GPU**, when the capture carries GPU data in either channel shape: the legacy channel's
  per-rendered-frame busy microseconds (`GpuFrameRow.busy_us`, placed by a measured clock scale),
  or the *current* channel's queue timeline (`queues.align`, one clock with the CPU's, checked for
  fit -- REFERENCE §19). The legacy frames win when both are present; the report says which
  answered.

The decision tree is the engine's own, mirrored rather than invented
(`Engine/Private/ChartCreation.cpp:1325-1349`: *"if frame time is greater than our target then we are
bounded by something"*, and `Engine/Private/DynamicResolution.cpp:280-290`):

    if frame time > target:                       # something bounded this frame
        game-thread bound if game thread time >= target
        render-thread bound if render thread time >= target
        GPU bound if GPU time >= target (and not already CPU bound)

with the engine's `GameThreadTimeMs`/`RenderThreadTimeMs` standing in for the **work** time we can
measure -- the thread's non-wait scope coverage, because a thread inside `WaitForTasks` must not be
credited with work (the corpus's render thread is inside scopes for 97% of its frames and inside
waits for 99.7% of *that*; without the split every one of those frames would read as render-bound).

Everything is labelled as what it is: a frame span is *certain* (the file recorded the pair), a
scope's name reading as a wait is *heuristic* (`model.WAIT_NAME_MARKERS`), an absent channel is
*unknown*. Two rules this module will not break:

* **No CPU-bound claim without GPU data.** A capture with no GPU channel can show a CPU thread
  burning the frame, and cannot show the GPU was free -- so the verdict carries the CPU finding
  *plus* "the GPU side is unknown here" and the re-record line, never a bare bound.
* **Absent is not zero.** No cycle frequency, no timer specs, no aligned GPU timeline: each is its
  own unknown, a frame nothing measured is `unexplained`, and a report that cannot decide exits 2
  rather than painting zeroes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple, cast

import queues
from shapes import FrameRow
from summary import Budget, frame_work_of, series_of, Series, times_ms, work_text

#: The roles the capture's own thread names can suggest (a heuristic, said so wherever it is used).
GAME_ROLE = "game"
RENDER_ROLE = "render"
RHI_ROLE = "rhi"

#: Lowercased name fragments -> role. The engine names these threads `GameThread`, `RenderThread N`
#: and `RHIThread`, so this reads the capture's own vocabulary rather than inventing one.
ROLE_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("gamethread", GAME_ROLE),
    ("renderthread", RENDER_ROLE),
    ("rhi", RHI_ROLE),
)

#: The frame-rate-cap heuristic: how close a frame has to sit to a multiple of a display period, and
#: which periods count (the refresh rates a capture's frame times cluster at).
CAP_TOLERANCE = 0.05
CAP_PERIODS = (1.0 / 60.0, 1.0 / 30.0, 1.0 / 120.0, 1.0 / 90.0)

#: A GPU timeline is trusted only when its measured scale is within this of 1.0 (the two clocks
#: differ by 0.9% on the corpus; anything wilder is a different clock, not a drift) and this share
#: of its frames lands inside a frame window.
ALIGN_SCALE_TOLERANCE = 0.25
ALIGN_MIN_CONTAINED = 0.5

#: The verdicts, in the order the engine's decision tree tests them.
VERDICTS = ("game", "render", "gpu", "unexplained", "within")


class Alignment(NamedTuple):
    """How a capture's GPU timeline was placed on a frame series, and how well it fitted."""

    scale: float
    contained: int
    total: int

    def describe(self) -> str:
        return ("%d of %d GPU frame(s) land inside a frame window (clock scale %.4f); the rest "
                "are outside every frame and are not evidence about one"
                % (self.contained, self.total, self.scale))


class FrameVerdict(NamedTuple):
    """One frame's verdict, with every number that decided it."""

    frame: FrameRow
    milliseconds: float
    covered_ms: Optional[float]
    wait_ms: Optional[float]
    work_ms: Optional[float]
    gpu_ms: Optional[float]
    gpu_number: Optional[int]
    verdict: str
    why: str


class RoleRow(NamedTuple):
    """One thread the classification looked at, and what its frames did."""

    tid: int
    role: str
    name: str
    frames: int
    over_budget: int


class Report(NamedTuple):
    """The classification of one frame series: every verdict, and what decided them."""

    series: Series
    budget: Budget
    verdicts: List[FrameVerdict]
    counts: Dict[str, int]
    roles: List[RoleRow]
    alignment: Optional[Alignment]
    notes: List[str]
    gpu_present: bool

    def bound(self) -> int:
        return sum(self.counts.get(kind, 0) for kind in ("game", "render", "gpu"))


def role_of(name: str) -> str:
    """The role a thread's own name suggests, or "" when it says nothing (a heuristic)."""
    lowered = name.lower()
    for marker, role in ROLE_MARKERS:
        if marker in lowered:
            return role
    return ""


def _frequency(model: Mapping[str, Any]) -> int:
    session = model.get("session", {})
    value = session.get("cycle_frequency", 0) if isinstance(session, dict) else 0
    return int(value) if isinstance(value, (int, float)) else 0


def _ms(cycles: Optional[int], frequency: int) -> Optional[float]:
    """Cycles as milliseconds, or None when the capture cannot say (no frequency, no measurement)."""
    if cycles is None or not frequency:
        return None
    return cycles * 1000.0 / frequency


def pick_series(model: Mapping[str, Any], tid: Optional[int] = None) -> Optional[Series]:
    """The series to judge: `--tid` if asked, else the capture's game thread, else its busiest."""
    if tid is not None:
        return series_of(model, tid)
    frames = [row for row in model.get("frames", []) if isinstance(row, dict)]
    if not frames:
        return None
    counts: Dict[int, int] = {}
    names = {int(row.get("tid", 0)): str(row.get("name", ""))
             for row in model.get("threads", []) if isinstance(row, dict)}
    for row in frames:
        tid_here = int(row.get("tid", 0))
        counts[tid_here] = counts.get(tid_here, 0) + 1
    named = [tid_here for tid_here in counts
             if role_of(names.get(tid_here, "")) == GAME_ROLE]
    chosen = min(named, key=lambda item: (-counts[item], item)) if named \
        else min(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return series_of(model, chosen)


def match_series(model: Mapping[str, Any], series: Series) -> Dict[int, List[FrameRow]]:
    """For each frame of `series`, the frames of the capture's other threads that overlap it.

    Overlap rather than index: a game frame and the render frame beside it are concurrent, so their
    windows cover each other, while an unrelated thread's frames usually cover nothing. More than one
    match is kept (a pipeline frame can contain several), and the caller uses the one whose work is
    largest -- the thread that was busiest inside *this* frame is the one worth naming.
    """
    others: Dict[int, List[FrameRow]] = {}
    for row in model.get("frames", []):
        tid = int(row.get("tid", 0))
        if tid == series.tid:
            continue
        others.setdefault(tid, []).append(row)
    matched: Dict[int, List[FrameRow]] = {}
    for _tid, rows in sorted(others.items()):
        ordered = sorted(rows, key=lambda item: (int(item["begin_cycle"]), int(item["end_cycle"])))
        cursor = 0
        for row in series.rows:
            low = int(row["begin_cycle"])
            high = int(row["end_cycle"])
            while cursor < len(ordered) and int(ordered[cursor]["end_cycle"]) <= low:
                cursor += 1
            scan = cursor
            while scan < len(ordered) and int(ordered[scan]["begin_cycle"]) < high:
                candidate = ordered[scan]
                begin = max(low, int(candidate["begin_cycle"]))
                end = min(high, int(candidate["end_cycle"]))
                if end > begin:
                    matched.setdefault(id(row), []).append(candidate)
                scan += 1
    return matched


def align_gpu(model: Mapping[str, Any], series: Series,
              frequency: int) -> Tuple[Optional[Alignment], Dict[int, Dict[int, float]]]:
    """Place the capture's GPU frames on `series`, and say how well they fitted.

    The GPU clock is not the CPU clock: on the corpus the GPU timeline spans 215.175 s where the
    render thread's frames span 217.171 s, so the scale is *measured* between the first and last GPU
    frame and the series' own window, and every GPU frame is then placed in the frame whose window
    its (scaled) time falls in. Too few inside a window, or a scale nowhere near 1: the alignment is
    refused, because an unplaced GPU number is not evidence about a frame.

    The result maps each frame to `{gpu frame number: milliseconds}` -- the number so a report can
    name the GPU frame and its passes, the milliseconds so it can add up a frame's GPU work.
    """
    frames = [row for row in model.get("gpu_frames", []) if isinstance(row, dict)]
    if not frames or not series.rows or not frequency:
        return None, {}
    usable = [row for row in frames
              if not int(row.get("unbalanced", 0)) and not int(row.get("truncated", 0))]
    if not usable:
        return None, {}
    usable.sort(key=lambda row: (int(row["base_us"]), int(row["number"]), int(row["tid"])))
    windows = sorted(series.rows, key=lambda row: int(row["begin_cycle"]))
    first_us = int(usable[0]["base_us"])
    gpu_span = int(usable[-1]["base_us"]) - first_us
    span_cycles = int(windows[-1]["end_cycle"]) - int(windows[0]["begin_cycle"])
    if gpu_span <= 0 or span_cycles <= 0:
        return None, {}
    scale = (span_cycles * 1000000.0 / frequency) / gpu_span
    if abs(scale - 1.0) > ALIGN_SCALE_TOLERANCE:
        return None, {}
    base_us = int(windows[0]["begin_cycle"]) * 1000000.0 / frequency
    per_frame: Dict[int, Dict[int, float]] = {}
    cursor = 0
    contained = 0
    for row in usable:
        at_us = base_us + (int(row["base_us"]) - first_us) * scale
        while cursor < len(windows) \
                and int(windows[cursor]["end_cycle"]) * 1000000.0 / frequency < at_us:
            cursor += 1
        if cursor >= len(windows) \
                or at_us < int(windows[cursor]["begin_cycle"]) * 1000000.0 / frequency:
            continue
        contained += 1
        key = id(windows[cursor])
        per_frame.setdefault(key, {})[int(row["number"])] = int(row["busy_us"]) / 1000.0
    if contained < len(usable) * ALIGN_MIN_CONTAINED:
        return None, {}
    return Alignment(scale=scale, contained=contained, total=len(usable)), per_frame


def cap_note(times: Sequence[float], budget: Budget) -> Optional[str]:
    """The frame-rate-cap heuristic: how many unexplained frames sit on a display period.

    `ChartCreation`'s spirit without its data (it reads the engine's own thread-time stats). When
    nothing measured accounts for the frame, "the frames are pinned to the display" is what is left --
    and being a heuristic on the numbers, the note says how many frames back it and how close they
    had to be.
    """
    if not times:
        return None
    pinned = 0
    for value in times:
        seconds = value / 1000.0
        for period in CAP_PERIODS:
            multiple = round(seconds / period)
            if multiple >= 1 and abs(seconds - multiple * period) <= CAP_TOLERANCE * period:
                pinned += 1
                break
    if pinned < max(1, len(times) // 4):
        return None
    return ("%d of %d unexplained frame(s) sit within %.0f%% of a multiple of 1/60 s, 1/30 s, "
            "1/120 s or 1/90 s (heuristic): consistent with a display or frame-rate cap"
            % (pinned, len(times), CAP_TOLERANCE * 100.0))


def classify(model: Mapping[str, Any], budget: Budget,
             tid: Optional[int] = None) -> Tuple[Optional[Report], List[str]]:
    """Classify every frame of a series; on a series it cannot judge, return the reasons instead."""
    notes: List[str] = []
    series = pick_series(model, tid)
    if series is None or not series.rows:
        return None, ["this capture carries no Misc.BeginFrame/EndFrame pair%s"
                      % (" on tid %d" % (tid,) if tid is not None else "",)]
    frequency = _frequency(model)
    times = times_ms(model, series.rows)
    if times is None:
        return None, ["the capture declares no cycle frequency, so frames cannot be timed"]
    if series.rows[0].get("covered_cycles") is None:
        return None, ["the capture declared no CpuProfiler timer specs, so no frame's occupancy "
                      "was measured and nothing here could be attributed"]
    matched = match_series(model, series)
    legacy_frames = [row for row in model.get("gpu_frames", []) if isinstance(row, dict)]
    queue_work = [row for row in model.get("gpu_spans", []) if isinstance(row, dict)
                  and str(row.get("kind")) == "work"]
    gpu_present = bool(legacy_frames) or bool(queue_work)
    alignment, gpu_ms = align_gpu(model, series, frequency)
    placement: Optional[queues.Placement] = None
    if alignment is None and queue_work:
        # the legacy channel answered nothing (absent, or unplaced): the current channel's queue
        # timeline is the other way a GPU verdict can be reached (REFERENCE §19). The map keeps the
        # legacy shape -- frame id -> {GPU frame number: ms} -- with -1 standing in for "the queue
        # spans", which is no legacy frame number and so names no pass evidence.
        placement = queues.align(model, series, frequency)
        if placement is not None:
            gpu_ms = {key: {-1: us / 1000.0} for key, us in placement.work_us.items()}
    if not gpu_present:
        notes.append("no GPU frames in this capture: the GPU side is unknown here, so a CPU "
                     "verdict is not a bound on its own (re-record with the gpu channel)")
    elif placement is not None:
        notes.append("GPU timeline: %s" % (placement.describe(),))
    elif alignment is None:
        notes.append("the capture carries GPU data but its timeline could not be placed on "
                     "this frame series: the GPU side is unknown here")
    else:
        notes.append("GPU timeline: %s" % (alignment.describe(),))

    verdicts: List[FrameVerdict] = []
    counts = {kind: 0 for kind in VERDICTS}
    names = {int(row.get("tid", 0)): str(row.get("name", ""))
             for row in model.get("threads", []) if isinstance(row, dict)}
    own = [0, 0]
    partners: Dict[int, List[int]] = {}
    for position, row in enumerate(series.rows):
        milliseconds = times[position]
        covered = _ms(row.get("covered_cycles"), frequency)
        wait = _ms(row.get("wait_cycles"), frequency)
        work = None if covered is None else covered - (wait or 0.0)
        gpu_map = gpu_ms.get(id(row), {})
        gpu = sum(gpu_map.values()) if gpu_map else None
        gpu_number = sorted(gpu_map)[0] if gpu_map else None
        partner: Optional[FrameRow] = None
        partner_work: Optional[float] = None
        for candidate in matched.get(id(row), []):
            candidate_covered = _ms(candidate.get("covered_cycles"), frequency)
            if candidate_covered is None:
                continue
            candidate_work = candidate_covered - (_ms(candidate.get("wait_cycles"), frequency) or 0.0)
            if partner_work is None or candidate_work > partner_work:
                partner, partner_work = candidate, candidate_work
        verdict = "within"
        why = "the frame meets the budget"
        if milliseconds > budget.ms:
            if work is not None and work >= budget.ms:
                verdict = "game"
                why = "the frame's own thread worked %.3f ms of its %.3f ms span" % (
                    work, milliseconds)
            elif partner_work is not None and partner_work >= budget.ms:
                verdict = "render"
                why = "tid %d worked %.3f ms inside this frame" % (
                    int(cast(Mapping[str, Any], partner).get("tid", 0)), partner_work)
            elif gpu is not None and gpu >= budget.ms:
                verdict = "gpu"
                why = "the GPU was busy %.3f ms of the frame" % (gpu,)
            else:
                verdict = "unexplained"
                why = "nothing measured accounts for it (thread work %s, GPU %s)" % (
                    "%.3f ms" % (work,) if work is not None else "unknown",
                    "%.3f ms" % (gpu,) if gpu is not None else "unknown")
        verdicts.append(FrameVerdict(
            frame=row, milliseconds=milliseconds, covered_ms=covered, wait_ms=wait, work_ms=work,
            gpu_ms=gpu, gpu_number=gpu_number, verdict=verdict, why=why,
        ))
        counts[verdict] += 1
        own[1] += 1
        if verdict == "game":
            own[0] += 1
        if partner is not None:
            entry = partners.setdefault(int(cast(Mapping[str, Any], partner).get("tid", 0)), [0, 0])
            entry[1] += 1
            if partner_work is not None and partner_work >= budget.ms:
                entry[0] += 1
    note = cap_note([item.milliseconds for item in verdicts if item.verdict == "unexplained"],
                    budget)
    if note:
        notes.append(note)
    roles = [RoleRow(tid=series.tid, role=role_of(names.get(series.tid, "")) or "other",
                     name=names.get(series.tid, ""), frames=own[1], over_budget=own[0])]
    for tid_here, entry in sorted(partners.items()):
        roles.append(RoleRow(tid=tid_here, role=role_of(names.get(tid_here, "")) or "other",
                             name=names.get(tid_here, ""), frames=entry[1],
                             over_budget=entry[0]))
    return Report(series=series, budget=budget, verdicts=verdicts, counts=counts, roles=roles,
                  alignment=alignment, notes=notes, gpu_present=gpu_present), notes


def verdict_text(report: Report) -> str:
    """One line: what is bound, how often, and what could not be explained."""
    total = len(report.verdicts)
    if not total:
        return "no frames to classify"
    parts = ["%d of %d frame(s) bound" % (report.bound(), total)]
    for kind in ("game", "render", "gpu"):
        if report.counts.get(kind):
            parts.append("%s %d" % (kind, report.counts[kind]))
    if report.counts.get("unexplained"):
        parts.append("unexplained %d" % (report.counts["unexplained"],))
    if report.counts.get("within"):
        parts.append("within budget %d" % (report.counts["within"],))
    return "; ".join(parts)


def evidence_text(model: Mapping[str, Any], verdict: FrameVerdict, top: int = 3) -> str:
    """What ran in one frame: its biggest timers, and the GPU passes that were busy in it."""
    names = {int(row.get("id", 0)): str(row.get("name", ""))
             for row in model.get("timers", []) if isinstance(row, dict)}
    gpu_names = {int(row.get("id", 0)): str(row.get("name", ""))
                 for row in model.get("gpu_specs", []) if isinstance(row, dict)}
    frequency = _frequency(model)
    row = verdict.frame
    parts: List[str] = []
    work = frame_work_of(model, int(row.get("tid", 0)), int(row.get("type", 0)),
                         int(row.get("begin_cycle", 0)))
    text = work_text(work, names, frequency, int(row.get("end_cycle", 0))
                     - int(row.get("begin_cycle", 0)), top)
    if text:
        parts.append(text)
    gpu_row = gpu_frame_of(model, verdict.gpu_number)
    if gpu_row is not None:
        passes = ", ".join(
            "%s %.2f ms" % (gpu_names.get(int(spec), "spec %s" % (spec,)), micro / 1000.0)
            for spec, micro in list(gpu_row.get("passes", []))[:top]
        )
        if passes:
            parts.append("GPU: " + passes)
    return "; ".join(parts)


def gpu_frame_of(model: Mapping[str, Any], number: Optional[int]) -> Optional[Mapping[str, Any]]:
    """The GPU frame with this number, when the capture has one (the first match in timeline order)."""
    if number is None:
        return None
    for row in model.get("gpu_frames", []):
        if int(row.get("number", -1)) == number:
            return row
    return None
