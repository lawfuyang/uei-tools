"""The commands: what the tool answers, and how each answer is rendered.

The five row commands (`packets`, `schema`, `threads`, `timers`, `frames`)
build their rows once and print them in one of three forms -- `table` (the
default), `csv` or `markdown` -- so the shapes cannot drift. A cap applies to
all three; in the non-table forms stdout is the table alone and the prose moves
to stderr. `info`, `verify`, `parse` and `cache` are prose.

Output rules that hold everywhere: deterministic for a fixed input and tool
version (sorted tables, no timestamps, no absolute paths -- a capture is named,
never located), and the cache is invisible (nothing here prints whether a
command was warm).
"""

from __future__ import annotations

import csv as csv_module
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, cast

import advice
import bottleneck
import cache
import compare
import container
import csvprof
import engine
import lz4
import parallel
import schema
import sources
import summary
import tasks
import timing
import toolrun
import streams
from container import Anomaly
from goldens import REPO_ROOT, cmd_goldens
from model import SessionModel, build_model, seconds_for_cycle, zero_counts
from shapes import (
    PROTOCOL_CURRENT,
    TID_EVENTS,
    TRANSPORT_TID_PACKET_SYNC,
    ContainerHeader,
    PacketRow,
    UeiaError,
    UsageError,
)

FORMATS = ("table", "csv", "markdown")
#: The machine form `advice` also takes: a schema-versioned document rather than a table (README
#: §4's output philosophy). Kept out of `FORMATS` because the table commands have no JSON form --
#: a report that prints a `meta` block and findings is a different shape of answer, not a rendering
#: of the same table.
JSON_FORMAT = "json"
_VALUE_OPTIONS = frozenset((
    "format", "limit", "tid", "filter", "jobs", "engine-dir",
    # the summary layer: a budget in either of the two spellings the practice uses
    "budget", "budget-ms",
    # and `advice`'s rule filter: a comma-separated list of rule ids to drop
    "skip",
    # and `compare`'s gate, its saved baseline, and the file to save one to
    "threshold", "baseline", "save",
    # the task graph's export form
    "graph",
    # the csv family: ours on the left, the exe's own flag in `_csv_*` below
    "out", "json", "stat", "stats", "csvs", "dir", "pattern", "outlier-stat", "outlier-threshold",
    "metadata-filter", "start-event", "in", "out-format", "compress", "set-metadata", "batch",
    "list", "type", "graph-xml", "report-xml", "summary-formats", "thresholds", "base",
    "dump-contents", "test-name", "delay", "stat-filter", "csv",
))
_BOOL_OPTIONS = frozenset((
    "clear", "build", "force",
    # the csv family
    "quiet", "show-averages", "show-min", "show-max", "show-totals", "show-all-stats",
    "show-events", "virtual-events", "verify", "in-place", "defaults", "recurse", "average",
    "update",
))

_DEFAULT_PACKET_LIMIT = 40
_DEFAULT_FRAME_LIMIT = 40
#: How many threads the parallelism table lists by default: the ones with work in the frame series
#: are few, and a pool of idle workers is one line each; `--limit 0` lists every one of them.
_DEFAULT_THREAD_LIMIT = 20
#: How many over-budget frames `summary` lists by default. Ten fits a page and is the worst of the
#: tail, which is what the practice says to look at; `--limit 0` asks for every one of them.
_DEFAULT_BREAKER_LIMIT = 10
#: How many source files `sources` lists by default: a page of the files that own the most work.
_DEFAULT_FILE_LIMIT = 40
_MAX_LISTED_ANOMALIES = 40

_ERROR_KINDS = frozenset((
    "truncated-packet-header",
    "bad-packet-size",
    "truncated-packet",
    "lz4-decode-error",
    "truncated-record",
    "truncated-event",
    "bad-aux-block",
    "truncated-aux-block",
    "missing-aux-terminal",
    "unknown-uid",
    "unknown-well-known-uid",
    "unknown-important-uid",
    "bad-new-event",
    "unexpected-events-record",
    "bad-batch",
))


class Options(object):
    """A parsed command line: `--name value` pairs and boolean `--flags`."""

    def __init__(self) -> None:
        self.values: Dict[str, str] = {}
        self.flags: Dict[str, bool] = {}

    def text(self, name: str, default: str = "") -> str:
        return self.values.get(name, default)

    def number(self, name: str, default: int) -> int:
        raw = self.values.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise UsageError("--%s wants a number, got %r" % (name, raw))

    def is_set(self, name: str) -> bool:
        return bool(self.flags.get(name, False))

    def fmt(self) -> str:
        value = self.text("format", "table")
        if value not in FORMATS:
            raise UsageError("--format is one of %s, got %r" % ("|".join(FORMATS), value))
        return value


def parse_options(args: Sequence[str]) -> Options:
    """Hand-rolled option parsing: no argparse, and every unknown flag is an error."""
    options = Options()
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("--"):
            raise UsageError("unexpected argument %r" % (arg,))
        name = arg[2:]
        inline: Optional[str] = None
        if "=" in name:
            name, inline = name.split("=", 1)
        if name in _VALUE_OPTIONS:
            value = inline
            if value is None:
                index += 1
                if index >= len(args):
                    raise UsageError("--%s needs a value" % (name,))
                value = args[index]
            options.values[name] = value
        elif name in _BOOL_OPTIONS:
            if inline is not None:
                raise UsageError("--%s takes no value" % (name,))
            options.flags[name] = True
        else:
            raise UsageError("unknown option %r" % (arg,))
        index += 1
    return options


def render_rows(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    right: Sequence[bool],
    fmt: str,
    prose: Sequence[str],
) -> None:
    """Print one row table in the wanted form, with its prose in the right place."""
    if fmt == "table":
        for line in prose:
            sys.stdout.write(line + "\n")
        if not rows:
            return
        widths = [len(header) for header in headers]
        for row in rows:
            for column, cell in enumerate(row):
                if len(cell) > widths[column]:
                    widths[column] = len(cell)

        def layout(cells: Sequence[str]) -> str:
            parts = []
            for column, cell in enumerate(cells):
                padded = (
                    cell.rjust(widths[column]) if right[column] else cell.ljust(widths[column])
                )
                parts.append(padded)
            return "  ".join(parts).rstrip()

        sys.stdout.write(layout(headers) + "\n")
        sys.stdout.write("  ".join("-" * width for width in widths) + "\n")
        for row in rows:
            sys.stdout.write(layout(row) + "\n")
        return

    for line in prose:
        sys.stderr.write(line + "\n")
    if fmt == "csv":
        writer = csv_module.writer(sys.stdout, lineterminator="\n")
        writer.writerow(list(headers))
        for row in rows:
            writer.writerow(list(row))
        return
    sys.stdout.write("| " + " | ".join(headers) + " |\n")
    sys.stdout.write("| " + " | ".join("---" for _ in headers) + " |\n")
    for row in rows:
        cells = [cell.replace("|", "\\|") for cell in row]
        sys.stdout.write("| " + " | ".join(cells) + " |\n")


class CaptureView(object):
    """A capture read up to its packet layer: the bytes and header always, the walk on demand.

    The walk is 163,600 packets of Python over the corpus (0.25 s, a quarter of every cold phase
    table this tool prints) and every warm command used to pay for it whether or not it wanted the
    table: `info`, `packets`, `schema` and `verify` do, while the model-based commands (`threads`,
    `timers`, `frames`, `summary`, `bottleneck`, the `csv` bridge) never look at it at all. So it is
    a property, walked once on first use -- which is half of a warm command's half-second.
    """

    def __init__(self, path: Path, data: bytes, header: ContainerHeader) -> None:
        self.path = path
        self.data = data
        self.header = header
        self._packets: Optional[List[PacketRow]] = None
        self._packet_anomalies: List[Anomaly] = []

    @property
    def packets(self) -> List[PacketRow]:
        if self._packets is None:
            with timing.timed("packets"):
                self._packets, self._packet_anomalies = container.walk_packets(
                    self.data, self.header
                )
        return self._packets

    @property
    def packet_anomalies(self) -> List[Anomaly]:
        self.packets  # the walk produces both, and neither is meaningful without it
        return self._packet_anomalies


def load_view(capture: str) -> CaptureView:
    """Read the file and its container header; the packet walk happens when it is asked for."""
    path = Path(capture)
    with timing.timed("read"):
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise UeiaError("cannot read capture %s: %s" % (path.name, exc))
    with timing.timed("container"):
        header = container.parse_header(data)
    return CaptureView(path=path, data=data, header=header)


def _jobs(options: "Options") -> int:
    """`--jobs N`: how many worker processes the walk may use; 0 (the default) lets it choose."""
    jobs = options.number("jobs", 0)
    if jobs < 0:
        raise UsageError("--jobs takes 0 (choose automatically) or a positive count")
    return jobs


def load_model(capture: str, jobs: int = 0) -> Tuple[CaptureView, SessionModel, bool]:
    """The session model, from the parse cache or from a full decode.

    Returns (view, model, from_cache). The cache never changes an answer -- only
    the seconds a command takes to print one.
    """
    view = load_view(capture)
    identity = cache.capture_identity(view.path)
    cached = cache.load(view.path, identity)
    if cached is not None:
        return view, cast(SessionModel, cached), True
    packets = view.packets  # walked here, so its phase is "packets" and not "streams"
    with timing.timed("streams"):
        stream_set = streams.assemble(view.data, packets)
    with timing.timed("model"):
        model, counts, _anomalies = build_model(stream_set, jobs)
    timing.progress(
        "parsed: %d packet(s), %d event(s)"
        % (len(view.packets), counts.get("events", 0))
    )
    cache.store(view.path, identity, dict(model))
    return view, model, False


def _counts(model: SessionModel) -> Dict[str, int]:
    return model.get("counts", {})


def _form_counts(packets: Sequence[PacketRow]) -> Dict[str, int]:
    forms: Dict[str, int] = {}
    for packet in packets:
        forms[packet.form] = forms.get(packet.form, 0) + 1
    return forms


