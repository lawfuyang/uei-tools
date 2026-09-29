"""The task graph and its critical path: the protocol by hand, the arithmetic by hand, the exits.

Two levels, deliberately. `build_graph` is tested against **event rows written by hand** -- exactly
the rows the walk emits, so the durations, edges and chain lengths the tests assert are arithmetic on
paper. The command is tested against a fixture **capture** built event by event, which is what proves
the walk packs the engine's fields the way this module expects: 13 events in, one 5 ms chain out.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from testcase import UeiaTestCase

import container
import goldens
import model as model_mod
import streams
import tasks
from shapes import TaskEventRow, TaskRow
from fixtures import (
    EVENT_FLAG_MAYBE_HAS_AUX,
    EVENT_FLAG_NOSYNC,
    build_trace,
    demo_trace,
    event,
    important_record,
    new_event_record,
    pack,
)

FREQUENCY = 1000000                       # 1 cycle = 1 microsecond, so 1000 cycles = 1 ms
GAME_THREAD = 0x100 | 1                   # ENamedThreads::GameThread | LocalQueue
UID_CREATED, UID_LAUNCHED, UID_SCHEDULED = 70, 71, 72
UID_SUBSEQUENT, UID_STARTED, UID_FINISHED = 73, 74, 75
UID_COMPLETED, UID_DESTROYED = 76, 77
UID_WAIT_STARTED, UID_WAIT_FINISHED = 78, 79


# -- event rows, exactly as `model._walk_tid` builds them --------------------------------------------

def created(task: int, stamp: int, size: int = 64, tid: int = 1) -> TaskEventRow:
    return TaskEventRow(kind="Created", task=task, stamp=stamp, other=0, size=size, flags=0,
                        text="", tid=tid)


def launched(task: int, stamp: int, name: str, execute_on: int = GAME_THREAD,
             tracked: bool = True, size: int = 64, tid: int = 1) -> TaskEventRow:
    return TaskEventRow(kind="Launched", task=task, stamp=stamp, other=0, size=size,
                        flags=((execute_on << 1) | (1 if tracked else 0)), text=name, tid=tid)


def scheduled(task: int, stamp: int, tid: int = 1) -> TaskEventRow:
    return TaskEventRow(kind="Scheduled", task=task, stamp=stamp, other=0, size=0, flags=0,
                        text="", tid=tid)


def edge(predecessor: int, subsequent: int, stamp: int = 0, tid: int = 1) -> TaskEventRow:
    return TaskEventRow(kind="SubsequentAdded", task=predecessor, stamp=stamp, other=subsequent,
                        size=0, flags=0, text="", tid=tid)


def running(task: int, begin: int, end: int, tid: int = 1) -> List[TaskEventRow]:
    return [
        TaskEventRow(kind="Started", task=task, stamp=begin, other=0, size=0, flags=0, text="",
                     tid=tid),
        TaskEventRow(kind="Finished", task=task, stamp=end, other=0, size=0, flags=0, text="",
                     tid=tid),
    ]


def waited(begin: int, end: int, count: int, tid: int = 1) -> List[TaskEventRow]:
    return [
        TaskEventRow(kind="WaitingStarted", task=0, stamp=begin, other=count, size=0, flags=0,
                     text="", tid=tid),
        TaskEventRow(kind="WaitingFinished", task=0, stamp=end, other=0, size=0, flags=0, text="",
                     tid=tid),
    ]


def chain_events(*spec: Tuple[int, str, int, int]) -> List[TaskEventRow]:
    """Tasks that unlock each other in order: `(id, name, begin, end)`, each after the last."""
    rows: List[TaskEventRow] = []
    previous: Optional[int] = None
    for task_id, name, begin, end in spec:
        rows.append(launched(task_id, begin - 10, name))
        if previous is not None:
            rows.append(edge(previous, task_id, begin - 5))
        rows.extend(running(task_id, begin, end))
        previous = task_id
    return rows


# -- the fixture capture ------------------------------------------------------------------------------

def schema() -> bytes:
    """The twelve events, declared as the engine declares them (no NoSync: all are sync events)."""
    return (
        new_event_record(16, "$Trace", "NewTrace",
                         [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                         flags=EVENT_FLAG_NOSYNC)
        + new_event_record(UID_CREATED, "TaskTrace", "Created",
                           [("Timestamp", "u64"), ("TaskId", "u64"), ("TaskSize", "u64")])
        + new_event_record(UID_LAUNCHED, "TaskTrace", "Launched",
                           [("Timestamp", "u64"), ("TaskId", "u64"), ("DebugName", "s"),
                            ("Tracked", "u8"), ("ThreadToExecuteOn", "i32"), ("TaskSize", "u64")],
                           flags=EVENT_FLAG_MAYBE_HAS_AUX)
        + new_event_record(UID_SCHEDULED, "TaskTrace", "Scheduled",
                           [("Timestamp", "u64"), ("TaskId", "u64")])
        + new_event_record(UID_SUBSEQUENT, "TaskTrace", "SubsequentAdded",
                           [("Timestamp", "u64"), ("TaskId", "u64"), ("SubsequentId", "u64")])
        + new_event_record(UID_STARTED, "TaskTrace", "Started",
                           [("Timestamp", "u64"), ("TaskId", "u64")])
        + new_event_record(UID_FINISHED, "TaskTrace", "Finished",
                           [("Timestamp", "u64"), ("TaskId", "u64")])
        + new_event_record(UID_COMPLETED, "TaskTrace", "Completed",
                           [("Timestamp", "u64"), ("TaskId", "u64")])
        + new_event_record(UID_DESTROYED, "TaskTrace", "Destroyed",
                           [("Timestamp", "u64"), ("TaskId", "u64")])
        + new_event_record(UID_WAIT_STARTED, "TaskTrace", "WaitingStarted",
                           [("Timestamp", "u64"), ("Tasks", "arr")],
                           flags=EVENT_FLAG_MAYBE_HAS_AUX)
        + new_event_record(UID_WAIT_FINISHED, "TaskTrace", "WaitingFinished",
                           [("Timestamp", "u64")])
    )


class Builder(object):
    """A stream of task events with serials counted, so the framing stays valid."""

    def __init__(self) -> None:
        self.data = b""
        self.serial = 0

    def _emit(self, uid: int, payload: bytes,
              aux: Sequence[Tuple[int, bytes]] = ()) -> "Builder":
        self.serial += 1
        self.data += event(uid, payload, serial=self.serial, aux=list(aux), maybe_aux=bool(aux))
        return self

    def created(self, stamp: int, task: int, size: int = 64) -> "Builder":
        return self._emit(UID_CREATED, pack("u64", stamp) + pack("u64", task) + pack("u64", size))

    def launched(self, stamp: int, task: int, name: str, execute_on: int = GAME_THREAD,
                 tracked: int = 1, size: int = 64) -> "Builder":
        return self._emit(
            UID_LAUNCHED,
            pack("u64", stamp) + pack("u64", task) + pack("u8", tracked)
            + pack("i32", execute_on) + pack("u64", size),
            aux=[(2, name.encode("utf-8") + b"\x00")],
        )

    def unlocks(self, stamp: int, task: int, subsequent: int) -> "Builder":
        return self._emit(UID_SUBSEQUENT, pack("u64", stamp) + pack("u64", task)
                          + pack("u64", subsequent))

    def ran(self, begin: int, end: int, task: int) -> "Builder":
        self._emit(UID_STARTED, pack("u64", begin) + pack("u64", task))
        return self._emit(UID_FINISHED, pack("u64", end) + pack("u64", task))

    def completed(self, stamp: int, task: int) -> "Builder":
        return self._emit(UID_COMPLETED, pack("u64", stamp) + pack("u64", task))

    def waited(self, begin: int, end: int, on: Sequence[int]) -> "Builder":
        blob = b"".join(pack("u64", task) for task in on)
        self._emit(UID_WAIT_STARTED, pack("u64", begin), aux=[(1, blob)])
        return self._emit(UID_WAIT_FINISHED, pack("u64", end))


def trace(streams: Dict[int, bytes], frames: Optional[Dict[int, List[Tuple[int, int]]]] = None,
          frequency: int = FREQUENCY) -> bytes:
    """A capture carrying task events on the given threads, optionally with frame pairs."""
    declared = schema()
    if frames:
        declared += (
            new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
            + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        )
    threads: Dict[int, bytes] = {}
    for tid, data in streams.items():
        body = data
        for position, (begin, end) in enumerate((frames or {}).get(tid, [])):
            body += event(22, pack("u64", begin) + pack("u8", 0), serial=1000 + position * 2)
            body += event(23, pack("u64", end) + pack("u8", 0), serial=1001 + position * 2)
        threads[tid] = body
    return build_trace(
        events_stream=declared,
        importants_stream=important_record(16, pack("u64", 1000000) + pack("u64", frequency)),
        threads=threads,
    )


def model_of(data: bytes) -> Dict[str, Any]:
    """A fixture capture decoded end to end -- what a command sees after `load_model`."""
    rows, _anomalies = container.walk_packets(data, container.parse_header(data))
    built, _counts, _all = model_mod.build_model(streams.assemble(data, rows))
    return dict(built)


class TestTheThreadPacking(unittest.TestCase):
    """`ENamedThreads::Type`: the low byte names the thread, the rest is the engine's own packing."""

    def test_the_three_named_threads(self) -> None:
        self.assertEqual(tasks.thread_name(0), "RHIThread")
        self.assertEqual(tasks.thread_name(1), "GameThread")
        self.assertEqual(tasks.thread_name(2), "RenderThread")

    def test_the_queue_and_priority_bits_are_not_part_of_the_name(self) -> None:
        self.assertEqual(tasks.thread_name(GAME_THREAD), "GameThread")
        self.assertEqual(tasks.describe_execution(GAME_THREAD), "GameThread, local queue")
        self.assertEqual(tasks.describe_execution(0x400 | 1), "GameThread, high thread priority")
        self.assertIn("high task priority", tasks.describe_execution(0x200 | 1))

    def test_any_thread_is_not_a_named_one(self) -> None:
        self.assertEqual(tasks.thread_name(0xFF), "")
        self.assertIn("any worker thread", tasks.describe_execution(0xFF))
        self.assertIn("background priority", tasks.describe_execution(0xFF | 0x800))

    def test_an_unknown_index_is_still_named(self) -> None:
        self.assertEqual(tasks.thread_name(9), "thread 9")


