"""The TaskTrace channel: the task graph, and the longest chain through it.

A `.utrace` can carry the task system's own lifecycle (`-trace=...,task`, or the `TaskGraph` preset),
and with it the one question a timeline cannot answer by itself: *which* work a slow frame was
actually waiting on. The channel is task-centric rather than per-thread -- `FTaskBase` writes
`TaskTrace` events from whichever thread it is on -- so the graph is built once, after the per-thread
walks have merged, by `build_graph`.

The protocol (`Engine/Source/Runtime/Core/Private/Async/TaskTrace.cpp:14-73`; every event is
flag-less and carries `uint64 Timestamp` = `FPlatformTime::Cycles64()`, a *global* counter, which is
why timestamps from different threads are comparable here when the trace's own event times are not):

| event | fields | meaning |
|---|---|---|
| `Created` | `Timestamp, TaskId, TaskSize` | the task object exists |
| `Launched` | `Timestamp, TaskId, WideString DebugName, bool Tracked, int32 ThreadToExecuteOn, TaskSize` | its name, and where it may run |
| `Scheduled` | `Timestamp, TaskId` | its prerequisites are met, it is queued |
| `SubsequentAdded` | `Timestamp, TaskId, SubsequentId` | completing `TaskId` unlocks `SubsequentId` -- the **only** dependency record |
| `Started` / `Finished` | `Timestamp, TaskId` | the body runs: this pair is a task's **duration** |
| `Completed` / `Destroyed` | `Timestamp, TaskId` | its nested tasks are done; the object is freed |
| `WaitingStarted` / `WaitingFinished` | `Timestamp[, uint64[] Tasks]` | the *recording* thread blocks inside a body, waiting for those tasks |

So a task's life has four intervals, and only one of them is work (`TasksProfiler.cpp:601-676`):

    Launched  - Scheduled    waiting for prerequisites   (blocked by a dependency)
    Scheduled - Started      queued                       (waiting for a worker)
    Started   - Finished     **executing**                (the duration everything ranks by)
    Finished  - Completed    waiting for nested tasks

The critical path is the engine's own arithmetic, from the Insights task-graph profiler
(`TraceInsights/Private/Insights/TaskGraphProfiler/TaskGraphProfilerManager.cpp:759-760, 807-808`):
walk the prerequisite edges and take, at every branch, the chain whose **sum of executing durations**
is longest -- `MaxChainDuration + (Finished - Started)`, recursively. It adds no waiting and no gaps:
what it answers is "if this chain could not be shorter, the frame could not be faster", and a gap
between two tasks of the chain is a scheduling artefact, drawn as an edge rather than summed.

Two rules this module keeps, both from the repo's standing rules (AGENTS.md, README §4):

* **Absent is not zero.** A capture with no `TaskTrace` events is *exit 2 with the re-record line*
  (`-trace=cpu,frame,...,task`), never an empty path and never a guess.
* **Every claim says what it is.** A duration is a recorded `Started`/`Finished` pair; a
  `ThreadToExecuteOn` reading is the engine's own `ENamedThreads` packing
  (`TaskGraphInterfaces.h:56-108` -- the low byte names the thread, the high bits its queue and
  priorities); nothing here infers a dependency the capture did not write, and a cycle in those
  edges is counted, never spun on.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from shapes import TaskEventRow, TaskRow, TaskStepRow, TaskWaitRow

#: The logger the channel's events carry, and the event names as the schema spells them.
LOGGER = "TaskTrace"
PREFIX = LOGGER + "."
CREATED = PREFIX + "Created"
LAUNCHED = PREFIX + "Launched"
SCHEDULED = PREFIX + "Scheduled"
SUBSEQUENT_ADDED = PREFIX + "SubsequentAdded"
STARTED = PREFIX + "Started"
FINISHED = PREFIX + "Finished"
COMPLETED = PREFIX + "Completed"
DESTROYED = PREFIX + "Destroyed"
WAITING_STARTED = PREFIX + "WaitingStarted"
WAITING_FINISHED = PREFIX + "WaitingFinished"

#: The event-name suffix -> the `TaskRow` timestamp column it fills.
STAMP_COLUMNS = {
    "Created": "created", "Launched": "launched", "Scheduled": "scheduled",
    "Started": "started", "Finished": "finished", "Completed": "completed",
    "Destroyed": "destroyed",
}

#: `ENamedThreads::Type`'s low byte (`TaskGraphInterfaces.h:56-77`): the named threads, and the value
#: the writer uses for "any worker thread at all".
THREAD_INDEX_MASK = 0xFF
ANY_THREAD = 0xFF
NAMED_THREADS = {0: "RHIThread", 1: "GameThread", 2: "RenderThread"}

#: `Launched`'s thread field packs a queue index and two priorities above the index
#: (`TaskGraphInterfaces.h:71-108`); only the name is interpreted, the rest is described.
QUEUE_MASK = 0x100
TASK_PRIORITY_MASK = 0x200
THREAD_PRIORITY_MASK = 0xC00

#: How much of the graph the model keeps. The *critical path* is always complete (a chain, not a
#: graph); what is bounded is the table of tasks a report can list and the edges it can draw -- the
#: longest executed win, and `task_counts["dropped"]` says how many did not fit, because a capture
#: that ran a million tasks must not put a million rows in the cache.
TASK_KEEP = 512
#: How many edges the model keeps between kept tasks (a graph export draws these).
EDGE_KEEP = 4096
#: How many steps of a chain the report keeps (a longer chain says so in `path_truncated`).
PATH_KEEP = 512


class Chain(NamedTuple):
    """The longest executing-duration chain, and the numbers that make it defensible."""

    steps: List[TaskStepRow]
    total_cycles: int
    ignored_edges: int


def thread_name(value: int) -> str:
    """A `ThreadToExecuteOn` value as a thread name, or "" when the task may run anywhere."""
    index = value & THREAD_INDEX_MASK
    if index == ANY_THREAD:
        return ""
    return NAMED_THREADS.get(index, "thread %d" % (index,))


def describe_execution(value: int) -> str:
    """The engine's packing in words: the thread, its queue, and the priorities when they are set."""
    parts = [thread_name(value) or "any worker thread"]
    if value & QUEUE_MASK:
        parts.append("local queue")
    if value & TASK_PRIORITY_MASK:
        parts.append("high task priority")
    priority = value & THREAD_PRIORITY_MASK
    if priority == 0x400:
        parts.append("high thread priority")
    elif priority == 0x800:
        parts.append("background priority")
    return ", ".join(parts)


