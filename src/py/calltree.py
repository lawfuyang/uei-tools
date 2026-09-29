"""The call tree of a frame, and the **self** time the inclusive view cannot show.

`summary` answers *which timer owns this frame* from the inclusive cycles the walk keeps -- enough to
say "timer X owns 31% of frame 4121", but a scope's inclusive time includes everything it calls, so
the timer to blame is often one level down, and the frame someone asks about is rarely one of the
sixteen the model keeps. This module builds the **nesting tree** from the batch pairs instead:
inclusive and **self** time per timer, top-N by self, the callee expansion of any node, and *any*
frame of any thread.

The two questions the roadmap item left open, answered here:

* **Self time does not want the bounded sample.** The sample (`model._FRAME_WORK_KEEP`) exists so a
  100,000-frame capture does not carry 100,000 trees; the point of this report is a frame nobody
  kept, so it is a **second pass over the streams** for the thread asked about -- seconds, paid only
  when this command runs, never by a parse.
* **The tree is not in the cache.** A tree per frame for every frame of every thread would dwarf the
  model (6.74 MB for the corpus, REFERENCE §13); the cache holds the *answers* the reports ask for,
  and this is a different question.

**What a pair belongs to.** A scope is *present* in every frame it overlaps, clipped to each window,
and its children for a frame are the pairs that closed inside it *in that frame* -- which is what
makes a tree out of intervals whose depth came from the wire. That is not the same rule as the
model's work attribution (which credits a pair to the frame its **end** falls in, `model`), and the
difference is real for a scope that straddles a boundary: here it appears in both frames, there it
appears once. Both are said where they are printed; a scope that straddles is exactly the
`scope_pairs_spanning` the walk counts.

**Shape of the pass** (three attempts, and the first two are recorded because they were measured):
pairing a whole thread into a list of Python tuples costs 200-300 MB and 8-16 s of garbage on the
corpus's biggest thread, and building a node **per pair** costs minutes on a 121-second frame. What
runs now accumulates while the pairs close, into one accumulator per `(depth, spec)` per frame, and
weighs a frame against a heap of the biggest by self as soon as no open pair can still add to it --
so memory is bounded by the frames being reported, not by the capture.
"""

from __future__ import annotations

import bisect
from typing import Any, Dict, List, Mapping, NamedTuple, Sequence, Tuple

import decode
import events
import schema
from shapes import FrameRow


class Node(NamedTuple):
    """One timer in a frame's tree, with its callees merged by spec."""

    spec: int
    name: str
    calls: int
    inclusive: int
    self: int
    children: List["Node"]


class Report(NamedTuple):
    """One frame's tree, plus what the pass had to say about pairing it."""

    frame: FrameRow
    tid: int
    frequency: int
    roots: List[Node]
    top: List[Node]
    total_inclusive: int
    total_self: int
    pairs: int
    ends_unpaired: int
    begins_unpaired: int
    seconds: float
    frame_self: List[Tuple[FrameRow, int]]


#: One open pair: `[spec, begin, {frame: span}, {frame: [child pairs]}, {frame: children's span}]`.
#: The per-frame maps are what let a scope that straddles a boundary be a proper parent in *both*
#: frames -- a child is always inside its parent's clip, so the parent is present wherever it is.
Pair = List[Any]


def clip(begin: int, end: int, low: int, high: int) -> int:
    """How much of `[begin, end)` is inside `[low, high)` -- 0 when none of it is."""
    low = begin if begin > low else low
    high = end if end < high else high
    return high - low if high > low else 0


def materialise(entries: Sequence[Pair], frame: int, names: Mapping[int, str]) -> List[Node]:
    """Turn one frame's root pairs into nodes, merging siblings that name the same timer.

    Siblings of one spec become a single node with `calls` counting them, which is what a reader
    wants: "`StaticLoadObjectInternal` was called 12 times and 40 ms of it was not in a callee", not
    twelve identical rows. `self` is the spec's clipped cycles in this frame minus its children's --
    both sums are over the *clips*, so a scope that straddles the frame edge contributes only the
    part inside it, and a child that closed in another frame is not subtracted here.
    """
    merged: Dict[int, Pair] = {}
    order: List[int] = []
    for entry in entries:
        spec = int(entry[0])
        span = int(entry[2].get(frame, 0))
        kids = entry[3].get(frame, [])
        child_span = int(entry[4].get(frame, 0))
        seen = merged.get(spec)
        if seen is None:
            merged[spec] = [spec, span, child_span, list(kids), 1]
            order.append(spec)
            continue
        seen[1] = int(seen[1]) + span
        seen[2] = int(seen[2]) + child_span
        seen[3].extend(kids)
        seen[4] = int(seen[4]) + 1
    out: List[Node] = []
    for spec in order:
        entry = merged[spec]
        out.append(Node(spec=spec, name=str(names.get(spec, "")) if spec >= 0 else "",
                        calls=int(entry[4]), inclusive=int(entry[1]),
                        self=max(0, int(entry[1]) - int(entry[2])),
                        children=materialise(entry[3], frame, names)))
    return out


