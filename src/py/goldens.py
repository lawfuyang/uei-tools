"""The corpus harness: pinned transcripts and hand-written labels.

A capture is a **key + SHA-256**. The key's SHA-256 and notes live in the
committed `goldens/captures.json`; where *this* machine keeps the file is
`goldens/captures.local.json`, which is gitignored because a path is local
state. Transcripts are written under `goldens/local/<key>/` (also gitignored:
a transcript is the capture's own words, and those are published only with the
author's say-so), while the hand-written labels in `goldens/labels/<key>.json`
pin numbers only.

Exit codes: **0** every present capture compared and matched, **1** a mismatch
or a bad corpus file, **2** nothing to compare on this machine -- which means
"not compared", never "pass".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import container
import schema
import streams
from shapes import ENV_NO_ENGINE_SCAN, UeiaError

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDENS_DIR = REPO_ROOT / "goldens"
CAPTURES_FILE = GOLDENS_DIR / "captures.json"
CAPTURES_LOCAL_FILE = GOLDENS_DIR / "captures.local.json"
LABELS_DIR = GOLDENS_DIR / "labels"
TRANSCRIPTS_DIR = GOLDENS_DIR / "local"

#: (name, argv after the capture path) -- each is pinned byte-for-byte. `summary` is pinned in its
#: table form (prose, histogram and rows) because that is the whole report: the csv form would pin
#: the table and drop the verdict.
PINNED_COMMANDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("info", ()),
    ("verify", ()),
    ("summary", ("--budget", "60")),
    ("bottleneck", ("--budget", "60")),
    ("gpu", ()),
    ("parallelism", ("--budget", "60")),
    ("sources", ("--budget", "60", "--limit", "12")),
    ("advice", ("--budget", "60")),
    ("tasks", ()),
    ("threads", ("--format", "csv")),
    ("timers", ("--format", "csv", "--limit", "0")),
    ("frames", ("--format", "csv", "--limit", "0")),
    ("schema", ("--format", "csv", "--limit", "0")),
)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(str(path), "r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def capture_paths() -> Dict[str, Path]:
    """The captures this machine has, key -> path (empty when none are known)."""
    local = _load_json(CAPTURES_LOCAL_FILE)
    paths: Dict[str, Path] = {}
    if local:
        for key, value in local.items():
            if isinstance(value, str):
                paths[key] = Path(value)
    return paths


def label_facts(capture: Path) -> Dict[str, Any]:
    """The numbers a label may pin, computed through the module API."""
    data = capture.read_bytes()
    header = container.parse_header(data)
    packets, packet_anomalies = container.walk_packets(data, header)
    stream_set = streams.assemble(data, packets)
    counts: Dict[str, int] = {
        "new_events": 0, "redefined": 0, "events": 0, "sync_events": 0,
        "scopes": 0, "aux_blocks": 0, "unknown_uid": 0,
    }
    anomalies: List[Tuple[str, int, int, str]] = []
    registry = schema.build_registry(
        stream_set.streams.get(0, b""), anomalies, counts
    )
    forms: Dict[str, int] = {"raw": 0, "lz4": 0, "sync": 0, "verify": 0}
    tid_packets: Dict[int, int] = {}
    for packet in packets:
        forms[packet.form] = forms.get(packet.form, 0) + 1
        tid_packets[packet.tid] = tid_packets.get(packet.tid, 0) + 1
    top_tids = sorted(tid_packets.items(), key=lambda item: (-item[1], item[0]))[:5]
    payload_bytes = 0
    for packet in packets:
        if packet.form in ("raw", "lz4"):
            payload_bytes += packet.decoded_size
    facts: Dict[str, Any] = {
        "size": len(data),
        "magic": header.magic.decode("ascii", "replace"),
        "transport": header.transport_version,
        "protocol": header.protocol_version,
        "packets": len(packets),
        "packets_raw": forms.get("raw", 0),
        "packets_lz4": forms.get("lz4", 0),
        "packets_sync": forms.get("sync", 0),
        "payload_bytes": payload_bytes,
        "tids": len(tid_packets),
        "top_tid_packets": {str(tid): count for tid, count in top_tids},
        "events_stream_packets": tid_packets.get(0, 0),
        "importants_stream_packets": tid_packets.get(1, 0),
        "schema_types": registry.count(),
        "schema_records": counts["new_events"],
        "schema_redefinitions": counts["redefined"],
        "packet_anomalies": len(packet_anomalies),
        "stream_anomalies": len(stream_set.anomalies) + len(anomalies),
        "threads_with_packets": len([tid for tid in tid_packets if 2 <= tid < 0x3FF0]),
    }
    return facts


def redact(text: str, capture: Path) -> str:
    """Remove every trace of where this machine keeps the capture."""
    resolved = capture.resolve()
    replacements = [str(resolved), str(capture), resolved.name, capture.name]
    for needle in replacements:
        if needle:
            text = text.replace(needle, "<capture>")
    return text


def _run_command(name: str, argv: Tuple[str, ...], capture: Path) -> Tuple[int, str]:
    # `<command> <capture> [args...]`, the CLI's own order: without `name` the first option became
    # the command, every transcript was the help text and exit 2, and the harness compared nothing
    # (found 2026-09-28 by a transcript diff -- six "matches" that were all the same help output).
    command = [
        sys.executable,
        str(REPO_ROOT / "src" / "py" / "ueia.py"),
        name,
        str(capture),
    ] + list(argv)
    # The engine search is switched off for the pinned commands, and that is not incidental: with it
    # on, `sources` and `advice` map file:line through whatever engine tree the *machine* happens to
    # have (found 2026-09-29, 6 transcripts differing the moment auto-discovery landed), which is
    # precisely what a transcript must not depend on -- it would embed a machine's paths and only
    # compare on machines with the same install. The transcripts therefore pin the no-engine-tree
    # behaviour, which is the deterministic half, and discovery is covered by its own tests.
    environment = dict(os.environ)
    environment[ENV_NO_ENGINE_SCAN] = "1"
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        cwd=str(REPO_ROOT),
        env=environment,
    )
    return completed.returncode, completed.stdout


def _transcript(capture: Path, name: str, argv: Tuple[str, ...]) -> str:
    exit_code, stdout = _run_command(name, argv, capture)
    body = redact(stdout, capture)
    return "# exit=%d\n" % (exit_code,) + body


def _check_labels(key: str) -> List[str]:
    """Compare one capture's hand-written label against a fresh walk."""
    problems: List[str] = []
    labels = _load_json(LABELS_DIR / (key + ".json"))
    if labels is None:
        return ["%s: no labels file" % key]
    paths = capture_paths()
    capture = paths.get(key)
    if capture is None or not capture.exists():
        return []
    captured = labels.get("capture", {})
    expected_sha = str(captured.get("sha256", ""))
    from cache import capture_identity

    identity = capture_identity(capture)
    if expected_sha and identity["sha256"] != expected_sha:
        return [
            "%s: the file here is not the labelled capture (sha256 %s, labels say %s)"
            % (key, identity["sha256"][:16], expected_sha[:16])
        ]
    facts = label_facts(capture)
    for name, expected in labels.get("facts", {}).items():
        actual = facts.get(name)
        if actual != expected:
            problems.append(
                "%s: label %s says %r, the file says %r" % (key, name, expected, actual)
            )
    return problems


