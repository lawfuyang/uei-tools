"""What every ueia test shares: the paths, a scratch dir, and a CLI runner.

This is not a test file (`selftest` only discovers `test_*.py`): it is the
fixture floor the test files import first. Importing it puts `src/py` on the
path so the tool's modules can be imported by name, exactly as the CLI does.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src" / "py"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import shapes  # noqa: E402  (after the path setup, on purpose)


class UeiaTestCase(unittest.TestCase):
    """A scratch directory, no cache, and one way to run the CLI."""

    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory(prefix="ueia-test-")
        self.addCleanup(self._scratch.cleanup)
        self.dir = Path(self._scratch.name)
        self._env_patch = _EnvPatch({shapes.ENV_NO_CACHE: "1"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def write_capture(self, data: bytes, name: str = "fixture.utrace") -> Path:
        """Write fixture bytes into the scratch directory."""
        path = self.dir / name
        path.write_bytes(data)
        return path

    def run_cli(self, args: List[str]) -> Tuple[int, str, str]:
        """Run the CLI in-process. Returns (exit code, stdout, stderr)."""
        import ueia

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ueia.main(list(args))
            except SystemExit as exc:  # pragma: no cover - main never raises it in tests
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()


class _EnvPatch(object):
    """A tiny environment patch (no mock dependency, pyright-friendly)."""

    def __init__(self, values: Dict[str, str]) -> None:
        self.values = values
        self.saved: Dict[str, Optional[str]] = {}

    def start(self) -> None:
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value

    def stop(self) -> None:
        for key, saved in self.saved.items():
            if saved is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved
        self.saved = {}
