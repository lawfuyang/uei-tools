"""The commands: exit codes, output shapes, caps, formats and determinism."""

from __future__ import annotations

import os
import unittest
from typing import List
from unittest import mock

from testcase import UeiaTestCase

import cache
import commands
import container
import shapes
from fixtures import demo_trace


class TestRowCommands(UeiaTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.capture = str(self.write_capture(demo_trace()))

    def test_info_reports_the_file(self) -> None:
        code, out, _err = self.run_cli(["info", self.capture])
        self.assertEqual(code, 0)
        self.assertIn("magic        : 2CRT", out)
        self.assertIn("protocol     : 7", out)
        self.assertIn("stream end   : offset", out)
        self.assertIn("(exact)", out)
        self.assertIn("threads      : 1 thread id(s) with packets", out)

    def test_info_refuses_options(self) -> None:
        code, _out, err = self.run_cli(["info", self.capture, "--limit", "1"])
        self.assertEqual(code, 2)
        self.assertIn("info takes no options", err)

    def test_packets_table_and_cap(self) -> None:
        code, out, _err = self.run_cli(["packets", self.capture, "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertIn("packets: 3 (showing 1)", out)
        self.assertIn("index", out)
        self.assertIn("raw", out)

    def test_packets_all_by_default_here(self) -> None:
        _code, out, _err = self.run_cli(["packets", self.capture])
        self.assertIn("packets: 3 (showing 3)", out)

    def test_packets_tid_filter(self) -> None:
        _code, out, _err = self.run_cli(["packets", self.capture, "--tid", "0"])
        self.assertIn("Events", out)
        self.assertIn("packets: 1 (showing 1)", out)

    def test_csv_form_puts_prose_on_stderr(self) -> None:
        code, out, err = self.run_cli(["packets", self.capture, "--format", "csv"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "index,offset,size,decoded,tid,form")
        self.assertTrue(out.splitlines()[1].startswith("0,"))
        self.assertIn("packets: 3", err)

    def test_markdown_form_is_a_table(self) -> None:
        code, out, err = self.run_cli(["threads", self.capture, "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("| tid |"))
        self.assertIn("threads: 1", err)

    def test_schema_lists_the_vocabulary(self) -> None:
        code, out, _err = self.run_cli(["schema", self.capture])
        self.assertEqual(code, 0)
        self.assertIn("event types: 7", out)
        self.assertIn("CpuProfiler.EventSpec", out)
        self.assertIn("Name AnsiString", out)

    def test_schema_filter_and_count_column(self) -> None:
        _code, out, _err = self.run_cli(["schema", self.capture, "--filter", "misc."])
        self.assertIn("event types: 7 (showing 2)", out)
        self.assertIn("Misc.BeginFrame", out)
        self.assertNotIn("CpuProfiler.EventSpec", out)

    def test_threads_row(self) -> None:
        _code, out, _err = self.run_cli(["threads", self.capture])
        self.assertIn("threads: 1 (1 named by the capture)", out)
        self.assertIn("Game", out)
        self.assertIn("batches", out)

    def test_timers_rows(self) -> None:
        code, out, _err = self.run_cli(["timers", self.capture])
        self.assertEqual(code, 0)
        self.assertIn("timer specs: 1 (showing 1; 1 with file:line)", out)
        self.assertIn("Tick", out)
        self.assertIn("Game.cpp", out)
        self.assertIn("91", out)

    def test_timers_filter(self) -> None:
        _code, out, _err = self.run_cli(["timers", self.capture, "--filter", "nothing"])
        self.assertIn("timer specs: 1 (showing 0", out)

    def test_frames_rows_and_seconds(self) -> None:
        code, out, _err = self.run_cli(["frames", self.capture])
        self.assertEqual(code, 0)
        self.assertIn("frames: 1 (showing 1); unpaired begins 0, ends 0", out)
        self.assertIn("0.300", out)  # end cycle 1300, start cycle 1000, 1 MHz

    def test_every_row_command_takes_every_format(self) -> None:
        for command in ("packets", "schema", "threads", "timers", "frames"):
            for fmt in ("table", "csv", "markdown"):
                code, _out, err = self.run_cli([command, self.capture, "--format", fmt])
                self.assertEqual(code, 0, err)

    def test_unknown_option_is_exit_two(self) -> None:
        code, _out, err = self.run_cli(["schema", self.capture, "--nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown option", err)

    def test_bad_format_is_exit_two(self) -> None:
        code, _out, err = self.run_cli(["schema", self.capture, "--format", "yaml"])
        self.assertEqual(code, 2)
        self.assertIn("--format", err)

    def test_missing_capture_is_exit_one(self) -> None:
        code, _out, err = self.run_cli(["info", str(self.dir / "nope.utrace")])
        self.assertEqual(code, 1)
        self.assertIn("cannot read capture", err)

    def test_unknown_command_is_exit_two(self) -> None:
        code, out, err = self.run_cli(["frobnicate", self.capture])
        self.assertEqual(code, 2)
        self.assertIn("unknown command", err)
        self.assertIn("Usage:", out)

    def test_no_arguments_prints_the_commands(self) -> None:
        code, out, _err = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("verify <capture>", out)

    def test_output_is_byte_identical_between_runs(self) -> None:
        first = self.run_cli(["schema", self.capture, "--format", "csv"])
        second = self.run_cli(["schema", self.capture, "--format", "csv"])
        self.assertEqual(first, second)
        first_info = self.run_cli(["info", self.capture])
        second_info = self.run_cli(["info", self.capture])
        self.assertEqual(first_info, second_info)


class TestTheLazyView(UeiaTestCase):
    """`CaptureView.packets`: walked when it is asked for, once, and not by commands that never look.

    The walk is 0.25 s over the corpus -- half of what a warm command used to spend -- and seven of
    the commands never touch the table, so it is a property rather than a field of `load_view`.
    """

    def test_reading_the_view_does_not_walk_the_packets(self) -> None:
        capture = self.write_capture(demo_trace())
        with mock.patch.object(container, "walk_packets",
                               wraps=container.walk_packets) as walk:
            view = commands.load_view(str(capture))
            self.assertEqual(walk.call_count, 0, "only reading the file and the header")
            self.assertTrue(view.data)
        with mock.patch.object(container, "walk_packets",
                               wraps=container.walk_packets) as walk:
            packets = view.packets
            self.assertEqual(walk.call_count, 1)
            self.assertTrue(packets)
            self.assertIs(view.packets, packets, "and the answer is kept, not recomputed")
            self.assertEqual(walk.call_count, 1)

    def test_the_anomalies_come_from_the_same_walk(self) -> None:
        capture = self.write_capture(demo_trace())
        view = commands.load_view(str(capture))
        self.assertEqual(view.packet_anomalies, [], "asking for them walks, and finds none")
        self.assertEqual(view.packet_anomalies, [])

    def test_a_cached_model_never_walks_the_packet_table(self) -> None:
        """The point of the property: a warm command is answered from the cache, table untouched."""
        capture = self.write_capture(demo_trace())
        self.addCleanup(os.environ.__setitem__, shapes.ENV_NO_CACHE, "1")
        os.environ.pop(shapes.ENV_NO_CACHE, None)   # this test wants the cache, unlike the others
        with mock.patch.object(container, "walk_packets", wraps=container.walk_packets) as walk:
            _view, first, cached_first = commands.load_model(str(capture))
            self.assertFalse(cached_first)
            walked_cold = walk.call_count
            _view, second, cached_second = commands.load_model(str(capture))
        self.assertEqual(second, first, "the same model, from the cache")
        self.assertTrue(cached_second)
        self.assertEqual(walk.call_count, walked_cold,
                         "the second load did not walk the packets, the first had to")


class TestVerifyAndParse(UeiaTestCase):
    def test_verify_on_an_intact_fixture(self) -> None:
        capture = str(self.write_capture(demo_trace()))
        code, out, _err = self.run_cli(["verify", capture])
        self.assertEqual(code, 0)
        self.assertIn("findings  : 0 error(s)", out)
        self.assertIn("schema    : 7 type(s) from 7 record(s), 0 redefinition(s)", out)
        self.assertIn("serials   : 3 carried", out)

    def test_verify_fails_on_a_corrupt_packet(self) -> None:
        import container

        data = bytearray(demo_trace())
        header = container.parse_header(bytes(data))
        # the first packet's size: claim far more than the file holds
        data[header.first_packet_offset] = 0xFF
        data[header.first_packet_offset + 1] = 0xFF
        capture = str(self.write_capture(bytes(data)))
        code, out, _err = self.run_cli(["verify", capture])
        self.assertEqual(code, 1)
        self.assertIn("findings  : 1 error(s)", out)
        self.assertIn("truncated-packet", out)

    def test_parse_reports_the_model_and_the_cache(self) -> None:
        os.environ.pop(shapes.ENV_NO_CACHE, None)
        capture = str(self.write_capture(demo_trace()))
        code, out, _err = self.run_cli(["parse", capture])
        self.assertEqual(code, 0)
        self.assertIn("cache    : stored", out)
        self.assertIn("model    : 7 type(s), 1 thread(s), 1 timer spec(s)", out)
        self.assertIn("duration : ", out)
        self.assertTrue(cache.cache_path(self.dir / "fixture.utrace").exists())
        code, out, _err = self.run_cli(["parse", capture])
        self.assertEqual(code, 0)
        self.assertIn("cache    : reused", out)

    def test_parse_says_so_when_caching_is_off(self) -> None:
        capture = str(self.write_capture(demo_trace()))
        code, out, _err = self.run_cli(["parse", capture])
        self.assertEqual(code, 0)
        self.assertIn("cache    : disabled ($UEI_NO_CACHE)", out)
        self.assertFalse(cache.cache_path(self.dir / "fixture.utrace").exists())

    def test_cache_command_reports_and_clears(self) -> None:
        os.environ.pop(shapes.ENV_NO_CACHE, None)
        capture = str(self.write_capture(demo_trace()))
        code, out, _err = self.run_cli(["cache", capture])
        self.assertEqual(code, 0)
        self.assertIn("exists   : no", out)
        self.run_cli(["parse", capture])
        _code, out, _err = self.run_cli(["cache", capture])
        self.assertIn("exists   : yes", out)
        self.assertIn("matches  : yes", out)
        code, out, _err = self.run_cli(["cache", capture, "--clear"])
        self.assertEqual(code, 0)
        self.assertIn("cache    : removed", out)
        self.assertFalse(cache.cache_path(self.dir / "fixture.utrace").exists())


class TestSelftestCommand(UeiaTestCase):
    def test_no_matching_test_is_exit_two(self) -> None:
        code, out, _err = self.run_cli(["selftest", "-k", "zzz-no-such-test-zzz"])
        self.assertEqual(code, 2)
        self.assertIn("no test matched", out)

    def test_bad_option_is_exit_two(self) -> None:
        code, _out, err = self.run_cli(["selftest", "--nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown selftest option", err)

    def test_missing_pattern_is_exit_two(self) -> None:
        code, _out, err = self.run_cli(["selftest", "-k"])
        self.assertEqual(code, 2)
        self.assertIn("-k needs a pattern", err)


class TestTheQuickSelection(UeiaTestCase):
    """`selftest --quick`: which tests it leaves out, and that it never guesses quietly.

    The selection is read from the test's own class (`testcase.UeiaTestCase.corpus`), so the marker
    lives beside the class that reads a capture. A class that *forgets* the marker is run -- the
    failure mode to avoid is a test that stops being exercised without anyone noticing.
    """

    def _tests(self) -> List[unittest.TestCase]:
        class Plain(unittest.TestCase):
            def test_plain(self) -> None:
                pass

        class Real(UeiaTestCase):
            corpus = True

            def test_real(self) -> None:
                pass

        return [Plain("test_plain"), Real("test_real")]

    def test_it_keeps_the_hermetic_tests_and_counts_the_corpus_ones(self) -> None:
        kept, left_out = commands._without_corpus(self._tests())
        self.assertEqual(len(kept), 1)
        self.assertIn("test_plain", kept[0].id(), "the marked class is the only one left out")
        self.assertEqual(left_out, 1)

    def test_nothing_to_leave_out_is_not_an_error(self) -> None:
        class Plain(unittest.TestCase):
            def test_only(self) -> None:
                pass

        kept, left_out = commands._without_corpus([Plain("test_only")])
        self.assertEqual(len(kept), 1)
        self.assertEqual(left_out, 0)


if __name__ == "__main__":
    unittest.main()
