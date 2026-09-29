"""Where the timers live: the trace's own file:line, mapped into engine vs project.

A `CpuProfiler.EventSpec` carries a name, a file and a line (`CpuProfilerTrace.h`, REFERENCE §3), and
on the corpus 4,879 of 27,760 specs carry all three. This module turns those strings into an answer
a reader can act on -- *which module, which side of the project boundary, how much of the measured
work it owns* -- and cross-references the engine's own well-known anti-patterns by name.

Three decisions shape it:

* **The path is classified by its shape, not by the machine.** The file strings are the *recording*
  machine's absolute paths (`D:\\TikiStarMain_TMR\\UnrealEngine\\Engine\\Source\\Runtime\\...`), which
  no tree on this machine can contain. What is portable is the structure the engine itself enforces:
  `<root>\\Engine\\Source\\<Group>\\<Module>\\Public|Private\\...` for engine code,
  `<root>\\Engine\\Plugins\\<Category>\\<Plugin>\\Source\\<Module>\\...` for engine plugins and
  `<project>\\Plugins\\<...>\\Source\\<Module>\\...` for a project's own. A path with no `Source`
  segment (a bare file name, say) is **unknown**, and the report says unknown rather than guessing.
* **The engine tree is optional and only ever *checks*.** `--engine-dir` (or `$UEI_ENGINE_DIR`,
  `engine.py`) lets the report say how many of the recorded engine paths exist in the tree at hand --
  a version/consistency check, not a requirement, because the mapping above needs no tree. Without
  one, the report says the paths were classified and nothing was verified.
* **A file's weight is the work it owns, measured -- and it is a sample.** The cycles come from
  `model["frame_work"]`, which the walk keeps for the longest frames of each thread
  (`model._FRAME_WORK_KEEP`), and they are **inclusive** (a scope that contains another counts its
  children too), so shares of a frame can add up past 100%: that is the point, and the report labels
  both the sample and the inclusiveness. A capture with no frames still maps its specs, with no
  weight to them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from engine import EngineDir
from parallel import ms, seconds_text
from summary import Budget

#: The segment that separates a module's project files from its headers and private code.
SOURCE_SEGMENT = "Source"
#: The two segments an engine *root* is recognised by: `Engine\\Source\\...` or `Engine\\Plugins\\...`.
ENGINE_SEGMENT = "Engine"
PLUGINS_SEGMENT = "Plugins"

#: The engine's own anti-patterns, matched on the **timer name** and cross-referenced with what the
#: frames say. Each one is a name rule plus the guidance behind it; a name is a heuristic, and the
#: report says so where it prints them.
PATTERNS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("tick", "per-frame work in Tick", ("tick",)),
    ("sync-load", "synchronous loading on the calling thread",
     ("staticloadobject", "loadobject", "loadpackage", "flushasyncloading", "synchronousloading",
      "loadmap", "getorload")),
    ("object-churn", "object and actor construction in a frame",
     ("newobject", "spawnactor", "constructobject", "createdefaultsubobject", "duplicateobject")),
    ("gc", "garbage collection in a frame",
     ("collectgarbage", "garbagecollect", "gclock", "incrementalpurge")),
    ("serialize", "asset serialization on the calling thread", ("serialize",)),
    ("wait", "waiting inside a frame", ("waitfortasks", "waitfor")),
)

#: A rule's finding is only printed when at least this many specs match it -- one `Tick` timer is
#: not a pattern, it is a timer.
PATTERN_MIN_SPECS = 2


class Location(NamedTuple):
    """One timer spec's location, as far as its own text can say."""

    file: str
    line: int
    basename: str
    side: str        # engine | game | unknown
    kind: str        # engine-source | engine-plugin | project-plugin | project | unknown
    group: str       # Runtime / Editor / Experimental / ... ("" when the shape does not say)
    module: str      # Core / PythonScriptPlugin / WwiseConcurrency / ... ("" when unknown)
    relative: str    # the path from the tree root as recorded, for a reader and for the tree check

    def label(self) -> str:
        """`Runtime/Core`, `Experimental/PythonScriptPlugin`, or the module alone."""
        if self.group and self.module:
            return "%s/%s" % (self.group, self.module)
        return self.module or self.group or "(unknown)"

    def where(self) -> str:
        """The location as a report prints it: the path from the tree root, and the line."""
        return "%s:%d" % (self.relative or self.basename, self.line) if self.line else \
            (self.relative or self.basename)