def _tid_label(tid: int) -> str:
    if tid < 2 or tid >= 0x3FF0:
        return container.tid_name(tid)
    return str(tid)


def cmd_info(capture: str, args: List[str]) -> int:
    """`info <capture>`: the file's own account of itself, and its packet layer."""
    if args:
        raise UsageError("info takes no options")
    view = load_view(capture)
    header = view.header
    packets = view.packets
    forms = _form_counts(packets)
    wire_bytes = sum(
        packet.size - 4 for packet in packets if packet.form in ("raw", "lz4")
    )
    decoded_bytes = sum(
        packet.decoded_size for packet in packets if packet.form in ("raw", "lz4")
    )
    last_end = header.first_packet_offset
    if packets:
        last_end = packets[-1].offset + packets[-1].size
    thread_ids = set(packet.tid for packet in packets if 2 <= packet.tid < 0x3FF0)
    transport = header.transport_version
    transport_text = (
        "%d (TidPacketSync)" % (transport,)
        if transport == TRANSPORT_TID_PACKET_SYNC
        else str(transport)
    )
    metadata = container.metadata_summary(header)
    lines = [
        "capture      : %s" % (view.path.name,),
        "size         : %d bytes" % (len(view.data),),
        "magic        : %s" % (header.magic.decode("ascii", "replace"),),
        "transport    : %s" % (transport_text,),
        "protocol     : %d" % (header.protocol_version,),
        "metadata     : %d bytes (%s)" % (
            header.metadata_size,
            ", ".join("%s %s" % item for item in sorted(metadata.items())),
        ),
        "packets      : %d (raw %d, lz4 %d, sync %d)" % (
            len(packets), forms.get("raw", 0), forms.get("lz4", 0), forms.get("sync", 0),
        ),
        "payload      : %d bytes on the wire, %d decoded" % (wire_bytes, decoded_bytes),
        "stream end   : offset %d of %d (%s)" % (
            last_end,
            len(view.data),
            "exact" if last_end == len(view.data)
            else "%d byte(s) past the last packet" % (len(view.data) - last_end,),
        ),
        "threads      : %d thread id(s) with packets" % (len(thread_ids),),
    ]
    if header.protocol_version != PROTOCOL_CURRENT:
        lines.append(
            "note         : protocol %d is not the current %d; decode at your own risk"
            % (header.protocol_version, PROTOCOL_CURRENT)
        )
    anomaly_count = len(header.warnings) + len(view.packet_anomalies)
    lines.append("anomalies    : %d (see `verify`)" % (anomaly_count,))
    for line in lines:
        sys.stdout.write(line + "\n")
    return 0


def cmd_packets(capture: str, args: List[str]) -> int:
    """`packets <capture> [--limit N] [--tid N]`: the packet table."""
    options = parse_options(args)
    limit = options.number("limit", _DEFAULT_PACKET_LIMIT)
    tid_filter = options.number("tid", -1)
    view = load_view(capture)
    selected = [
        packet for packet in view.packets if tid_filter < 0 or packet.tid == tid_filter
    ]
    shown = selected if limit == 0 else selected[:limit]
    rows: List[Tuple[str, ...]] = []
    for packet in shown:
        rows.append((
            str(packet.index),
            str(packet.offset),
            str(packet.size),
            str(packet.decoded_size),
            _tid_label(packet.tid),
            packet.form,
        ))
    prose = ["packets: %d (showing %d)" % (len(selected), len(shown))]
    if view.packet_anomalies:
        prose.append("anomalies: %d (see `verify`)" % (len(view.packet_anomalies),))
    render_rows(
        ("index", "offset", "size", "decoded", "tid", "form"),
        rows,
        (True, True, True, True, True, False),
        options.fmt(),
        prose,
    )
    return 0


def cmd_schema(capture: str, args: List[str]) -> int:
    """`schema <capture> [--filter TEXT] [--limit N]`: the capture's vocabulary."""
    options = parse_options(args)
    limit = options.number("limit", 0)
    needle = options.text("filter").lower()
    _view, model, _cached = load_model(capture, _jobs(options))
    counts = _counts(model)
    uid_counts = model.get("uid_counts", {})
    schema = model.get("schema", [])
    selected = [
        row for row in schema if not needle or needle in str(row.get("full_name", "")).lower()
    ]
    shown = selected if limit == 0 else selected[:limit]
    rows: List[Tuple[str, ...]] = []
    for row in shown:
        fields = []
        for field in row.get("fields", []):
            text = "%s %s" % (field.get("name", ""), field.get("type_name", ""))
            if field.get("family", 0) != 0:
                text += " (%s)" % (field.get("family_name", ""),)
            fields.append(text)
        seen = uid_counts.get(str(row.get("uid", 0)))
        rows.append((
            str(row.get("uid", 0)),
            str(row.get("flag_names", "none")),
            str(row.get("full_name", "")),
            ", ".join(fields),
            "-" if seen is None else str(seen),
        ))
    prose = [
        "event types: %d (showing %d); records %d, redefinitions %d" % (
            len(schema), len(shown), counts.get("new_events", 0), counts.get("redefined", 0),
        ),
        "count = events on thread streams (specs live on the importants stream)",
    ]
    render_rows(
        ("uid", "flags", "name", "fields", "count"),
        rows,
        (True, False, False, False, True),
        options.fmt(),
        prose,
    )
    return 0


def cmd_threads(capture: str, args: List[str]) -> int:
    """`threads <capture>`: every thread the capture knows, with its numbers."""
    options = parse_options(args)
    _view, model, _cached = load_model(capture, _jobs(options))
    threads = model.get("threads", [])
    rows: List[Tuple[str, ...]] = []
    for row in threads:
        rows.append((
            str(row.get("tid", 0)),
            str(row.get("name", "")) or "-",
            str(row.get("group", "")) or "-",
            str(row.get("packets", 0)),
            str(row.get("bytes", 0)),
            str(row.get("events", 0)),
            str(row.get("batches", 0)),
            str(row.get("batch_records", 0)),
            str(row.get("first_cycle", 0)),
            str(row.get("last_cycle", 0)),
        ))
    named = len([row for row in threads if row.get("name", "")])
    prose = ["threads: %d (%d named by the capture)" % (len(threads), named)]
    render_rows(
        ("tid", "name", "group", "packets", "bytes", "events", "batches", "records",
         "first cycle", "last cycle"),
        rows,
        (True, False, False, True, True, True, True, True, True, True),
        options.fmt(),
        prose,
    )
    return 0


def cmd_timers(capture: str, args: List[str]) -> int:
    """`timers <capture> [--filter TEXT] [--limit N]`: the CPU profiler's specs."""
    options = parse_options(args)
    limit = options.number("limit", 0)
    needle = options.text("filter").lower()
    _view, model, _cached = load_model(capture, _jobs(options))
    timers = model.get("timers", [])
    selected = [
        row for row in timers if not needle or needle in str(row.get("name", "")).lower()
    ]
    shown = selected if limit == 0 else selected[:limit]
    rows: List[Tuple[str, ...]] = []
    for row in shown:
        line = row.get("line", 0)
        rows.append((
            str(row.get("id", 0)),
            str(row.get("name", "")),
            str(row.get("file", "")) or "-",
            str(line) if line else "-",
        ))
    with_source = len([row for row in timers if row.get("file", "")])
    prose = [
        "timer specs: %d (showing %d; %d with file:line)" % (
            len(timers), len(shown), with_source,
        ),
    ]
    render_rows(
        ("id", "name", "file", "line"),
        rows,
        (True, False, False, True),
        options.fmt(),
        prose,
    )
    return 0


def cmd_frames(capture: str, args: List[str]) -> int:
    """`frames <capture> [--limit N]`: BeginFrame/EndFrame pairs, in cycles."""
    options = parse_options(args)
    limit = options.number("limit", _DEFAULT_FRAME_LIMIT)
    _view, model, _cached = load_model(capture, _jobs(options))
    frames = model.get("frames", [])
    shown = frames if limit == 0 else frames[:limit]
    counts = _counts(model)
    rows: List[Tuple[str, ...]] = []
    duration_known = False
    for row in shown:
        begin = int(row.get("begin_cycle", 0))
        end = int(row.get("end_cycle", 0))
        seconds = seconds_for_cycle(model, end)
        if seconds is not None:
            duration_known = True
        rows.append((
            str(row.get("index", 0)),
            str(row.get("type", 0)),
            str(row.get("tid", 0)),
            str(begin),
            str(end),
            "-" if seconds is None else "%.3f" % (seconds,),
        ))
    prose = [
        "frames: %d (showing %d); unpaired begins %d, ends %d" % (
            len(frames), len(shown),
            counts.get("unpaired_frame_begin", 0), counts.get("unpaired_frame_end", 0),
        ),
    ]
    if frames and not duration_known:
        prose.append("seconds are '-' because the capture carries no cycle frequency")
    render_rows(
        ("index", "type", "tid", "begin cycle", "end cycle", "seconds"),
        rows,
        (True, True, True, True, True, True),
        options.fmt(),
        prose,
    )
    return 0


def _positional(args: Sequence[str]) -> Tuple[Optional[str], List[str]]:
    """The first argument that is neither an option nor an option's value, and the rest of the line.

    `compare` is the one command with a second positional (the capture to compare against), and this
    is what keeps `--budget 30` from being read as it -- the same job `csv`'s own reader does, and
    for the same reason.
    """
    found: Optional[str] = None
    out: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--"):
            name = arg[2:].split("=", 1)[0]
            out.append(arg)
            if "=" not in arg and name in _VALUE_OPTIONS and index + 1 < len(args):
                index += 1
                out.append(args[index])
            index += 1
            continue
        if found is None:
            found = arg
        index += 1
    return found, out


