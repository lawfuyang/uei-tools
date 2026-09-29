"""The parallelism report: who worked, who waited, and what ran with nobody beside it.

The practice's question, one step past "what bounds this frame": a frame is slow, the budget is
explicit -- **was the work spread, or was it one thread and a machine of parked ones?** Everything
here is measured (REFERENCE §13 has the definitions and the corpus's own numbers), and the
measurement is in the model rather than derived here: the walk folds every thread's coverage
timeline into every frame (`coverage.measure_frames`), so this module only reads rows.

What it reads, and what each number means:

* **Occupancy** -- per thread, the cycles it spent inside a scope in the frame series' windows, split
  into work and wait (`ThreadRow`). A thread inside `WaitForTasks` is not a thread doing work beside
  the frame thread, and the corpus shows why the split has to exist: its render thread is inside
  scopes for 100% of every frame and *waiting* for 98.5% of that.
* **Solo work** -- the frame thread's work while no other thread was working at the same cycle, as
  cycles and as a share of its work. This is the measured form of "this frame was serial", and the
  one number a parallelism argument rests on.
* **Simultaneity** -- the most threads working at the same cycle per frame, and the histogram of it.
  A capture records no core count, so "more work than cores" is *not* decided here; what is decided
  is how much of the machine was ever in use.
* **Contention** -- cycles two or more threads spent inside lock-named scopes at once
  (`model.LOCK_NAME_WORDS`). Named locks only: a lock the heuristic does not recognise is invisible,
  and the report says so rather than reporting zero contention for the whole capture.
* **Candidates** -- the timers that own a quarter or more of the frames the model keeps work for
  (`model._FRAME_WORK_KEEP`), whose names read as *work* rather than as something already parallel.
  This one is a **sample and a name heuristic**, and both are said where it is printed.

Two rules, from AGENTS.md and from what the report is for:

* **A ceiling is a heuristic, and says so.** `speedup_ceiling` is Amdahl's arithmetic on the measured
  solo share: it answers "if all of that could be spread", which is exactly the thing a scope name
  cannot promise. It is labelled wherever it is printed and never presented as a prediction.
* **Absent is not zero.** No frames, no cycle frequency, no timer specs: `classify` returns the
  reasons and the command exits 2 rather than printing a table of zeroes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple, cast

import coverage
from bottleneck import pick_series, role_of
from model import span_kind
from shapes import FrameOccupancyRow
from summary import Budget, Series, times_ms

#: Name fragments that say a scope is *already* parallel, so it is not a candidate for being made
#: parallel: the engine's own vocabulary (`ParallelFor`, `TaskGraph`, `Async...`, `...Worker`).
PARALLEL_NAME_MARKERS = (
    "parallel", "async", "task", "worker", "job", "thread", "concurrent", "dispatch", "lockfree",
)
#: A timer has to own at least this share of the frames the model keeps before it is named a
#: candidate -- the practice's own rule of thumb is "one timer owning a quarter of the frame".
CANDIDATE_SHARE = 0.25
#: How many candidates the report names at most (a list, not a ranking of everything).
CANDIDATE_TOP = 3
#: A thread counts as *idle* in a frame series when its work there is under this share of the span.
IDLE_SHARE = 0.01


class ThreadRow(NamedTuple):
    """One thread's occupancy over the frame series' own span.

    `busy_cycles` is every cycle the thread was inside a scope *inside those frames* -- the windows
    are the series', not the thread's -- and `wait_cycles` the part of it inside a wait-named scope.
    `elsewhere` marks a thread with coverage in the capture but none in these frames: idle here is
    not idle, and the report says which it is.
    """

    tid: int
    name: str
    role: str
    frames: int
    busy_cycles: int
    wait_cycles: int
    lock_cycles: int
    elsewhere: bool
    coarsened: int

    def work_cycles(self) -> int:
        """The cycles this thread spent working (inside a scope that is not a wait)."""
        return max(0, self.busy_cycles - self.wait_cycles)


class Candidate(NamedTuple):
    """A timer that owns a big share of the frames the model keeps work for."""

    spec: int
    name: str
    cycles: int
    frames: int
    share: float


class Report(NamedTuple):
    """Everything the parallelism report measured, before it is put into words."""

    series: Series
    budget: Budget
    span_cycles: int
    own_busy: int
    own_wait: int
    solo: int
    others_work: int
    all_cycles: int
    contended_cycles: int
    contended_frames: int
    peaks: Dict[int, int]
    threads: List[ThreadRow]
    candidates: List[Candidate]
    kept_frames: int
    over_frames: int
    over_work: int
    over_solo: int
    notes: List[str]

    def own_work(self) -> int:
        """The frame thread's work inside its own frames."""
        return max(0, self.own_busy - self.own_wait)

    def threads_working(self) -> int:
        """How many threads a *typical* frame had working at once: the commonest peak.

        Deliberately not the maximum: one startup frame where sixty threads run at once would make
        every ceiling here meaningless, while the peak a frame usually sees is the parallelism the
        capture actually exercises. The maximum is still printed, in the histogram.
        """
        if not self.peaks:
            return 0
        return min(self.peaks.items(), key=lambda item: (-item[1], item[0]))[0]

    def workers(self) -> int:
        """The ceiling's own worker count: `threads_working()`, and never fewer than two.

        Two, because the ceiling is about spreading the frame thread's work *over other threads*: a
        capture where nothing ever overlapped cannot be sped up by pretending one thread is two, and
        a ceiling of one is not worth computing.
        """
        return max(2, self.threads_working())

    def solo_share(self) -> float:
        """The share of the frame thread's work that happened with no other thread working."""
        work = self.own_work()
        return (self.solo / float(work)) if work else 0.0

    def over_solo_share(self) -> Optional[float]:
        """The same share, over the frames that miss the budget (None when none do)."""
        return (self.over_solo / float(self.over_work)) if self.over_work else None


