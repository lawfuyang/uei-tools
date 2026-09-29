"""Two analyses, compared section by section -- and the CI gate the practice describes.

`compare` answers the question a change asks: *did this make it better or worse?* Two cached models
(a build before and after, two configurations, two scenes) reduce to the same set of named metrics,
the metrics are differenced **directionally** (each one knows whether lower is better), and the
practice's gate is applied: a candidate whose **p99 is more than 10% worse** fails, while
improvements are **logged rather than gated** -- a gate that fails on improvement is a gate nobody
keeps. The threshold is per-metric and `--threshold` changes it.

Three rules keep it honest:

* **Every number comes from a model**, so both sides are the same parser and the same definitions:
  a comparison never mixes a fresh measurement with a cached one.
* **A missing metric is not zero.** A capture with no frames, no scopes or no task channel
  contributes no metrics rather than zeroes, and a metric present on one side only is reported as
  such instead of being invented for the other.
* **A self-comparison is empty.** Comparing a capture with itself produces no moved metric, no
  breach and no improvement -- the A/B honesty check rcd-tools uses, and a test here.

`--baseline FILE` compares one capture against a saved baseline (a rolling baseline committed under
`goldens/` can then ratchet down instead of drifting up), `--save FILE` writes one. The artefacts
are deliberately compatible with Gauntlet/`AutomatedPerfTesting` runs: their `.utrace` is what we
read and their `.csv` is what `ueia csv` wraps -- their *runner* is not reimplemented here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import parallel
import sources
import summary
from engine import EngineDir
from shapes import UsageError
from summary import Budget

#: The schema of the machine-facing document and of a saved baseline.
SCHEMA = "ueia.compare/1"
#: The default gate: a metric more than 10% worse fails the run.
DEFAULT_THRESHOLD = 0.10
#: The metrics the gate looks at -- the practice's own list (mean/p95/p99 and the hitch count).
GATED = ("frames.mean_ms", "frames.p95_ms", "frames.p99_ms", "frames.hitches")


class Metric(NamedTuple):
    """One comparable number: where it came from, which way is better, and both sides' values."""

    key: str
    section: str
    label: str
    unit: str            # "ms" | "%" | "count" | "threads"
    lower_is_better: bool
    before: Optional[float]
    after: Optional[float]

    def delta(self) -> Optional[float]:
        """The relative change (`after / before - 1`), or None when it cannot be stated."""
        if self.before is None or self.after is None or not self.before:
            return None
        return (self.after - self.before) / self.before

    def moved(self) -> bool:
        return self.before is not None and self.after is not None and self.before != self.after

    def text(self) -> str:
        def one(value: Optional[float]) -> str:
            if value is None:
                return "-"
            if self.unit == "%":
                return "%.1f%%" % (value * 100.0,)
            if self.unit == "ms":
                return "%.3f" % (value,)
            return "%d" % (int(value),)

        delta = self.delta()
        return "%s -> %s%s" % (one(self.before), one(self.after),
                               "" if delta is None else "  (%+.1f%%)" % (delta * 100.0,))


class Report(NamedTuple):
    """The comparison: every metric, the breaches, the improvements, and the verdict."""

    before: str
    after: str
    threshold: float
    metrics: List[Metric]
    breached: List[Metric]
    improved: List[Metric]
    unavailable: List[str]

    def passed(self) -> bool:
        """The gate: no *gated* metric may be worse than the threshold, and improvements never fail."""
        return not self.breached

    def verdict(self) -> str:
        if self.breached:
            return "FAIL: %d metric(s) worse than %.0f%% -- %s" % (
                len(self.breached), self.threshold * 100.0,
                ", ".join(metric.key for metric in self.breached))
        if self.improved:
            return "PASS: nothing worse than %.0f%%; %d metric(s) improved (%s)" % (
                self.threshold * 100.0, len(self.improved),
                ", ".join(metric.key for metric in self.improved))
        return "PASS: nothing worse than %.0f%%, and nothing improved" % (self.threshold * 100.0,)