def cmd_compare(capture: str, args: List[str]) -> int:
    """`compare <before> <after> | <capture> --baseline FILE | <capture> --save FILE`: A/B, and a gate.

    Two cached analyses reduced to the same named metrics and differenced section by section
    (`compare.py` has the sections and the rules). The gate is the practice's: a candidate whose
    **p99 is more than `--threshold` (10% by default) worse** fails -- exit 1, so a CI job can branch
    on it -- while improvements are logged, never gated. `--baseline FILE` compares one capture
    against a saved baseline and `--save FILE` writes one, which is how a rolling baseline under
    `goldens/` ratchets down instead of drifting up.

    Exit codes: 0 nothing worse than the threshold (or a baseline saved), 1 the gate failed, 2 a
    usage error or nothing comparable, 1 a failure.
    """
    second, rest = _positional(args)
    options = parse_options(rest)
    if options.values.keys() - {"budget", "budget-ms", "tid", "threshold", "format", "jobs",
                               "engine-dir", "baseline", "save"} or options.flags:
        raise UsageError(
            "compare takes a second capture (or --baseline FILE / --save FILE), then --budget, "
            "--budget-ms, --tid, --threshold, --engine-dir, --format and --jobs"
        )
    budget = summary.parse_budget(options.values.get("budget"), options.values.get("budget-ms"))
    threshold = options.number("threshold", int(compare.DEFAULT_THRESHOLD * 100)) / 100.0
    if threshold < 0:
        raise UsageError("--threshold takes a percentage (10 means 10%% worse), not a negative")
    baseline_path = options.text("baseline")
    save_path = options.text("save")
    if not second and not baseline_path and not save_path:
        raise UsageError("compare needs a second capture, --baseline FILE or --save FILE")
    if second and baseline_path:
        raise UsageError("compare takes a second capture or --baseline FILE, not both")
    root, notes = _engine_dir(options)
    view, model, _cached = load_model(capture, _jobs(options))
    after_name = view.path.name
    after_metrics, after_missing = compare.metrics(model, budget, root)
    meta: Dict[str, object] = {
        "tool_version": str(model.get("tool_version", "")),
        "after": cache.capture_identity(Path(capture)),
        "budget": {"label": budget.label(), "ms": budget.ms},
        "threshold": threshold,
        "engine_dir": str(root.root) if root is not None else "",
        "gated": list(compare.GATED),
    }
    if save_path:
        if second or baseline_path:
            raise UsageError("--save writes a baseline, so it takes no second capture or baseline")
        written = compare.save_baseline(save_path, after_metrics, meta)
        sys.stdout.write("baseline  : %s written from %s (%d metric(s))\n"
                         % (written, after_name, len(after_metrics)))
        return 0

    if baseline_path:
        before_metrics, before_name = compare.load_baseline(baseline_path)
        before_missing: List[str] = []
        meta["before"] = {"baseline": baseline_path}
    else:
        other_view, other_model, _cached = load_model(second or "", _jobs(options))
        before_metrics, before_missing = compare.metrics(other_model, budget, root)
        before_name = other_view.path.name
        meta["before"] = cache.capture_identity(Path(second or ""))
    report = compare.compare(before_metrics, after_metrics, threshold, before_name, after_name,
                             sorted(set(after_missing + before_missing)))

    fmt = options.fmt() if options.text("format", "table") != JSON_FORMAT else JSON_FORMAT
    if fmt == JSON_FORMAT:
        sys.stdout.write(json.dumps(compare.document(report, meta), indent=2, sort_keys=True) + "\n")
        return 0 if report.passed() else 1
    if fmt == "markdown":
        sys.stdout.write(compare.markdown(report, meta) + "\n")
        return 0 if report.passed() else 1
    lines = ["before    : %s" % (report.before,), "after     : %s" % (report.after,)]
    lines.extend(notes)
    prose, rows = compare.lines(report)
    lines.extend(prose)
    render_rows(
        ("section", "metric", "what", "before -> after", "delta", "verdict"),
        rows,
        (False, False, False, False, True, False),
        fmt,
        lines,
    )
    return 0 if report.passed() else 1


def cmd_advice(capture: str, args: List[str]) -> int:
    """`advice <capture> [--budget ...] [--tid N] [--skip IDS] [--limit N]`: what to do next.

    The practice's last question, answered as rules over the measurements the other reports already
    make: the budget verdict, the tail, the bottleneck, the occupancy, the source mapping, the
    channel inventory. Every finding carries its evidence, the file:line where the trace has one, and
    the next command to run (`advice.py` has the rules and the ranking). `--format json` is the
    machine form -- schema-versioned, with a `meta` block -- and `--skip id,id` drops rules.

    Exit codes: 0 always (this is the command that always has something to say, even if it is "this
    capture carries no frame pair"), 2 a usage error, 1 a failure.
    """
    options = parse_options(args)
    if options.values.keys() - {"budget", "budget-ms", "tid", "limit", "skip", "format", "jobs",
                               "engine-dir"} or options.flags:
        raise UsageError(
            "advice takes --budget, --budget-ms, --tid, --limit, --skip, --engine-dir, --format "
            "and --jobs"
        )
    fmt = options.text("format", "table")
    if fmt not in FORMATS + (JSON_FORMAT,):
        raise UsageError("--format is one of %s, got %r"
                         % ("|".join(FORMATS + (JSON_FORMAT,)), fmt))
    budget = summary.parse_budget(options.values.get("budget"), options.values.get("budget-ms"))
    explicit = bool(options.values.get("budget") or options.values.get("budget-ms"))
    skipped = tuple(part.strip() for part in options.text("skip").split(",") if part.strip())
    limit = options.number("limit", _DEFAULT_BREAKER_LIMIT)
    root, notes = _engine_dir(options)
    started = time.monotonic()
    view, model, _cached = load_model(capture, _jobs(options))
    context = advice.context(model, budget, explicit, root, capture)
    findings = advice.build(context, skipped)
    seconds = time.monotonic() - started
    if fmt == JSON_FORMAT:
        document = advice.document(findings, context, cache.capture_identity(Path(capture)), root,
                                   seconds)
        sys.stdout.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        return 0
    session = model.get("session", {})
    duration = seconds_for_cycle(model, int(session.get("last_cycle", 0)))
    lines = ["capture   : %s%s" % (
        view.path.name, ", %.3f s" % (duration,) if duration is not None else "",
    )]
    identity = " / ".join(
        part for part in (
            str(session.get("app", "")), str(session.get("project", "")),
            str(session.get("target", "")),
        ) if part
    )
    if identity:
        lines.append("session   : %s" % (identity,))
    lines.append("budget    : %s%s" % (
        budget.label(), "" if explicit else " (the default: no --budget was given)",
    ))
    lines.extend(notes)
    prose, rows = advice.lines(findings, context, limit)
    lines.extend(prose)
    render_rows(
        ("id", "severity", "confidence", "effort", "where", "next command"),
        rows,
        (False, False, False, False, False, False),
        fmt,
        lines,
    )
    return 0


def cmd_sources(capture: str, args: List[str]) -> int:
    """`sources <capture> [--engine-dir DIR] [--filter TEXT] [--limit N]`: where the timers live.

    The trace's own file:line for every timer spec that carries one, classified into engine vs
    project by the **shape** of the path (the recorded paths belong to the machine that recorded it),
    weighted by the work the model keeps for the longest frames, and cross-referenced with the
    engine's own anti-pattern names. `--engine-dir` is optional and only ever *checks*: how many of
    the recorded engine paths exist in the tree at hand, which is a version-mismatch signal and not a
    requirement (`sources.py` has the rules).

    Exit codes: 0 a report was produced, 2 the capture cannot answer (no timer specs at all, or none
    of them carries a file:line), 1 a failure.
    """
    options = parse_options(args)
    if options.values.keys() - {"engine-dir", "filter", "limit", "budget", "budget-ms", "format",
                                "jobs"} or options.flags:
        raise UsageError(
            "sources takes --engine-dir, --filter, --limit, --budget, --budget-ms, --format and "
            "--jobs"
        )
    budget = summary.parse_budget(options.values.get("budget"), options.values.get("budget-ms"))
    limit = options.number("limit", _DEFAULT_FILE_LIMIT)
    root, notes = _engine_dir(options)
    view, model, _cached = load_model(capture, _jobs(options))
    report, reasons = sources.build(model, budget, root)
    if report is None:
        lines = ["capture   : %s" % (view.path.name,)]
        lines.extend(notes)
        for reason in reasons:
            lines.append("cannot    : %s" % (reason,))
        lines.append("hint      : a source mapping needs the capture's own spec table; it is what "
                     "-trace=cpu writes, with symbols")
        for line in lines:
            sys.stdout.write(line + "\n")
        return 2
    session = model.get("session", {})
    duration = seconds_for_cycle(model, int(session.get("last_cycle", 0)))
    lines = ["capture   : %s%s" % (
        view.path.name, ", %.3f s" % (duration,) if duration is not None else "",
    )]
    identity = " / ".join(
        part for part in (
            str(session.get("app", "")), str(session.get("project", "")),
            str(session.get("target", "")),
        ) if part
    )
    if identity:
        build = str(session.get("build_version", ""))
        configuration = str(session.get("configuration", ""))
        qualifiers = ", ".join(part for part in (configuration, build) if part)
        lines.append("session   : %s%s" % (identity, " (%s)" % (qualifiers,) if qualifiers else ""))
    lines.extend(notes)
    frequency = int(session.get("cycle_frequency", 0) or 0) if isinstance(session, dict) else 0
    prose, rows = sources.summarize_lines(
        report, frequency, limit, options.text("filter"),
    )
    lines.extend(prose)
    render_rows(
        ("file:line", "side", "module", "specs", "share", "over", "top timer"),
        rows,
        (False, False, False, True, True, True, False),
        options.fmt(),
        lines,
    )
    return 0