class TestTheGraph(unittest.TestCase):
    """`build_graph`: tasks out of events, edges out of `SubsequentAdded`, and its own counts."""

    def test_one_task_through_its_whole_life(self) -> None:
        rows = [created(7, 1000, size=128), launched(7, 1010, "MyTask"), scheduled(7, 1020)]
        rows += running(7, 1030, 2030)
        rows.append(TaskEventRow(kind="Completed", task=7, stamp=2040, other=0, size=0, flags=0,
                                 text="", tid=1))
        graph, edges, chain, _waits, counts = tasks.build_graph(rows, FREQUENCY)
        task = graph[7]
        self.assertEqual(
            (task["created"], task["launched"], task["scheduled"], task["started"],
             task["finished"], task["completed"]),
            (1000, 1010, 1020, 1030, 2030, 2040),
        )
        self.assertEqual((task["name"], task["size"], task["tracked"]), ("MyTask", 64, True),
                         "the size the Launched event carries, which is the last one written")
        self.assertEqual(task["thread_to_execute_on"], GAME_THREAD)
        self.assertEqual((task["started_tid"], task["finished_tid"]), (1, 1))
        self.assertEqual(edges, [])
        self.assertEqual(counts["tasks"], 1)
        self.assertEqual(counts["events"], 6)
        self.assertEqual(counts["unreadable"], 0)
        self.assertEqual(len(chain.steps), 1)
        self.assertEqual(chain.steps[0]["duration_cycles"], 1000)
        self.assertAlmostEqual(chain.steps[0]["duration_ms"], 1.0, places=6)

    def test_an_edge_links_both_ways_and_is_written_once(self) -> None:
        graph, edges, chain, _waits, counts = tasks.build_graph(
            chain_events((1, "First", 1000, 2000), (2, "Second", 2000, 4000)), FREQUENCY,
        )
        self.assertEqual(edges, [(1, 2)])
        self.assertEqual(graph[1]["subsequents"], [2])
        self.assertEqual(graph[2]["prerequisites"], [1])
        self.assertEqual([step["name"] for step in chain.steps], ["First", "Second"])
        self.assertEqual(counts["edges"], 1)

    def test_a_wait_span_pairs_up_and_carries_its_task_count(self) -> None:
        _graph, _edges, _chain, waits, counts = tasks.build_graph(waited(5000, 7500, 3), FREQUENCY)
        self.assertEqual(counts["wait_spans"], 1)
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0]["begin_cycle"], waits[0]["end_cycle"], waits[0]["tasks"]),
                         (5000, 7500, 3))
        self.assertAlmostEqual(waits[0]["duration_ms"], 2.5, places=6)

    def test_a_wait_that_never_finishes_is_counted_unreadable(self) -> None:
        rows = [TaskEventRow(kind="WaitingStarted", task=0, stamp=5000, other=2, size=0, flags=0,
                             text="", tid=1)]
        _graph, _edges, _chain, waits, counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual(waits, [])
        self.assertEqual(counts["unreadable"], 1)

    def test_an_unknown_event_kind_is_counted_not_crashed(self) -> None:
        rows = [TaskEventRow(kind="SomethingNew", task=1, stamp=1, other=0, size=0, flags=0,
                             text="", tid=1)]
        graph, _edges, _chain, _waits, counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual(counts["unreadable"], 1)
        self.assertEqual(graph, {})

    def test_an_unfinished_task_is_counted_and_ranks_last(self) -> None:
        rows = [launched(1, 1000, "Runs")] + running(1, 1100, 5100)
        rows += [launched(2, 1100, "Never"), TaskEventRow(
            kind="Started", task=2, stamp=1200, other=0, size=0, flags=0, text="", tid=1)]
        graph, _edges, chain, _waits, counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual(counts["unfinished"], 1)
        self.assertEqual(chain.steps[0]["id"], 1, "a task that never finished has no duration")
        self.assertEqual([task["id"] for task in tasks.top_tasks(graph, 10)], [1, 2])

    def test_the_events_can_come_from_different_threads(self) -> None:
        """The channel is task-centric: one task's life can be recorded by two threads."""
        rows = [created(7, 1000, tid=1), launched(7, 1010, "Split", tid=1), scheduled(7, 1020, tid=2)]
        rows += running(7, 1030, 2030, tid=2)
        graph, _edges, chain, _waits, _counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual((graph[7]["created"], graph[7]["started"]), (1000, 1030))
        self.assertEqual(graph[7]["started_tid"], 2)
        self.assertEqual(len(chain.steps), 1)


