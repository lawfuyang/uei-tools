"""The recommendations engine: the rules, the ranking, the machine form, and the corpus.

The hermetic half runs the rules over `work_trace` (four frames, three of them over budget, one
timer owning 40% of the kept frames) and over captures that cannot answer at all. The corpus half
pins what the three registered captures actually recommend -- including the rule that must *not*
fire, which is the one that would otherwise call every slow frame a frame-rate cap.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, cast

from testcase import UeiaTestCase

import advice
import goldens
import summary
from fixtures import (
    EVENT_FLAG_NOSYNC,
    build_trace,
    event,
    important_record,
    new_event_record,
    pack,
    work_trace,
)

BUDGET = summary.parse_budget("60", None)


class TestTheRules(UeiaTestCase):
    """`build` over the fixture: which rules fire, and in what order."""
    #: This class reads a registered capture: the cache beside it is content-keyed.
    use_cache = True


    def _context(self, data: "bytes | None" = None, explicit: bool = False) -> advice.Context:
        import commands

        path = self.write_capture(data if data is not None else work_trace())
        _view, model, _cached = commands.load_model(str(path))
        return advice.context(cast(Dict[str, Any], model), BUDGET, explicit, capture="fx.utrace")

    def _ids(self, findings: List[advice.Finding]) -> List[str]:
        return [finding.id for finding in findings]

    def test_the_fixture_fires_the_frame_rules(self) -> None:
        findings = advice.build(self._context())
        ids = self._ids(findings)
        self.assertIn("over-budget", ids, "three of four frames miss 60 FPS")
        self.assertIn("tail-vs-mean", ids, "p50 20 ms against p99 60 ms")
        self.assertIn("hitches", ids, "the 60 ms frame is over 33.333 ms")
        self.assertIn("budget-default", ids, "no --budget was given")

    def test_a_thin_capture_says_its_percentiles_are_thin(self) -> None:
        numbers = advice.build(self._context())
        thin = next(finding for finding in numbers if finding.id == "thin-sample")
        self.assertIn("only 4 frame(s)", thin.title)

    def test_an_explicit_budget_silences_the_default_budget_finding(self) -> None:
        findings = advice.build(self._context(explicit=True))
        self.assertNotIn("budget-default", self._ids(findings))

    def test_one_timer_owning_a_quarter_is_named_with_its_command(self) -> None:
        findings = advice.build(self._context())
        owner = next(finding for finding in findings if finding.id == "one-timer")
        self.assertIn("Tick", owner.title, "the fixture's biggest kept timer")
        self.assertEqual(owner.confidence, "heuristic", "a name rule, and it says so")
        self.assertTrue(owner.next_command.startswith("ueia sources fx.utrace --filter"),)

    def test_the_missing_channels_are_findings_rather_than_silence(self) -> None:
        findings = advice.build(self._context())
        ids = self._ids(findings)
        self.assertIn("gpu-unknown", ids, "no GpuProfiler channel in the fixture")
        self.assertIn("task-unknown", ids, "nor TaskTrace")

    def test_the_ranking_is_severity_then_confidence_then_effort(self) -> None:
        findings = advice.build(self._context())
        ranks = [finding.rank() for finding in findings]
        self.assertEqual(ranks, sorted(ranks), "the documented order, as a property of the list")
        self.assertEqual(findings[0].severity, "high")
        self.assertLess(findings[0].rank(), findings[-1].rank())

    def test_a_rule_can_be_dropped_and_an_unknown_id_is_refused(self) -> None:
        context = self._context()
        findings = advice.build(context, skip=("gpu-unknown", "task-unknown"))
        self.assertNotIn("gpu-unknown", self._ids(findings))
        self.assertNotIn("task-unknown", self._ids(findings))
        self.assertGreater(len(findings), 0, "dropping two rules drops two rules")
        with self.assertRaises(Exception) as caught:
            advice.build(context, skip=("no-such-rule",))
        self.assertIn("unknown: no-such-rule", str(caught.exception))

    def test_a_capture_that_cannot_answer_says_that_first(self) -> None:
        findings = advice.build(self._context(_no_frames()))
        ids = self._ids(findings)
        self.assertEqual(ids[0], "no-frames", "the most severe finding, and it is certain of why")
        self.assertIn("gpu-unknown", ids)

    def test_every_finding_carries_the_documented_fields(self) -> None:
        for finding in advice.build(self._context()):
            self.assertIn(finding.severity, advice.SEVERITY)
            self.assertIn(finding.effort, advice.EFFORT)
            self.assertIn(finding.confidence, advice.CONFIDENCE)
            self.assertTrue(finding.title)
            self.assertTrue(finding.evidence, "%s has no evidence" % (finding.id,))
            self.assertTrue(finding.next_command.startswith("ueia "),
                            "%s's next command is not a command" % (finding.id,))


def _no_frames() -> bytes:
    """A capture with frames declared and none recorded: `no-frames` must be the first finding."""
    schema = (
        new_event_record(16, "$Trace", "NewTrace", [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                         flags=EVENT_FLAG_NOSYNC)
        + new_event_record(30, "Misc", "Something", [("Cycle", "u64")])
    )
    return build_trace(
        events_stream=schema,
        importants_stream=important_record(16, pack("u64", 1000000) + pack("u64", 1000000)),
        threads={2: event(30, pack("u64", 1000000))},
    )


class TestTheCommand(UeiaTestCase):
    """`ueia advice`: the prose, the table, the machine form, and the options it refuses."""

    def _capture(self) -> str:
        return str(self.write_capture(work_trace()))

    def test_the_table_indexes_the_findings_and_the_prose_explains_them(self) -> None:
        code, out, _err = self.run_cli(["advice", self._capture(), "--jobs", "1"])
        self.assertEqual(code, 0, "advice always has something to say")
        self.assertIn("budget    : 60 FPS = 16.667 ms (the default: no --budget was given)", out)
        self.assertIn("next command", out, "the table's own header")
        self.assertIn("over-budget", out)
        self.assertIn("next    : ueia summary", out)
        self.assertIn("evidence:", out)

    def test_the_json_form_is_schema_versioned_with_a_meta_block(self) -> None:
        code, out, _err = self.run_cli(["advice", self._capture(), "--jobs", "1",
                                        "--format", "json"])
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["schema"], "ueia.advice/1")
        meta = document["meta"]
        self.assertEqual(len(meta["capture"]["sha256"]), 64, "the cache key's own digest")
        self.assertEqual(meta["budget"]["label"], "60 FPS = 16.667 ms")
        self.assertFalse(meta["budget"]["given"])
        self.assertEqual(meta["executables"], [], "this report wraps no engine program, said so")
        findings = {item["id"]: item for item in document["findings"]}
        self.assertIn("over-budget", findings)
        for item in document["findings"]:
            self.assertEqual(sorted(item),
                             ["category", "confidence", "effort", "evidence", "id",
                              "next_command", "severity", "title", "where"])

    def test_skip_and_budget_are_honoured(self) -> None:
        code, out, _err = self.run_cli(["advice", self._capture(), "--jobs", "1", "--skip",
                                        "one-timer,budget-default", "--budget", "30"])
        self.assertEqual(code, 0)
        self.assertNotIn("[high/heuristic/medium] `FrameTime`", out)
        self.assertNotIn("no goal was given", out)
        self.assertIn("budget    : 30 FPS", out)

    def test_a_bad_format_or_a_bad_rule_id_is_a_usage_error(self) -> None:
        code, _out, err = self.run_cli(["advice", self._capture(), "--format", "yaml"])
        self.assertEqual(code, 2)
        self.assertIn("--format is one of table|csv|markdown|json", err)
        code, _out, err = self.run_cli(["advice", self._capture(), "--skip", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown: nope", err)


class TestTheRealCaptures(UeiaTestCase):
    """The registered captures: what they recommend, and the rule that must not fire."""
    #: This class reads a registered capture: the cache beside it is content-keyed.
    use_cache = True


    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = goldens.capture_paths()
        cls._models: Dict[str, Dict[str, Any]] = {}

    def _model(self, key: str) -> Dict[str, Any]:
        path = self.paths.get(key)
        if path is None or not path.is_file():
            self.skipTest("the %s capture is not on this machine" % (key,))
        model = self._models.get(key)
        if model is None:
            import commands

            _view, loaded, _cached = commands.load_model(str(path))
            model = cast(Dict[str, Any], loaded)
            self._models[key] = model
        return model

    def _findings(self, key: str) -> Dict[str, advice.Finding]:
        context = advice.context(self._model(key), BUDGET)
        return {finding.id: finding for finding in advice.build(context)}

    def test_the_editor_session_is_capped_and_serial(self) -> None:
        findings = self._findings("editor-pie-1")
        self.assertIn("frame-cap", findings)
        self.assertIn("2 x 16.667 ms", findings["frame-cap"].title,
                      "55% of the frames sit on two display periods")
        self.assertEqual(findings["frame-cap"].confidence, "heuristic")
        self.assertIn("over-budget", findings)
        self.assertIn("workers-idle", findings)
        self.assertIn("93.0%", findings["workers-idle"].title)
        self.assertIn("one-timer", findings)
        self.assertEqual(findings["one-timer"].where, "Source/Editor/UnrealEd/Private/PlayLevel.cpp:2572")
        self.assertNotIn("gpu-unknown", findings, "the editor capture has the GPU channel")

    def test_the_game_capture_is_not_called_throttled(self) -> None:
        """905 ms frames are 54 display periods: the cap rule must not read that as a cap."""
        findings = self._findings("game-pc-2")
        self.assertNotIn("frame-cap", findings)
        self.assertIn("gpu-unknown", findings, "and it has no GPU channel to check against")
        self.assertIn("bound-unexplained", findings)

    def test_the_frame_less_capture_says_what_it_cannot_answer(self) -> None:
        findings = self._findings("viewer-pc-3")
        self.assertEqual(sorted(findings), ["budget-default", "gpu-unknown", "no-frames",
                                            "task-unknown"])
        self.assertEqual(findings["no-frames"].severity, "high")
        self.assertEqual(findings["no-frames"].confidence, "unknown")


if __name__ == "__main__":  # pragma: no cover - unittest discovery runs it
    unittest.main()