def build_graph(events: Sequence[TaskEventRow],
                frequency: int) -> Tuple[Dict[int, TaskRow], List[Tuple[int, int]], Chain,
                                         List[TaskWaitRow], Dict[str, int]]:
    """Fold the per-thread event rows into tasks, dependency edges, waits and a critical path.

    Returns (tasks, edges, chain, waits, counts). `edges` are `(predecessor, subsequent)` pairs --
    "completing the first unlocks the second", as `SubsequentAdded` writes them -- and `chain` is the
    longest executing-duration chain over them, with cycles counted instead of followed.
    """
    tasks: Dict[int, TaskRow] = {}
    edges: List[Tuple[int, int]] = []
    waits: List[TaskWaitRow] = []
    counts: Dict[str, int] = {
        "events": 0, "tasks": 0, "edges": 0, "wait_spans": 0, "ignored_edges": 0,
        "unfinished": 0, "unreadable": 0, "path_truncated": 0,
    }
    pending_wait: Dict[int, int] = {}          # tid -> the cycle its wait started at
    pending_tasks: Dict[int, int] = {}         # tid -> how many tasks that wait named
    for row in events:
        counts["events"] += 1
        kind = row["kind"]
        tid = int(row["tid"])
        stamp = int(row["stamp"])
        if kind == "WaitingStarted":
            pending_wait[tid] = stamp
            pending_tasks[tid] = int(row["other"])
            continue
        if kind == "WaitingFinished":
            started = pending_wait.pop(tid, None)
            named = pending_tasks.pop(tid, 0)
            if started is None:
                counts["unreadable"] += 1
                continue
            counts["wait_spans"] += 1
            waits.append(TaskWaitRow(
                tid=tid, begin_cycle=started, end_cycle=stamp, tasks=named,
                duration_ms=(stamp - started) * 1000.0 / frequency if frequency else 0.0,
            ))
            continue
        if kind == "SubsequentAdded":
            edges.append((int(row["task"]), int(row["other"])))
            counts["edges"] += 1
            continue
        column = STAMP_COLUMNS.get(kind)
        if column is None:
            counts["unreadable"] += 1
            continue
        task_id = int(row["task"])
        task = tasks.get(task_id)
        if task is None:
            task = TaskRow(
                id=task_id, name="", size=0, tracked=False, thread_to_execute_on=0,
                created=None, launched=None, scheduled=None, started=None, finished=None,
                completed=None, destroyed=None, started_tid=0, finished_tid=0,
                prerequisites=[], subsequents=[],
            )
            tasks[task_id] = task
        task[column] = stamp  # type: ignore[literal-required]  (column is one of that row's keys)
        if kind == "Launched":
            # the row packs this event's three non-id values into two ints: the low bit of `flags`
            # is `Tracked`, the rest is `ThreadToExecuteOn` (the walk does the packing, see model.py)
            packed = int(row["flags"])
            task["size"] = int(row["size"])
            task["thread_to_execute_on"] = packed >> 1
            task["tracked"] = bool(packed & 1)
            task["name"] = str(row["text"])
        elif kind == "Created":
            task["size"] = int(row["size"])
        elif kind == "Started":
            task["started_tid"] = tid
        elif kind == "Finished":
            task["finished_tid"] = tid
    counts["tasks"] = len(tasks)
    if pending_wait:
        # a thread that started waiting and never stopped: the capture ends mid-wait
        counts["unreadable"] += len(pending_wait)
    counts["unfinished"] = sum(
        1 for task in tasks.values()
        if task["started"] is not None and task["finished"] is None
    )
    link(tasks, edges)
    chain = critical_path(tasks, edges, frequency, counts)
    return tasks, edges, chain, waits, counts