def self_of(entries: Sequence[Pair], frame: int) -> int:
    """The self cycles of one frame's roots: their clips, their children's clips subtracted."""
    return sum(max(0, int(entry[2].get(frame, 0)) - int(entry[4].get(frame, 0)))
               for entry in entries)


def top_by_self(roots: Sequence[Node], limit: int) -> List[Node]:
    """The biggest nodes in a tree by **self** time, biggest first."""
    flat: List[Node] = []

    def walk(nodes: Sequence[Node]) -> None:
        for node in nodes:
            flat.append(node)
            walk(node.children)

    walk(roots)
    flat.sort(key=lambda node: (-node.self, -node.inclusive, node.spec))
    return flat[:limit] if limit else flat


def totals(roots: Sequence[Node]) -> Tuple[int, int]:
    """`(inclusive of the roots, self of the whole tree)`."""
    return sum(node.inclusive for node in roots), sum(node.self for node in roots)


def stream_thread(stream: bytes, tid: int, registry: "schema.SchemaRegistry",
                  counts: Dict[str, int], anomalies: List[Any], frames: Sequence[FrameRow],
                  names: Mapping[int, str], keep: int = 3,
                  ) -> Tuple[Dict[int, List[Node]], Dict[int, int], int]:
    """One pass over one thread: `(the kept frames' trees, self cycles per frame, pairs paired)`.

    The trees of the `keep` frames with the biggest **self** time are kept (`frame_self` carries the
    rest), and a frame is weighed as soon as no open pair can still add to it -- which is what keeps
    memory bounded by the frames being reported. Every pair's clip is credited to every frame it
    overlaps, so a scope that straddles a boundary appears in both frames' trees, clipped.
    """
    windows = [(int(row.get("index", 0)), int(row.get("begin_cycle", 0)),
                int(row.get("end_cycle", 0))) for row in frames]
    frame_ends = [row[2] for row in windows]
    per_frame: Dict[int, int] = {}
    trees: Dict[int, List[Node]] = {}
    keepers: List[Tuple[int, int]] = []           # (self, frame index), biggest first
    #: roots **per frame**: a scope that straddles a boundary is a root in both frames, and a flat
    #: list would hold it twice and count its clip twice -- which is exactly what the fixture caught
    roots: Dict[int, List[Pair]] = {}
    stack: List[Pair] = []
    pending = -1                                  # the newest frame not yet weighed
    pairs = 0
    last_cycle = 0

    def weigh(index: int) -> None:
        """Add a finished frame to the ranking, materialising its tree only if it places."""
        if index < 0 or index in per_frame:
            return
        mine = roots.get(index, [])
        own = self_of(mine, index)
        per_frame[index] = own
        keepers.append((own, index))
        keepers.sort(reverse=True)
        while len(keepers) > keep:
            trees.pop(keepers.pop()[1], None)
        if any(entry_index == index for _own, entry_index in keepers):
            trees[index] = materialise(mine, index, names)

    for event in events.iter_thread_events(stream, tid, registry, anomalies, counts):
        if event.b_scope:
            continue
        row = registry.get(event.uid)
        if row is None:
            continue
        if str(row["full_name"]) not in ("CpuProfiler.EventBatchV2", "CpuProfiler.EventBatchV3"):
            continue
        values = decode.event_values(row, stream, event)
        blob = values.get("Data")
        if not isinstance(blob, (bytes, bytearray)):
            counts["bad-batch"] = counts.get("bad-batch", 0) + 1
            continue
        records, _coroutines, error = decode.decode_batch(bytes(blob))
        if error:
            counts["bad-batch"] = counts.get("bad-batch", 0) + 1
        for delta, spec_id, is_begin in records:
            cycle = delta
            if cycle < last_cycle:
                cycle += last_cycle
            last_cycle = cycle
            if is_begin:
                stack.append([spec_id if spec_id is not None else -1, cycle, {}, {}, {}])
                continue
            if not stack:
                counts["ends_unpaired"] = counts.get("ends_unpaired", 0) + 1
                continue
            entry = stack.pop()
            pairs += 1
            begin = int(entry[1])
            # the first frame that begins after this pair's begin: a pair closed out of begin order
            # (a long scope closes after a short one that started inside it) must still find the
            # frames it *started* in, so this is a search and not a cursor that only moves forward
            scan = bisect.bisect_right(frame_ends, begin)
            while scan < len(windows) and windows[scan][1] < cycle:
                index, wbegin, wend = windows[scan]
                span = clip(begin, cycle, wbegin, wend)
                if not span:
                    scan += 1
                    continue
                entry[2][index] = span
                parent = stack[-1] if stack else None
                if parent is None:
                    roots.setdefault(index, []).append(entry)
                else:
                    parent[3].setdefault(index, []).append(entry)
                    parent[4][index] = int(parent[4].get(index, 0)) + span
                if index > pending:
                    # a newer frame is being filled: the older ones can no longer gain a pair,
                    # because a pair's frames are contiguous and it is inside every open pair
                    while 0 <= pending < index and pending < len(windows):
                        if any(int(open_pair[1]) < windows[pending][2] for open_pair in stack):
                            break
                        weigh(pending)
                        pending += 1
                    if pending < index:
                        pending = index if index < len(windows) else -1
                scan += 1
    counts["begins_unpaired"] = counts.get("begins_unpaired", 0) + len(stack)
    for index in range(len(windows)):
        weigh(index)
    return trees, per_frame, pairs


