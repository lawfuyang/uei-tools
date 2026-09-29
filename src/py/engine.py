"""The engine directory: the one place engine-provided tooling is found, and how one is found at all.

`--engine-dir <dir>` (or `$UEI_ENGINE_DIR`) names an Unreal Engine tree. One contract, three
consumers:

* `Engine\\Binaries\\DotNET\\CsvTools\\*.exe` — the CSV toolbox this repo wraps and never
  reimplements (README §2), found by `EngineDir.csvtools`;
* `Engine\\Source` — the tree later phases map timers into (module/source mapping), by
  `EngineDir.source_tree`;
* `Engine\\Build\\Build.version` — the version every report's `meta` block carries, by
  `EngineDir.version`.

Resolution order is the flag, then the environment, then **a search of this machine**:

* a **flag** that names an engine tree wins outright and nothing is searched -- an explicit
  instruction is never second-guessed;
* a **flag** that names something which is *not* one is said out loud and then searched past
  (2026-09-29: it used to be the end of the command; a typo is a mistake worth reporting, not a
  reason to refuse work that a tree on this machine can do). If the search finds nothing the flag's
  mistake is still a usage error (exit 2);
* a **variable** that names one is reported and ignored rather than obeyed, because an ambient
  setting must never change what a command does without saying so;
* nothing named at all is legal: the commands that need engine tooling report *skipped* with the
  hint, never a silent pass (AGENTS.md).

**What the search is, and what it is not.** It reads the Epic Games Launcher's own install manifests
(`%ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests\\*.item`), the source-build list the engine
records under `HKCU\\Software\\Epic Games\\Unreal Engine\\Builds`, and the handful of directories a
tree usually sits in on each fixed drive -- depth one or two, a few dozen `stat` calls, no walk of a
disk. It is therefore *quick* by construction, and it is deliberately **bounded**: a tree in an
unusual place is not found, which is what `--engine-dir` and `$UEI_ENGINE_DIR` are for. Every
candidate is judged by `EngineDir.missing` against `CSVTOOLS_EXES` + `Build.version` +
`Engine\\Source` -- what this repo can actually use -- and the winner is the newest tree that gives
the most, with what it lacks printed rather than glossed over.

`UEIA_NO_ENGINE_SCAN=1` switches the search off (`shapes.ENV_NO_ENGINE_SCAN`): the hermetic suite sets
it, so a test never depends on what is installed on the machine running it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from shapes import ENV_ENGINE_DIR, ENV_NO_ENGINE_SCAN, UsageError

#: Where an engine tree keeps each thing this tool asks it for.
CSVTOOLS_SUBPATH = ("Engine", "Binaries", "DotNET", "CsvTools")
VERSION_SUBPATH = ("Engine", "Build", "Build.version")
SOURCE_SUBPATH = ("Engine", "Source")

#: The CsvTools executables this repo wraps (`commands._CSV_SUBCOMMANDS` names them per subcommand).
#: A tree missing some of them is still usable -- the command that needs one says so -- but it is not
#: the tree to *choose* when a complete one is on the machine, which is what this inventory is for.
CSVTOOLS_EXES = (
    "csvinfo.exe", "CSVSplit.exe", "CsvConvert.exe", "CSVFilter.exe",
    "CSVCollate.exe", "CSVToSVG.exe", "PerfreportTool.exe", "RegressionsReport.exe",
)


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

    def tools(self) -> List[str]:
        """The `CSVTOOLS_EXES` this tree actually has, by name."""
        return [name for name in CSVTOOLS_EXES if self.csvtools(name) is not None]

    def missing(self) -> List[str]:
        """What this tree lacks, in the words a report prints -- empty when it gives us everything."""
        gaps: List[str] = []
        absent = [name for name in CSVTOOLS_EXES if self.csvtools(name) is None]
        if absent:
            gaps.append("%d CsvTools executable(s) (%s)"
                        % (len(absent), ", ".join(absent[:3]) + (", ..." if len(absent) > 3 else "")))
        if self.version() is None:
            gaps.append("Build.version")
        if self.source_tree() is None:
            gaps.append("Engine\\Source")
        return gaps

    def is_complete(self) -> bool:
        """True when every part of the tree this repo can use is present."""
        return not self.missing()

    def version_tuple(self) -> Tuple[int, ...]:
        """`(major, minor, patch)` from `Build.version`, or from the directory name, or `()`.

        Numbers, not text: `UE_5.10` is newer than `UE_5.9`, and a string comparison says the
        opposite. The directory name is the fallback because that is exactly how a launcher install
        is named (`UE_5.8`), and a tree with neither is ranked by what it holds instead.
        """
        document = self.version()
        if document is not None:
            parts = [document.get(key) for key in ("MajorVersion", "MinorVersion", "PatchVersion")]
            numbers = tuple(int(part) for part in parts if isinstance(part, int))
            if numbers:
                return numbers
        return version_from_name(self.root.name)

    def probe_line(self) -> str:
        """One line: the version, how many CsvTools it has, and whether the source tree is there."""
        version = self.version_string() or (
            ".".join(str(part) for part in self.version_tuple()) or "version unknown")
        return "%s, %d of %d CsvTools, %s" % (
            version, len(self.tools()), len(CSVTOOLS_EXES),
            "source tree present" if self.source_tree() is not None else "no Engine\\Source",
        )


def version_from_name(name: str) -> Tuple[int, ...]:
    """`(5, 8)` from `UE_5.8` / `UnrealEngine-5.8.3` / `UE5`, and `()` when the name says nothing.

    Digits are read as they come: `5.8` is two numbers, `UE5` is one, and a name with no digits at
    all (`UnrealEngine`) ranks below every named tree.
    """
    numbers: List[int] = []
    digits = ""
    for character in name:
        if character.isdigit():
            digits += character
            continue
        if digits:
            numbers.append(int(digits))
            digits = ""
        if character in ".-" and numbers:
            continue
    if digits:
        numbers.append(int(digits))
    return tuple(numbers[:3])


def _exes(folder: Path) -> List[Path]:
    try:
        return [path for path in folder.iterdir() if path.suffix.lower() == ".exe"]
    except OSError:
        return []


def is_engine_dir(root: Path) -> bool:
    """An engine tree is one with an `Engine` directory in it -- the only marker that must exist."""
    return (root / "Engine").is_dir()


#: Where a tree is looked for on a drive, relative to the drive root: depth one or two, no walk.
INSTALL_SUBDIRS = (
    ("Epic Games", "UE_*"),
    ("Epic Games", "UnrealEngine*"),
    ("Program Files", "Epic Games", "UE_*"),
    ("Program Files (x86)", "Epic Games", "UE_*"),
    ("UE_*",),
    ("UnrealEngine*",),
    ("Unreal Engine", "UE_*"),
    ("Unreal Engine", "UnrealEngine*"),
)

#: The drives searched when none are given: the fixed ones a tree realistically sits on. A tree on a
#: drive outside this list is what `--engine-dir` and `$UEI_ENGINE_DIR` are for, and a network drive
#: is never probed (a disconnected one can block for seconds, which is not a *quick* scan).
SEARCH_DRIVES = ("C:", "D:", "E:", "F:", "G:", "H:")

#: Where the Epic Games Launcher records what it installed, one JSON `.item` per install.
MANIFEST_SUBPATH = ("Epic", "EpicGamesLauncher", "Data", "Manifests")

#: Where a source build registers itself, so it can be run without a launcher entry.
REGISTRY_SUBPATH = ("Software", "Epic Games", "Unreal Engine", "Builds")


def search_roots(drives: Optional[Sequence[str]] = None) -> List[Path]:
    """The directories to look in: `INSTALL_SUBDIRS` under every base that exists.

    `drives` takes either drive letters (`"D:"`) or whole directories, which is what makes the search
    testable without writing to a real drive root.
    """
    found: List[Path] = []
    for drive in (SEARCH_DRIVES if drives is None else drives):
        base = Path(drive + os.sep) if len(str(drive)) == 2 and str(drive).endswith(":") \
            else Path(drive)
        if not base.is_dir():
            continue
        for subdirs in INSTALL_SUBDIRS:
            try:
                found.extend(sorted(base.glob(os.path.join(*subdirs))))
            except OSError:
                continue
    return found


def from_manifests(folder: Optional[Path] = None) -> List[Path]:
    """The `InstallLocation` of every install the launcher recorded -- its own list, in its own words.

    Read rather than guessed: a launcher install is in `%ProgramData%\\Epic\\...\\Manifests`, one
    `.item` per install, and its JSON carries both the location and the version. A file that cannot be
    read is skipped: a search is allowed to find nothing, never to fail.
    """
    directory = folder
    if directory is None:
        program_data = os.environ.get("ProgramData", "")
        directory = Path(program_data).joinpath(*MANIFEST_SUBPATH) if program_data else None
    if directory is None or not directory.is_dir():
        return []
    found: List[Path] = []
    for item in sorted(directory.glob("*.item")):
        try:
            with open(str(item), "r", encoding="utf-8") as stream:
                document = json.load(stream)
        except (OSError, ValueError):
            continue
        location = document.get("InstallLocation") if isinstance(document, dict) else None
        if isinstance(location, str) and location:
            found.append(Path(location))
    return found


def from_registry() -> List[Path]:
    """The source builds `HKCU\\...\\Unreal Engine\\Builds` names, best-effort and Windows only."""
    try:
        import winreg
    except ImportError:  # pragma: no cover - this repo targets Windows, the guard is for others
        return []
    found: List[Path] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "\\".join(REGISTRY_SUBPATH)) as key:
            index = 0
            while True:
                try:
                    _name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                if isinstance(value, str) and value:
                    found.append(Path(value))
                index += 1
    except OSError:
        return []
    return found


def rank(candidates: Iterable[EngineDir]) -> List[EngineDir]:
    """The candidates best-first: complete trees before partial ones, then newest, then most tools.

    Completeness first because the question this answers is "which tree gives *us* the most": a 5.8
    install with no CsvTools cannot run the CSV toolbox at all, while a complete 5.6 can run every
    part of this repo. Then the version (numbers, not text: 5.10 is newer than 5.9), then how many
    tools are actually there, and finally the path, so the answer is stable run to run.
    """
    return sorted(
        candidates,
        key=lambda found: (
            found.is_complete(), found.version_tuple(), len(found.tools()), str(found.root),
        ),
        reverse=True,
    )


def discover(manifest_dir: Optional[Path] = None, drives: Optional[Sequence[str]] = None,
             registry: Optional[Callable[[], List[Path]]] = None,
             roots: Optional[Sequence[Path]] = None) -> List[EngineDir]:
    """Every engine tree this machine admits to, best-first (`rank`): the search behind `resolve`.

    Injectable at every edge -- the manifest folder, the drives, the registry reader and the roots
    themselves -- because a test must never depend on what is installed on the machine running it
    (`UEIA_NO_ENGINE_SCAN` is what the suite uses at the top level).
    """
    candidates: List[Path] = list(roots if roots is not None else search_roots(drives))
    candidates.extend(from_manifests(manifest_dir))
    reader = from_registry if registry is None else registry
    candidates.extend(reader())
    seen: Dict[str, EngineDir] = {}
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:  # pragma: no cover - a path that cannot be resolved is simply not a tree
            continue
        if str(resolved) in seen or not is_engine_dir(resolved):
            continue
        seen[str(resolved)] = EngineDir(resolved)
    return rank(seen.values())


def resolve(cli_value: Optional[str] = None, env: Optional[Dict[str, str]] = None,
            notes: Optional[List[str]] = None,
            find: Optional[Callable[[], List[EngineDir]]] = None) -> Optional[EngineDir]:
    """The engine tree in play: `--engine-dir`, then `$UEI_ENGINE_DIR`, then a search of the machine.

    `notes` collects anything that has to be said out loud -- a variable that names something which
    is not a tree, a flag that does not, or the fact that the answer came from a search rather than
    from the caller -- so the caller can print it instead of letting a misconfiguration change
    behaviour quietly. `find` is the searcher (`discover`), injectable for tests.
    """
    environment = os.environ if env is None else env
    wrong_flag: Optional[Path] = None
    if cli_value:
        root = Path(cli_value).expanduser()
        if is_engine_dir(root):
            return EngineDir(root.resolve())
        wrong_flag = root

    named = environment.get(ENV_ENGINE_DIR)
    if named:
        root = Path(named).expanduser()
        if is_engine_dir(root):
            return EngineDir(root.resolve())
        if notes is not None:
            notes.append(
                "$%s is set to %s, which is not an engine tree: ignored" % (ENV_ENGINE_DIR, root)
            )

    if str(environment.get(ENV_NO_ENGINE_SCAN, "")).strip() not in ("", "0"):
        if wrong_flag is not None:
            raise UsageError(
                "--engine-dir %s is not an engine tree (no Engine\\ directory in it)" % (wrong_flag,)
            )
        return None

    searcher = discover if find is None else find
    found = searcher()
    if not found:
        if wrong_flag is not None:
            raise UsageError(
                "--engine-dir %s is not an engine tree (no Engine\\ directory in it), and none was "
                "found on this machine" % (wrong_flag,)
            )
        return None

    winner = found[0]
    if notes is not None:
        if wrong_flag is not None:
            notes.append(
                "--engine-dir %s is not an engine tree (no Engine\\ directory in it): searched this "
                "machine instead" % (wrong_flag,)
            )
        notes.append("scanned  : %s (%s) -- %d engine tree(s) on this machine"
                     % (winner.root, winner.probe_line(), len(found)))
        gaps = winner.missing()
        if gaps:
            # `rank` puts a complete tree first whatever its version, so a winner with gaps means
            # there is no complete tree on this machine at all -- and the gaps are what to fix
            notes.append("scanned  : it lacks %s" % ("; ".join(gaps),))
    return winner


def hint() -> str:
    """The one line every skipped engine-tooling path prints, so the fix is always in the output."""
    return (
        "pass --engine-dir <engine tree> (or set $%s) to use the engine's own tools; this machine's "
        "usual install locations and the launcher's own install list are searched automatically"
        % (ENV_ENGINE_DIR,)
    )