def cmd_summary(capture: str, args: List[str]) -> int:
    """`summary <capture> [--budget FPS|--budget-ms MS] [--tid N] [--limit N]`: is this capture fast?

    The session at a glance, the frame-time distribution against an explicit budget, and the frames
    that break it -- worst first, each naming the timers that ran in it. The distribution is the
    answer, never its mean (REFERENCE §10 and `summary.py` have the definitions); the table is
    rendered like every row command, prose included.

    Exit codes: 0 a report was produced, 2 the capture cannot answer a frame-time question (no
    `Misc.BeginFrame` pairs, or no cycle frequency -- cycles are not milliseconds without it), 1 a
    failure. Being *over* budget is not a failure: it is the report.
    """
    options = parse_options(args)
    if options.values.keys() - {"budget", "budget-ms", "tid", "limit", "format", "jobs"} \
            or options.flags:
        raise UsageError(
            "summary takes --budget, --budget-ms, --tid, --limit, --format and --jobs"
        )
    budget = summary.parse_budget(options.values.get("budget"), options.values.get("budget-ms"))
    limit = options.number("limit", _DEFAULT_BREAKER_LIMIT)
    tid = options.number("tid", -1)
    _view, model, _cached = load_model(capture, _jobs(options))
    series = summary.series_of(model, None if tid < 0 else tid)
    lines: List[str] = []
    if series is None:
        lines.append(
            "frames    : none -- this capture carries no Misc.BeginFrame/EndFrame pair%s"
            % (" on tid %d" % (tid,) if tid >= 0 else "",)
        )
        lines.append("hint      : a frame-time report needs a capture recorded with -trace=cpu,frame")
        for line in lines:
            sys.stdout.write(line + "\n")
        return 2
    times = summary.times_ms(model, series.rows)
    frequency = int(model.get("session", {}).get("cycle_frequency", 0) or 0)
    if times is None:
        lines.append("frames    : %s" % (series.describe(),))
        lines.append(
            "frequency : 0 -- the capture declares no cycle frequency, so frame times cannot be "
            "stated in milliseconds"
        )
        for line in lines:
            sys.stdout.write(line + "\n")
        return 2
    distribution = summary.distribute(times, budget)
    session = model.get("session", {})
    duration = seconds_for_cycle(model, int(session.get("last_cycle", 0)))
    names = {int(row.get("id", 0)): str(row.get("name", "")) for row in model.get("timers", [])}
    counts = _counts(model)
    lines.append("capture   : %s%s" % (
        _view.path.name, ", %.3f s" % (duration,) if duration is not None else "",
    ))
    identity = " / ".join(
        part for part in (
            str(session.get("app", "")), str(session.get("project", "")),
            str(session.get("target", "")),
        ) if part
    )
    if identity:
        build = str(session.get("build_version", ""))
        configuration = str(session.get("configuration", ""))
        qualifiers = ", ".join(part for part in (configuration, build) if part)
        lines.append("session   : %s%s" % (identity, " (%s)" % (qualifiers,) if qualifiers else ""))
    lines.append("frames    : %s, spanning %s" % (
        series.describe(), "%.3f s" % (series.span_s,) if series.span_s is not None else "?",
    ))
    lines.append("budget    : %s; a hitch is a frame over %.3f ms" % (
        budget.label(), budget.ms * summary.HITCH_FACTOR,
    ))
    lines.append("time      : mean %.3f, min %.3f, p50 %.3f, p95 %.3f, p99 %.3f, max %.3f ms" % (
        distribution["mean_ms"], distribution["min_ms"], distribution["p50_ms"],
        distribution["p95_ms"], distribution["p99_ms"], distribution["max_ms"],
    ))
    lines.append("verdict   : %s" % (summary.verdict_text(distribution, budget),))
    report, reasons = bottleneck.classify(model, budget, series.tid)
    if report is None:
        lines.append("bottleneck: cannot be decided here -- %s" % ("; ".join(reasons),))
    else:
        lines.append("bottleneck: %s" % (bottleneck.verdict_text(report),))
    lines.append("histogram :")
    lines.extend(summary.histogram_lines(summary.histogram(times, budget.ms)))
    lines.append("work      : %d scope pair(s) attributed (%d spanning a frame edge, %d with no "
                 "spec, %d unpaired end(s), %d unpaired begin(s)); the work column is filled for "
                 "the longest frames only" % (
                     counts.get("scope_pairs", 0), counts.get("scope_pairs_spanning", 0),
                     counts.get("scope_pairs_no_spec", 0), counts.get("scope_ends_unpaired", 0),
                     counts.get("scope_begins_unpaired", 0),
                 ))
    over = [
        (row, ms) for row, ms in zip(series.rows, times) if ms > budget.ms
    ]
    over.sort(key=lambda item: (-item[1], int(item[0].get("begin_cycle", 0))))
    shown = over if limit == 0 else over[:limit]
    rows: List[Tuple[str, ...]] = []
    for row, ms in shown:
        work = summary.frame_work_of(
            model, series.tid, series.type, int(row.get("begin_cycle", 0)),
        )
        at = seconds_for_cycle(model, int(row.get("begin_cycle", 0)))
        rows.append((
            str(row.get("index", 0)),
            "-" if at is None else "%.3f" % (at,),
            "%.3f" % (ms,),
            "%.2f" % (ms / budget.ms,),
            summary.work_text(work, names, frequency, int(row.get("end_cycle", 0))
                              - int(row.get("begin_cycle", 0))),
        ))
    if over and not shown:
        lines.append("note      : %d frame(s) over budget, none shown (--limit 0 lists them all)" % (
            len(over),
        ))
    render_rows(
        ("frame", "at s", "ms", "x budget", "top work"),
        rows,
        (True, True, True, True, False),
        options.fmt(),
        lines,
    )
    return 0


def cmd_bottleneck(capture: str, args: List[str]) -> int:
    """`bottleneck <capture> [--budget FPS|--budget-ms MS] [--tid N] [--limit N]`: what bounds a frame?

    The first question the practice asks: is this frame bound by the game thread, the render thread,
    the GPU -- or by none of them (a wait, a cap, or something this capture cannot see)? Every
    verdict is a measurement against the budget: the thread's own non-wait scope coverage, the
    sibling series' coverage inside the same frame, and the GPU's busy time when the capture carries
    the legacy GPU channel. The rule is enforced in the output (REFERENCE §11): a capture with no
    GPU data gets its CPU finding *plus* "the GPU side is unknown here" and the re-record line, never
    a bare "CPU-bound".

    Exit codes: 0 classified, 2 the capture cannot answer (no frame pairs, no cycle frequency, no
    timer specs), 1 a failure.
    """
    options = parse_options(args)
    if options.values.keys() - {"budget", "budget-ms", "tid", "limit", "format", "jobs"} \
            or options.flags:
        raise UsageError(
            "bottleneck takes --budget, --budget-ms, --tid, --limit, --format and --jobs"
        )
    budget = summary.parse_budget(options.values.get("budget"), options.values.get("budget-ms"))
    limit = options.number("limit", _DEFAULT_BREAKER_LIMIT)
    tid = options.number("tid", -1)
    view, model, _cached = load_model(capture, _jobs(options))
    report, notes = bottleneck.classify(model, budget, None if tid < 0 else tid)
    session = model.get("session", {})
    duration = seconds_for_cycle(model, int(session.get("last_cycle", 0)))
    lines: List[str] = []
    if report is None:
        lines.append("capture   : %s" % (view.path.name,))
        for note in notes:
            lines.append("cannot    : %s" % (note,))
        lines.append("hint      : `bottleneck` needs frame pairs, a cycle frequency and "
                     "CpuProfiler scopes in the same capture")
        for line in lines:
            sys.stdout.write(line + "\n")
        return 2
    lines.append("capture   : %s%s" % (
        view.path.name, ", %.3f s" % (duration,) if duration is not None else "",
    ))
    lines.append("frames    : %s, spanning %s" % (
        report.series.describe(),
        "%.3f s" % (report.series.span_s,) if report.series.span_s is not None else "?",
    ))
    lines.append("budget    : %s" % (budget.label(),))
    lines.append("verdict   : %s" % (bottleneck.verdict_text(report),))
    for role in report.roles:
        lines.append("thread    : tid %d %s (%s, heuristic) -- %d frame(s), %d over budget" % (
            role.tid, role.name or "unnamed", role.role, role.frames, role.over_budget,
        ))
    if report.gpu_present:
        lines.append("gpu       : %d frame(s) decoded from the capture's GpuProfiler channel" % (
            len([row for row in model.get("gpu_frames", []) if isinstance(row, dict)]),
        ))
    for note in report.notes:
        lines.append("note      : %s" % (note,))
    ordered = sorted(report.verdicts, key=lambda item: (-item.milliseconds,
                                                        int(item.frame["begin_cycle"])))
    shown = ordered if limit == 0 else ordered[:limit]
    rows: List[Tuple[str, ...]] = []
    for verdict in shown:
        at = seconds_for_cycle(model, int(verdict.frame.get("begin_cycle", 0)))
        rows.append((
            str(verdict.frame.get("index", 0)),
            "-" if at is None else "%.3f" % (at,),
            "%.3f" % (verdict.milliseconds,),
            verdict.verdict,
            "-" if verdict.work_ms is None else "%.3f" % (verdict.work_ms,),
            "-" if verdict.gpu_ms is None else "%.3f" % (verdict.gpu_ms,),
            bottleneck.evidence_text(model, verdict),
        ))
    render_rows(
        ("frame", "at s", "ms", "verdict", "work ms", "gpu ms", "why / what ran in it"),
        rows,
        (True, True, True, False, True, True, False),
        options.fmt(),
        lines,
    )
    return 0