def metrics(model: Mapping[str, Any], budget: Budget,
            engine_dir: Optional[EngineDir] = None) -> Tuple[Dict[str, Metric], List[str]]:
    """Every comparable number of one capture, keyed by `section.name`, plus what was unavailable.

    The sections follow the reports: `frames` (the distribution and its hitches), `work` (what the
    walk attributed and what it could not), `occupancy` (the parallelism measures), `sources` (the
    mapping) and `tasks` (the critical path, when the channel is there).
    """
    out: Dict[str, Metric] = {}
    missing: List[str] = []

    def add(key: str, section: str, label: str, unit: str, lower: bool,
            before_value: Optional[float]) -> None:
        out[key] = Metric(key=key, section=section, label=label, unit=unit, lower_is_better=lower,
                          before=before_value, after=None)

    series = summary.series_of(model)
    times = summary.times_ms(model, series.rows) if series is not None else None
    if times is not None:
        distribution = summary.distribute(times, budget)
        add("frames.count", "frames", "frames measured", "count", False,
            distribution["count"])
        plain: Dict[str, Any] = dict(distribution)
        for name in ("mean", "p50", "p95", "p99", "max"):
            add("frames.%s_ms" % (name,), "frames", "%s frame time" % (name,), "ms", True,
                float(plain["%s_ms" % (name,)]))
        add("frames.over", "frames", "frames over budget", "count", True, distribution["over"])
        add("frames.hitches", "frames", "hitches", "count", True, distribution["hitches"])
    else:
        missing.append("no frame series (or no cycle frequency): the frame metrics are unavailable")

    counts = model.get("counts", {})
    if isinstance(counts, dict) and counts.get("scope_pairs"):
        pairs = int(counts.get("scope_pairs", 0))
        gaps = sum(int(counts.get(key, 0)) for key in
                   ("scope_pairs_spanning", "scope_pairs_no_spec", "scope_ends_unpaired",
                    "scope_begins_unpaired"))
        add("work.pairs", "work", "scope pairs attributed", "count", False, pairs)
        add("work.unattributed_share", "work", "share of pairs not attributed", "%", True,
            gaps / float(pairs + gaps) if pairs + gaps else 0.0)
        add("work.coarsened", "work", "timelines the cap had to merge", "count", True,
            counts.get("spans_coarsened", 0))
    else:
        missing.append("no CpuProfiler scope pairs: the work metrics are unavailable")

    who, who_notes = parallel.classify(model, budget)
    if who is not None:
        add("occupancy.solo_share", "occupancy", "frame thread's work with nobody beside it", "%",
            True, who.solo_share())
        add("occupancy.others_share", "occupancy", "other threads' work in the frames", "%", False,
            (who.others_work / float(who.span_cycles)) if who.span_cycles else 0.0)
        add("occupancy.threads_working", "occupancy", "threads working in the commonest frame",
            "threads", False, who.threads_working())
        add("occupancy.contended_frames", "occupancy", "frames with two threads inside a lock",
            "count", True, who.contended_frames)
    else:
        missing.append("parallelism: %s" % (who_notes[0] if who_notes else "unavailable"))

    mapped, mapped_notes = sources.build(model, budget, engine_dir)
    if mapped is not None:
        add("sources.located_share", "sources", "specs carrying a file:line", "%", False,
            mapped.located / float(mapped.specs) if mapped.specs else 0.0)
        add("sources.files", "sources", "distinct files", "count", False, len(mapped.files))
    else:
        missing.append("sources: %s" % (mapped_notes[0] if mapped_notes else "unavailable"))

    tasks = model.get("tasks", [])
    if isinstance(tasks, list) and tasks:
        add("tasks.count", "tasks", "tasks in the graph", "count", False, len(tasks))
        add("tasks.chain_ms", "tasks", "critical path", "ms", True,
            _chain_ms(model, budget))
    else:
        missing.append("no TaskTrace channel: the task metrics are unavailable")
    return out, missing


def _chain_ms(model: Mapping[str, Any], budget: Budget) -> Optional[float]:
    """The critical path's duration in milliseconds (the task channel's own number)."""
    session = model.get("session")
    frequency = int(session.get("cycle_frequency", 0) or 0) if isinstance(session, dict) else 0
    path = model.get("task_path", [])
    if not frequency or not isinstance(path, list) or not path:
        return None
    total = 0
    for step in path:
        if isinstance(step, dict):
            total += int(step.get("cycles", 0) or step.get("duration", 0) or 0)
    return total * 1000.0 / frequency if total else None


def compare(before: Mapping[str, Metric], after: Mapping[str, Metric], threshold: float,
            before_name: str, after_name: str,
            unavailable: Sequence[str] = ()) -> Report:
    """Difference two metric sets, apply the gate, and keep the improvements as information."""
    found: List[Metric] = []
    for key in sorted(set(before) | set(after)):
        left = before.get(key)
        right = after.get(key)
        if left is None and right is None:
            continue
        prototype = left or right
        assert prototype is not None
        found.append(prototype._replace(before=None if left is None else left.before,
                                        after=None if right is None else right.before))
    breached: List[Metric] = []
    improved: List[Metric] = []
    for metric in found:
        delta = metric.delta()
        if delta is None or not metric.moved():
            continue
        worse = delta > 0 if metric.lower_is_better else delta < 0
        better = delta < 0 if metric.lower_is_better else delta > 0
        if metric.key in GATED and worse and abs(delta) > threshold:
            breached.append(metric)
        elif better and abs(delta) > threshold:
            improved.append(metric)
    return Report(before=before_name, after=after_name, threshold=threshold, metrics=found,
                  breached=breached, improved=improved, unavailable=list(unavailable))


