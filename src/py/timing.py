"""Phase timing and progress lines -- both to stderr, both invisible by default.

$UEI_PROFILE=1 prints a per-phase table at the end of a run; $UEI_PROGRESS=1
prints live lines while long work runs. Neither may ever touch stdout: stdout
is the contract, and a command's stderr stays empty otherwise.
"""

from __future__ import annotations

import sys
import time
from typing import List, Tuple

from shapes import ENV_PROFILE, ENV_PROGRESS, env_flag

_entries: List[Tuple[str, float]] = []
_depth = 0


class timed(object):
    """Context manager: one phase, one name, one row when the table is printed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.start = 0.0

    def __enter__(self) -> "timed":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        seconds = time.perf_counter() - self.start
        _entries.append((self.name, seconds))


def progress(message: str) -> None:
    """One live progress line, only when $UEI_PROGRESS is set."""
    if env_flag(ENV_PROGRESS):
        sys.stderr.write(message.rstrip() + "\n")
        sys.stderr.flush()


def report() -> None:
    """Print the phase table to stderr, only when $UEI_PROFILE is set."""
    if not env_flag(ENV_PROFILE) or not _entries:
        return
    width = max(len(name) for name, _ in _entries)
    total = sum(seconds for _, seconds in _entries)
    for name, seconds in _entries:
        sys.stderr.write("  %-*s  %7.3f s\n" % (width, name, seconds))
    sys.stderr.write("  %-*s  %7.3f s\n" % (width, "total", total))
    sys.stderr.flush()


def reset() -> None:
    """Forget the phases recorded so far (used between selftest cases)."""
    del _entries[:]