def cmd_tasks(capture: str, args: List[str]) -> int:
    """`tasks <capture> [--limit N] [--graph dot|mermaid|json]`: the task graph and its longest chain.

    The task channel's own answer to "what was this capture waiting on": every `TaskTrace` event the
    walk collected becomes a task, `SubsequentAdded` becomes the dependency edges, and the chain the
    engine's own task-graph profiler computes -- the max sum of executing durations along those edges
    (`TaskGraphProfilerManager.cpp:759-760`) -- is reported step by step, oldest first, with the
    frame each step ran in.

    `--graph` writes the dependency graph (between the tasks the model kept, and the chain) as
    Graphviz DOT, Mermaid, or the model's own JSON, on stdout with the prose on stderr -- the shape
    of every row command's machine form.

    Exit codes: 0 reported, 2 the capture carries no `TaskTrace` events (the re-record line, never an
    empty path), 1 a failure.
    """
    options = parse_options(args)
    if options.values.keys() - {"limit", "graph", "format", "jobs"} or options.flags:
        raise UsageError("tasks takes --limit, --graph, --format and --jobs")
    limit = options.number("limit", _DEFAULT_BREAKER_LIMIT * 4)
    graph = options.values.get("graph", "")
    if graph and graph not in ("dot", "mermaid", "json"):
        raise UsageError("--graph takes dot, mermaid or json, not %r" % (graph,))
    view, model, _cached = load_model(capture, _jobs(options))
    session = model.get("session", {})
    task_counts = model.get("task_counts", {})
    lines: List[str] = []
    if not task_counts.get("events"):
        lines.append("capture   : %s" % (view.path.name,))
        lines.append("tasks     : none -- this capture carries no TaskTrace events")
        lines.append("hint      : re-record with `-trace=cpu,frame,log,bookmark,counters,task` "
                     "(or the engine's `TaskGraph` preset) to get a task graph")
        for line in lines:
            sys.stdout.write(line + "\n")
        return 2
    frequency = int(session.get("cycle_frequency", 0) or 0)
    path = [row for row in model.get("task_path", []) if isinstance(row, dict)]
    steps = path if limit == 0 else path[:limit]
    duration = seconds_for_cycle(model, int(session.get("last_cycle", 0)))
    lines.append("capture   : %s%s" % (
        view.path.name, ", %.3f s" % (duration,) if duration is not None else "",
    ))
    lines.append(
        "tasks     : %d task(s) from %d TaskTrace event(s), %d dependency edge(s)" % (
            task_counts.get("tasks", 0), task_counts.get("events", 0), task_counts.get("edges", 0),
        )
    )
    lines.append(
        "states    : %d ran, %d never finished, %d event(s) unreadable, %d edge(s) in a cycle "
        "(ignored)" % (
            task_counts.get("tasks", 0) - task_counts.get("unfinished", 0),
            task_counts.get("unfinished", 0), task_counts.get("unreadable", 0),
            task_counts.get("ignored_edges", 0),
        )
    )
    frames = sorted(set(
        int(step["frame_index"]) for step in path
        if step["frame_index"] is not None
    ))
    lines.append(
        "critical  : %.3f ms over %d step(s), %d frame(s) touched (%s)" % (
            task_counts.get("path_ms", 0), task_counts.get("path_steps", 0), len(frames),
            ", ".join("frame %d" % (index,) for index in frames[:6]) or "no frame on this thread",
        )
    )
    waits = [row for row in model.get("task_waits", []) if isinstance(row, dict)]
    if waits:
        lines.append("waiting   : %d span(s) on %d thread(s), %.3f ms total" % (
            len(waits), len(set(int(row["tid"]) for row in waits)),
            sum(float(row["duration_ms"]) for row in waits),
        ))
    else:
        lines.append("waiting   : no WaitingStarted/Finished span in this capture")
    if task_counts.get("dropped"):
        lines.append("note      : %d task(s) beyond the %d the model keeps are not listed "
                     "(the chain is always complete)" % (
                         task_counts["dropped"], len(model.get("tasks", [])),
                     ))
    if graph:
        edges = [(int(edge[0]), int(edge[1])) for edge in model.get("task_edges", [])]
        labels = tasks.labels_for(
            [row for row in model.get("tasks", []) if isinstance(row, dict)], frequency,
        )
        if graph == "dot":
            text = tasks.dot(edges, labels)
        elif graph == "mermaid":
            text = tasks.mermaid(edges, labels)
        else:
            text = json.dumps({
                "schemaVersion": 1,
                "capture": view.path.name,
                "tasks": [
                    {
                        "id": int(row["id"]), "name": row["name"], "size": int(row["size"]),
                        "tracked": bool(row["tracked"]),
                        "threadToExecuteOn": tasks.describe_execution(
                            int(row["thread_to_execute_on"])
                        ),
                        "started": row["started"], "finished": row["finished"],
                        "durationMs": round(tasks.duration_cycles(row) * 1000.0 / frequency, 3)
                        if frequency else 0.0,
                        "prerequisites": list(row["prerequisites"]),
                    }
                    for row in model.get("tasks", []) if isinstance(row, dict)
                ],
                "edges": [[int(first), int(second)] for first, second in edges],
                "criticalPath": [
                    {"id": int(step["id"]), "name": step["name"],
                     "durationMs": round(float(step["duration_ms"]), 3),
                     "frame": step.get("frame_index")}
                    for step in path
                ],
                "counts": dict(task_counts),
            }, indent=2, sort_keys=True) + "\n"
        if graph != "json":
            for line in lines:
                sys.stderr.write(line + "\n")
        sys.stdout.write(text)
        if graph == "json" and options.fmt() != "json":
            for line in lines:
                sys.stderr.write(line + "\n")
        return 0
    rows: List[Tuple[str, ...]] = []
    for step in steps:
        tid = int(step["started_tid"])
        frame = step.get("frame_index")
        rows.append((
            str(step["id"]),
            "%.3f" % (float(step["duration_ms"]),),
            tasks.thread_name(int(step["thread_to_execute_on"]))
            or ("tid %d" % (tid,) if tid else "unknown thread"),
            "-" if frame is None else str(frame),
            step["name"],
        ))
    if len(path) > len(steps):
        lines.append("note      : %d more step(s) on the chain; --limit 0 lists them all" % (
            len(path) - len(steps),
        ))
    render_rows(
        ("task", "ms", "thread", "frame", "name"),
        rows,
        (True, True, False, True, False),
        options.fmt(),
        lines,
    )
    return 0


def cmd_parallelism(capture: str, args: List[str]) -> int:
    """`parallelism <capture> [--budget FPS|--budget-ms MS] [--tid N] [--limit N]`: was it spread?

    The practice's first question about a slow frame after "what bounds it": **how much of the
    machine was ever working beside the frame thread?** The occupancy table is per thread -- busy,
    waiting and lock-named cycles inside the frame series' own windows, measured by the walk and
    folded into frames at build time (`coverage.measure_frames`) -- and the findings are the
    measured ones: the frame thread's **solo** work, the most threads working at once, lock
    overlap, and the timers that own a quarter of the frames the model keeps. Every ceiling printed
    is Amdahl on a measured share and is labelled a heuristic; a capture with no frames, no cycle
    frequency or no scopes exits 2 rather than printing zeroes.

    Exit codes: 0 a report was produced, 2 the capture cannot answer, 1 a failure.
    """
    options = parse_options(args)
    if options.values.keys() - {"budget", "budget-ms", "tid", "limit", "format", "jobs"} \
            or options.flags:
        raise UsageError(
            "parallelism takes --budget, --budget-ms, --tid, --limit, --format and --jobs"
        )
    budget = summary.parse_budget(options.values.get("budget"), options.values.get("budget-ms"))
    limit = options.number("limit", _DEFAULT_THREAD_LIMIT)
    tid = options.number("tid", -1)
    view, model, _cached = load_model(capture, _jobs(options))
    report, reasons = parallel.classify(model, budget, None if tid < 0 else tid)
    session = model.get("session", {})
    duration = seconds_for_cycle(model, int(session.get("last_cycle", 0)))
    lines: List[str] = []
    if report is None:
        lines.append("capture   : %s" % (view.path.name,))
        for reason in reasons:
            lines.append("cannot    : %s" % (reason,))
        lines.append("hint      : `parallelism` needs frame pairs, a cycle frequency and "
                     "CpuProfiler scopes in the same capture")
        for line in lines:
            sys.stdout.write(line + "\n")
        return 2
    frequency = int(session.get("cycle_frequency", 0) or 0)
    lines.append("capture   : %s%s" % (
        view.path.name, ", %.3f s" % (duration,) if duration is not None else "",
    ))
    identity = " / ".join(
        part for part in (
            str(session.get("app", "")), str(session.get("project", "")),
            str(session.get("target", "")),
        ) if part
    )
    if identity:
        configuration = str(session.get("configuration", ""))
        build = str(session.get("build_version", ""))
        qualifiers = ", ".join(part for part in (configuration, build) if part)
        lines.append("session   : %s%s" % (identity, " (%s)" % (qualifiers,) if qualifiers else ""))
    lines.append("frames    : %s, spanning %s" % (
        report.series.describe(),
        "%.3f s" % (report.series.span_s,) if report.series.span_s is not None else "?",
    ))
    lines.append("budget    : %s" % (budget.label(),))
    lines.append("verdict   : %s" % (parallel.verdict_text(report, frequency),))
    lines.append("occupancy : %s" % (parallel.occupancy_text(report),))
    lines.extend(parallel.finding_lines(report, frequency))
    shown = report.threads if limit == 0 else report.threads[:limit]
    rows: List[Tuple[str, ...]] = []
    for row in shown:
        note = ""
        if row.tid == report.series.tid:
            note = "the frame thread: %s of its work was solo" % (
                parallel.share_text(report.solo, report.own_work()),)
        elif row.coarsened:
            note = "timeline coarsened at the cap (%d span(s) merged: coverage overstated)" % (
                row.coarsened,)
        elif row.elsewhere:
            note = "no coverage in these frames"
        elif row.busy_cycles and row.wait_cycles * 2 >= row.busy_cycles:
            note = "waiting %s of the time it was inside a scope" % (
                parallel.share_text(row.wait_cycles, row.busy_cycles),)
        rows.append((
            str(row.tid),
            row.name or "-",
            row.role or "other",
            str(row.frames),
            parallel.share_text(row.busy_cycles, report.span_cycles),
            parallel.share_text(row.wait_cycles, report.span_cycles),
            parallel.share_text(row.lock_cycles, report.span_cycles),
            note,
        ))
    render_rows(
        ("tid", "thread", "role", "frames", "busy", "wait", "lock", "note"),
        rows,
        (True, False, False, True, True, True, True, False),
        options.fmt(),
        lines,
    )
    return 0


