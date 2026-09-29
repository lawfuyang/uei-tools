"""ueia -- offline Unreal Insights `.utrace` analyser.

Usage:
  python src\\py\\ueia.py <command> <capture.utrace> [args...]
  python src\\py\\ueia.py csv <subcommand> [args...] [--engine-dir DIR]
  python src\\py\\ueia.py lz4 [--build] [--force]
  python src\\py\\ueia.py selftest [-v] [-k PATTERN]
  python src\\py\\ueia.py goldens [--check|--write] [--capture KEY] [-v]

Commands:
  info <capture>              the file's own account of itself, and its packet layer
  packets <capture>           the packet table (--limit N, 0 = all; --tid N)
  schema <capture>            the capture's vocabulary, as the file declares it
                              (--filter TEXT, --limit N)
  threads <capture>           every thread: packets, bytes, events, batches, cycles
  timers <capture>            the CPU profiler's timer specs, with file:line
                              (--filter TEXT, --limit N)
  frames <capture>            BeginFrame/EndFrame pairs, in cycles (--limit N)
  tasks <capture>             the task graph and the longest dependency chain through it
                              (--limit N caps the steps listed, --graph dot|mermaid|json
                              writes the graph itself; exit 2 when the capture carries no
                              TaskTrace events -- re-record with `...,task`)
  bottleneck <capture>        what bounds a frame: the game thread, the render thread,
                              the GPU, or none of them -- every verdict measured against
                              the budget, with the evidence and the reasons it cannot
                              decide (--budget, --budget-ms, --tid, --limit; exit 2 when
                              the capture has no frames, no cycle frequency or no scopes)
  summary <capture>           the frame-time distribution against a budget, and the
                              frames that break it with the timers that ran in them
                              (--budget FPS | --budget-ms MS, default 60 FPS; --tid N
                              picks the frame series, --limit N the rows; exit 2 when
                              the capture has no frames or no cycle frequency)
  parallelism <capture>       was the work spread? Per thread: busy / waiting / lock
                              cycles inside the frame series' own frames, the frame
                              thread's solo work, the most threads ever working at once
                              and the lock overlap -- every ceiling Amdahl on a measure,
                              and every absence said out loud (--budget, --budget-ms,
                              --tid, --limit; exit 2 when the capture has no frames, no
                              cycle frequency or no scopes)
  verify <capture>            walk everything and report what does not add up
                              (exit 1 when anything error-level was found)
  parse <capture>             build (and cache) the session model; prints what it holds
  cache <capture> [--clear]   the parse cache beside a capture: status, or remove it

  csv <subcommand> ...        the engine's CSV toolbox, wrapped and never reimplemented
                              (--engine-dir DIR or $UEI_ENGINE_DIR; exit 2 when there
                              is no engine tree, never a silent pass):
    info <capture.csv>          a CSV's shape and numbers (--json FILE for the machine
                                form, --show averages|min|max|totals|all-stats|events)
    split <capture.csv>         one CSV per distinct value of a stat (--stat NAME)
    convert                     text <-> .csv.bin, metadata edits, integrity (--in,
                                --out-format, --set-metadata, --verify)
    filter <capture.csv>        only the wanted columns (--stats LIST | --defaults)
    collate                     many CSVs into one table (--csvs LIST | --dir DIR)
    svg                         the SVG graph renderer (--stats LIST, --out FILE)
    report                      the full HTML/CSV/JSON report bundle (--csv F | --dir D)
    regressions                 threshold report between a summary CSV and thresholds
    from-trace <capture.utrace> synthesize the CSV Profiler .csv from a capture's own
                                CSV events -- the bridge no engine exe offers (--out FILE)

  lz4 [--build] [--force]     the LZ4 decoder library the encoded packets need: is it
                              there and is it current, and build it when it is not
                              (exit 0 usable and current, 1 stale or broken, 2 nothing
                              to decode with). `--build` is the pipeline's first step
                              and does nothing when there is nothing to do

  selftest [-v] [-k PATTERN]  the hermetic unit-test suite (exit 0 pass, 1 fail,
                              2 bad option): no capture, no engine, no network
  goldens [--check|--write] [--capture KEY] [-v]
                              the corpus: re-run the pinned commands over the
                              captures this machine has and compare (exit 2 when
                              there is nothing to compare)

Row commands take --format table|csv|markdown (table is the default; in the
non-table forms stdout is the table alone and the prose moves to stderr) and
every cap applies in all three. --jobs N says how many worker processes the
per-thread walk may use (any command that has to build the model; 0, the
default, chooses for the machine, 1 stays in this process) and never changes
the answer. Nothing is timed on stdout: $UEI_PROFILE=1 prints a per-phase
table on stderr, $UEI_PROGRESS=1 live progress lines, and $UEI_NO_CACHE=1
leaves the parse cache untouched. Reads files, runs offline: no device, no
network, no engine build required.
"""