def _segments(file_text: str) -> List[str]:
    """A path split on either separator, with a Windows drive letter and empty parts dropped."""
    out: List[str] = []
    for part in file_text.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part == ".":
            continue
        if len(part) == 2 and part[0].isalpha() and part[1] == ":":
            continue
        out.append(part)
    return out


def _index_of(segments: Sequence[str], name: str, start: int = 0) -> int:
    for position in range(start, len(segments)):
        if segments[position].lower() == name.lower():
            return position
    return -1


def parse_location(file_text: str, line: int = 0) -> Optional[Location]:
    """One spec's `file`/`line` as a `Location`, or None when there is nothing to parse.

    The rules, in order: an `Engine` segment followed by `Source` or `Plugins` is the engine root
    (that is the only pair engine source and engine plugins are ever laid out under); otherwise a
    `Plugins` segment makes it a project plugin and a `Source` segment a project module; otherwise
    the path says nothing about who owns it and every field but the file is empty.
    """
    segments = _segments(file_text)
    if not segments:
        return None
    basename = segments[-1]
    if not basename:
        return None
    engine = _index_of(segments, ENGINE_SEGMENT)
    kind = ""
    side = "unknown"
    root = -1
    group = ""
    if engine >= 0 and engine + 1 < len(segments):
        following = segments[engine + 1].lower()
        if following == SOURCE_SEGMENT.lower():
            kind, side, root = "engine-source", "engine", engine
        elif following == PLUGINS_SEGMENT.lower():
            kind, side, root = "engine-plugin", "engine", engine
    if not kind:
        plugins = _index_of(segments, PLUGINS_SEGMENT)
        if plugins >= 0:
            kind, side, root = "project-plugin", "game", plugins
    if not kind:
        source = _index_of(segments, SOURCE_SEGMENT)
        if source >= 0:
            kind, side, root = "project", "game", -1
    if not kind:
        return Location(file=file_text, line=line, basename=basename, side="unknown",
                        kind="unknown", group="", module="",
                        relative="/".join(segments))

    source = _index_of(segments, SOURCE_SEGMENT, root + 1 if root >= 0 else 0)
    after = segments[source + 1:] if source >= 0 else []
    module = after[0] if after else ""
    if kind == "engine-source":
        # Engine code is `Source\<Group>\<Module>\...`; a path with one segment after Source is
        # treated as the module itself, because guessing a group for it would be inventing a level.
        group = after[0] if len(after) >= 2 else ""
        module = after[1] if len(after) >= 2 else module
    elif kind == "engine-plugin":
        category = segments[_index_of(segments, PLUGINS_SEGMENT) + 1] \
            if _index_of(segments, PLUGINS_SEGMENT) + 1 < len(segments) else ""
        group = category
    elif kind == "project-plugin":
        # The plugin's own folder is what a reader recognises: the segments before its `Source`.
        group = "/".join(segments[root + 1:source]) if source > root else ""
    # The root marker is *part* of the path for a project plugin (`Plugins/...` is what a reader
    # recognises) and is dropped for engine code (`Engine\\Source\\...` -> `Source\\...`), because
    # there `Engine` is the tree's own name and every path in the capture would repeat it.
    start = root if kind == "project-plugin" else root + 1
    relative = "/".join(segments[start:]) if root >= 0 else "/".join(segments)
    return Location(file=file_text, line=line, basename=basename, side=side, kind=kind,
                    group=group, module=module, relative=relative)


def matches(name: str, words: Sequence[str]) -> bool:
    """True when a timer name carries one of these words -- as a substring of its lower-case form.

    Deliberately loose, and for a different reason than `model.span_kind`'s camel-case rules: the
    engine's own anti-pattern names (`SpawnActor`, `FlushAsyncLoading`, `UWorld::Tick`) are written
    by everybody and matched in the wild with prefixes and suffixes, so `tick` matching `Tick` counts
    in `UWorld::Tick` matches `FTickTaskLevel` too. A hit is a lead, and the report prints it as one.
    """
    lowered = name.lower()
    return any(word in lowered for word in words)