def cmd_goldens(args: List[str]) -> int:
    """Run the corpus harness: `goldens --check` or `goldens --write`."""
    write = False
    verbose = False
    only: Optional[str] = None
    commands: Optional[List[str]] = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--write":
            write = True
        elif arg == "--check":
            write = False
        elif arg == "-v" or arg == "--verbose":
            verbose = True
        elif arg == "--capture":
            index += 1
            if index >= len(args):
                raise UeiaError("--capture needs a key")
            only = args[index]
        elif arg == "--only":
            # a command filter, for iterating on one transcript and for the suite's own harness
            # tests: re-running all thirteen commands as subprocesses is ~4 s per capture, and a
            # test about *the harness* does not need to pay it three times over.
            index += 1
            if index >= len(args):
                raise UeiaError("--only needs a command name, or a comma-separated list")
            wanted = [name.strip() for name in args[index].split(",") if name.strip()]
            known = [name for name, _argv in PINNED_COMMANDS]
            unknown = [name for name in wanted if name not in known]
            if unknown:
                raise UeiaError("no such pinned command: %s (have %s)"
                                % (", ".join(unknown), ", ".join(known)))
            commands = wanted
        else:
            raise UeiaError("unknown goldens option %r" % (arg,))
        index += 1

    captures = _load_json(CAPTURES_FILE)
    if captures is None:
        raise UeiaError("goldens/captures.json is missing or unreadable")
    local_paths = capture_paths()
    keys = sorted(captures)
    if only is not None:
        if only not in captures:
            raise UeiaError("no such capture key: %s" % (only,))
        keys = [only]

    problems: List[str] = []
    compared = 0
    not_present: List[str] = []
    for key in keys:
        capture = local_paths.get(key)
        if capture is None or not capture.exists():
            not_present.append(key)
            continue
        compared += 1
        if verbose:
            sys.stdout.write("comparing %s\n" % (key,))
        transcript_dir = TRANSCRIPTS_DIR / key
        chosen = [(name, argv) for name, argv in PINNED_COMMANDS
                  if commands is None or name in commands]
        for name, argv in chosen:
            text = _transcript(capture, name, argv)
            path = transcript_dir / (name + ".txt")
            if write:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                continue
            try:
                previous = path.read_text(encoding="utf-8")
            except OSError:
                if verbose:
                    sys.stdout.write("  %-8s no transcript here (run --write)\n" % (name,))
                continue
            if previous != text:
                problems.append("%s: %s transcript differs" % (key, name))
            elif verbose:
                sys.stdout.write("  %-8s matched\n" % (name,))
        problems.extend(_check_labels(key))

    for key in not_present:
        sys.stdout.write("%s: not present on this machine (not compared)\n" % (key,))
    for problem in problems:
        sys.stdout.write("%s\n" % (problem,))
    if compared == 0:
        sys.stdout.write("nothing to compare (%d capture(s) not present)\n" % (len(not_present),))
        return 2
    if problems:
        sys.stdout.write("%d problem(s) over %d capture(s)\n" % (len(problems), compared))
        return 1
    sys.stdout.write("%d capture(s) compared and matched%s\n" % (
        compared, "" if commands is None else " (%d of %d commands: %s)"
        % (len(commands), len(PINNED_COMMANDS), ", ".join(commands))))
    return 0


def _selftest_paths() -> Tuple[Path, Path]:
    tests = REPO_ROOT / "tests"
    return REPO_ROOT / "src" / "py", tests