from __future__ import annotations

import sys
from typing import List, Optional

from shapes import UeiaError, UsageError
from timing import report
from goldens import cmd_goldens

# Re-exported for the CLI and tests, the way the sibling rdc-tools entry point does it: a script
# writes `ueia.container.parse_header(...)` without knowing which layer holds what.
from cache import *  # noqa: F401,F403
from container import *  # noqa: F401,F403
from decode import *  # noqa: F401,F403
from events import *  # noqa: F401,F403
from lz4 import *  # noqa: F401,F403
from model import *  # noqa: F401,F403
from schema import *  # noqa: F401,F403
from shapes import *  # noqa: F401,F403
from streams import *  # noqa: F401,F403
from timing import *  # noqa: F401,F403
from commands import (
    cmd_bottleneck,
    cmd_cache,
    cmd_csv,
    cmd_frames,
    cmd_info,
    cmd_lz4,
    cmd_packets,
    cmd_parallelism,
    cmd_parse,
    cmd_schema,
    cmd_selftest,
    cmd_summary,
    cmd_tasks,
    cmd_threads,
    cmd_timers,
    cmd_verify,
    load_model as load_model,  # re-exported: scripts and tests call `ueia.load_model`
    load_view as load_view,
    render_rows as render_rows,
)

_CAPTURE_COMMANDS = {
    "info": cmd_info,
    "packets": cmd_packets,
    "schema": cmd_schema,
    "threads": cmd_threads,
    "timers": cmd_timers,
    "frames": cmd_frames,
    "bottleneck": cmd_bottleneck,
    "summary": cmd_summary,
    "parallelism": cmd_parallelism,
    "tasks": cmd_tasks,
    "verify": cmd_verify,
    "parse": cmd_parse,
    "cache": cmd_cache,
}


HELP = __doc__ or "ueia -- offline Unreal Insights trace analyser"


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch one command line; returns the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stdout.write(HELP + "\n")
        return 0
    command = args[0]
    try:
        if command in ("-h", "--help", "help"):
            sys.stdout.write(HELP + "\n")
            return 0
        if command == "lz4":
            return cmd_lz4(args[1:])
        if command == "csv":
            return cmd_csv(args[1:])
        if command == "selftest":
            return cmd_selftest(args[1:])
        if command == "goldens":
            return cmd_goldens(args[1:])
        handler = _CAPTURE_COMMANDS.get(command)
        if handler is None:
            sys.stderr.write("ueia: unknown command %r\n" % (command,))
            sys.stdout.write(HELP + "\n")
            return 2
        if len(args) < 2:
            raise UsageError("command %r needs a capture path" % (command,))
        return handler(args[1], args[2:])
    except UsageError as exc:
        sys.stderr.write("ueia: %s\n" % (exc,))
        return 2
    except UeiaError as exc:
        sys.stderr.write("ueia: %s\n" % (exc,))
        return 1
    finally:
        report()


if __name__ == "__main__":
    sys.exit(main())
