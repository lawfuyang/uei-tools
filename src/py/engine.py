"""The engine directory: the one place engine-provided tooling is found.

`--engine-dir <dir>` (or `$UEI_ENGINE_DIR`) names an Unreal Engine tree. One contract, three
consumers:

* `Engine\\Binaries\\DotNET\\CsvTools\\*.exe` — the CSV toolbox this repo wraps and never
  reimplements (README §2), found by `EngineDir.csvtools`;
* `Engine\\Source` — the tree later phases map timers into (module/source mapping), by
  `EngineDir.source_tree`;
* `Engine\\Build\\Build.version` — the version every report's `meta` block carries, by
  `EngineDir.version`.

Resolution order is the flag, then the environment. A **flag** that names something which is not an
engine tree is a usage error (exit 2): naming the wrong directory is a mistake, not an absence. A
**variable** that names one is reported and ignored, because an ambient setting must never be able
to change what a command does without saying so. Nothing named at all is legal: the commands that
need engine tooling then report *skipped* with the hint, never a silent pass (AGENTS.md).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

from shapes import ENV_ENGINE_DIR, UsageError

#: Where an engine tree keeps each thing this tool asks it for.
CSVTOOLS_SUBPATH = ("Engine", "Binaries", "DotNET", "CsvTools")
VERSION_SUBPATH = ("Engine", "Build", "Build.version")
SOURCE_SUBPATH = ("Engine", "Source")


class EngineDir(NamedTuple):
    """A validated engine tree, and the paths inside it this repo cares about."""

    root: Path

    def csvtools_dir(self) -> Path:
        return self.root.joinpath(*CSVTOOLS_SUBPATH)

    def csvtools(self, exe: str) -> Optional[Path]:
        """The path of one CsvTools executable, or None when this tree does not have it."""
        path = self.csvtools_dir() / exe
        return path if path.is_file() else None

    def source_tree(self) -> Optional[Path]:
        path = self.root.joinpath(*SOURCE_SUBPATH)
        return path if path.is_dir() else None

    def version(self) -> Optional[Dict[str, Any]]:
        """`Build.version` as a dict (MajorVersion/MinorVersion/PatchVersion/...), or None."""
        path = self.root.joinpath(*VERSION_SUBPATH)
        if not path.is_file():
            return None
        try:
            with open(str(path), "r", encoding="utf-8") as stream:
                document = json.load(stream)
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def version_string(self) -> Optional[str]:
        """"5.8.3" from `Build.version`, or None -- the string a report prints."""
        document = self.version()
        if document is None:
            return None
        parts = [
            str(document.get(key)) for key in ("MajorVersion", "MinorVersion", "PatchVersion")
            if document.get(key) is not None
        ]
        if not parts:
            return None
        text = ".".join(parts)
        changelist = document.get("Changelist")
        if isinstance(changelist, int) and changelist > 0:
            text += " (CL %d)" % (changelist,)
        return text

    def describe(self) -> List[str]:
        """The report lines for `csv`/`lz4`-style output: what this tree gave us, honestly."""
        lines = ["engine   : %s" % (self.root.name or str(self.root),)]
        version = self.version_string()
        if version is not None:
            lines.append("version  : %s" % (version,))
        tools = sorted(path.name for path in _exes(self.csvtools_dir()))
        lines.append("csvtools : %s" % (
            "%d executable(s)" % (len(tools),) if tools else "none (is this a full engine tree?)",
        ))
        if self.source_tree() is None:
            lines.append("source   : none (module/source mapping will report module unknown)")
        return lines


def _exes(folder: Path) -> List[Path]:
    try:
        return [path for path in folder.iterdir() if path.suffix.lower() == ".exe"]
    except OSError:
        return []


def is_engine_dir(root: Path) -> bool:
    """An engine tree is one with an `Engine` directory in it -- the only marker that must exist."""
    return (root / "Engine").is_dir()


def resolve(cli_value: Optional[str] = None, env: Optional[Dict[str, str]] = None,
            notes: Optional[List[str]] = None) -> Optional[EngineDir]:
    """The engine tree in play, or None: `--engine-dir`, then `$UEI_ENGINE_DIR`, then nothing.

    `notes` collects anything that has to be said out loud -- an environment variable that names
    something which is not an engine tree, for instance -- so the caller can print it instead of
    letting a misconfiguration change behaviour quietly.
    """
    environment = os.environ if env is None else env
    if cli_value:
        root = Path(cli_value).expanduser()
        if not is_engine_dir(root):
            raise UsageError(
                "--engine-dir %s is not an engine tree (no Engine\\ directory in it)" % (root,)
            )
        return EngineDir(root.resolve())

    named = environment.get(ENV_ENGINE_DIR)
    if named:
        root = Path(named).expanduser()
        if is_engine_dir(root):
            return EngineDir(root.resolve())
        if notes is not None:
            notes.append(
                "$%s is set to %s, which is not an engine tree: ignored" % (ENV_ENGINE_DIR, root)
            )
    return None


def hint() -> str:
    """The one line every skipped engine-tooling path prints, so the fix is always in the output."""
    return "pass --engine-dir <engine tree> (or set $%s) to use the engine's own tools" % (
        ENV_ENGINE_DIR,
    )