def cmd_verify(capture: str, args: List[str]) -> int:
    """`verify <capture>`: walk everything and say what does not add up.

    Exit 1 when anything error-level was found; warnings do not fail the run.
    """
    options = parse_options(args)
    if options.values.keys() - {"jobs"} or options.flags:
        raise UsageError("verify takes only --jobs")
    view = load_view(capture)
    header = view.header
    with timing.timed("streams"):
        stream_set = streams.assemble(view.data, view.packets)
    with timing.timed("model"):
        model, counts, anomalies = build_model(stream_set, _jobs(options))

    found: List[Anomaly] = list(anomalies)
    found.extend(view.packet_anomalies)
    errors = [item for item in found if item[0] in _ERROR_KINDS]
    warnings = [item for item in found if item[0] not in _ERROR_KINDS]

    forms = _form_counts(view.packets)
    serial_carried = counts.get("serial_carried", 0)
    serial_min = counts.get("serial_min", -1)
    serial_max = counts.get("serial_max", 0)
    missing = 0
    span = 0
    if serial_min >= 0 and serial_carried:
        span = ((serial_max - serial_min) & 0xFFFFFF) + 1
        missing = max(0, span - serial_carried)
    lines = [
        "capture   : %s" % (view.path.name,),
        "container : ok (magic %s, transport %d, protocol %d, metadata %d bytes)" % (
            header.magic.decode("ascii", "replace"),
            header.transport_version,
            header.protocol_version,
            header.metadata_size,
        ),
        "packets   : %d walked, %d anomaly(ies)" % (
            len(view.packets), len(view.packet_anomalies),
        ),
        "streams   : %d packet(s) decoded, %d stream anomaly(ies)" % (
            stream_set.counts.get("raw", 0) + stream_set.counts.get("lz4", 0),
            len(stream_set.anomalies),
        ),
        "schema    : %d type(s) from %d record(s), %d redefinition(s)" % (
            len(model.get("schema", [])), counts.get("new_events", 0), counts.get("redefined", 0),
        ),
        "events    : %d event(s), %d sync event(s), %d scope(s), %d batch(es) / %d record(s)" % (
            counts.get("events", 0), counts.get("sync_events", 0), counts.get("scopes", 0),
            counts.get("batches", 0), counts.get("batch_records", 0),
        ),
        "serials   : %d carried, span %d (min %d, max %d), %d missing" % (
            serial_carried, span, serial_min, serial_max, missing,
        ),
        "frames    : %d pair(s), %d unpaired begin(s), %d unpaired end(s)" % (
            len(model.get("frames", [])),
            counts.get("unpaired_frame_begin", 0),
            counts.get("unpaired_frame_end", 0),
        ),
        "bookmarks : %d joined, %d unknown point(s)" % (
            counts.get("bookmarks", 0), counts.get("unknown_bookmark_points", 0),
        ),
    ]
    if model.get("frames"):
        lines.append(
            "work      : %d scope pair(s) attributed to frames (%d spanning a frame edge, %d with "
            "no spec, %d in no frame window), %d unpaired end(s), %d unpaired begin(s)" % (
                counts.get("scope_pairs", 0), counts.get("scope_pairs_spanning", 0),
                counts.get("scope_pairs_no_spec", 0), counts.get("scope_pairs_unframed", 0),
                counts.get("scope_ends_unpaired", 0), counts.get("scope_begins_unpaired", 0),
            )
        )
    if model.get("gpu_frames"):
        lines.append(
            "gpu       : %d rendered frame(s) decoded from the GpuProfiler channel (%d pass "
            "record(s), %d batch(es) that did not read cleanly)" % (
                counts.get("gpu_frames", 0), counts.get("gpu_events", 0),
                counts.get("gpu_unreadable", 0),
            )
        )
    if serial_carried and missing:
        lines.append(
            "note      : holes in the serial range are expected only before the first sync "
            "packet (%d in this file); after that they mean lost data" % (forms.get("sync", 0),)
        )
    lines.append("findings  : %d error(s), %d warning(s)" % (len(errors), len(warnings)))
    for line in lines:
        sys.stdout.write(line + "\n")
    listed = 0
    for kind, offset, value, message in errors + warnings:
        if listed >= _MAX_LISTED_ANOMALIES:
            remaining = len(errors) + len(warnings) - listed
            sys.stdout.write("  ... and %d more\n" % (remaining,))
            break
        sys.stdout.write("  %-24s offset %d (value %d): %s\n" % (kind, offset, value, message))
        listed += 1
    return 1 if errors else 0


def cmd_parse(capture: str, args: List[str]) -> int:
    """`parse <capture> [--jobs N]`: build the session model (and cache it)."""
    options = parse_options(args)
    if options.values.keys() - {"jobs"} or options.flags:
        raise UsageError("parse takes only --jobs")
    view, model, from_cache = load_model(capture, _jobs(options))
    counts = _counts(model)
    forms = _form_counts(view.packets)
    decoded_bytes = sum(
        packet.decoded_size for packet in view.packets if packet.form in ("raw", "lz4")
    )
    error_count = 0
    warning_count = 0
    for kind, count in model.get("anomaly_counts", {}).items():
        if kind in _ERROR_KINDS:
            error_count += count
        else:
            warning_count += count
    session = model.get("session", {})
    lines = [
        "capture  : %s" % (view.path.name,),
        "packets  : %d (raw %d, lz4 %d, sync %d)" % (
            len(view.packets), forms.get("raw", 0), forms.get("lz4", 0), forms.get("sync", 0),
        ),
        "streams  : %d decoded byte(s) over %d stream(s)" % (
            decoded_bytes, len(set(packet.tid for packet in view.packets)),
        ),
        "model    : %d type(s), %d thread(s), %d timer spec(s), %d frame(s), %d bookmark(s), "
        "%d counter spec(s)" % (
            len(model.get("schema", [])), len(model.get("threads", [])),
            len(model.get("timers", [])), len(model.get("frames", [])),
            len(model.get("bookmarks", [])), len(model.get("counters", [])),
        ),
        "events   : %d event(s), %d batch record(s), %d scope(s)" % (
            counts.get("events", 0), counts.get("batch_records", 0), counts.get("scopes", 0),
        ),
        "anomalies: %d error(s), %d warning(s)" % (error_count, warning_count),
        "cache    : %s" % (
            "disabled ($UEI_NO_CACHE)"
            if cache.disabled()
            else ("reused" if from_cache else "stored"),
        ),
    ]
    app = str(session.get("app", ""))
    project = str(session.get("project", ""))
    target = str(session.get("target", ""))
    if app or project or target:
        lines.insert(
            3,
            "session  : %s" % (" / ".join(part for part in (app, project, target) if part),),
        )
    duration = seconds_for_cycle(model, int(session.get("last_cycle", 0)))
    if duration is not None:
        lines.insert(4, "duration : %.3f s" % (duration,))
    for line in lines:
        sys.stdout.write(line + "\n")
    return 0


def cmd_cache(capture: str, args: List[str]) -> int:
    """`cache <capture> [--clear]`: the parse cache beside a capture."""
    options = parse_options(args)
    path = Path(capture)
    if not path.exists():
        raise UeiaError("cannot read capture %s" % (path.name,))
    if options.is_set("clear"):
        removed = cache.clear(path)
        sys.stdout.write("cache    : %s\n" % ("removed" if removed else "nothing to remove",))
        return 0
    identity = cache.capture_identity(path)
    status = cache.status(path, identity)
    lines = [
        "capture  : %s" % (path.name,),
        "cache    : %s" % (Path(str(status["path"])).name,),
        "exists   : %s" % ("yes" if status["exists"] else "no",),
    ]
    if status["exists"]:
        lines.append("bytes    : %d" % (int(status.get("bytes", 0)),))
        lines.append("matches  : %s" % ("yes" if status.get("matches") else "no",))
        model = status.get("model")
        if isinstance(model, dict):
            lines.append("model    : %d type(s), %d thread(s), %d timer spec(s)" % (
                len(model.get("schema", [])), len(model.get("threads", [])),
                len(model.get("timers", [])),
            ))
    if cache.disabled():
        lines.append("caching  : disabled ($UEI_NO_CACHE)")
    for line in lines:
        sys.stdout.write(line + "\n")
    return 0


# --------------------------------------------------------------------------- the CSV toolbox
# The engine's executables, wrapped (README §2). Each subcommand builds one argv, runs one exe in a
# scratch directory, prints the tool's own stdout as the answer and its identity on stderr, and
# reports *skipped* (exit 2) when there is no engine tree -- never a silent pass.

#: Our subcommand -> the executable in `<engine-dir>\Engine\Binaries\DotNET\CsvTools`.
_CSV_EXES = {
    "info": "csvinfo.exe",
    "split": "CSVSplit.exe",
    "convert": "CsvConvert.exe",
    "filter": "CSVFilter.exe",
    "collate": "CSVCollate.exe",
    "svg": "CSVToSVG.exe",
    "report": "PerfreportTool.exe",
    "regressions": "RegressionsReport.exe",
}

#: The exe's own `Format:` banner, quoted by our usage errors so the contract is never guessed.
_CSV_FORMATS = {
    "info": "csv info <capture.csv> [--json FILE] [--stat-filter LIST] [--quiet] [--show ...]",
    "split": "csv split <capture.csv> --stat NAME [--out FILE] [--delay N] [--virtual-events]",
    "convert": "csv convert --in FILE --out-format csv|bin|csvNoMetadata [--out FILE] "
               "[--compress 0|1|2] [--verify] [--force|--in-place] [--set-metadata k=v;...]",
    "filter": "csv filter <capture.csv> (--stats LIST|--defaults) --out FILE",
    "collate": "csv collate (--csvs LIST|--dir DIR) --out FILE [--pattern P] [--recurse] [--average] "
               "[--outlier-stat S --outlier-threshold N] [--metadata-filter k=v] [--start-event E]",
    "svg": "csv svg (--csvs LIST|--dir DIR) --stats LIST --out FILE [--batch FILE] [--update]",
    "report": "csv report (--csv F|--dir D|--list L) --out DIR [--type T] [--graph-xml F] "
              "[--report-xml F] [--summary-formats L]",
    "regressions": "csv regressions <summary.csv> --out DIR --thresholds FILE [--base F] "
                   "[--dump-contents F] [--test-name N]",
    "from-trace": "csv from-trace <capture.utrace> --out FILE [--jobs N]",
}