class TestTheCriticalPath(unittest.TestCase):
    """The engine's arithmetic: the max sum of executing durations along the prerequisite edges."""

    def test_a_chain_of_three_beats_one_long_task(self) -> None:
        """A(1 ms) -> B(2) -> C(1) = 4 ms, against an independent D of 3 ms."""
        rows = chain_events((1, "A", 1000, 2000), (2, "B", 2000, 4000), (3, "C", 4000, 5000))
        rows += [launched(4, 1000, "D")] + running(4, 1000, 4000)
        _graph, _edges, chain, _waits, _counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual([step["name"] for step in chain.steps], ["A", "B", "C"])
        self.assertEqual(chain.total_cycles, 4000)

    def test_the_heavier_branch_wins_at_a_join(self) -> None:
        """A -> B(1 ms) and A -> C(5 ms), both leading to D: the chain takes C."""
        rows = [launched(1, 990, "A")] + running(1, 1000, 2000)
        rows += [launched(2, 1990, "B")] + running(2, 2000, 3000)
        rows += [launched(3, 1990, "C")] + running(3, 2000, 7000)
        rows += [launched(4, 6990, "D")] + running(4, 7000, 8000)
        rows += [edge(1, 2, 1500), edge(1, 3, 1500), edge(2, 4, 3500), edge(3, 4, 7500)]
        _graph, _edges, chain, _waits, _counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual([step["name"] for step in chain.steps], ["A", "C", "D"])
        self.assertEqual(chain.total_cycles, 1000 + 5000 + 1000)

    def test_waiting_is_not_part_of_a_tasks_duration(self) -> None:
        """`Finished - Started` only: the nine milliseconds it sat queued do not count."""
        rows = [launched(1, 1000, "Blocked"), scheduled(1, 1000)] + running(1, 9000, 10000)
        _graph, _edges, chain, _waits, _counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual(chain.total_cycles, 1000)

    def test_a_cycle_is_counted_and_never_followed(self) -> None:
        rows = [launched(1, 990, "T1")] + running(1, 1000, 2000)
        rows += [launched(2, 1990, "T2")] + running(2, 2000, 3000)
        rows += [edge(1, 2, 1500), edge(2, 1, 2500)]
        _graph, _edges, chain, _waits, counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual(counts["ignored_edges"], 1)
        self.assertLessEqual(len(chain.steps), 2, "a chain never visits a task twice")

    def test_an_equal_diamond_takes_the_same_branch_every_time(self) -> None:
        rows = [launched(1, 990, "A")] + running(1, 1000, 2000)
        rows += [launched(2, 1990, "B")] + running(2, 2000, 4000)
        rows += [launched(3, 1990, "C")] + running(3, 2000, 4000)
        rows += [launched(4, 3990, "D")] + running(4, 4000, 5000)
        rows += [edge(1, 2, 1500), edge(1, 3, 1500), edge(2, 4, 3500), edge(3, 4, 3500)]
        first = tasks.build_graph(rows, FREQUENCY)[2]
        again = tasks.build_graph(rows, FREQUENCY)[2]
        self.assertEqual([step["id"] for step in first.steps], [1, 2, 4])
        self.assertEqual([step["id"] for step in again.steps],
                         [step["id"] for step in first.steps],
                         "two equal branches resolve the same way every run")

    def test_a_task_that_never_ran_breaks_a_chain_rather_than_joining_it(self) -> None:
        rows = [launched(1, 1000, "Runs")] + running(1, 1000, 2000)
        rows += [launched(2, 1000, "Never"), edge(1, 2, 1500), TaskEventRow(
            kind="Started", task=2, stamp=1500, other=0, size=0, flags=0, text="", tid=1)]
        _graph, _edges, chain, _waits, counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual([step["id"] for step in chain.steps], [1])
        self.assertEqual(counts["unfinished"], 1)

    def test_a_pair_that_goes_backwards_is_clamped_not_believed(self) -> None:
        rows = [launched(1, 1000, "Backwards")] + running(1, 5000, 4000)
        _graph, _edges, chain, _waits, _counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual(chain.steps[0]["duration_cycles"], 0)

    def test_a_task_with_no_events_at_all_is_not_a_task(self) -> None:
        graph, edges, chain, _waits, _counts = tasks.build_graph([], FREQUENCY)
        self.assertEqual((graph, edges, chain.steps, chain.total_cycles), ({}, [], [], 0))


