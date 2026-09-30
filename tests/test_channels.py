"""`ueia coverage`: what a capture carries, and therefore what it can answer.

Hermetic and fixture-based like the rest of the suite: `channels` is a pure function of a model
dictionary, so most of what follows feeds it models built here (and, where a case needs a capture the
tool would never record, a model doctored on purpose -- a registry that disagrees with its own
events, a frame series with a warm-up frame).
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, cast

from testcase import UeiaTestCase

import channels
import container
import model as model_mod
import streams
from fixtures import calltree_trace, work_trace


def _model(data: bytes) -> Dict[str, Any]:
    """One fixture capture decoded end to end -- what a command sees after `load_model`."""
    rows, _anomalies = container.walk_packets(data, container.parse_header(data))
    built, _counts, _all = model_mod.build_model(streams.assemble(data, rows))
    return cast(Dict[str, Any], dict(built))


def _verdict(report: channels.Report, opening: str) -> channels.Verdict:
    for verdict in report.verdicts:
        if verdict.title.startswith(opening):
            return verdict
    raise AssertionError("no analysis starts with %r" % (opening,))


def _state(report: channels.Report, name: str) -> channels.ChannelState:
    for state in report.channels:
        if state.name == name:
            return state
    raise AssertionError("the capture's registry has no channel %r" % (name,))


class TestTheTable(unittest.TestCase):
    """The table itself, because everything else reads it."""

    def test_every_analysis_names_channels_it_cannot_run_without(self) -> None:
        self.assertTrue(channels.ANALYSES)
        for analysis in channels.ANALYSES:
            self.assertTrue(analysis.needs, "%s names no channel" % (analysis.title,))
            self.assertTrue(analysis.commands)
            for channel in analysis.needs:
                self.assertEqual(channel, channel.lower())

    def test_a_memory_channel_quotes_the_memory_preset_and_the_rest_the_default_one(self) -> None:
        """Two recordings, not one: `-trace=Default` does not carry allocations (REFERENCE §8)."""
        self.assertEqual(channels.redo_line([]), "")
        self.assertEqual(channels.redo_line(["task"]), "-trace=%s" % (channels.DEFAULT_TRACE,))
        self.assertIn("-trace=%s" % (channels.MEMORY_TRACE,), channels.redo_line(["memalloc"]))
        both = channels.redo_line(["memalloc", "task"])
        self.assertIn(channels.MEMORY_TRACE, both)
        self.assertIn(channels.DEFAULT_TRACE, both)


class TestTheVerdicts(UeiaTestCase):
    """A fixture capture: `cpu` and `frame` are there, everything else is a question it cannot answer."""

    def setUp(self) -> None:
        super().setUp()
        self.model = _model(work_trace())
        self.report = channels.report_of([self.model], ["fixture"])

    def test_what_it_can_answer_is_ready_and_what_it_cannot_is_skipped(self) -> None:
        frame_times = _verdict(self.report, "frame times")
        self.assertTrue(frame_times.ready, frame_times.missing)
        tasks = _verdict(self.report, "the task graph")
        self.assertFalse(tasks.ready)
        self.assertEqual(tasks.missing, ("task",))

    def test_the_missing_channels_come_with_the_line_that_would_have_recorded_them(self) -> None:
        self.assertIn("task", self.report.missing)
        self.assertIn(channels.DEFAULT_TRACE, self.report.redo)
        self.assertIn("-trace=%s" % (channels.MEMORY_TRACE,), self.report.redo,
                      "a memory analysis is in the table, so the memory preset is quoted too")

    def test_a_channel_the_registry_declares_but_the_model_holds_nothing_for_is_not_ready(self) -> None:
        """The registry can be wrong, and the events are the truth (REFERENCE §8)."""
        doctored = dict(self.model)
        doctored["timers"] = []
        doctored["channels"] = [{"id": 1, "name": "Cpu", "is_enabled": True, "read_only": False}]
        report = channels.report_of([doctored], ["no-cpu"])
        self.assertFalse(_verdict(report, "frame times").ready)
        self.assertIn("cpu", _verdict(report, "frame times").missing)
        cpu = _state(report, "cpu")
        self.assertEqual(cpu.held, 0)
        self.assertTrue(cpu.enabled, "the registry says it was recorded -- that is the point")
        self.assertIn("disagree", cpu.note)

    def test_events_are_evidence_even_when_the_registry_says_nothing(self) -> None:
        """The fixtures are synthesised, so their registry is empty: the model still answers."""
        cpu = _state(self.report, "cpu")
        self.assertGreater(cpu.held if cpu.held is not None else 0, 0)
        self.assertFalse(cpu.enabled)
        self.assertIn("events are the evidence", cpu.note)

    def test_a_channel_the_registry_says_was_off_but_the_model_holds_events_for_says_so(self) -> None:
        doctored = dict(self.model)
        doctored["channels"] = [{"id": 1, "name": "Cpu", "is_enabled": False, "read_only": False}]
        cpu = _state(channels.report_of([doctored], ["off"]), "cpu")
        self.assertIn("registry says it was off", cpu.note)

    def test_the_report_renders_prose_and_one_row_per_analysis(self) -> None:
        prose, rows = channels.render_lines(self.report)
        self.assertEqual(len(rows), len(channels.ANALYSES))
        self.assertTrue(any(line.startswith("channels  :") for line in prose))
        self.assertTrue(any(line.startswith("warm-up   :") for line in prose))
        self.assertTrue(any(line.startswith("to record :") for line in prose))
        self.assertEqual([row[2] for row in rows if row[2] not in ("ready", "skipped")], [])


class TestTheHygiene(UeiaTestCase):
    """Frames, length, a warm-up window and run-to-run noise -- from the model, by hand."""

    def _frame(self, index: int, begin: int, end: int, tid: int = 2) -> Dict[str, Any]:
        return {"index": index, "tid": tid, "type": 0, "begin_cycle": begin, "end_cycle": end,
                "covered_cycles": None, "wait_cycles": None, "self_cycles": None,
                "self_detail": None}

    def _capture(self, lengths: List[int]) -> Dict[str, Any]:
        """A model whose frame series has the given lengths in milliseconds at 1 GHz."""
        base = 1000000
        frames = []
        cycle = 0
        for index, length in enumerate(lengths):
            begin = base + cycle * 1000000
            frames.append(self._frame(index, begin, begin + length * 1000000))
            cycle += length
        return {"session": {"cycle_frequency": 1000000000, "duration_cycles": cycle * 1000000},
                "frames": frames, "channels": [], "counts": {}}

    def test_a_frame_series_is_described_by_its_own_distribution(self) -> None:
        model = self._capture([10, 10, 10, 10, 10, 1000])
        series = channels.series_of(model)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].frames, 6)
        self.assertAlmostEqual(series[0].median, 10.0)
        self.assertAlmostEqual(series[0].longest, 1000.0)
        self.assertAlmostEqual(series[0].seconds, 1.05)

    def test_a_slow_first_frame_is_called_a_warm_up(self) -> None:
        warm = channels.report_of([self._capture([400, 10, 10, 10, 10, 10])], ["warm"])
        self.assertIn("warm-up", warm.warm_up)
        steady = channels.report_of([self._capture([10, 10, 10, 10, 10, 10])], ["steady"])
        self.assertIn("no warm-up window", steady.warm_up)

    def test_several_captures_are_compared_for_run_to_run_noise(self) -> None:
        one = self._capture([10, 10, 12, 10, 10])
        two = self._capture([10, 11, 10, 10, 10])
        report = channels.report_of([one, two], ["run-a", "run-b"])
        self.assertEqual(len(report.variance), 3, "one line per capture, plus the spread")
        self.assertIn("apart", report.variance[-1])
        self.assertEqual(channels.report_of([one], ["only-one"]).variance, [])


class TestTheCommand(UeiaTestCase):
    """The command end to end: a written capture, the report, and the exit code."""

    def test_a_fixture_capture_reports_what_it_carries_and_can_answer(self) -> None:
        path = self.dir / "channels-fixture.utrace"
        path.write_bytes(calltree_trace())
        code, out, _err = self.run_cli(["coverage", str(path)])
        self.assertEqual(code, 0, out)
        self.assertIn("capture   : channels-fixture.utrace", out)
        self.assertIn("analysis", out)
        self.assertIn("skipped", out, "a fixture capture is missing most channels, and says which")

    def test_the_channel_table_is_also_a_table(self) -> None:
        path = self.dir / "channels-fixture.utrace"
        path.write_bytes(work_trace())
        code, out, _err = self.run_cli(["coverage", str(path), "--format", "csv"])
        self.assertEqual(code, 0, out)
        self.assertIn("analysis,reported by,verdict,missing", out)


if __name__ == "__main__":  # pragma: no cover - unittest discovery runs it
    unittest.main()