def link(tasks: Mapping[int, TaskRow], edges: Sequence[Tuple[int, int]]) -> None:
    """Fill each task's `prerequisites` and `subsequents` from the edges the capture recorded."""
    for predecessor, subsequent in edges:
        first = tasks.get(predecessor)
        second = tasks.get(subsequent)
        if first is not None and subsequent not in first["subsequents"]:
            first["subsequents"].append(subsequent)
        if second is not None and predecessor not in second["prerequisites"]:
            second["prerequisites"].append(predecessor)
    for task in tasks.values():
        task["prerequisites"].sort()
        task["subsequents"].sort()


def critical_path(tasks: Mapping[int, TaskRow], edges: Sequence[Tuple[int, int]], frequency: int,
                  counts: Dict[str, int]) -> Chain:
    """The engine's longest chain: the max sum of executing durations along the dependency edges.

    `Finished - Started` per task, `MaxChainDuration + TaskDuration` at every branch
    (`TaskGraphProfilerManager.cpp:759-760`), walked here with an explicit stack so a deep graph
    cannot exhaust the interpreter's, and with a three-colour mark so a cycle -- which the writer
    could not have meant -- is counted and its edge ignored rather than spun on.
    """
    prerequisites: Dict[int, List[int]] = {task_id: [] for task_id in tasks}
    for predecessor, subsequent in edges:
        if subsequent in prerequisites and predecessor in tasks:
            prerequisites[subsequent].append(predecessor)
    for task_id in prerequisites:
        prerequisites[task_id] = sorted(set(prerequisites[task_id]))

    best: Dict[int, int] = {}
    origin: Dict[int, Optional[int]] = {}
    marks: Dict[int, int] = {}                 # 1 = on the stack, 2 = finished
    ignored = 0
    for start in sorted(tasks):
        if marks.get(start):
            continue
        if not _runs(tasks[start]):
            marks[start] = 2
            best[start] = 0
            origin[start] = None
            continue
        stack: List[Tuple[int, int]] = [(start, 0)]
        marks[start] = 1
        while stack:
            task_id, index = stack[-1]
            parents = prerequisites.get(task_id, ())
            if index < len(parents):
                stack[-1] = (task_id, index + 1)
                parent = parents[index]
                mark = marks.get(parent, 0)
                if mark == 1:
                    ignored += 1               # a cycle: this edge would revisit the chain
                    continue
                if mark == 0:
                    marks[parent] = 1
                    if not _runs(tasks[parent]):
                        marks[parent] = 2
                        best[parent] = 0
                        origin[parent] = None
                    else:
                        stack.append((parent, 0))
                continue
            marks[task_id] = 2
            running = duration_cycles(tasks[task_id])
            longest = 0
            chosen: Optional[int] = None
            for parent in parents:
                if marks.get(parent) != 2:
                    continue
                candidate = best.get(parent, 0)
                if candidate > longest or (candidate == longest and chosen is None):
                    longest, chosen = candidate, parent
            best[task_id] = longest + running
            origin[task_id] = chosen
            stack.pop()
    counts["ignored_edges"] = ignored
    return _chain_of(tasks, best, origin, frequency, counts)