def _csv_positional(args: Sequence[str], what: str) -> Tuple[str, List[str]]:
    """Peel the first non-option argument (a CSV, a summary, a capture); the rest is options."""
    for index, arg in enumerate(args):
        if not arg.startswith("--"):
            return arg, list(args[:index]) + list(args[index + 1:])
    raise UsageError("csv needs a %s: %s" % (what, _CSV_FORMATS.get(what, "")))


def _csv_argv(parts: Sequence[Optional[str]]) -> List[str]:
    """Flags in the exe's own spelling: our `--outlier-stat` is its `-outlierStat`."""
    return [str(part) for part in parts if part]


def _csv_kv(option: str, value: str) -> List[str]:
    return [option, value] if value else []


def _csv_emit(lines: Sequence[str], code: int, verdict: bool = False) -> int:
    """Our lines: a *verdict* (nothing to compare) is the answer and goes to stdout; a
    diagnostic about a tool that ran goes to stderr, so stdout stays the tool's own."""
    stream = sys.stdout if verdict else sys.stderr
    for line in lines:
        stream.write(line + "\n")
    return code


def _engine_dir(options: Options) -> Tuple[Optional[engine.EngineDir], List[str]]:
    """The engine tree for this call, plus anything that has to be said out loud about it."""
    notes: List[str] = []
    return engine.resolve(options.text("engine-dir") or None, notes=notes), notes


def _csv_skipped(exe_name: str, root: Optional[engine.EngineDir], notes: Sequence[str]) -> int:
    lines = list(notes)
    if root is None:
        lines.append("csv      : skipped: no engine directory, so the CSV toolbox is not available")
    else:
        lines.append("csv      : skipped: %s has no %s" % (root.root.name or root.root, exe_name))
    lines.append("hint     : " + engine.hint())
    return _csv_emit(lines, 2, verdict=True)


def _absolute(path: str) -> str:
    return path if os.path.isabs(path) else str(Path(path).resolve())


def _csv_run(exe_name: str, argv: List[str], options: Options,
             expect: Sequence[str] = ()) -> int:
    """Run one CsvTools executable and report the call: its stdout, our stamp, and the exit code.

    The exe runs in a scratch directory, so its own temporary files never land in this repo -- which
    is why every path in `argv` is made absolute first. `toolrun.run_tool` is what the tests patch:
    the exes are the engine's, not ours, so their half of the suite is the *not compared* half.
    """
    root, notes = _engine_dir(options)
    if root is None:
        return _csv_skipped(exe_name, root, notes)
    exe = root.csvtools(exe_name)
    if exe is None:
        return _csv_skipped(exe_name, root, notes)
    with tempfile.TemporaryDirectory(prefix="ueia-csv-") as scratch:
        result = toolrun.run_tool(exe, _csv_absolute(argv), cwd=Path(scratch))
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    for line in result.describe():
        sys.stderr.write(line + "\n")
    for line in notes:
        sys.stderr.write(line + "\n")
    lines: List[str] = []
    for path in expect:
        if not path:
            continue
        candidate = Path(path)
        if candidate.is_file():
            lines.append("wrote    : %s (%d bytes)" % (candidate, candidate.stat().st_size))
    if result.exit != 0:
        lines.append("error    : %s exited %d" % (exe_name, result.exit))
        return _csv_emit(lines, 1)
    return _csv_emit(lines, 0)


def _csv_absolute(argv: Sequence[str]) -> List[str]:
    """Paths become absolute: the tool runs in a scratch directory, the user's paths are theirs."""
    out: List[str] = []
    for index, item in enumerate(argv):
        previous = argv[index - 1] if index else ""
        if item.startswith("-"):
            out.append(item)
        elif previous in _CSV_PATH_FLAGS and not os.path.isabs(item):
            out.append(str(Path(item).resolve()))
        elif previous in _CSV_LIST_FLAGS:
            out.append(";".join(
                str(Path(part).resolve()) if part and not os.path.isabs(part) else part
                for part in item.split(";")
            ))
        else:
            out.append(item)
    return out


#: The exe flags whose value is a single path, and whose value is a `;`-separated path list.
_CSV_PATH_FLAGS = frozenset(("-csv", "-csvDir", "-o", "-csvFile", "-thresholds", "-base",
                             "-dumpContents", "-graphXML", "-reportXML", "-batchCommands", "-in"))
_CSV_LIST_FLAGS = frozenset(("-csvs", "-csvList"))


def _csv_info(args: List[str]) -> int:
    """`csv info <capture.csv> [--json FILE] [--quiet] [--show ...]`: a CSV's shape and numbers."""
    csv_path, rest = _csv_positional(args, "info")
    options = parse_options(rest)
    argv = [_absolute(csv_path)]
    if options.is_set("quiet"):
        argv.append("-quiet")
    for flag, exe_flag in (("show-averages", "-showAverages"), ("show-min", "-showMin"),
                           ("show-max", "-showMax"), ("show-totals", "-showTotals"),
                           ("show-all-stats", "-showAllStats"), ("show-events", "-showEvents")):
        if options.is_set(flag):
            argv.append(exe_flag)
    argv += _csv_kv("-statFilters", options.text("stat-filter"))
    json_path = options.text("json")
    if json_path:
        argv += ["-toJson", str(Path(json_path).resolve())]
        expect = [json_path]
    else:
        expect = []
    return _csv_run(_CSV_EXES["info"], argv, options, expect)


def _csv_split(args: List[str]) -> int:
    """`csv split <capture.csv> --stat NAME [--out FILE]`: one CSV per value of a stat."""
    csv_path, rest = _csv_positional(args, "split")
    options = parse_options(rest)
    if not options.text("stat"):
        raise UsageError(_CSV_FORMATS["split"])
    argv = ["-csv", _absolute(csv_path), "-splitStat", options.text("stat")]
    argv += _csv_kv("-o", options.text("out"))
    argv += _csv_kv("-delay", options.text("delay") or "")
    if options.is_set("virtual-events"):
        argv.append("-virtualEvents")
    return _csv_run(_CSV_EXES["split"], argv, options, [options.text("out")])


def _csv_convert(args: List[str]) -> int:
    """`csv convert --in FILE --out-format FMT ...`: text ⇄ `.csv.bin`, metadata, integrity."""
    options = parse_options(args)
    if not options.text("in") or not options.text("out-format"):
        raise UsageError(_CSV_FORMATS["convert"])
    argv = ["-in", options.text("in"), "-outFormat", options.text("out-format")]
    argv += _csv_kv("-o", options.text("out"))
    argv += _csv_kv("-binCompress", options.text("compress"))
    argv += _csv_kv("-setMetadata", options.text("set-metadata"))
    if options.is_set("verify"):
        argv.append("-verify")
    if options.is_set("force"):
        argv.append("-force")
    if options.is_set("in-place"):
        argv.append("-inPlace")
    return _csv_run(_CSV_EXES["convert"], argv, options, [options.text("out")])


def _csv_filter(args: List[str]) -> int:
    """`csv filter <capture.csv> --stats LIST --out FILE`: only the wanted columns."""
    csv_path, rest = _csv_positional(args, "filter")
    options = parse_options(rest)
    out = options.text("out")
    if not out or not (options.text("stats") or options.is_set("defaults")):
        raise UsageError(_CSV_FORMATS["filter"])
    argv = ["-csv", _absolute(csv_path), "-o", out]
    argv += _csv_kv("-stats", options.text("stats"))
    if options.is_set("defaults"):
        argv.append("-defaults")
    return _csv_run(_CSV_EXES["filter"], argv, options, [out])


def _csv_collate(args: List[str]) -> int:
    """`csv collate (--csvs LIST|--dir DIR) --out FILE ...`: many CSVs into one table."""
    options = parse_options(args)
    out = options.text("out")
    if not out or not (options.text("csvs") or options.text("dir")):
        raise UsageError(_CSV_FORMATS["collate"])
    argv: List[str] = []
    argv += _csv_kv("-csvs", options.text("csvs"))
    argv += _csv_kv("-csvDir", options.text("dir"))
    argv += _csv_kv("-searchPattern", options.text("pattern"))
    argv += _csv_kv("-filterOutlierStat", options.text("outlier-stat"))
    argv += _csv_kv("-filterOutlierThreshold", options.text("outlier-threshold"))
    argv += _csv_kv("-metadataFilter", options.text("metadata-filter"))
    argv += _csv_kv("-startEvent", options.text("start-event"))
    if options.is_set("recurse"):
        argv.append("-recurse")
    if options.is_set("average"):
        argv.append("-avg")
    argv += ["-o", out]
    return _csv_run(_CSV_EXES["collate"], argv, options, [out])


def _csv_svg(args: List[str]) -> int:
    """`csv svg (--csvs LIST|--dir DIR) --stats LIST --out FILE`: the SVG graph renderer."""
    options = parse_options(args)
    argv: List[str] = []
    argv += _csv_kv("-csvs", options.text("csvs"))
    argv += _csv_kv("-csvDir", options.text("dir"))
    argv += _csv_kv("-stats", options.text("stats"))
    argv += _csv_kv("-batchCommands", options.text("batch"))
    if options.is_set("update"):
        argv.append("-updatesvg")
    out = options.text("out")
    if not out or not (options.text("csvs") or options.text("dir") or options.text("batch")):
        raise UsageError(_CSV_FORMATS["svg"])
    argv += ["-o", out]
    return _csv_run(_CSV_EXES["svg"], argv, options, [out])


def _csv_report_cmd(args: List[str]) -> int:
    """`csv report (--csv F|--dir D|--list L) --out DIR ...`: the full report bundle."""
    options = parse_options(args)
    out = options.text("out")
    if not out or not (options.text("csv") or options.text("dir") or options.text("list")):
        raise UsageError(_CSV_FORMATS["report"])
    argv: List[str] = []
    argv += _csv_kv("-csv", options.text("csv"))
    argv += _csv_kv("-csvDir", options.text("dir"))
    argv += _csv_kv("-csvList", options.text("list"))
    argv += _csv_kv("-reportType", options.text("type"))
    argv += _csv_kv("-graphXML", options.text("graph-xml"))
    argv += _csv_kv("-reportXML", options.text("report-xml"))
    argv += _csv_kv("-summaryTableOutputFormats", options.text("summary-formats"))
    argv += ["-o", out]
    return _csv_run(_CSV_EXES["report"], argv, options)