class TestTheGraphText(unittest.TestCase):
    def test_dot_is_deterministic_and_quotes_its_labels(self) -> None:
        labels = {2: 'Second "quoted"', 1: "First 1.000 ms"}
        text = tasks.dot([(1, 2)], labels)
        self.assertTrue(text.startswith("digraph tasks {"))
        self.assertIn('  t1 [label="First 1.000 ms"];', text)
        self.assertIn('  t2 [label="Second \'quoted\'"];', text)
        self.assertIn("  t1 -> t2;", text)
        self.assertEqual(text, tasks.dot([(1, 2)], labels))

    def test_mermaid_is_a_flowchart(self) -> None:
        text = tasks.mermaid([(1, 2)], {1: "A", 2: "B"})
        self.assertEqual(text.splitlines()[0], "flowchart LR")
        self.assertIn('  t1["A"]', text)
        self.assertIn("  t1 --> t2", text)

    def test_the_labels_name_the_task_and_its_duration(self) -> None:
        task = TaskRow(
            id=5, name="LoadPackage", size=8, tracked=True, thread_to_execute_on=1,
            created=None, launched=None, scheduled=None, started=1000, finished=3500,
            completed=None, destroyed=None, started_tid=1, finished_tid=1,
            prerequisites=[], subsequents=[],
        )
        self.assertEqual(tasks.labels_for([task], FREQUENCY), {5: "LoadPackage 2.500 ms"})
        self.assertEqual(tasks.labels_for([task], 0), {5: "LoadPackage 0.000 ms"})

    def test_edges_are_filtered_to_the_kept_tasks_and_capped(self) -> None:
        edges = [(1, 2), (2, 3), (3, 4)]
        self.assertEqual(tasks.edges_between(edges, [1, 2], 10), [(1, 2)])
        self.assertEqual(tasks.edges_between(edges, [1, 2, 3, 4], 2), [(1, 2), (2, 3)])

    def test_the_top_table_keeps_the_longest(self) -> None:
        rows = [launched(1, 0, "Short")] + running(1, 1000, 2000)
        rows += [launched(2, 0, "Long")] + running(2, 1000, 4000)
        graph, _edges, _chain, _waits, _counts = tasks.build_graph(rows, FREQUENCY)
        self.assertEqual([task["name"] for task in tasks.top_tasks(graph, 1)], ["Long"])


