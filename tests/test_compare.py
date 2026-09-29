"""`compare`: the directional gate, the metrics, the baseline, and the empty self-diff.

The gate is unit-tested on synthetic metrics (where every breach and every improvement can be
placed exactly), the command on the fixture captures, and the corpus holds the rule the item asks
for by name: a trace compared with itself moves nothing.
"""

from __future__ import annotations

import json
import unittest
from typing import Dict

from testcase import UeiaTestCase

import compare
import goldens
import summary
from fixtures import work_trace

BUDGET = summary.parse_budget("60", None)


def metric(key: str, value: float, lower: bool = True, unit: str = "ms",
           section: str = "frames") -> compare.Metric:
    """One side's metric, as `compare.metrics` produces it: a value in `before`, no `after` yet."""
    return compare.Metric(key=key, section=section, label=key, unit=unit, lower_is_better=lower,
                          before=value, after=None)


class TestTheGate(unittest.TestCase):
    """The rule the practice describes: worse than the threshold fails, better never does."""

    def _report(self, before: Dict[str, compare.Metric], after: Dict[str, compare.Metric],
                threshold: float = 0.10) -> compare.Report:
        return compare.compare(before, after, threshold, "a", "b")

    def test_a_worse_p99_beyond_the_threshold_fails(self) -> None:
        report = self._report({"frames.p99_ms": metric("frames.p99_ms", 100.0)},
                              {"frames.p99_ms": metric("frames.p99_ms", 111.0)})
        self.assertFalse(report.passed())
        self.assertEqual([item.key for item in report.breached], ["frames.p99_ms"])
        self.assertIn("FAIL", report.verdict())

    def test_the_same_change_inside_the_threshold_passes(self) -> None:
        report = self._report({"frames.p99_ms": metric("frames.p99_ms", 100.0)},
                              {"frames.p99_ms": metric("frames.p99_ms", 109.0)})
        self.assertTrue(report.passed())
        self.assertEqual(report.improved, [])

    def test_an_improvement_is_logged_and_never_gated(self) -> None:
        report = self._report({"frames.p99_ms": metric("frames.p99_ms", 100.0)},
                              {"frames.p99_ms": metric("frames.p99_ms", 50.0)})
        self.assertTrue(report.passed())
        self.assertEqual([item.key for item in report.improved], ["frames.p99_ms"])
        self.assertIn("improved", report.verdict())

    def test_only_the_gated_metrics_can_fail_the_run(self) -> None:
        """A worse `frames.max_ms` is information; the practice gates p99/p95/mean and the hitches."""
        report = self._report({"frames.max_ms": metric("frames.max_ms", 100.0)},
                              {"frames.max_ms": metric("frames.max_ms", 900.0)})
        self.assertTrue(report.passed())

    def test_higher_is_better_metrics_are_read_the_other_way(self) -> None:
        before = {"occupancy.threads_working": metric("occupancy.threads_working", 4.0, lower=False,
                                                      unit="threads")}
        after = {"occupancy.threads_working": metric("occupancy.threads_working", 1.0, lower=False,
                                                     unit="threads")}
        report = self._report(before, after, threshold=0.10)
        self.assertTrue(report.passed(), "fewer threads working is not gated, it is logged")
        self.assertEqual(report.improved, [])

    def test_a_count_whose_baseline_is_zero_is_not_a_ratio(self) -> None:
        report = self._report({"frames.hitches": metric("frames.hitches", 0.0, unit="count")},
                              {"frames.hitches": metric("frames.hitches", 5.0, unit="count")})
        self.assertIsNone(report.metrics[0].delta())
        self.assertTrue(report.passed(), "there is no ratio to gate, and the count is printed")

    def test_a_metric_on_one_side_only_is_reported_but_not_differenced(self) -> None:
        report = self._report({}, {"tasks.chain_ms": metric("tasks.chain_ms", 12.0)})
        self.assertIsNone(report.metrics[0].delta())
        self.assertTrue(report.passed())

    def test_a_self_comparison_is_an_empty_diff(self) -> None:
        one = {"frames.p99_ms": metric("frames.p99_ms", 100.0),
               "frames.hitches": metric("frames.hitches", 7.0, unit="count")}
        report = self._report(one, dict(one))
        self.assertTrue(report.passed())
        self.assertEqual((report.breached, report.improved), ([], []))
        prose, rows = compare.lines(report)
        self.assertEqual(rows, [])
        self.assertIn("a capture compared with itself is an empty diff", "\n".join(prose))