def _runs(task: TaskRow) -> bool:
    return task["started"] is not None and task["finished"] is not None


def duration_cycles(task: TaskRow) -> int:
    """Executing cycles: `Finished - Started`, never negative (both ends are guarded here rather
    than through `_runs`, so the types narrow for the checker as well as the interpreter)."""
    started = task["started"]
    finished = task["finished"]
    if started is None or finished is None:
        return 0
    return max(0, int(finished) - int(started))


def _chain_of(tasks: Mapping[int, TaskRow], best: Mapping[int, int],
              origin: Mapping[int, Optional[int]], frequency: int,
              counts: Dict[str, int]) -> Chain:
    """The winning task and everything behind it, oldest first, as the report's steps."""
    if not best:
        return Chain(steps=[], total_cycles=0, ignored_edges=counts.get("ignored_edges", 0))
    winner = max(sorted(best), key=lambda task_id: best[task_id])
    steps: List[TaskStepRow] = []
    seen: Dict[int, bool] = {}
    task_id: Optional[int] = winner
    while task_id is not None and not seen.get(task_id):
        if len(steps) >= PATH_KEEP:
            counts["path_truncated"] += 1
            break
        seen[task_id] = True
        task = tasks[task_id]
        start = int(task["started"] or 0)
        end = int(task["finished"] or 0)
        steps.append(TaskStepRow(
            id=task_id, name=task["name"] or "task %d" % (task_id,),
            duration_cycles=max(0, end - start),
            duration_ms=max(0, end - start) * 1000.0 / frequency if frequency else 0.0,
            started_tid=task["started_tid"], thread_to_execute_on=task["thread_to_execute_on"],
            begin_cycle=start, end_cycle=end, frame_index=None,
        ))
        task_id = origin.get(task_id)
    steps.reverse()
    return Chain(steps=steps, total_cycles=sum(step["duration_cycles"] for step in steps),
                 ignored_edges=counts.get("ignored_edges", 0))


def top_tasks(tasks: Mapping[int, TaskRow], keep: int) -> List[TaskRow]:
    """The tasks a report lists: the longest executed first, then by id (deterministic).

    A task that never finished has no duration to rank; it sorts last, and `counts["unfinished"]`
    says how many there are -- a capture cut off mid-frame is the normal reason, and the report says
    so rather than pretending the task took no time.
    """
    return sorted(tasks.values(), key=lambda task: (-duration_cycles(task), task["id"]))[:keep]


def edges_between(edges: Sequence[Tuple[int, int]], kept: Sequence[int],
                  keep: int) -> List[Tuple[int, int]]:
    """The dependency edges whose ends are both among `kept`, capped at `keep` edges."""
    allowed = set(kept)
    return [edge for edge in edges if edge[0] in allowed and edge[1] in allowed][:keep]


def labels_for(tasks: Sequence[TaskRow], frequency: int) -> Dict[int, str]:
    """The node labels a graph export draws: the task's name and its executing duration."""
    labels: Dict[int, str] = {}
    for task in tasks:
        milliseconds = duration_cycles(task) * 1000.0 / frequency if frequency else 0.0
        labels[task["id"]] = "%s %.3f ms" % (task["name"] or "task %d" % (task["id"],),
                                             milliseconds)
    return labels


def dot(edges: Sequence[Tuple[int, int]], labels: Mapping[int, str],
        name: str = "tasks") -> str:
    """The graph as Graphviz DOT: deterministic, one line per node and per edge."""
    lines = ["digraph %s {" % (name,), '  rankdir="LR";']
    for task_id in sorted(labels):
        lines.append('  t%d [label="%s"];' % (task_id, _escape(labels[task_id])))
    for predecessor, subsequent in edges:
        lines.append("  t%d -> t%d;" % (predecessor, subsequent))
    lines.append("}")
    return "\n".join(lines) + "\n"


def mermaid(edges: Sequence[Tuple[int, int]], labels: Mapping[int, str]) -> str:
    """The graph as a Mermaid flowchart, the form a pull request comment can render."""
    lines = ["flowchart LR"]
    for task_id in sorted(labels):
        lines.append('  t%d["%s"]' % (task_id, _escape(labels[task_id])))
    for predecessor, subsequent in edges:
        lines.append("  t%d --> t%d" % (predecessor, subsequent))
    return "\n".join(lines) + "\n"


def _escape(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ").strip()