class TestTheFixtureThroughTheWalk(UeiaTestCase):
    """The command on a fixture capture: what proves the walk packs the engine's fields right."""

    def _fixture(self) -> bytes:
        builder = Builder()
        for task, name in ((1, "LoadMap"), (2, "Tick"), (3, "Draw")):
            builder.created(999000, task).launched(999100, task, name)
        builder.unlocks(1005000, 1, 2).unlocks(1020000, 2, 3)
        builder.ran(1000000, 1010000, 1).ran(1010000, 1040000, 2).ran(1040000, 1050000, 3)
        builder.completed(1060000, 3).waited(1015000, 1035000, [2])
        return trace({3: builder.data}, frames={3: [(1000000, 1050000)]})

    def _capture(self) -> str:
        return str(self.write_capture(self._fixture()))

    def test_the_model_carries_the_graph_the_chain_and_the_counts(self) -> None:
        model = model_of(self._fixture())
        counts = model["task_counts"]
        self.assertEqual((counts["tasks"], counts["events"], counts["edges"]), (3, 17, 2),
                         "3 created + 3 launched + 2 edges + 6 ran + 1 completed + 2 waited")
        self.assertEqual(counts["path_steps"], 3)
        self.assertEqual(counts["path_cycles"], 50000)
        self.assertEqual(counts["path_ms"], 50)
        self.assertEqual(counts["wait_spans"], 1)
        self.assertEqual(counts["dropped"], 0)
        names = {row["id"]: row["name"] for row in model["tasks"]}
        self.assertEqual(names, {1: "LoadMap", 2: "Tick", 3: "Draw"})
        self.assertEqual([step["name"] for step in model["task_path"]],
                         ["LoadMap", "Tick", "Draw"])
        self.assertEqual([step["frame_index"] for step in model["task_path"]], [0, 0, 0])
        self.assertEqual(model["task_edges"], [(1, 2), (2, 3)],
                         "in memory the edges are pairs; the cache round-trip makes them lists")
        self.assertEqual(model["task_waits"][0]["tasks"], 1)
        by_id = {row["id"]: row for row in model["tasks"]}
        self.assertEqual(by_id[1]["subsequents"], [2])
        self.assertEqual(by_id[3]["prerequisites"], [2])
        self.assertEqual([row["id"] for row in model["tasks"]], [2, 1, 3],
                         "the top table is the longest first: Tick 30 ms, then the 10 ms pair")

    def test_the_report_of_the_fixture_by_hand(self) -> None:
        code, out, err = self.run_cli(["tasks", self._capture()])
        self.assertEqual(code, 0)
        self.assertEqual(err, "", "table form keeps everything on stdout")
        self.assertIn("tasks     : 3 task(s) from 17 TaskTrace event(s), 2 dependency edge(s)", out)
        self.assertIn("critical  : 50.000 ms over 3 step(s), 1 frame(s) touched (frame 0)", out)
        self.assertIn("waiting   : 1 span(s) on 1 thread(s), 20.000 ms total", out)

    def test_the_table_lists_the_chain_oldest_first(self) -> None:
        code, out, _err = self.run_cli(["tasks", self._capture()])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("task "))
        rows = [line for line in lines[start + 2:] if line.strip()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].split()[:2], ["1", "10.000"])
        self.assertIn("LoadMap", rows[0])
        self.assertIn("GameThread", rows[0])
        self.assertEqual(rows[1].split()[:2], ["2", "30.000"])
        self.assertIn("Draw", rows[2])

    def test_the_limit_caps_the_steps_listed(self) -> None:
        code, out, _err = self.run_cli(["tasks", self._capture(), "--limit", "1"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("task "))
        self.assertEqual(len([line for line in lines[start + 2:] if line.strip()]), 1)
        self.assertIn("2 more step(s) on the chain", out)

    def test_the_dot_graph_goes_to_stdout_and_the_prose_to_stderr(self) -> None:
        code, out, err = self.run_cli(["tasks", self._capture(), "--graph", "dot"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("digraph tasks {"))
        self.assertTrue(out.rstrip().endswith("}"))
        self.assertIn("t1 -> t2;", out)
        self.assertNotIn("critical  :", out, "the prose belongs on stderr in every graph form")
        self.assertIn("critical  : 50.000 ms", err)

    def test_the_mermaid_graph(self) -> None:
        code, out, _err = self.run_cli(["tasks", self._capture(), "--graph", "mermaid"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "flowchart LR")
        self.assertIn("t2 --> t3", out)

    def test_the_json_graph_carries_the_chain_and_the_counts(self) -> None:
        code, out, _err = self.run_cli(["tasks", self._capture(), "--graph", "json"])
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(len(document["tasks"]), 3)
        self.assertEqual(document["edges"], [[1, 2], [2, 3]])
        self.assertEqual([step["name"] for step in document["criticalPath"]],
                         ["LoadMap", "Tick", "Draw"])
        self.assertEqual(document["criticalPath"][0]["frame"], 0)
        self.assertEqual(document["criticalPath"][1]["durationMs"], 30.0)
        self.assertEqual(document["counts"]["path_cycles"], 50000)
        self.assertIn("GameThread", document["tasks"][0]["threadToExecuteOn"])

    def test_a_capture_without_task_events_exits_two_with_the_hint(self) -> None:
        capture = self.write_capture(demo_trace(), name="plain.utrace")
        code, out, _err = self.run_cli(["tasks", str(capture)])
        self.assertEqual(code, 2)
        self.assertIn("tasks     : none -- this capture carries no TaskTrace events", out)
        self.assertIn("hint      : re-record with `-trace=cpu,frame,log,bookmark,counters,task`", out)

    def test_a_cycle_in_the_edges_is_reported_and_not_followed(self) -> None:
        builder = Builder()
        for task, begin, end in ((1, 1000000, 1010000), (2, 1010000, 1020000)):
            builder.launched(999000, task, "T%d" % task).ran(begin, end, task)
        builder.unlocks(1005000, 1, 2).unlocks(1015000, 2, 1)
        capture = self.write_capture(trace({3: builder.data}), name="cycle.utrace")
        code, out, _err = self.run_cli(["tasks", str(capture)])
        self.assertEqual(code, 0)
        self.assertIn("1 edge(s) in a cycle (ignored)", out)

    def test_events_spread_over_two_threads_still_form_one_graph(self) -> None:
        first = Builder().created(999000, 1).launched(999100, 1, "Across")
        second = Builder().ran(1000000, 1020000, 1)
        capture = self.write_capture(trace({3: first.data, 4: second.data}), name="two.utrace")
        code, out, _err = self.run_cli(["tasks", str(capture)])
        self.assertEqual(code, 0)
        self.assertIn("tasks     : 1 task(s) from 4 TaskTrace event(s), 0 dependency edge(s)", out)
        self.assertIn("Across", out)

    def test_bad_options_are_usage_errors(self) -> None:
        capture = self._capture()
        self.assertEqual(self.run_cli(["tasks", capture, "--graph"])[0], 2)
        self.assertEqual(self.run_cli(["tasks", capture, "--graph", "svg"])[0], 2)
        self.assertEqual(self.run_cli(["tasks", capture, "--top", "5"])[0], 2)

    def test_the_same_capture_gives_the_same_report_twice(self) -> None:
        capture = self._capture()
        self.assertEqual(self.run_cli(["tasks", capture]), self.run_cli(["tasks", capture]))


class TestTheRealCaptures(UeiaTestCase):
    """Every registered capture, and the honest answer each one gets: no task events, exit 2."""
    #: This class reads a registered capture: the cache beside it is content-keyed.
    corpus = True


    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = goldens.capture_paths()
        cls._models: Dict[str, Dict[str, Any]] = {}

    def _model(self, key: str) -> Dict[str, Any]:
        import commands

        cached = TestTheRealCaptures._models.get(key)
        if cached is None:
            _view, model, _cached = commands.load_model(str(self.paths[key]))
            cached = dict(model)
            TestTheRealCaptures._models[key] = cached
        return cached

    def test_every_registered_capture_carries_no_task_events(self) -> None:
        checked = 0
        for key, path in sorted(self.paths.items()):
            if not path.is_file():
                continue
            checked += 1
            counts = self._model(key)["task_counts"]
            self.assertEqual(counts["events"], 0, "%s: expected no task events" % (key,))
            self.assertEqual(counts["tasks"], 0)
        if not checked:
            self.skipTest("no capture is registered on this machine")

    def test_the_model_of_a_capture_without_task_events_says_zero_not_none(self) -> None:
        path = next((item for item in sorted(self.paths) if self.paths[item].is_file()), None)
        if path is None:
            self.skipTest("no capture is registered on this machine")
        model = self._model(path)
        counts = model["task_counts"]
        self.assertEqual((counts["events"], counts["tasks"], counts["edges"]), (0, 0, 0))
        self.assertEqual(model["task_path"], [])
        self.assertEqual(model["tasks"], [])
        self.assertEqual(model["task_waits"], [])
        self.assertIn("path_ms", counts)

    def test_the_command_says_so_on_one_capture_at_least(self) -> None:
        key = next((item for item in sorted(self.paths) if self.paths[item].is_file()), None)
        if key is None:
            self.skipTest("no capture is registered on this machine")
        code, out, _err = self.run_cli(["tasks", str(self.paths[key])])
        self.assertEqual(code, 2)
        self.assertIn("carries no TaskTrace events", out)


if __name__ == "__main__":
    unittest.main()
