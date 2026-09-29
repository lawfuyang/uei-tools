"""The recommendations engine: what the reports measured, turned into what to look at next.

Every analysis this tool has -- the budget and its distribution, the bottleneck verdict, the
occupancy, the source mapping, the channel inventory -- ends in numbers, and the practice's last
question is *"so what do I do?"*. This module answers it as **rules over those same measurements**,
never as new ones: each finding carries what it observed, where, how sure it is, and **the next
command to run**, because a finding a reader cannot act on is a worse finding than none.

The shape of a finding is the README's own philosophy, and this is where it is enforced:

* `severity` (impact), `effort` and `confidence` are *ranked on*, and the list is sorted by them;
* `confidence` is `certain` (the file recorded it), `heuristic` (a name matched a pattern) or
  `unknown` (a channel is absent) -- the three words the rest of this repo uses;
* `evidence` is the measurement itself (frames, percentages, timer names), and `where` is the
  `file:line` when the trace has one;
* a rule that **cannot** be certain says so instead of being quiet: "no GPU channel" is a finding,
  not an absence of one, and so is "the capture declares no cycle frequency".

Ranking, documented because a reader will disagree with it: **severity, then confidence, then
effort, then id**. Impact first because it is what a reader is deciding about; a certain finding
before a heuristic one of the same impact; cheap fixes before expensive ones; the id only to keep
two runs comparable.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import bottleneck
import parallel
import sources
import summary
from engine import EngineDir
from shapes import UsageError
from summary import Budget

#: The three scales a finding carries. `SEVERITY` is the practice's own notion of impact, `EFFORT`
#: is what the *fix* costs (a re-record is cheap, a ParallelFor is not), and `CONFIDENCE` is the
#: vocabulary this repo already uses for a claim.
SEVERITY = ("high", "medium", "low")
EFFORT = ("low", "medium", "high")
CONFIDENCE = ("certain", "heuristic", "unknown")

#: The share of frames that have to miss the budget before it is the headline.
OVER_SHARE = 0.05
#: How far the p99 has to sit above the p50 before the mean is called out as hiding a tail.
TAIL_RATIO = 2.0
#: How much of a frame one timer has to own before it is worth opening that timer's file.
OWNER_SHARE = 0.25
#: How much of the frame thread's work has to be solo before "spread it" is the finding.
SOLO_SHARE = 0.80
#: The name fragments that say "streaming" when they dominate a frame that misses the budget.
STREAMING_NAMES = ("asyncloading", "flushasyncloading", "loadpackage", "loadtime", "asyncpackage")
#: Fewer frames than this and a percentile is a description of a handful of frames, not a rate.
THIN_FRAMES = 100
#: The largest display-period multiple that still reads as a *cap*: a frame that is 54 periods long
#: is a slow frame, not a throttled one, and the tolerance grows with the multiple.
CAP_MAX_MULTIPLE = 4
#: How many frames have to sit on such a multiple before the capture is called capped.
CAP_SHARE = 0.25


class Finding(NamedTuple):
    """One ranked recommendation: what was seen, how sure, what it costs, and what to run next."""

    id: str
    category: str
    severity: str
    effort: str
    confidence: str
    title: str
    evidence: List[str]
    next_command: str
    where: str = ""

    def rank(self) -> Tuple[int, int, int, str]:
        """`severity`, then `confidence`, then `effort`, then the id -- the documented order."""
        return (SEVERITY.index(self.severity), CONFIDENCE.index(self.confidence),
                EFFORT.index(self.effort), self.id)

    def as_json(self) -> Dict[str, Any]:
        """The finding as the machine form: the same fields, JSON-serialisable."""
        return {
            "id": self.id, "category": self.category, "severity": self.severity,
            "effort": self.effort, "confidence": self.confidence, "title": self.title,
            "evidence": list(self.evidence), "next_command": self.next_command, "where": self.where,
        }


class Context(NamedTuple):
    """Everything the rules read: the model, the budget, and the reports built from them.

    The reports are built once, here, because they are expensive to build per rule and because a
    rule that needs one must be able to say *why* it is missing (`bound_notes`, `who_note`,
    `mapped_note`) rather than going quiet -- that is the "missing channels" rule by construction.
    """

    model: Mapping[str, Any]
    budget: Budget
    explicit_budget: bool
    capture: str
    frequency: int
    series: Optional[summary.Series]
    times: Optional[List[float]]
    distribution: Optional[summary.Distribution]
    bound: Optional[bottleneck.Report]
    bound_notes: List[str]
    who: Optional[parallel.Report]
    who_note: str
    mapped: Optional[sources.Report]
    mapped_note: str
    counts: Mapping[str, int]

    def frames(self) -> List[Any]:
        rows = self.model.get("frames", [])
        return list(rows) if isinstance(rows, list) else []

    def timer_names(self) -> Dict[int, str]:
        return {int(row.get("id", 0)): str(row.get("name", ""))
                for row in self.model.get("timers", []) if isinstance(row, dict)}

    def where_capture(self) -> str:
        """The capture as the caller named it, so a `next command` line is runnable as printed.

        It is the *user's* spelling (the path they typed, when they typed one) rather than a name
        this module invents: a next command that cannot be pasted back into a shell is not a next
        command.
        """
        return self.capture or "<capture>"


def _frequency(model: Mapping[str, Any]) -> int:
    session = model.get("session")
    value = session.get("cycle_frequency", 0) if isinstance(session, dict) else 0
    return int(value) if isinstance(value, (int, float)) else 0


def context(model: Mapping[str, Any], budget: Budget, explicit_budget: bool = False,
            engine_dir: Optional[EngineDir] = None, capture: str = "") -> Context:
    """Build every report the rules read, once, and keep the reasons for the ones that cannot be."""
    frequency = _frequency(model)
    series = summary.series_of(model)
    times = summary.times_ms(model, series.rows) if series is not None else None
    distribution = summary.distribute(times, budget) if times is not None else None
    bound, bound_notes = bottleneck.classify(model, budget)
    who, who_notes = parallel.classify(model, budget)
    mapped, mapped_notes = sources.build(model, budget, engine_dir)
    counts = model.get("counts", {})
    known = counts if isinstance(counts, dict) else {}
    return Context(
        model=model, budget=budget, explicit_budget=explicit_budget, capture=capture, frequency=frequency,
        series=series, times=times, distribution=distribution,
        bound=bound, bound_notes=list(bound_notes),
        who=who, who_note=(who_notes[0] if who_notes else ""),
        mapped=mapped, mapped_note=(mapped_notes[0] if mapped_notes else ""),
        counts={str(key): int(value) for key, value in known.items()
                if isinstance(value, (int, float))},
    )


def _rule_budget_default(ctx: Context) -> Optional[Finding]:
    if ctx.explicit_budget:
        return None
    return Finding(
        id="budget-default", category="budget", severity="low", effort="low", confidence="certain",
        title="no goal was given: this ran against the default 60 FPS (16.667 ms)",
        evidence=["the practice's first rule is to decide the target before reading any number",
                  "pass --budget FPS or --budget-ms MS, and the distribution is judged against it"],
        next_command="ueia summary %s --budget 30" % (ctx.where_capture(),),
    )


def _rule_no_frames(ctx: Context) -> Optional[Finding]:
    if ctx.series is not None:
        return None
    return Finding(
        id="no-frames", category="data", severity="high", effort="low", confidence="unknown",
        title="the capture carries no Misc.BeginFrame/EndFrame pair, so no frame-time question "
              "can be answered at all",
        evidence=["a frame-time report needs one pair per frame: re-record with `-trace=...,frame`"],
        next_command="ueia frames %s" % (ctx.where_capture(),),
    )


def _rule_no_frequency(ctx: Context) -> Optional[Finding]:
    if ctx.series is None or ctx.times is not None:
        return None
    return Finding(
        id="no-frequency", category="data", severity="high", effort="low", confidence="certain",
        title="the capture declares no cycle frequency, so cycles cannot be stated in milliseconds",
        evidence=["every frame time in this report would be a cycle count without it"],
        next_command="ueia info %s" % (ctx.where_capture(),),
    )


def _capped(times: Sequence[float]) -> Optional[Tuple[float, float, int, int]]:
    """`(share, period_ms, multiple, count)` when a capture's frames sit on a *small* period multiple.

    Stricter than `bottleneck.cap_note` on purpose, and for a reason its own note cannot express: a
    905 ms frame is within 5% of 54 display periods, so *any* slow frame is "a multiple of a period"
    if the multiple is allowed to grow with the frame. A cap is what makes a frame *short* on
    purpose, so only the small multiples count here (1..`CAP_MAX_MULTIPLE`), and the finding names
    the period and the multiple it found rather than asserting a cap in general.
    """
    if not times:
        return None
    counted: Dict[Tuple[float, int], int] = {}
    for value in times:
        seconds = value / 1000.0
        for period in bottleneck.CAP_PERIODS:
            multiple = int(round(seconds / period))
            if 1 <= multiple <= CAP_MAX_MULTIPLE and \
                    abs(seconds - multiple * period) <= bottleneck.CAP_TOLERANCE * period:
                key = (period, multiple)
                counted[key] = counted.get(key, 0) + 1
                break
    if not counted:
        return None
    (period, multiple), count = max(counted.items(), key=lambda item: (item[1], -item[0][1]))
    share = count / float(len(times))
    if share < CAP_SHARE:
        return None
    return share, period * 1000.0, multiple, count


def _rule_frame_cap(ctx: Context) -> Optional[Finding]:
    """A capture whose frames sit on a *small* display-period multiple is capped, not bound."""
    if ctx.times is None:
        return None
    capped = _capped(ctx.times)
    if capped is None:
        return None
    share, period_ms, multiple, count = capped
    return Finding(
        id="frame-cap", category="frames", severity="high", effort="low", confidence="heuristic",
        title="%.0f%% of the frames sit on %d x %.3f ms: this capture looks throttled, not bound" % (
            share * 100.0, multiple, period_ms),
        evidence=["%d of %d frame(s) are within %.0f%% of a 1/60 s, 1/30 s, 1/120 s or 1/90 s "
                  "multiple, at a multiple of at most %d (a heuristic on the numbers: the engine's "
                  "own thread-time stats are what would settle it)" % (
                      count, len(ctx.times), bottleneck.CAP_TOLERANCE * 100.0, CAP_MAX_MULTIPLE),
                  "if it is a cap the budget is met by construction, and a faster frame is not what "
                  "is missing"],
        next_command="ueia bottleneck %s" % (ctx.where_capture(),),
    )


def _rule_over_budget(ctx: Context) -> Optional[Finding]:
    if ctx.distribution is None:
        return None
    over, count = int(ctx.distribution["over"]), int(ctx.distribution["count"])
    share = over / float(count) if count else 0.0
    if share < OVER_SHARE:
        return None
    return Finding(
        id="over-budget", category="frames",
        severity="high" if share > 0.5 else "medium", effort="medium", confidence="certain",
        title="%d of %d frame(s) (%.1f%%) miss the budget" % (over, count, share * 100.0),
        evidence=["p50 %.3f ms, p95 %.3f ms, max %.3f ms" % (
            ctx.distribution["p50_ms"], ctx.distribution["p95_ms"], ctx.distribution["max_ms"]),
            "the distribution is the answer: %d of them are hitches (over %.3f ms)" % (
                int(ctx.distribution["hitches"]), ctx.budget.ms * summary.HITCH_FACTOR)],
        next_command="ueia summary %s --limit 0" % (ctx.where_capture(),),
    )


def _rule_tail_vs_mean(ctx: Context) -> Optional[Finding]:
    if ctx.distribution is None or not int(ctx.distribution["over"]):
        return None
    p50, p99 = float(ctx.distribution["p50_ms"]), float(ctx.distribution["p99_ms"])
    if not p50 or p99 < p50 * TAIL_RATIO:
        return None
    return Finding(
        id="tail-vs-mean", category="frames", severity="medium", effort="medium",
        confidence="certain",
        title="the p99 is %.1fx the p50: the mean describes no frame in particular" % (p99 / p50,),
        evidence=["mean %.3f ms against p50 %.3f ms and p99 %.3f ms" % (
            ctx.distribution["mean_ms"], p50, p99),
            "hunt the tail: the worst frames own the experience, and they are listed by "
            "`summary --limit 0`"],
        next_command="ueia summary %s --limit 0" % (ctx.where_capture(),),
    )


def _rule_hitches(ctx: Context) -> Optional[Finding]:
    if ctx.distribution is None:
        return None
    hitches = int(ctx.distribution["hitches"])
    if not hitches:
        return None
    return Finding(
        id="hitches", category="frames", severity="medium", effort="medium", confidence="certain",
        title="%d hitch(es): frame(s) over %.3f ms" % (
            hitches, ctx.budget.ms * summary.HITCH_FACTOR),
        evidence=["each one is a frame a player saw as a stutter, and each one names the timers "
                  "that ran in it"],
        next_command="ueia summary %s --limit 0" % (ctx.where_capture(),),
    )


def _rule_thin_sample(ctx: Context) -> Optional[Finding]:
    if ctx.distribution is None:
        return None
    count = int(ctx.distribution["count"])
    if count >= THIN_FRAMES:
        return None
    return Finding(
        id="thin-sample", category="data", severity="low", effort="low", confidence="certain",
        title="only %d frame(s) were measured: percentiles over a handful of frames describe those "
              "frames" % (count,),
        evidence=["record longer, or record the scene that matters, before trusting a p99"],
        next_command="ueia frames %s" % (ctx.where_capture(),),
    )


def _rule_bound_thread(ctx: Context) -> Optional[Finding]:
    """The bottleneck verdict, as a recommendation: who the frames are spent on.

    `unexplained` is a finding too, and often the most useful one: it means nothing this capture
    recorded was busy inside those frames, which is a statement about the capture as much as about
    the frames.
    """
    if ctx.bound is None:
        return None
    counts = ctx.bound.counts
    total = sum(int(value) for value in counts.values())
    if not total:
        return None
    leader, count = max(counts.items(), key=lambda item: (int(item[1]), item[0]))
    count = int(count)
    if count <= 0:
        return None
    if leader == "unexplained":
        return Finding(
            id="bound-unexplained", category="cpu",
            severity="high" if count * 2 > total else "medium", effort="medium",
            confidence="unknown",
            title="%d of %d frame(s) have nothing this capture can name as their bound" % (count,
                                                                                           total),
            evidence=["no game/render/RHI thread and no GPU pass was busy inside them: the channels "
                      "that would explain them were not recorded",
                      "the frames are listed, with what ran in them, by `bottleneck`"],
            next_command="ueia bottleneck %s" % (ctx.where_capture(),),
        )
    return Finding(
        id="bound-thread", category="cpu", severity="high" if count * 2 > total else "medium",
        effort="medium", confidence="certain",
        title="%d of %d classified frame(s) are %s-bound" % (count, total, leader),
        evidence=["the verdicts are measured per frame against the budget, and the reader thread "
                  "counts as waiting while it is parked"],
        next_command="ueia bottleneck %s" % (ctx.where_capture(),),
    )


def _rule_gpu_unknown(ctx: Context) -> Optional[Finding]:
    if ctx.counts.get("gpu_frames", 0):
        return None
    return Finding(
        id="gpu-unknown", category="channels", severity="medium", effort="low", confidence="unknown",
        title="the capture carries no GpuProfiler channel, so the GPU side is unknown here",
        evidence=["a CPU finding without the GPU side is half a verdict: re-record with "
                  "`-trace=...,gpu`"],
        next_command="ueia info %s" % (ctx.where_capture(),),
    )


def _rule_task_unknown(ctx: Context) -> Optional[Finding]:
    if ctx.counts.get("task_events", 0):
        return None
    return Finding(
        id="task-unknown", category="channels", severity="low", effort="low", confidence="unknown",
        title="the capture carries no TaskTrace channel, so no critical path can be built",
        evidence=["`ueia tasks` needs it; re-record with `-trace=...,task`"],
        next_command="ueia tasks %s" % (ctx.where_capture(),),
    )


def _rule_csv_values(ctx: Context) -> Optional[Finding]:
    """Counter and CSV definitions without values: the recorder registered them and did not run."""
    stats = ctx.model.get("csv_stats", [])
    values = ctx.model.get("counter_values", {})
    if not stats or values:
        return None
    return Finding(
        id="csv-no-values", category="channels", severity="low", effort="medium",
        confidence="certain",
        title="%d CSV stat definition(s) were registered and no value was recorded" % (len(stats),),
        evidence=["a capture with a CSV capture running is what turns thresholds into findings; "
                  "`csv from-trace` reads one"],
        next_command="ueia csv from-trace %s --out capture.csv" % (ctx.where_capture(),),
    )


def _rule_one_timer(ctx: Context) -> Optional[Finding]:
    """One timer owning a quarter of the frames the model keeps: open that file."""
    if ctx.who is None or not ctx.who.candidates:
        return None
    candidate = ctx.who.candidates[0]
    if candidate.share < OWNER_SHARE:
        return None
    where = ""
    if ctx.mapped is not None:
        for row in ctx.mapped.files:
            if row.biggest is not None and row.biggest[0] == candidate.spec:
                where = row.location.where()
                break
    return Finding(
        id="one-timer", category="cpu", severity="high", effort="medium", confidence="heuristic",
        title="`%s` owns %.0f%% of the frames the model keeps work for" % (
            candidate.name, candidate.share * 100.0),
        evidence=["inclusive cycles, so it includes what it calls; the frames are the longest of "
                  "each thread (`model._FRAME_WORK_KEEP`)",
                  "a sample of frames plus a name heuristic: the name is not a verdict"],
        next_command="ueia sources %s --filter %s" % (
            ctx.where_capture(), candidate.name.split("::")[0][:24] or candidate.name[:24]),
        where=where,
    )


def _rule_workers_idle(ctx: Context) -> Optional[Finding]:
    """The machine was there and the frame thread did not use it: the parallelism question."""
    if ctx.who is None or ctx.who.threads_working() < 2:
        return None
    share = ctx.who.solo_share()
    if share < SOLO_SHARE:
        return None
    return Finding(
        id="workers-idle", category="parallelism", severity="high", effort="high",
        confidence="certain",
        title="%.1f%% of the frame thread's work had no other thread working at the same cycle" % (
            share * 100.0),
        evidence=["the commonest frame had %d thread(s) working at once" % (
            ctx.who.threads_working(),),
            "measured overlap, not a dependency: a scope beside it is not proof it could be spread"],
        next_command="ueia parallelism %s" % (ctx.where_capture(),),
    )


def _rule_sync_load(ctx: Context) -> Optional[Finding]:
    """A named synchronous load with a share: the streaming question, with a file to open."""
    if ctx.mapped is None:
        return None
    finding = next((item for item in ctx.mapped.findings if item.key == "sync-load"), None)
    if finding is None or finding.owned_cycles <= 0:
        return None
    share = ctx.mapped.share_of(finding.owned_cycles)
    if share < OVER_SHARE:
        return None
    where = finding.biggest[2].where() if finding.biggest is not None else ""
    name = finding.biggest[1] if finding.biggest is not None else "a synchronous load"
    return Finding(
        id="sync-load", category="streaming", severity="medium", effort="high",
        confidence="heuristic", title="`%s` and %d other synchronous load(s) hold %.1f%% of the "
                                      "kept frames" % (name, finding.specs - 1, share * 100.0),
        evidence=["%d of those frames are over budget" % (finding.over_frames,),
                  "matched on the timer *name*: a load called something else is invisible here"],
        next_command="ueia sources %s --filter %s" % (ctx.where_capture(), name[:24]),
        where=where,
    )


def _rule_async_loading(ctx: Context) -> Optional[Finding]:
    """A frame that misses the budget and is mostly waiting on streaming."""
    names = ctx.timer_names()
    worst = 0
    worst_frame = 0
    for row in ctx.model.get("frame_work", []):
        if not isinstance(row, dict):
            continue
        span = int(row.get("cycles", 0))
        if ctx.frequency and parallel.ms(span, ctx.frequency) <= ctx.budget.ms:
            continue
        items = [item for item in row.get("items", []) if isinstance(item, (list, tuple))
                 and len(item) == 2]
        total = sum(int(item[1]) for item in items)
        if not total:
            continue
        streaming = sum(int(item[1]) for item in items
                        if any(word in names.get(int(item[0]), "").lower()
                               for word in STREAMING_NAMES))
        share = streaming / float(total)
        if share > worst:
            worst, worst_frame = share, int(row.get("index", 0))
    if worst < OWNER_SHARE:
        return None
    return Finding(
        id="async-loading", category="streaming", severity="medium", effort="high",
        confidence="heuristic",
        title="frame %d misses the budget with %.0f%% of its kept timers inside async-loading "
              "scopes" % (worst_frame, worst * 100.0),
        evidence=["this is the streaming shape: the frame is not short of CPU, it is short of data",
                  "the kept timers are a sample of one frame's work, matched on names"],
        next_command="ueia sources %s --filter AsyncLoading" % (ctx.where_capture(),),
    )


def _rule_attribution_gaps(ctx: Context) -> Optional[Finding]:
    no_spec = ctx.counts.get("scope_pairs_no_spec", 0)
    if not no_spec:
        return None
    return Finding(
        id="attribution-gaps", category="data", severity="low", effort="medium",
        confidence="certain",
        title="%d scope pair(s) could not be attributed to a timer (no spec id on the wire)"
              % (no_spec,),
        evidence=["the V3 coroutine record carries no spec id, so the work attribution is a floor",
                  "`ueia verify` prints every such count"],
        next_command="ueia verify %s" % (ctx.where_capture(),),
    )


def _rule_anomalies(ctx: Context) -> Optional[Finding]:
    counts = ctx.model.get("anomaly_counts", {})
    total = sum(int(value) for value in counts.values()) if isinstance(counts, dict) else 0
    if not total:
        return None
    kinds = ", ".join(sorted(str(key) for key in counts)) if isinstance(counts, dict) else ""
    return Finding(
        id="anomalies", category="data", severity="medium", effort="low", confidence="certain",
        title="%d anomaly(ies) were recorded while decoding this capture" % (total,),
        evidence=["kinds: %s" % (kinds or "unknown",),
                  "an anomaly is a packet or event that did not read cleanly, so every number "
                  "above is a floor"],
        next_command="ueia verify %s" % (ctx.where_capture(),),
    )


#: The rules, in report order. Each one either returns a finding or nothing; a rule that cannot run
#: for lack of input returns nothing *and* the reader still learns why, because the report prints the
#: reasons the other analyses were unavailable (`Context`'s notes).
RULES: Tuple[Tuple[str, str, Callable[[Context], Optional[Finding]]], ...] = (
    ("budget-default", "budget", _rule_budget_default),
    ("no-frames", "data", _rule_no_frames),
    ("no-frequency", "data", _rule_no_frequency),
    ("frame-cap", "frames", _rule_frame_cap),
    ("over-budget", "frames", _rule_over_budget),
    ("tail-vs-mean", "frames", _rule_tail_vs_mean),
    ("hitches", "frames", _rule_hitches),
    ("thin-sample", "data", _rule_thin_sample),
    ("bound-thread", "cpu", _rule_bound_thread),
    ("one-timer", "cpu", _rule_one_timer),
    ("workers-idle", "parallelism", _rule_workers_idle),
    ("sync-load", "streaming", _rule_sync_load),
    ("async-loading", "streaming", _rule_async_loading),
    ("gpu-unknown", "channels", _rule_gpu_unknown),
    ("task-unknown", "channels", _rule_task_unknown),
    ("csv-no-values", "channels", _rule_csv_values),
    ("attribution-gaps", "data", _rule_attribution_gaps),
    ("anomalies", "data", _rule_anomalies),
)

#: Every rule id, for `--skip` to validate against: a typo in a rule name must not silently drop
#: nothing.
RULE_IDS = tuple(rule_id for rule_id, _category, _run in RULES)


def build(ctx: Context, skip: Sequence[str] = ()) -> List[Finding]:
    """Run every rule against the context and return the findings, ranked.

    `skip` drops rules by id (the item's "droppable by the user"): an unknown id is a usage error
    from the caller's side, because a filter that matches nothing is a filter that did nothing.
    """
    dropped = set(skip)
    unknown = sorted(dropped - set(RULE_IDS))
    if unknown:
        raise UsageError("--skip takes rule ids (%s); unknown: %s"
                         % ("|".join(RULE_IDS), ", ".join(unknown)))
    findings: List[Finding] = []
    for rule_id, _category, run in RULES:
        if rule_id in dropped:
            continue
        finding = run(ctx)
        if finding is not None:
            findings.append(finding)
    findings.sort(key=lambda item: item.rank())
    return findings


def unavailable(ctx: Context) -> List[str]:
    """The analyses that could not run, and why -- what a rule could not see, said out loud."""
    reasons: List[str] = []
    if ctx.series is None:
        reasons.append("no frame series: no frame-time, bottleneck or parallelism verdict")
    elif ctx.times is None:
        reasons.append("no cycle frequency: frame times cannot be stated in milliseconds")
    if ctx.bound_notes:
        reasons.extend("bottleneck: %s" % (note,) for note in ctx.bound_notes)
    if ctx.who_note:
        reasons.append("parallelism: %s" % (ctx.who_note,))
    if ctx.mapped_note:
        reasons.append("sources: %s" % (ctx.mapped_note,))
    return reasons


def meta_document(ctx: Context, capture: Mapping[str, Any], engine_dir: Optional[EngineDir],
                  seconds: float) -> Dict[str, Any]:
    """The `meta` block every machine-readable report carries (README §4).

    `capture` is what `cache.capture_identity` measured (the SHA-256 and the size), `seconds` is
    what this analysis took, `limitations` is `unavailable`, and `executables` is empty because this
    report wraps no engine program -- stated rather than omitted, so a reader can tell "none" from
    "not asked".
    """
    session = ctx.model.get("session", {})
    return {
        "tool_version": str(ctx.model.get("tool_version", "")),
        "capture": dict(capture),
        "session": {
            "app": str(session.get("app", "")), "project": str(session.get("project", "")),
            "target": str(session.get("target", "")),
            "build_version": str(session.get("build_version", "")),
            "cycle_frequency": ctx.frequency,
        },
        "budget": {"label": ctx.budget.label(), "ms": ctx.budget.ms,
                   "given": ctx.explicit_budget},
        "engine_dir": str(engine_dir.root) if engine_dir is not None else "",
        "duration_s": round(seconds, 3),
        "limitations": unavailable(ctx),
        "executables": [],
    }


def lines(findings: Sequence[Finding], ctx: Context, limit: int = 0) -> Tuple[List[str], List[
        Tuple[str, ...]]]:
    """`(prose, rows)`: one block per finding, and a table that indexes them.

    The prose is the finding (title, evidence, where, the next command); the table is what a reader
    scans on a second pass. `limit` caps the table only: a finding a reader cannot see is a finding
    the tool did not make.
    """
    prose: List[str] = []
    prose.append("findings  : %d, ranked by impact, then confidence, then effort -- a rule can be "
                 "dropped with --skip ID" % (len(findings),))
    if not findings:
        prose.append("nothing   : every rule that could run, ran and found nothing to recommend")
    for finding in findings:
        prose.append("[%s/%s/%s] %s (%s)" % (finding.severity, finding.confidence, finding.effort,
                                             finding.title, finding.category))
        for item in finding.evidence:
            prose.append("            evidence: %s" % (item,))
        if finding.where:
            prose.append("            where   : %s" % (finding.where,))
        prose.append("            next    : %s" % (finding.next_command,))
    reasons = unavailable(ctx)
    if reasons:
        prose.append("could not : %s" % ("; ".join(reasons),))
    rows = [
        (finding.id, finding.severity, finding.confidence, finding.effort, finding.where or "-",
         finding.next_command)
        for finding in (findings if not limit else findings[:limit])
    ]
    return prose, rows


def document(findings: Sequence[Finding], ctx: Context, capture: Mapping[str, Any],
             engine_dir: Optional[EngineDir], seconds: float) -> Dict[str, Any]:
    """The schema-versioned machine form: `meta`, then the findings, in the ranked order."""
    return {
        "schema": "ueia.advice/1",
        "meta": meta_document(ctx, capture, engine_dir, seconds),
        "findings": [finding.as_json() for finding in findings],
    }