class TestTheCommand(UeiaTestCase):
    """`ueia compare`: the forms, the baseline round trip, and the options it refuses."""

    def _both(self) -> "tuple[str, str]":
        return str(self.write_capture(work_trace(), "a.utrace")), \
            str(self.write_capture(work_trace(), "b.utrace"))

    def test_comparing_a_capture_with_itself_passes_and_moves_nothing(self) -> None:
        capture = str(self.write_capture(work_trace()))
        code, out, _err = self.run_cli(["compare", capture, capture, "--jobs", "1"])
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)
        self.assertIn("no metric moved: a capture compared with itself is an empty diff", out)

    def test_two_identical_captures_compare_clean(self) -> None:
        first, second = self._both()
        code, out, _err = self.run_cli(["compare", first, second, "--jobs", "1"])
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_a_baseline_is_written_and_read_back(self) -> None:
        capture = str(self.write_capture(work_trace(), "a.utrace"))
        baseline = str(self.dir / "baseline.json")
        code, out, _err = self.run_cli(["compare", capture, "--save", baseline, "--jobs", "1"])
        self.assertEqual(code, 0)
        self.assertIn("baseline  :", out)
        with open(baseline, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["schema"], compare.SCHEMA)
        self.assertIn("frames.p99_ms", {row["key"] for row in document["metrics"]})
        code, out, _err = self.run_cli(["compare", capture, "--baseline", baseline, "--jobs", "1"])
        self.assertEqual(code, 0, "the capture against its own baseline moved nothing")
        self.assertIn("PASS", out)

    def test_the_json_form_carries_the_verdict_and_every_metric(self) -> None:
        first, second = self._both()
        code, out, _err = self.run_cli(["compare", first, second, "--jobs", "1", "--format",
                                        "json"])
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["schema"], compare.SCHEMA)
        self.assertTrue(document["verdict"]["passed"])
        self.assertEqual(document["breached"], [])
        self.assertIn("frames.p99_ms", {row["key"] for row in document["metrics"]})
        self.assertEqual(document["meta"]["threshold"], 0.10)

    def test_the_markdown_form_is_the_pr_comment_shape(self) -> None:
        first, second = self._both()
        code, out, _err = self.run_cli(["compare", first, second, "--jobs", "1", "--format",
                                        "markdown"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("## uei-tools: "), out.splitlines()[:1])
        self.assertIn("PASS", out)
        self.assertIn("`ueia.compare/1`", out)

    def test_it_refuses_an_incomplete_or_contradictory_line(self) -> None:
        capture = str(self.write_capture(work_trace()))
        code, _out, err = self.run_cli(["compare", capture])
        self.assertEqual(code, 2)
        self.assertIn("compare needs a second capture", err)
        code, _out, err = self.run_cli(["compare", capture, capture, "--baseline", "x.json"])
        self.assertEqual(code, 2)
        self.assertIn("not both", err)
        code, _out, err = self.run_cli(["compare", capture, "--baseline", "missing.json"])
        self.assertEqual(code, 2)
        self.assertIn("could not be read", err)
        code, _out, err = self.run_cli(["compare", capture, capture, "--threshold", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("--threshold takes a percentage", err)


class TestTheRealCapture(UeiaTestCase):
    """The corpus: the self-diff the item asks for by name, over a real capture's metrics."""
    #: This class reads a registered capture: the cache beside it is content-keyed.
    use_cache = True


    def test_the_editor_capture_against_itself_is_empty(self) -> None:
        paths = goldens.capture_paths()
        path = paths.get("editor-pie-1")
        if path is None or not path.is_file():
            self.skipTest("the editor-pie-1 capture is not on this machine")
        code, out, _err = self.run_cli(["compare", str(path), str(path)])
        self.assertEqual(code, 0)
        self.assertIn("no metric moved", out)
        code, out, _err = self.run_cli(["compare", str(path), str(path), "--format", "json"])
        document = json.loads(out)
        self.assertEqual(document["breached"], [])
        keys = {row["key"] for row in document["metrics"]}
        self.assertIn("frames.p99_ms", keys)
        self.assertIn("work.pairs", keys)
        self.assertEqual(document["meta"]["tool_version"],
                         str(json.loads(out)["meta"]["tool_version"]))


if __name__ == "__main__":  # pragma: no cover - unittest discovery runs it
    unittest.main()