def render_lines(report: Report, limit: int = 12, depth: int = 6) -> Tuple[List[str], List[
        Tuple[str, ...]]]:
    """`(prose, rows)`: the tree as indented prose, and a top-by-self table beside it."""
    import parallel

    frequency = report.frequency
    lines: List[str] = []
    lines.append("frame     : %d (tid %d, type %d), %.3f ms inside a scope, %.3f ms self" % (
        int(report.frame.get("index", 0)), report.tid, int(report.frame.get("type", 0)),
        parallel.ms(report.total_inclusive, frequency), parallel.ms(report.total_self, frequency)))
    lines.append("pairing   : %d scope pair(s)%s%s" % (
        report.pairs,
        "" if not report.ends_unpaired else ", %d end(s) with no begin" % (report.ends_unpaired,),
        "" if not report.begins_unpaired else ", %d begin(s) that never closed"
        % (report.begins_unpaired,)))
    if report.frame_self:
        lines.append("worst     : by *self* time across this thread's frames: %s" % (
            ", ".join("#%d %.3f ms" % (int(row.get("index", 0)), parallel.ms(count, frequency))
                      for row, count in report.frame_self),))
    lines.append("pass      : %.2f s over the streams -- this report reads them a second time, "
                 "because self time must not be a bounded sample and a tree per frame does not "
                 "belong in the cache" % (report.seconds,))
    lines.append("tree      : inclusive / self, flat at every level")
    for node in report.roots:
        _render_node(lines, node, frequency, 12, depth)
    rows = [(
        node.name or "spec %d" % (node.spec,),
        "%.3f" % (parallel.ms(node.self, frequency),),
        "%.3f" % (parallel.ms(node.inclusive, frequency),),
        str(node.calls),
        ", ".join(child.name or "spec %d" % (child.spec,) for child in node.children[:3]),
    ) for node in report.top[:limit]]
    return lines, rows


def _render_node(lines: List[str], node: Node, frequency: int, indent: int, depth: int) -> None:
    import parallel

    if depth < 0:
        return
    lines.append("%s%-50s %10.3f / %9.3f ms x%d" % (
        " " * indent, (node.name or "spec %d" % (node.spec,))[:50],
        parallel.ms(node.inclusive, frequency), parallel.ms(node.self, frequency), node.calls))
    for child in node.children:
        _render_node(lines, child, frequency, indent + 2, depth - 1)
