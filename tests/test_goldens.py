"""The corpus harness: redaction, labels, and the exit-code contract."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Any, List
from unittest import mock

from testcase import UeiaTestCase

import cache
import goldens
from fixtures import demo_trace

KEY = "fixture-1"


class TestRedact(UeiaTestCase):
    def test_paths_and_names_become_the_placeholder(self) -> None:
        capture = self.dir / "SomeCapture.utrace"
        text = "capture : %s\nname: %s" % (capture, capture.name)
        redacted = goldens.redact(text, capture)
        self.assertNotIn("SomeCapture", redacted)
        self.assertNotIn(str(self.dir), redacted)
        self.assertEqual(redacted.count("<capture>"), 2)


class TestTheRunner(UeiaTestCase):
    """The command line the harness builds -- the bug that made every transcript the help text."""

    def test_the_command_name_comes_first_and_the_capture_after_it(self) -> None:
        seen: List[List[str]] = []

        def fake_run(command: List[str], **kwargs: Any) -> Any:
            seen.append(list(command))
            return subprocess.CompletedProcess(command, 0, "out", "")

        with mock.patch.object(goldens.subprocess, "run", side_effect=fake_run):
            code, stdout = goldens._run_command(
                "threads", ("--format", "csv"), self.dir / "x.utrace")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "out")
        self.assertEqual(seen[0][1:], [
            str(goldens.REPO_ROOT / "src" / "py" / "ueia.py"),
            "threads",
            str(self.dir / "x.utrace"),
            "--format",
            "csv",
        ])

    def test_no_pinned_command_is_missing_its_command_name(self) -> None:
        """A pinned entry whose argv *is* the command would run the help text again."""
        for name, argv in goldens.PINNED_COMMANDS:
            self.assertNotIn(name, argv, "%s: the argv already carries the command" % (name,))
            self.assertFalse(argv[:1] == ("--",), name)
        self.assertEqual([name for name, _argv in goldens.PINNED_COMMANDS][:2], ["info", "verify"])


class TestLabelFacts(UeiaTestCase):
    def test_the_demo_capture_measures_as_expected(self) -> None:
        capture = self.write_capture(demo_trace())
        facts = goldens.label_facts(capture)
        self.assertEqual(facts["packets"], 3)
        self.assertEqual(facts["packets_raw"], 3)
        self.assertEqual(facts["packets_lz4"], 0)
        self.assertEqual(facts["packets_sync"], 0)
        self.assertEqual(facts["protocol"], 7)
        self.assertEqual(facts["schema_types"], 7)
        self.assertEqual(facts["events_stream_packets"], 1)
        self.assertEqual(facts["importants_stream_packets"], 1)
        self.assertEqual(facts["threads_with_packets"], 1)
        self.assertEqual(facts["packet_anomalies"], 0)
        self.assertEqual(facts["stream_anomalies"], 0)


class TestHarness(UeiaTestCase):
    """The whole harness against a scratch corpus shaped like the real one."""

    def setUp(self) -> None:
        super().setUp()
        self.goldens_dir = self.dir / "goldens"
        self.goldens_dir.mkdir()
        self.captures_file = self.goldens_dir / "captures.json"
        self.local_file = self.goldens_dir / "captures.local.json"
        self.labels_dir = self.goldens_dir / "labels"
        self.labels_dir.mkdir()
        self.transcripts_dir = self.goldens_dir / "local"
        self.patches = [
            mock.patch.object(goldens, "CAPTURES_FILE", self.captures_file),
            mock.patch.object(goldens, "CAPTURES_LOCAL_FILE", self.local_file),
            mock.patch.object(goldens, "LABELS_DIR", self.labels_dir),
            mock.patch.object(goldens, "TRANSCRIPTS_DIR", self.transcripts_dir),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _write_corpus(self) -> Path:
        capture = self.write_capture(demo_trace())
        identity = cache.capture_identity(capture)
        self.captures_file.write_text(
            json.dumps({KEY: {"sha256": identity["sha256"], "size": identity["size"]}}),
            encoding="utf-8",
        )
        self.local_file.write_text(json.dumps({KEY: str(capture)}), encoding="utf-8")
        facts = goldens.label_facts(capture)
        self.labels_dir.joinpath(KEY + ".json").write_text(
            json.dumps({
                "capture": {"sha256": identity["sha256"]},
                "facts": {"packets": facts["packets"], "schema_types": facts["schema_types"]},
            }),
            encoding="utf-8",
        )
        return capture

    def test_no_capture_present_means_nothing_to_compare(self) -> None:
        self.captures_file.write_text(json.dumps({KEY: {}}), encoding="utf-8")
        code, out, _err = self.run_cli(["goldens", "--check"])
        self.assertEqual(code, 2)
        self.assertIn("not present", out)
        self.assertIn("nothing to compare", out)

    def test_write_then_check_then_tamper(self) -> None:
        self._write_corpus()
        code, out, _err = self.run_cli(["goldens", "--write"])
        self.assertEqual(code, 0, out)
        transcripts = sorted(self.transcripts_dir.joinpath(KEY).glob("*.txt"))
        self.assertEqual(len(transcripts), len(goldens.PINNED_COMMANDS))
        code, out, _err = self.run_cli(["goldens", "--check"])
        self.assertEqual(code, 0, out)
        self.assertIn("compared and matched", out)
        transcripts[0].write_text("# exit=0\nnot what the tool says\n", encoding="utf-8")
        code, out, _err = self.run_cli(["goldens", "--check"])
        self.assertEqual(code, 1)
        self.assertIn("differs", out)

    def test_transcripts_carry_no_paths(self) -> None:
        capture = self._write_corpus()
        self.run_cli(["goldens", "--write"])
        seen_placeholder = 0
        for transcript in self.transcripts_dir.joinpath(KEY).glob("*.txt"):
            text = transcript.read_text(encoding="utf-8")
            self.assertNotIn(str(self.dir), text)
            self.assertNotIn(capture.name, text)
            # the prose transcripts name the capture (redacted to the placeholder); the CSV ones are
            # the table alone, which is why this is counted rather than asserted per file
            seen_placeholder += text.count("<capture>")
        self.assertGreaterEqual(seen_placeholder, 2, "no transcript carried the placeholder at all")

    def test_a_wrong_label_is_a_problem(self) -> None:
        capture = self._write_corpus()
        identity = cache.capture_identity(capture)
        self.labels_dir.joinpath(KEY + ".json").write_text(
            json.dumps({"capture": {"sha256": identity["sha256"]}, "facts": {"packets": 9999}}),
            encoding="utf-8",
        )
        self.run_cli(["goldens", "--write"])
        code, out, _err = self.run_cli(["goldens", "--check"])
        self.assertEqual(code, 1)
        self.assertIn("label packets says 9999", out)

    def test_a_different_file_than_the_label_says_is_a_problem(self) -> None:
        self._write_corpus()
        self.labels_dir.joinpath(KEY + ".json").write_text(
            json.dumps({"capture": {"sha256": "AB" * 32}, "facts": {}}), encoding="utf-8"
        )
        self.run_cli(["goldens", "--write"])
        code, out, _err = self.run_cli(["goldens", "--check"])
        self.assertEqual(code, 1)
        self.assertIn("not the labelled capture", out)

    def test_unknown_key_is_an_error(self) -> None:
        self._write_corpus()
        code, _out, err = self.run_cli(["goldens", "--check", "--capture", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("no such capture key", err)

    def test_check_limits_to_one_capture(self) -> None:
        self._write_corpus()
        self.captures_file.write_text(
            json.dumps({KEY: {}, "other": {}}), encoding="utf-8"
        )
        self.run_cli(["goldens", "--write", "--capture", KEY])
        code, out, _err = self.run_cli(["goldens", "--check", "--capture", KEY])
        self.assertEqual(code, 0, out)
        self.assertNotIn("other", out)


if __name__ == "__main__":
    unittest.main()