class Finding(NamedTuple):
    """One anti-pattern with the evidence that put it in the report."""

    key: str
    label: str
    specs: int
    owned_cycles: int
    over_frames: int
    biggest: Optional[Tuple[int, str, Location]]


class FileRow(NamedTuple):
    """One source file, with what the model measures about its timers.

    `location` is the location of the file's **biggest** timer, so the line a report shows is the one
    worth opening rather than an arbitrary member of the file.
    """

    location: Location
    specs: int
    owned_cycles: int
    over_frames: int
    biggest: Optional[Tuple[int, str]]


class Report(NamedTuple):
    """The mapping: what was parsed, what it came to, and what it could not say."""

    specs: int
    located: int
    side_counts: Dict[str, int]
    kinds: Dict[str, int]
    files: List[FileRow]
    modules: List[Tuple[str, str, int, int, int]]   # (label, side, specs, owned, over)
    findings: List[Finding]
    owned_located: int
    owned_total: int
    frames: int
    over_budget: int
    budget: Budget
    checked: int
    found: int
    engine_dir: Optional[str]

    def located_share(self) -> float:
        """The share of the kept frames' own cycles that a located spec owns (0.0 without any)."""
        return self.share_of(self.owned_located)

    def share_of(self, cycles: int) -> float:
        """One row's cycles as a share of the kept frames' own span (0.0 when there are no frames)."""
        return (cycles / float(self.owned_total)) if self.owned_total else 0.0


class Weights(NamedTuple):
    """What the kept frames can say about a spec or a group of them.

    `kept` is one entry per frame the walk kept work for -- `(span, over budget, items)` -- and it is
    the only structure the two weight functions need, because the model does not keep a full call
    tree (`ROADMAP` §4 does that): what it keeps is the biggest few timers of each frame.
    """

    kept: List[Tuple[int, bool, List[Tuple[int, int]]]]
    total: int
    over_frames: int

    def presence(self, members: "set[int]") -> Tuple[int, int]:
        """`(cycles, over-budget frames)` for a **group**: the biggest of its timers per frame, summed.

        Deliberately not the sum of every member's cycles: the kept items are **inclusive**, so a
        group holding a scope and its nested children would count the same cycles twice -- measured on
        the corpus, `Runtime/CoreUObject` sums to 306,239 s of a 332 s capture, which is arithmetic
        and not an answer. Taking the biggest match *within each frame* is bounded by that frame's own
        span (the walk clips a pair's cycles to the window it ended in), so the group's share of the
        frames stays a share -- and it is a **lower bound** on the group's presence, never an
        overstatement.
        """
        cycles = 0
        frames = 0
        for _span, too_slow, items in self.kept:
            biggest = 0
            for spec, count in items:
                if spec in members and count > biggest:
                    biggest = count
            if biggest:
                cycles += biggest
                if too_slow:
                    frames += 1
        return cycles, frames

    def share(self, cycles: int) -> float:
        """`cycles` as a share of every kept frame's own span (0.0 when there are no frames)."""
        return (cycles / float(self.total)) if self.total else 0.0


