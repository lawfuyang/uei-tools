"""One place that runs an engine executable, and records everything about the call.

Every wrapped invocation is evidence, so a `ToolResult` carries the exe, **its SHA-256 and size**
(the engine's own version strings live inside the tools; a hash is what pins which *build* answered
on this machine), the exact argv, the working directory, the exit code, both streams and the wall
time. Our output quotes that, the same way the CsvTools SVGs embed their own command line.

`subprocess` is called with an argument **list** -- never `shell=True`, never a command string --
so the spaces in both this repo's path and the engine's default path are just characters, and no
argument can become syntax. A timeout is an error, not a wait: the caller gets a `UeiaError` naming
the tool and the budget, because a wrapped exe that hangs would otherwise hang a gate.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from shapes import UeiaError

#: How long a wrapped tool may run before the call is abandoned. A folder-wide PerfreportTool run
#: over dozens of CSVs is the slowest thing here by far, so the budget is generous rather than tight.
_TIMEOUT_SECONDS = 900.0

#: Hashes are cached per process, keyed by path and mtime: a command that wraps several tools, or
#: the same tool twice, must not hash an executable twice.
_IDENTITIES: Dict[Tuple[str, int, int], Dict[str, Any]] = {}


class ToolResult(NamedTuple):
    """What one wrapped call came back with -- the whole of it, so a report can quote the call."""

    exe: str
    sha256: str
    size: int
    argv: List[str]
    cwd: str
    exit: int
    stdout: str
    stderr: str
    seconds: float

    def to_json(self) -> Dict[str, Any]:
        return {
            "exe": self.exe,
            "sha256": self.sha256,
            "size": self.size,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit": self.exit,
            "seconds": round(self.seconds, 3),
        }

    def describe(self) -> List[str]:
        """The lines every wrapped call leaves in our output (stderr: stdout is the tool's)."""
        return [
            "tool     : %s (sha256 %s, %d bytes)" % (
                Path(self.exe).name, self.sha256[:12], self.size,
            ),
            "argv     : %s" % (" ".join(self.argv),),
            "exit     : %d in %.2f s" % (self.exit, self.seconds),
        ]


def identity(path: Path) -> Dict[str, Any]:
    """`{sha256, size}` for an executable, cached by path+mtime: the build that answered."""
    try:
        stat = os.stat(str(path))
    except OSError as exc:
        raise UeiaError("cannot read %s (%s)" % (path, exc)) from exc
    key = (str(path), int(stat.st_mtime), int(stat.st_size))
    cached = _IDENTITIES.get(key)
    if cached is None:
        digest = hashlib.sha256()
        with open(str(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        cached = {"sha256": digest.hexdigest(), "size": int(stat.st_size)}
        _IDENTITIES.clear()
        _IDENTITIES[key] = cached
    return cached


def run_tool(exe: Path, argv: Sequence[str], cwd: Optional[Path] = None,
             timeout: float = _TIMEOUT_SECONDS,
             runner: Optional[Callable[..., Any]] = None) -> ToolResult:
    """Run `exe` with `argv` and return everything about the call.

    A non-zero exit is *data* (the caller decides what it means); a missing exe, an unrunnable
    one and a timeout are errors, because in each of those cases there is no result to read.
    `runner` is injected by the tests, which is the only way to exercise the argv mapping without
    the engine's executables on the machine.
    """
    if not exe.is_file():
        raise UeiaError("no such tool: %s" % (exe,))
    call = [str(exe)] + [str(item) for item in argv]
    workdir = str(cwd or Path.cwd())
    run = runner or subprocess.run
    started = time.perf_counter()
    try:
        done = run(call, cwd=workdir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise UeiaError(
            "%s did not finish within %.0f s: %s" % (Path(exe).name, timeout, " ".join(call))
        ) from exc
    except OSError as exc:
        raise UeiaError("could not run %s (%s)" % (exe, exc)) from exc
    seconds = time.perf_counter() - started
    stamp = identity(exe)
    return ToolResult(
        exe=str(exe),
        sha256=str(stamp["sha256"]),
        size=int(stamp["size"]),
        argv=call,
        cwd=workdir,
        exit=int(done.returncode),
        stdout=done.stdout or "",
        stderr=done.stderr or "",
        seconds=seconds,
    )