def _frequency(model: Mapping[str, Any]) -> int:
    session = model.get("session", {})
    value = session.get("cycle_frequency", 0) if isinstance(session, dict) else 0
    return int(value) if isinstance(value, (int, float)) else 0


def ms(cycles: int, frequency: int) -> float:
    """Cycles as milliseconds (0.0 when the capture cannot say -- a report never divides by zero)."""
    return cycles * 1000.0 / frequency if frequency else 0.0


def speedup_ceiling(share: float, workers: int) -> float:
    """Amdahl's arithmetic on a measured share: "if all of it could be spread" (a heuristic).

    The honest use is a *ceiling*, and the report prints it as one: the measured solo share is real,
    and whether any of it is parallelisable is what a scope name cannot say.
    """
    if workers < 2 or share <= 0.0:
        return 1.0
    return 1.0 / ((1.0 - share) + share / float(workers))


def candidates_of(model: Mapping[str, Any], series: Series, top: int = CANDIDATE_TOP
                  ) -> Tuple[List[Candidate], int, int]:
    """The timers owning the kept frames, with the sample's own totals: `(candidates, frames, cycles)`.

    Read from `frame_work`, which the model keeps only for a thread's **longest** frames
    (`model._FRAME_WORK_KEEP`): this is a sample of the worst frames by construction, which is where
    the practice looks and is not the same as a capture-wide ranking. `share` is of those frames'
    own span -- the inclusive cycles are clipped to the frame, so the shares of one frame can add up
    past 1 ("this timer owns the frame" means its children's time counts too).
    """
    names = {int(row["id"]): str(row["name"]) for row in model.get("timers", [])}
    totals: Dict[int, int] = {}
    frames_seen: Dict[int, int] = {}
    frames = 0
    span = 0
    for row in model.get("frame_work", []):
        if int(row["tid"]) != series.tid or int(row["type"]) != series.type:
            continue
        frames += 1
        span += int(row["cycles"])
        for spec, cycles in row["items"]:
            key = int(spec)
            totals[key] = totals.get(key, 0) + int(cycles)
            frames_seen[key] = frames_seen.get(key, 0) + 1
    if not frames or not span:
        return [], 0, 0
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    out: List[Candidate] = []
    for spec, cycles in ranked:
        name = names.get(spec, "")
        share = cycles / float(span)
        if share < CANDIDATE_SHARE:
            break
        lowered = name.lower()
        if any(marker in lowered for marker in PARALLEL_NAME_MARKERS):
            continue
        out.append(Candidate(spec=spec, name=name, cycles=cycles, frames=frames_seen.get(spec, 0),
                             share=share))
        if len(out) >= top:
            break
    return out, frames, span