def lines(report: Report) -> Tuple[List[str], List[Tuple[str, ...]]]:
    """`(prose, rows)`: the verdict and the caveats, then one row per metric, sections in order."""
    prose = [
        "verdict   : %s" % (report.verdict(),),
        "threshold : %.0f%% on %s (--threshold PCT); improvements are logged, never gated"
        % (report.threshold * 100.0, ", ".join(GATED)),
    ]
    for reason in report.unavailable:
        prose.append("note      : %s" % (reason,))
    rows: List[Tuple[str, ...]] = []
    for metric in report.metrics:
        if not metric.moved():
            continue
        delta = metric.delta() or 0.0
        rows.append((metric.section, metric.key, metric.label, metric.text(),
                     "%+.1f%%" % (delta * 100.0,),
                     "worse" if (delta > 0) == metric.lower_is_better else "better"))
    if not rows:
        prose.append("no metric moved: a capture compared with itself is an empty diff")
    return prose, rows


def document(report: Report, meta: Mapping[str, Any]) -> Dict[str, Any]:
    """The machine form: `meta`, the verdict, and every metric with both sides and its delta."""
    return {
        "schema": SCHEMA,
        "meta": dict(meta),
        "verdict": {"passed": report.passed(), "text": report.verdict(),
                    "threshold": report.threshold, "gated": list(GATED)},
        "breached": [metric.key for metric in report.breached],
        "improved": [metric.key for metric in report.improved],
        "metrics": [
            {"key": metric.key, "section": metric.section, "label": metric.label,
             "unit": metric.unit, "lower_is_better": metric.lower_is_better,
             "before": metric.before, "after": metric.after, "delta": metric.delta()}
            for metric in report.metrics
        ],
    }


def markdown(report: Report, meta: Mapping[str, Any]) -> str:
    """The PR-comment shape: the verdict, a table of what moved, and what could not be compared."""
    out = ["## uei-tools: %s vs %s" % (report.before, report.after), ""]
    out.append("**%s**" % (report.verdict(),))
    out.append("")
    moved = [metric for metric in report.metrics if metric.moved()]
    if moved:
        out.append("| section | metric | before -> after | delta | verdict |")
        out.append("|---|---|---|---|---|")
        for metric in moved:
            delta = metric.delta() or 0.0
            out.append("| %s | `%s` | %s | %+.1f%% | %s |" % (
                metric.section, metric.key, metric.text().split("  (")[0], delta * 100.0,
                "worse" if (delta > 0) == metric.lower_is_better else "better"))
    else:
        out.append("No metric moved.")
    if report.unavailable:
        out.append("")
        out.append("Not compared: " + "; ".join(report.unavailable))
    out.append("")
    out.append("<sub>`%s` -- threshold %.0f%%, gated metrics: %s</sub>" % (
        SCHEMA, report.threshold * 100.0, ", ".join(GATED)))
    return "\n".join(out)


def baseline_document(before: Mapping[str, Metric], meta: Mapping[str, Any]) -> Dict[str, Any]:
    """A saved baseline: the same schema, one side filled in (what `--save` writes)."""
    return {
        "schema": SCHEMA,
        "meta": dict(meta),
        "metrics": [
            {"key": metric.key, "section": metric.section, "label": metric.label,
             "unit": metric.unit, "lower_is_better": metric.lower_is_better,
             "before": metric.before}
            for metric in before.values()
        ],
    }


def read_baseline(document: Mapping[str, Any]) -> Dict[str, Metric]:
    """A baseline document back into metrics, or a usage error naming what is wrong with it."""
    if not isinstance(document, Mapping) or document.get("schema") != SCHEMA:
        raise UsageError("--baseline wants a %s document (write one with --save FILE)" % (SCHEMA,))
    rows = document.get("metrics")
    if not isinstance(rows, list):
        raise UsageError("--baseline document has no metrics list")
    out: Dict[str, Metric] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        out[str(row.get("key", ""))] = Metric(
            key=str(row.get("key", "")), section=str(row.get("section", "")),
            label=str(row.get("label", "")), unit=str(row.get("unit", "ms")),
            lower_is_better=bool(row.get("lower_is_better", True)),
            before=row.get("before"), after=None,
        )
    return out


def load_baseline(path: str) -> Tuple[Dict[str, Metric], str]:
    """Read a baseline file: `(metrics, the name it was saved under)`, or a usage error."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as error:
        raise UsageError("--baseline %s could not be read: %s" % (path, error))
    name = ""
    meta = document.get("meta") if isinstance(document, Mapping) else None
    if isinstance(meta, Mapping):
        name = str(meta.get("capture", ""))
    return read_baseline(document), (name or path)


def save_baseline(path: str, metrics: Mapping[str, Metric], meta: Mapping[str, Any]) -> str:
    """Write a baseline document; returns the path it wrote, or raises a usage error."""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(baseline_document(metrics, meta), handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as error:
        raise UsageError("--save %s could not be written: %s" % (path, error))
    return path