def _csv_regressions(args: List[str]) -> int:
    """`csv regressions <summary.csv> --out DIR --thresholds FILE`: a threshold regression report."""
    summary, rest = _csv_positional(args, "regressions")
    options = parse_options(rest)
    out = options.text("out")
    thresholds = options.text("thresholds")
    if not out or not thresholds:
        raise UsageError(_CSV_FORMATS["regressions"])
    argv = ["-csvFile", _absolute(summary), "-o", out, "-thresholds", thresholds]
    argv += _csv_kv("-base", options.text("base"))
    argv += _csv_kv("-dumpContents", options.text("dump-contents"))
    argv += _csv_kv("-testName", options.text("test-name"))
    return _csv_run(_CSV_EXES["regressions"], argv, options, [options.text("dump-contents")])


def _csv_from_trace(args: List[str]) -> int:
    """`csv from-trace <capture.utrace> --out FILE`: the bridge no engine exe offers.

    Reads a capture's CSV Profiler definitions and per-frame values (which ride the `counters`
    channel, REFERENCE §5) and writes the `.csv` the rest of this family reads, in the format
    `CsvStats.ReadCSVFromLines` accepts. The cache does not hold the values, so this walks the
    streams every time -- and when the capture has definitions but no values (no CSV capture was
    running), that is *skipped* (exit 2) with the re-record line, not an empty file.
    """
    capture, rest = _csv_positional(args, "from-trace")
    options = parse_options(rest)
    out = options.text("out")
    if not out:
        raise UsageError(_CSV_FORMATS["from-trace"])
    view = load_view(capture)
    with timing.timed("streams"):
        stream_set = streams.assemble(view.data, view.packets)
    with timing.timed("model"):
        model, _counts, _anomalies = build_model(stream_set, _jobs(options))
    with timing.timed("csv"):
        registry = schema.build_registry(
            stream_set.streams.get(TID_EVENTS, b""), [], zero_counts(),
        )
        labels = {int(row["tid"]): str(row.get("name") or "") for row in model["threads"]}
        values = csvprof.collect(
            stream_set, registry, model["csv_stats"], model["csv_categories"], model["frames"],
            dict(model["session"]), labels,
        )
    if not values["value_events"]:
        return _csv_emit([
            "capture  : %s" % (view.path.name,),
            "stats    : %d definition(s), 0 value(s)" % (values["stat_count"],),
            "csv      : skipped: this capture ran no CSV capture, so there are no per-frame values",
            "hint     : record with the CSV profiler capturing (`CsvProfiler.Start`, or "
            "-csvcapture) and trace the `counters` channel",
        ], 2, verdict=True)
    written = csvprof.write_csv(values, Path(out), csvprof.default_metadata(values, view.path.name))
    return _csv_emit([
        "capture  : %s" % (view.path.name,),
        "stats    : %d definition(s), %d series" % (values["stat_count"], len(values["series"])),
        "values   : %d event(s) over %d frame(s) from %s" % (
            values["value_events"], len(values["frames"]), values["frames_from"],
        ),
        "dropped  : %d value(s) outside every frame" % (values["dropped"],),
        "wrote    : %s (%d bytes)" % (out, written),
    ], 0, verdict=True)


_CSV_SUBCOMMANDS: Dict[str, Callable[[List[str]], int]] = {
    "info": _csv_info,
    "split": _csv_split,
    "convert": _csv_convert,
    "filter": _csv_filter,
    "collate": _csv_collate,
    "svg": _csv_svg,
    "report": _csv_report_cmd,
    "regressions": _csv_regressions,
    "from-trace": _csv_from_trace,
}


def cmd_csv(args: List[str]) -> int:
    """`csv <subcommand> ...`: the engine's CSV toolbox, wrapped (README §2).

    Subcommands, 1:1 with the executables: `info`, `split`, `convert`, `filter`, `collate`, `svg`,
    `report`, `regressions` -- each a thin invocation whose stdout is the answer, stamped on stderr
    with the exe's identity and exact argv -- plus `from-trace`, the one thing no engine exe does:
    synthesize a CSV Profiler `.csv` from a capture's own CSV Profiler events.

    Exit codes: 0 the tool ran, 1 it failed (its own output is printed), 2 *skipped*: no engine
    directory, or a tree without that executable. Nothing is ever re-derived in Python here.
    """
    if not args:
        raise UsageError("csv needs a subcommand: %s" % (", ".join(sorted(_CSV_SUBCOMMANDS)),))
    name = args[0]
    handler = _CSV_SUBCOMMANDS.get(name)
    if handler is None:
        raise UsageError("unknown csv subcommand %r (try: %s)" % (
            name, ", ".join(sorted(_CSV_SUBCOMMANDS)),
        ))
    return handler(args[1:])


def cmd_lz4(args: List[str]) -> int:
    """`lz4 [--build] [--force]`: the decoder library, and building it.

    The pipeline's first step (AGENTS.md): `--build` compiles `bin/ueia_lz4.dll` when it is missing
    or its recipe changed and does nothing at all when it is current; `--force` rebuilds either way.
    Exit codes: 0 usable and current, 1 a problem (stale, unloadable, a build that failed), 2 no
    library at all -- the corpus half's convention, where "nothing there" is never a pass.
    """
    options = parse_options(args)
    lines: List[str] = []
    if options.is_set("build") or options.is_set("force"):
        code, built = lz4.build(force=options.is_set("force"))
        lines.extend(built)
        if code != 0:
            for line in lines:
                sys.stdout.write(line + "\n")
            return 1
    state = lz4.build_state()
    works, detail = lz4.self_test()
    stamp = state["stamp"]
    lines.append("library  : %s" % (state["library"] or "none found",))
    if state["library"] is not None:
        lines.append("version  : %s" % (state["version"] or "unknown",))
        lines.append("origin   : %s" % (
            "this repository's build" if state["ours"]
            else "not this repository's build, so nothing here can say whether it is current",
        ))
    if stamp is not None:
        sources = stamp.get("sources")
        digest = ", ".join(
            "%s %s" % (Path(name).name, str(sources.get(name, ""))[:8])
            for name in lz4.RECIPE_SOURCES
        ) if isinstance(sources, dict) else "?"
        lines.append("recipe   : %s" % (stamp.get("flags", "?"),))
        lines.append("sources  : %s" % (digest,))
        lines.append("stamped  : %s by %s (cmake %s), dll %s, %s bytes" % (
            stamp.get("built_utc", "?"), stamp.get("compiler", "?"), stamp.get("cmake", "?"),
            str(stamp.get("dll_sha256", ""))[:12], stamp.get("dll_size", "?"),
        ))
    for problem in state["problems"]:
        lines.append("problem  : %s" % (problem,))
    if state["library"] is None:
        lines.append("state    : no library to decode with")
        lines.append("hint     : run `%s`" % (lz4.ENSURE_COMMAND,))
    elif state["ours"] and not state["current"]:
        lines.append("state    : stale -- run `%s`" % (lz4.ENSURE_COMMAND,))
    elif state["current"]:
        lines.append("state    : current")
    else:
        lines.append("state    : usable (a library this repository did not build)")
    lines.append("self-test: %s" % (detail,))
    for line in lines:
        sys.stdout.write(line + "\n")
    if state["library"] is None:
        return 2
    if not works:
        return 1
    return 1 if (state["ours"] and not state["current"]) else 0


def _flatten(suite: unittest.TestSuite) -> List[unittest.TestCase]:
    tests: List[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(_flatten(item))
        else:
            tests.append(item)
    return tests


def cmd_selftest(args: List[str]) -> int:
    """`selftest [-v] [-k PATTERN]`: the hermetic unit-test suite.

    Exit codes: 0 every test passed, 1 a failure, 2 a bad option or an empty
    selection. Hermetic means no capture, no engine directory and no network:
    the fixtures are built in memory by the tests themselves.
    """
    verbose = False
    pattern: Optional[str] = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-v", "--verbose"):
            verbose = True
        elif arg == "-k":
            index += 1
            if index >= len(args):
                raise UsageError("-k needs a pattern")
            pattern = args[index]
        else:
            raise UsageError("unknown selftest option %r" % (arg,))
        index += 1

    src_dir = REPO_ROOT / "src" / "py"
    tests_dir = REPO_ROOT / "tests"
    if not tests_dir.is_dir():
        raise UeiaError("the tests folder is missing beside this tool")
    sys.path.insert(0, str(tests_dir))
    sys.path.insert(0, str(src_dir))
    loader = unittest.TestLoader()
    # `test_*.py` rather than unittest's default `test*.py`: `testcase.py` is the suite's shared
    # floor, not a test file, and discovery must not collect it as one.
    suite = loader.discover(str(tests_dir), pattern="test_*.py", top_level_dir=str(tests_dir))
    tests = _flatten(suite)
    if pattern is not None:
        needle = pattern.lower()
        tests = [test for test in tests if needle in test.id().lower()]
    if not tests:
        sys.stdout.write("no test matched %r\n" % (pattern,))
        return 2
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2 if verbose else 1)
    result = runner.run(unittest.TestSuite(tests))
    return 0 if result.wasSuccessful() else 1


__all__ = [
    "FORMATS",
    "Options",
    "parse_options",
    "render_rows",
    "load_view",
    "load_model",
    "CaptureView",
    "cmd_info",
    "cmd_packets",
    "cmd_schema",
    "cmd_threads",
    "cmd_timers",
    "cmd_frames",
    "cmd_advice",
    "cmd_bottleneck",
    "cmd_compare",
    "cmd_parallelism",
    "cmd_sources",
    "cmd_summary",
    "cmd_tasks",
    "cmd_verify",
    "cmd_parse",
    "cmd_cache",
    "cmd_csv",
    "cmd_lz4",
    "cmd_selftest",
    "cmd_goldens",
]