def classify(model: Mapping[str, Any], budget: Budget,
             tid: Optional[int] = None) -> Tuple[Optional[Report], List[str]]:
    """Measure the frame series' occupancy and simultaneity; on a series it cannot judge, the reasons.

    The reasons are the three absences the model is explicit about: no `Misc.BeginFrame` pair at all,
    no cycle frequency (cycles are not milliseconds without it), and no occupancy rows -- which is
    what a capture without CpuProfiler timer specs has, because nothing could be attributed.
    """
    series = pick_series(model, tid)
    if series is None or not series.rows:
        return None, ["this capture carries no Misc.BeginFrame/EndFrame pair%s"
                      % (" on tid %d" % (tid,) if tid is not None else "",)]
    rows = [row for row in model.get("frame_occupancy", []) if isinstance(row, dict)]
    if not rows:
        return None, ["the capture declared no CpuProfiler timer specs (or its scopes carried no "
                      "pairs), so no thread's work inside a frame was measured"]
    times = times_ms(model, series.rows)
    if times is None:
        return None, ["the capture declares no cycle frequency, so a frame's cycles cannot be "
                      "stated in milliseconds"]

    names = {int(row.get("tid", 0)): str(row.get("name", ""))
             for row in model.get("threads", []) if isinstance(row, dict)}
    by_index: Dict[int, FrameOccupancyRow] = {
        int(row["frame"]): cast(FrameOccupancyRow, row) for row in rows
    }
    totals: Dict[int, List[int]] = {}
    own_busy = own_wait = solo = others_work = all_cycles = span = 0
    contended_cycles = contended_frames = 0
    peaks: Dict[int, int] = {}
    over_frames = over_work = over_solo = 0
    for position, frame in enumerate(series.rows):
        index = int(frame.get("index", -1))
        entry = by_index.get(index)
        if entry is None:
            continue
        span += int(frame["end_cycle"]) - int(frame["begin_cycle"])
        solo += int(entry["solo_cycles"])
        others_work += int(entry["others_work_cycles"])
        all_cycles += int(entry["all_cycles"])
        peak = int(entry["peak_workers"])
        peaks[peak] = peaks.get(peak, 0) + 1
        contended = int(entry["contended_cycles"])
        if contended:
            contended_frames += 1
            contended_cycles += contended
        own_work_here = 0
        for row in entry["threads"]:
            other = int(row["tid"])
            into = totals.setdefault(other, [0, 0, 0, 0])
            into[0] += 1
            into[1] += int(row["busy_cycles"])
            into[2] += int(row["wait_cycles"])
            into[3] += int(row["lock_cycles"])
            if other == series.tid:
                own_busy += int(row["busy_cycles"])
                own_wait += int(row["wait_cycles"])
                own_work_here += int(row["busy_cycles"]) - int(row["wait_cycles"])
        if times[position] > budget.ms:
            over_frames += 1
            over_work += own_work_here
            over_solo += int(entry["solo_cycles"])

    by_tid: Dict[int, ThreadRow] = {}
    for tid_here, entry in sorted(totals.items()):
        by_tid[tid_here] = ThreadRow(
            tid=tid_here, name=names.get(tid_here, ""), role=role_of(names.get(tid_here, "")) or "",
            frames=entry[0], busy_cycles=entry[1], wait_cycles=entry[2], lock_cycles=entry[3],
            elsewhere=False, coarsened=0,
        )
    # the walk's whole-timeline rows add what a frame cannot show: which threads were busy only
    # *outside* these frames, and which timelines the per-thread cap had to coarsen
    for row in model.get("thread_spans", []):
        tid_here = int(row.get("tid", 0))
        coarsened = int(row.get("coarsened", 0))
        seen = by_tid.get(tid_here)
        if seen is not None:
            by_tid[tid_here] = seen._replace(coarsened=coarsened)
        elif int(row.get("busy_cycles", 0)):
            by_tid[tid_here] = ThreadRow(
                tid=tid_here, name=names.get(tid_here, ""), role=role_of(names.get(tid_here, "")) or "",
                frames=0, busy_cycles=0, wait_cycles=0, lock_cycles=0, elsewhere=True,
                coarsened=coarsened,
            )
    threads = sorted(by_tid.values(), key=lambda row: (-row.busy_cycles, row.tid))

    candidates, kept_frames, _kept_span = candidates_of(model, series)
    notes: List[str] = []
    if not any(span_kind(str(row.get("name", ""))) == coverage.SPAN_LOCK
               for row in model.get("timers", [])):
        notes.append("no timer name in this capture reads as a lock, so contention was not measured "
                     "at all (not measured is not none)")
    return Report(
        series=series, budget=budget, span_cycles=span, own_busy=own_busy, own_wait=own_wait,
        solo=solo, others_work=others_work, all_cycles=all_cycles,
        contended_cycles=contended_cycles, contended_frames=contended_frames, peaks=peaks,
        threads=threads, candidates=candidates, kept_frames=kept_frames, over_frames=over_frames,
        over_work=over_work, over_solo=over_solo, notes=notes,
    ), notes


def share_text(part: int, whole: int) -> str:
    """A share of a span as a percentage (`0.0%` rather than a division by zero)."""
    return "%.1f%%" % (100.0 * part / whole,) if whole else "0.0%"