def _kept_frames(model: Mapping[str, Any], budget: Budget) -> Weights:
    """The frames the walk kept work for, with each one's span, its budget verdict and its items."""
    session = model.get("session")
    frequency = int(session.get("cycle_frequency", 0) or 0) if isinstance(session, dict) else 0
    kept: List[Tuple[int, bool, List[Tuple[int, int]]]] = []
    total = 0
    over_frames = 0
    for row in model.get("frame_work", []):
        if not isinstance(row, dict):
            continue
        span = int(row.get("cycles", 0))
        too_slow = bool(frequency) and ms(span, frequency) > budget.ms
        items: List[Tuple[int, int]] = []
        for item in row.get("items", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                items.append((int(item[0]), int(item[1])))
        kept.append((span, too_slow, items))
        total += span
        if too_slow:
            over_frames += 1
    return Weights(kept=kept, total=total, over_frames=over_frames)


def build(model: Mapping[str, Any], budget: Budget,
          engine_dir: Optional[EngineDir] = None) -> Tuple[Optional[Report], List[str]]:
    """Map every located timer spec, weight it by the work it owns, and cross-reference patterns.

    Returns `(report, reasons)`: the reasons are non-empty exactly when the report is None, and each
    one is a sentence a command can print -- a capture with no timer specs at all, or one where no
    spec carries a file:line, has nothing for this command to map, and saying so beats a table of
    rows whose only content is "(unknown)".
    """
    timers = [row for row in model.get("timers", []) if isinstance(row, dict)]
    if not timers:
        return None, ["the capture declares no CpuProfiler timer specs, so there is no source table"]
    located: Dict[int, Location] = {}
    for row in timers:
        file_text = str(row.get("file", "") or "").strip()
        if not file_text:
            continue
        location = parse_location(file_text, int(row.get("line", 0) or 0))
        if location is not None:
            located[int(row.get("id", 0))] = location
    if not located:
        return None, ["no timer spec in this capture carries a file:line, so there is nothing to map "
                      "(the trace was recorded without symbol names?)"]

    names = {int(row.get("id", 0)): str(row.get("name", "")) for row in timers}
    weights = _kept_frames(model, budget)
    # a single spec's own weight: the cycles it held in each frame it ran in, summed. A spec appears
    # at most once per frame, so this is exact -- it is only a *group* that has to avoid nesting
    owned: Dict[int, int] = {}
    for _span, _too_slow, items in weights.kept:
        for spec, count in items:
            owned[spec] = owned.get(spec, 0) + count

    by_file: Dict[str, List[int]] = {}
    side_counts = {"engine": 0, "game": 0, "unknown": 0}
    kinds: Dict[str, int] = {}
    by_module: Dict[Tuple[str, str], List[int]] = {}
    for spec, location in located.items():
        side_counts[location.side] = side_counts.get(location.side, 0) + 1
        kinds[location.kind] = kinds.get(location.kind, 0) + 1
        by_file.setdefault(location.relative or location.basename, []).append(spec)
        by_module.setdefault((location.label(), location.side), []).append(spec)

    files: List[FileRow] = []
    for specs in by_file.values():
        cycles, frames = weights.presence(set(specs))
        ranking = sorted(specs, key=lambda spec: (-owned.get(spec, 0), spec))
        top = ranking[0]
        files.append(FileRow(
            location=located[top], specs=len(specs), owned_cycles=cycles, over_frames=frames,
            biggest=(top, names.get(top, "")),
        ))
    files.sort(key=lambda row: (-row.owned_cycles, -row.specs, row.location.relative))

    modules: List[Tuple[str, str, int, int, int]] = []
    for (label, side), specs in by_module.items():
        cycles, frames = weights.presence(set(specs))
        modules.append((label, side, len(specs), cycles, frames))
    modules.sort(key=lambda item: (-item[3], -item[2], item[0]))

    findings: List[Finding] = []
    for key, label, words in PATTERNS:
        matching = [spec for spec, name in names.items() if matches(name, words)]
        if len(matching) < PATTERN_MIN_SPECS:
            continue
        cycles, frames = weights.presence(set(matching))
        with_location = [spec for spec in matching if spec in located]
        biggest: Optional[Tuple[int, str, Location]] = None
        if with_location:
            top = sorted(with_location, key=lambda spec: (-owned.get(spec, 0), spec))[0]
            biggest = (top, names.get(top, ""), located[top])
        findings.append(Finding(key=key, label=label, specs=len(matching), owned_cycles=cycles,
                                over_frames=frames, biggest=biggest))
    findings.sort(key=lambda item: (-item.owned_cycles, item.key))

    checked = found = 0
    if engine_dir is not None:
        seen: Dict[str, bool] = {}
        for location in located.values():
            if location.side != "engine" or not location.relative:
                continue
            if location.relative not in seen:
                seen[location.relative] = (engine_dir.root / ENGINE_SEGMENT
                                           / location.relative).is_file()
            checked += 1
            if seen[location.relative]:
                found += 1

    report = Report(
        specs=len(timers), located=len(located), side_counts=side_counts, kinds=kinds, files=files,
        modules=modules, findings=findings,
        owned_located=weights.presence(set(located))[0], owned_total=weights.total,
        frames=len(weights.kept), over_budget=weights.over_frames, budget=budget,
        checked=checked, found=found,
        engine_dir=str(engine_dir.root) if engine_dir is not None else None,
    )
    return report, []


def summarize_lines(report: Report, frequency: int, limit: int,
                    filter_text: str = "") -> Tuple[List[str], List[Tuple[str, ...]]]:
    """The report as `(prose, rows)`: the command renders the rows in any `--format`.

    `limit` caps the file rows (0 means every one), and `filter_text` keeps only the files whose
    path or module contains it -- the same filter `timers` and `schema` take, because this report is
    a view of that table.
    """
    lines: List[str] = []
    sides = report.side_counts
    lines.append(
        "sources   : %d timer spec(s), %d with a file:line (%.1f%%); of those %d engine, %d game, "
        "%d unknown (a bare file name says nothing about who owns it)"
        % (report.specs, report.located, 100.0 * report.located / report.specs if report.specs else 0.0,
           sides.get("engine", 0), sides.get("game", 0), sides.get("unknown", 0))
    )
    kinds = ", ".join("%s %d" % (key, count) for key, count in sorted(report.kinds.items(),
                                                                     key=lambda item: -item[1]))
    if kinds:
        lines.append("shapes    : %s" % (kinds,))
    if report.engine_dir is None:
        lines.append(
            "tree      : none given -- the file paths are the *recording* machine's, classified by "
            "their shape only; nothing was checked against a tree here (`--engine-dir <tree>` "
            "resolves them)"
        )
    else:
        lines.append(
            "tree      : %s -- %d of %d engine path(s) exist in it (a mismatch means the capture "
            "came from another revision)" % (report.engine_dir, report.found, report.checked)
        )
    if report.frames:
        lines.append(
            "weight    : the model keeps work for %d frame(s) spanning %s, %d of them over %s; the "
            "biggest located timer in each frame is %.1f%% of that span -- a **lower bound** on what "
            "a file or module held, because the walk keeps only the few biggest timers of a frame and "
            "the cycles are inclusive"
            % (report.frames, seconds_text(report.owned_total, frequency), report.over_budget,
               report.budget.label(), report.located_share() * 100.0)
        )
    else:
        lines.append("weight    : no frame work in this capture (no frames, or no scopes), so the "
                     "specs are mapped without a weight")
    top = [item for item in report.modules[:5] if item[3] > 0]
    if top:
        lines.append("modules   : %s" % (", ".join(
            "%s (%s, %.1f%%)" % (label, side, 100.0 * report.share_of(cycles))
            for label, side, _specs, cycles, _frames in top
        ),))
    lines.append(
        "files     : %d distinct file(s) carry those specs, and the table lists %s"
        % (len(report.files),
           "them all" if not limit or limit >= len(report.files) else "the top %d" % (limit,))
    )
    if report.findings:
        lines.append(
            "patterns  : the engine's own anti-patterns, matched on names (a heuristic) and %s:"
            % ("weighted by the sample of kept frames above" if report.frames
               else "unweighted, because this capture has no frame work to weight them with")
        )
        for finding in report.findings:
            evidence = ""
            if finding.biggest is not None:
                _spec, name, location = finding.biggest
                evidence = " -- e.g. %s (%s)" % (name[:60], location.where())
            if report.frames:
                lines.append(
                    "            %-12s %s: %d spec(s), %.1f%% of the kept frames, %d over budget%s"
                    % (finding.key, finding.label, finding.specs,
                       100.0 * report.share_of(finding.owned_cycles), finding.over_frames, evidence)
                )
            else:
                lines.append("            %-12s %s: %d spec(s)%s"
                             % (finding.key, finding.label, finding.specs, evidence))
    else:
        lines.append("patterns  : nothing matched the engine's anti-pattern names in this capture")

    rows: List[Tuple[str, ...]] = []
    shown = 0
    for row in report.files:
        if filter_text and filter_text.lower() not in (
                (row.location.relative + " " + row.location.module).lower()):
            continue
        if limit and shown >= limit:
            break
        shown += 1
        top_name = row.biggest[1] if row.biggest is not None else ""
        rows.append((
            row.location.where(),
            row.location.side,
            row.location.module or "-",
            str(row.specs),
            "%.1f%%" % (100.0 * report.share_of(row.owned_cycles),),
            str(row.over_frames),
            top_name[:44],
        ))
    if filter_text and not rows:
        lines.append("note      : no file matched --filter %s" % (filter_text,))
    return lines, rows
