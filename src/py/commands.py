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
import sys
import unittest
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple, cast

import cache
import container
import lz4
import timing
import streams
from container import Anomaly
from goldens import REPO_ROOT, cmd_goldens
from model import SessionModel, build_model, seconds_for_cycle
from shapes import (
    PROTOCOL_CURRENT,
    TRANSPORT_TID_PACKET_SYNC,
    ContainerHeader,
    PacketRow,
    UeiaError,
    UsageError,
)

FORMATS = ("table", "csv", "markdown")
_VALUE_OPTIONS = frozenset(("format", "limit", "tid", "filter", "jobs"))
_BOOL_OPTIONS = frozenset(("clear", "build", "force"))

_DEFAULT_PACKET_LIMIT = 40
_DEFAULT_FRAME_LIMIT = 40
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


class CaptureView(NamedTuple):
    """A capture read up to its packet layer (no LZ4, no model)."""

    path: Path
    data: bytes
    header: ContainerHeader
    packets: List[PacketRow]
    packet_anomalies: List[Anomaly]


def load_view(capture: str) -> CaptureView:
    """Read the file, its container header and its packet walk."""
    path = Path(capture)
    with timing.timed("read"):
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise UeiaError("cannot read capture %s: %s" % (path.name, exc))
    with timing.timed("container"):
        header = container.parse_header(data)
    with timing.timed("packets"):
        packets, anomalies = container.walk_packets(data, header)
    return CaptureView(
        path=path, data=data, header=header, packets=packets, packet_anomalies=anomalies
    )


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
    with timing.timed("streams"):
        stream_set = streams.assemble(view.data, view.packets)
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
    "cmd_verify",
    "cmd_parse",
    "cmd_cache",
    "cmd_lz4",
    "cmd_selftest",
    "cmd_goldens",
]
