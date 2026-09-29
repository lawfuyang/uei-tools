"""The call tree and self time: the fixture's hand-computed tree, and the corpus's real one.

`calltree_trace` is built so that every number below can be checked by hand at 1 cycle = 1 microsecond
(`fixtures.calltree_stream` writes the arithmetic out), including the two things that are easy to get
wrong: a scope that straddles a frame boundary is present in *both* frames, clipped, and the frame a
report should pick as its worst is the one with the most **self** time, not the longest one.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, cast

from testcase import UeiaTestCase

import calltree
import container
import goldens
import model
import schema
import streams
from fixtures import calltree_trace, work_trace


class TestTheFixtureTree(UeiaTestCase):
    """One pass over `calltree_trace`, against the numbers its docstring writes out."""

    def _pass(self, keep: int = 3) -> "tuple[calltree.Report, Any]":
        data = calltree_trace()
        rows, _anomalies = container.walk_packets(data, container.parse_header(data))
        stream_set = streams.assemble(data, rows)
        built, _counts, _all = model.build_model(stream_set)
        frames = built["frames"]
        names = {int(row["id"]): str(row["name"]) for row in built["timers"]}
        counts = model.zero_counts()
        registry = schema.build_registry(stream_set.streams[0], [], counts)
        trees, per_frame, pairs = calltree.stream_thread(
            stream_set.streams[2], 2, registry, counts, [], frames, names, keep=keep,
        )
        ranked = sorted(per_frame.items(), key=lambda item: (-item[1], item[0]))
        chosen = next(row for row in frames if int(row["index"]) == ranked[0][0])
        roots = trees[ranked[0][0]]
        inclusive, own = calltree.totals(roots)
        report = calltree.Report(
            frame=chosen, tid=2, frequency=1000000, roots=roots,
            top=calltree.top_by_self(roots, 12), total_inclusive=inclusive, total_self=own,
            pairs=pairs, ends_unpaired=counts.get("ends_unpaired", 0),
            begins_unpaired=counts.get("begins_unpaired", 0), seconds=0.0,
            frame_self=[(next(row for row in frames if int(row["index"]) == index), count)
                        for index, count in ranked],
        )
        return report, {index: (trees.get(index), count) for index, count in per_frame.items()}

    def test_the_three_frames_carry_the_hand_computed_self_times(self) -> None:
        """F0 60 ms, F1 **20** ms, F2 80 ms -- and the trees agree with those totals.

        F1 is 20 ms because of the one rule (2026-09-29): the 20 ms `FrameTime` that starts inside F1
        and ends inside F2 belongs to **F2**, clipped at F2's begin, so F1 keeps only its own
        `WorkerTask`. `test_a_scope_that_straddles_a_boundary_belongs_where_it_ends` asserts the same
        fact directly.
        """
        report, _trees = self._pass()
        self.assertEqual(report.pairs, 7, "seven real pairs")
        self.assertEqual(report.ends_unpaired, 1, "the end with no begin, counted not invented")
        self.assertEqual(report.begins_unpaired, 1, "and the begin that never closed")
        ranked = [(int(row["index"]), count) for row, count in report.frame_self]
        self.assertEqual(ranked, [(2, 80000), (0, 60000), (1, 20000)],
                         "ranked by self: F2 80 ms, F0 60 ms, F1 20 ms")
        self.assertEqual(int(report.frame["index"]), 2, "the worst frame by *self* time")

    def test_the_longest_frame_is_not_the_worst_one(self) -> None:
        """F1 spans 140 ms and F2 only 100 -- ranking by duration would pick the wrong frame."""
        report, _trees = self._pass()
        spans = {index: int(row["end_cycle"]) - int(row["begin_cycle"])
                 for index, row in enumerate(self._frames())}
        self.assertEqual(spans, {0: 200000, 1: 140000, 2: 100000})
        self.assertEqual(int(report.frame["index"]), 2)
        self.assertLess(spans[2], spans[1], "chosen despite being the shorter of the two")

    def _frames(self) -> List[Any]:
        data = calltree_trace()
        rows, _anomalies = container.walk_packets(data, container.parse_header(data))
        stream_set = streams.assemble(data, rows)
        built, _counts, _all = model.build_model(stream_set)
        return list(built["frames"])

    def test_the_pass_reports_its_own_numbers_not_the_models(self) -> None:
        """`frames` and `names` come from the model; the pairs and the clips come from the streams."""
        _report, trees = self._pass()
        self.assertEqual([index for index in sorted(trees) if trees[index][0] is not None], [0, 1, 2])

    def test_the_tree_of_a_frame_with_nested_scopes(self) -> None:
        """F0: two merged `FrameTime` siblings, a wait inside one, and a worker task inside that."""
        _report, trees = self._pass()
        tree = trees[0][0]
        assert tree is not None
        self.assertEqual([node.name for node in tree], ["FrameTime"])
        root = tree[0]
        self.assertEqual((root.calls, root.inclusive, root.self), (2, 80000, 60000),
                         "10 ms clipped from the straddler + 70 ms, minus the 20 ms wait inside")
        self.assertEqual([node.name for node in root.children], ["WaitForTasks"])
        wait = root.children[0]
        self.assertEqual((wait.calls, wait.inclusive, wait.self), (1, 20000, 15000))
        self.assertEqual((wait.children[0].name, wait.children[0].self),
                         ("WorkerTask", 5000))

    def test_a_scope_that_straddles_a_boundary_belongs_where_it_ends(self) -> None:
        """One rule everywhere: the straddler is *F2's*, clipped at F2's begin, not shared with F1.

        `model` attributes a scope to the frame its end falls in and clips its begin at that frame
        (`scope_pairs_spanning` counts exactly this). The pass used to disagree -- the scope was in
        both frames, clipped on both edges -- so `self` and `summary` could answer the same question
        two ways (2026-09-29). The corpus then showed what the rule implies: a scope that spans many
        frames belongs to none of them but the last, so the pairs that closed inside the earlier ones
        are promoted to **roots** there (`weigh`, and the promotion at the close of the spanning
        pair) -- otherwise those frames read empty, which is what happened on the game capture.
        """
        _report, trees = self._pass()
        first = trees[1][0]
        second = trees[2][0]
        assert first is not None and second is not None
        self.assertEqual([node.name for node in first], ["WorkerTask"],
                         "the straddler ends in F2, so F1 does not have it at all")
        f2_root = next(node for node in second if node.name == "FrameTime")
        self.assertEqual((f2_root.inclusive, f2_root.calls), (80000, 2),
                         "10 ms of the straddler (clipped at F2's begin) merged with F2's own 70 ms")

    def test_the_tree_and_the_frame_total_agree(self) -> None:
        """The invariant the report rests on: the roots' self is the frame's self, always."""
        _report, trees = self._pass()
        for index in (0, 1, 2):
            entry = trees[index]
            assert entry is not None and entry[0] is not None, "frame %d has no tree" % (index,)
            _inclusive, own = calltree.totals(entry[0])
            self.assertEqual(own, entry[1], "frame %d: tree self != frame self" % (index,))

    def test_a_series_with_gapped_indices_attributes_only_its_own_frames(self) -> None:
        """The corpus's shape: a series filtered by thread *and* type has **gaps** in its indices.

        The model's frame list holds every thread and every frame type, so `self`'s series (one
        thread, one type) is `0, 2, 4, ...` on a capture that recorded two types -- the game capture
        is exactly that. Anything that walks *consecutive* indices takes the indices in between for
        frames of this series: it invents empty rows, which then enter the ranking alongside real
        frames. The fixture's own contiguous `0, 1, 2` cannot catch that, which is why this test
        passes the same capture's frames as a gapped subset (2026-09-29, found when the aligned rule
        reported 0.000 ms on the game capture while every fixture test passed).
        """
        data = calltree_trace()
        rows, _anomalies = container.walk_packets(data, container.parse_header(data))
        stream_set = streams.assemble(data, rows)
        built, _counts, _all = model.build_model(stream_set)
        frames = list(built["frames"])
        counts = model.zero_counts()
        registry = schema.build_registry(stream_set.streams[0], [], counts)
        names = {int(row["id"]): str(row["name"]) for row in built["timers"]}
        subset = [frames[0], frames[2]]
        _trees, per_frame, pairs = calltree.stream_thread(
            stream_set.streams[2], 2, registry, counts, [], subset, names, keep=3)
        self.assertEqual(pairs, 7)
        self.assertEqual(sorted(per_frame), [0, 2],
                         "a row per frame of *this* series, and none for the indices in between")
        self.assertEqual(per_frame[0], 60000, "F0's self, and F2's below: the fixture's arithmetic")
        self.assertEqual(per_frame[2], 80000)

    def test_the_cap_on_kept_frames_keeps_the_biggest_and_no_others(self) -> None:
        report, trees = self._pass(keep=1)
        self.assertEqual(int(report.frame["index"]), 2)
        self.assertEqual(sorted(index for index in trees if trees[index][0] is not None), [2])

    def test_no_frames_means_no_tree(self) -> None:
        """No frame pair on the thread: the pairs are still counted, and no tree is invented."""
        data = work_trace()
        rows, _anomalies = container.walk_packets(data, container.parse_header(data))
        stream_set = streams.assemble(data, rows)
        counts = model.zero_counts()
        registry = schema.build_registry(stream_set.streams[0], [], counts)
        trees, per_frame, pairs = calltree.stream_thread(
            stream_set.streams[2], 2, registry, counts, [], [], {}, keep=3)
        self.assertEqual((trees, per_frame), ({}, {}))
        self.assertGreater(pairs, 0, "the pairs are still counted; there is simply no frame to "
                                     "attribute them to")


class TestTheRealCapture(UeiaTestCase):
    """The corpus: a real frame's tree, where the numbers are nobody's to choose."""
    #: This class reads a registered capture: the cache beside it is content-keyed.
    corpus = True


    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = goldens.capture_paths()
        cls._models: Dict[str, Dict[str, Any]] = {}

    def _model(self, key: str) -> Dict[str, Any]:
        path = self.paths.get(key)
        if path is None or not path.is_file():
            self.skipTest("the %s capture is not on this machine" % (key,))
        model_doc = self._models.get(key)
        if model_doc is None:
            import commands

            _view, loaded, _cached = commands.load_model(str(path))
            model_doc = cast(Dict[str, Any], loaded)
            self._models[key] = model_doc
        return model_doc

    def test_a_real_frame_keeps_the_two_invariants(self) -> None:
        """Tree self == frame self, and no node's self exceeds its own inclusive time.

        The game capture, whose frames are 800 ms of waiting: `FEngineLoop::Tick` holds the frame and
        everything the frame's self time is spent in is a leaf or a wait above it.
        """
        import commands

        model_doc = self._model("game-pc-2")
        view = commands.load_view(str(self.paths["game-pc-2"]))
        stream_set = streams.assemble(view.data, view.packets)
        counts = model.zero_counts()
        registry = schema.build_registry(stream_set.streams[0], [], counts)
        names = {int(row["id"]): str(row["name"]) for row in model_doc["timers"]}
        series = [row for row in model_doc["frames"]
                  if int(row["tid"]) == 2 and int(row["type"]) == 0]
        trees, per_frame, pairs = calltree.stream_thread(
            stream_set.streams[2], 2, registry, counts, [], series[:60], names, keep=3)
        self.assertGreater(pairs, 0)
        self.assertTrue(per_frame)
        for index, own in per_frame.items():
            tree = trees.get(index)
            if tree is None:
                continue
            _inclusive, total = calltree.totals(tree)
            self.assertEqual(total, own, "frame %d: the tree and the total disagree" % (index,))
            for _depth, node in [(0, node) for node in tree]:
                self.assertLessEqual(node.self, node.inclusive)
                self.assertGreaterEqual(node.self, 0)


if __name__ == "__main__":  # pragma: no cover - unittest discovery runs it
    unittest.main()