def seconds_text(cycles: int, frequency: int) -> str:
    """Cycles as seconds -- three decimals, because a capture's work is measured in milliseconds."""
    return "%.3f s" % (cycles / float(frequency),) if frequency else "?"


def verdict_text(report: Report, frequency: int) -> str:
    """One line: how much of the machine was ever in use, and how serial the frame thread was."""
    return ("%.1f%% of the frame thread's work had no other thread working beside it "
            "(%s of %s); the commonest frame had %d thread(s) working at once"
            % (report.solo_share() * 100.0, seconds_text(report.solo, frequency),
               seconds_text(report.own_work(), frequency), report.threads_working()))


def peak_text(report: Report, shown: int = 4) -> str:
    """The simultaneity histogram, commonest-first, with the maximum named separately.

    Compact on purpose: a capture's histogram can have a bucket per thread count (the corpus's editor
    run has 26), and what a reader needs is the shape -- what a frame usually ran, and the most it
    ever ran -- not the whole distribution, of which the `--format csv` form is the full answer.
    """
    if not report.peaks:
        return "no frame measured any thread working"
    ranked = sorted(report.peaks.items(), key=lambda item: (-item[1], item[0]))
    common = ", ".join("%d thread(s) in %d frame(s)" % (peak, count)
                       for peak, count in ranked[:shown])
    return "usually %s; the most ever was %d" % (common, max(report.peaks))


def occupancy_text(report: Report) -> str:
    """The frame series' own split: work, waiting, other threads' work, and nobody-in-a-scope."""
    span = report.span_cycles
    return ("frame thread: work %s, waiting %s; other threads' work %s; no thread inside a scope %s"
            % (share_text(report.own_work(), span), share_text(report.own_wait, span),
               share_text(report.others_work, span),
               share_text(max(0, span - report.all_cycles), span)))


def finding_lines(report: Report, frequency: int) -> List[str]:
    """The findings, in the order they matter, each saying whether it is measured or a heuristic."""
    lines: List[str] = []
    workers = report.workers()
    ceiling = speedup_ceiling(report.solo_share(), workers)
    lines.append(
        "serial    : %s of the frame thread's work (%s of %s) happened with no other thread working "
        "at the same cycle -- measured overlap; a scope beside it is not a dependency. Spreading all "
        "of it over the %d thread(s) a frame usually has would cut the work to %s (Amdahl on the "
        "measured share: a ceiling, not a prediction)"
        % (share_text(report.solo, report.own_work()), seconds_text(report.solo, frequency),
           seconds_text(report.own_work(), frequency), workers,
           share_text(int(100.0 / ceiling), 100))
    )
    over = report.over_solo_share()
    if report.over_frames and over is not None:
        lines.append(
            "over      : %d of %d frame(s) miss %s; %s of *their* work was solo"
            % (report.over_frames, len(report.series.rows), report.budget.label(),
               share_text(report.over_solo, report.over_work))
        )
    idle = [row for row in report.threads
            if not row.elsewhere and row.busy_cycles < report.span_cycles * IDLE_SHARE]
    elsewhere = [row for row in report.threads if row.elsewhere]
    lines.append(
        "threads   : %d thread(s) had coverage in these frames; %d of them under %.0f%% of the "
        "span; %d thread(s) are busy elsewhere in the capture and idle here"
        % (len(report.threads) - len(elsewhere), len(idle), IDLE_SHARE * 100.0, len(elsewhere))
    )
    if report.contended_frames:
        lines.append(
            "locks     : %d frame(s) had two or more threads inside lock-named scopes at once, "
            "%.3f ms of overlap in total (measured; which names count is a heuristic: "
            "*_Lock/*Mutex/*CriticalSection*)" % (report.contended_frames,
                                                  ms(report.contended_cycles, frequency))
        )
    else:
        lines.append(
            "locks     : no two threads were inside lock-named scopes at once in these frames "
            "(measured overlap of the names that read as locks -- a lock called something else is "
            "invisible here)"
        )
    lines.append(
        "peaks     : threads working at the same cycle per frame: %s. A capture records no core "
        "count, so over-subscription cannot be judged here" % (peak_text(report),)
    )
    if report.candidates:
        named = ", ".join("%s %.0f%%" % (candidate.name or "spec %d" % (candidate.spec,),
                                         candidate.share * 100.0)
                          for candidate in report.candidates)
        lines.append(
            "candidate : of the %d frame(s) the model keeps this thread's work for (its longest), "
            "%s -- and no name among them says it is already parallel (a sample of those frames plus "
            "a name heuristic; `ueia summary --limit 0` lists them)"
            % (report.kept_frames, named)
        )
    for note in report.notes:
        lines.append("note      : %s" % (note,))
    return lines
