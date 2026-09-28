"""The `csv` family: the engine's toolbox wrapped, and the one bridge we write ourselves.

The wrappers are tested with `toolrun.run_tool` patched, because the executables are the engine's
and not every machine has them -- what *our* code owns is the argv it builds, the exit codes it
passes through, and the lines it reports. `csv from-trace` is ours end to end, so it is tested
against a fixture capture whose CSV Profiler events are built here, and then -- when this machine
has an engine tree -- against the engine's own `csvinfo`, which must be able to parse what we write.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any, List, Tuple
from unittest import mock

from testcase import UeiaTestCase

import engine
import toolrun
from fixtures import (
    EVENT_FLAG_MAYBE_HAS_AUX,
    EVENT_FLAG_NOSYNC,
    build_trace,
    event,
    important_aux_block,
    important_record,
    new_event_record,
    pack,
)

IMPORTANT_AUX = 0x2 | 0x4  # EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX
NO_FRAMES = 0x4
FRAME_TYPE = 0

#: The CsvTools executables this machine's engine tree has, if it has one -- the real half's gate.
REAL_ENGINE = engine.resolve(os.environ.get(engine.ENV_ENGINE_DIR), env={}) if os.environ.get(
    engine.ENV_ENGINE_DIR) else None


def _csv_schema() -> bytes:
    return (
        new_event_record(16, "$Trace", "NewTrace",
                         [("StartCycle", "u64"), ("CycleFrequency", "u64"), ("Endian", "u16"),
                          ("PointerSize", "u8")], flags=EVENT_FLAG_NOSYNC)
        + new_event_record(17, "$Trace", "ThreadInfo",
                           [("ThreadId", "u32"), ("SystemId", "u32"), ("SortHint", "i32"),
                            ("Name", "s")], flags=IMPORTANT_AUX)
        + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")],
                           flags=EVENT_FLAG_NOSYNC)
        + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")],
                           flags=EVENT_FLAG_NOSYNC)
        + new_event_record(27, "CsvProfiler", "RegisterCategory", [("Index", "u32"), ("Name", "s")],
                           flags=IMPORTANT_AUX)
        + new_event_record(28, "CsvProfiler", "DefineDeclaredStat",
                           [("StatId", "u32"), ("CategoryIndex", "u32"), ("Name", "s")],
                           flags=IMPORTANT_AUX)
        + new_event_record(31, "CsvProfiler", "BeginStat", [("StatId", "u64"), ("Cycle", "u64")],
                           flags=EVENT_FLAG_NOSYNC)
        + new_event_record(32, "CsvProfiler", "EndStat", [("StatId", "u64"), ("Cycle", "u64")],
                           flags=EVENT_FLAG_NOSYNC)
        + new_event_record(33, "CsvProfiler", "CustomStatInt",
                           [("Cycle", "u64"), ("StatId", "u64"), ("Value", "i32"),
                            ("OpType", "u8")], flags=EVENT_FLAG_NOSYNC)
        + new_event_record(34, "CsvProfiler", "Event",
                           [("Cycle", "u64"), ("CategoryIndex", "u32"), ("Text", "s")],
                           flags=EVENT_FLAG_NOSYNC | EVENT_FLAG_MAYBE_HAS_AUX)
    )


def _csv_importants() -> bytes:
    """One category and one stat, as a real capture declares them (fixed fields + aux strings)."""
    return (
        important_record(16, pack("u64", 1000) + pack("u64", 1000000) + pack("u16", 0x524D)
                         + pack("u8", 8))
        + important_record(17, pack("u32", 2) + pack("u32", 4242) + pack("i32", 1)
                           + important_aux_block(3, b"GameThread"))
        + important_record(27, pack("u32", 0) + important_aux_block(1, b"Game"))
        + important_record(28, pack("u32", 7) + pack("u32", 0) + important_aux_block(2, b"FrameTime"))
        + important_record(28, pack("u32", 9) + pack("u32", 0) + important_aux_block(2, b"DrawCalls"))
    )


def _csv_thread(values: bool = True) -> bytes:
    """Two frames; with `values`, a timed stat of 1.0 ms and 0.5 ms, a custom stat and an event."""
    frames = event(22, pack("u64", 1000) + pack("u8", FRAME_TYPE))
    frames += event(23, pack("u64", 4000) + pack("u8", FRAME_TYPE))
    frames += event(22, pack("u64", 4000) + pack("u8", FRAME_TYPE))
    frames += event(23, pack("u64", 6000) + pack("u8", FRAME_TYPE))
    if not values:
        return frames
    return (
        event(22, pack("u64", 1000) + pack("u8", FRAME_TYPE))
        + event(31, pack("u64", 7) + pack("u64", 1100))
        + event(32, pack("u64", 7) + pack("u64", 2100))     # 1,000 cycles = 1.0 ms at 1 GHz
        + event(33, pack("u64", 2200) + pack("u64", 9) + pack("i32", 42) + pack("u8", 0))
        + event(34, pack("u64", 2300) + pack("u32", 0), aux=[(2, b"LevelLoad")], maybe_aux=True)
        + event(23, pack("u64", 4000) + pack("u8", FRAME_TYPE))
        + event(22, pack("u64", 4000) + pack("u8", FRAME_TYPE))
        + event(31, pack("u64", 7) + pack("u64", 4100))
        + event(32, pack("u64", 7) + pack("u64", 4600))     # 500 cycles = 0.5 ms
        + event(23, pack("u64", 6000) + pack("u8", FRAME_TYPE))
    )


def _csv_trace(values: bool = True) -> bytes:
    return build_trace(
        events_stream=_csv_schema(),
        importants_stream=_csv_importants(),
        threads={2: _csv_thread(values)},
    )


def _fake_result(exe: Path, exit_code: int = 0, stdout: str = "", stderr: str = "") -> toolrun.ToolResult:
    return toolrun.ToolResult(
        exe=str(exe), sha256="ab" * 32, size=1234, argv=[str(exe)], cwd=str(exe.parent),
        exit=exit_code, stdout=stdout, stderr=stderr, seconds=0.01,
    )


class TestFromTrace(UeiaTestCase):
    """`csv from-trace`: our own writer, against the format the engine's readers accept."""

    def _synthesize(self, values: bool = True) -> Tuple[int, str, Path]:
        capture = self.write_capture(_csv_trace(values))
        out = self.dir / "synthesized.csv"
        code, text, _err = self.run_cli(["csv", "from-trace", str(capture), "--out", str(out)])
        return code, text, out

    def test_the_written_file_has_the_shape_the_readers_parse(self) -> None:
        code, text, out = self._synthesize()
        self.assertEqual(code, 0, text)
        lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "EVENTS,Game/DrawCalls,GameThread/Game/FrameTime",
                         "series are sorted, so the file is the same however the capture ordered them")
        self.assertEqual(len(lines), 4, "a header, two frames and one metadata line")
        self.assertTrue(lines[3].startswith("[EventTimestamps],1,[FramesFrom],tid 2"))
        self.assertIn("[SynthesizedBy],ueia", lines[3])
        for row in lines[1:3]:
            self.assertEqual(len(row.split(",")), 3, "one events column and two series")

    def test_the_values_are_milliseconds_and_the_events_carry_their_time(self) -> None:
        _code, _text, out = self._synthesize()
        rows = [row.split(",") for row in out.read_text(encoding="utf-8").splitlines()[1:3]]
        self.assertEqual(rows[0][0], "Game/LevelLoad##0.001300", "seconds since the capture's start")
        self.assertEqual(rows[0][1], "42", "a custom stat's value, as the capture set it")
        self.assertEqual(rows[0][2], "1", "1,000 cycles at 1 GHz is a millisecond, as `%.0f`")
        self.assertEqual(rows[1][2], "0.5000")
        self.assertEqual(rows[1][1], "0", "no value in a frame is a zero, never a blank")

    def test_a_capture_with_definitions_but_no_values_is_skipped_not_empty(self) -> None:
        code, text, out = self._synthesize(values=False)
        self.assertEqual(code, 2)
        self.assertIn("no per-frame values", text)
        self.assertIn("hint", text)
        self.assertFalse(out.exists(), "a skipped synthesis writes no file at all")

    def test_the_report_says_how_many_values_and_where_the_frames_came_from(self) -> None:
        _code, text, _out = self._synthesize()
        self.assertIn("2 definition(s), 2 series", text)
        self.assertIn("4 event(s) over 2 frame(s) from tid 2", text)
        self.assertIn("dropped  : 0 value(s)", text)
        self.assertIn("wrote    :", text)

    def test_out_is_required(self) -> None:
        capture = self.write_capture(_csv_trace())
        code, _text, err = self.run_cli(["csv", "from-trace", str(capture)])
        self.assertEqual(code, 2)
        self.assertIn("--out", err)


class TestTheDispatch(UeiaTestCase):
    """The subcommand surface: what is offered, and what an unknown one does."""

    def test_no_subcommand_lists_them(self) -> None:
        code, _out, err = self.run_cli(["csv"])
        self.assertEqual(code, 2)
        for name in ("info", "split", "convert", "filter", "collate", "svg", "report",
                     "regressions", "from-trace"):
            self.assertIn(name, err)

    def test_an_unknown_subcommand_is_a_usage_error(self) -> None:
        code, _out, err = self.run_cli(["csv", "frobnicate"])
        self.assertEqual(code, 2)
        self.assertIn("unknown csv subcommand", err)

    def test_a_bad_engine_dir_is_a_usage_error(self) -> None:
        plain = self.dir / "not-an-engine"
        plain.mkdir()
        code, _out, err = self.run_cli(["csv", "info", "x.csv", "--engine-dir", str(plain)])
        self.assertEqual(code, 2)
        self.assertIn("is not an engine tree", err)


class TestTheWrappers(UeiaTestCase):
    """One argv per executable: our flags on the left, the exe's own spelling on the right."""

    def setUp(self) -> None:
        super().setUp()
        root = self.dir / "engine"
        binaries = root / "Engine" / "Binaries" / "DotNET" / "CsvTools"
        binaries.mkdir(parents=True, exist_ok=True)
        self.tools = binaries
        for name in ("csvinfo.exe", "CSVSplit.exe", "CsvConvert.exe", "CSVFilter.exe",
                     "CSVCollate.exe", "CSVToSVG.exe", "PerfreportTool.exe",
                     "RegressionsReport.exe"):
            (binaries / name).write_bytes(b"MZ")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(engine.ENV_ENGINE_DIR, None)
        self.calls: List[Any] = []
        self.patch = mock.patch.object(toolrun, "run_tool", side_effect=self._record)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _record(self, exe: Path, argv: List[str], **kwargs: Any) -> toolrun.ToolResult:
        self.calls.append((Path(exe).name, list(argv), kwargs.get("cwd")))
        for flag in ("-o", "-toJson", "-dumpContents"):
            if flag in argv:
                Path(argv[argv.index(flag) + 1]).write_bytes(b"stand-in output")
        return _fake_result(Path(exe), stdout="tool output\n")

    def _run(self, args: List[str]) -> Tuple[int, str, str]:
        return self.run_cli(["csv"] + args + ["--engine-dir", str((self.dir / "engine").resolve())])

    def _call(self) -> Tuple[str, List[str]]:
        self.assertEqual(len(self.calls), 1)
        name, argv, _cwd = self.calls[0]
        return name, argv

    def test_info_maps_its_flags_and_passes_the_json_through(self) -> None:
        code, out, _err = self._run(["info", "shape.csv", "--json", str((self.dir / "shape.json").resolve()),
                                     "--stat-filter", "LLM/*", "--show-averages", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "tool output\n", "the tool's stdout is the answer")
        name, argv = self._call()
        self.assertEqual(name, "csvinfo.exe")
        self.assertEqual(argv[0], str(Path("shape.csv").resolve()))
        self.assertIn("-quiet", argv)
        self.assertIn("-showAverages", argv)
        self.assertEqual(argv[argv.index("-statFilters") + 1], "LLM/*")
        self.assertEqual(argv[argv.index("-toJson") + 1], str((self.dir / "shape.json").resolve()))
        self.assertIn("wrote    :", _err)

    def test_split_requires_a_stat_and_maps_it(self) -> None:
        code, _out, err = self._run(["split", "a.csv"])
        self.assertEqual(code, 2)
        self.assertIn("--stat", err)
        code, _out, _err = self._run(["split", "a.csv", "--stat", "Map", "--delay", "5",
                                      "--virtual-events", "--out", str((self.dir / "b.csv").resolve())])
        self.assertEqual(code, 0)
        name, argv = self._call()
        self.assertEqual(name, "CSVSplit.exe")
        self.assertEqual(argv[:4], ["-csv", str(Path("a.csv").resolve()), "-splitStat", "Map"])
        self.assertEqual(argv[argv.index("-delay") + 1], "5")
        self.assertIn("-virtualEvents", argv)

    def test_convert_needs_its_input_and_format(self) -> None:
        code, _out, err = self._run(["convert", "--in", "a.csv"])
        self.assertEqual(code, 2)
        self.assertIn("--out-format", err)
        code, _out, _err = self._run(["convert", "--in", "a.csv", "--out-format", "bin",
                                      "--out", str((self.dir / "a.csv.bin").resolve()), "--compress", "2", "--verify",
                                      "--set-metadata", "map=Forest"])
        self.assertEqual(code, 0)
        name, argv = self._call()
        self.assertEqual(name, "CsvConvert.exe")
        self.assertEqual(argv[argv.index("-outFormat") + 1], "bin")
        self.assertEqual(argv[argv.index("-binCompress") + 1], "2")
        self.assertIn("-verify", argv)
        self.assertEqual(argv[argv.index("-setMetadata") + 1], "map=Forest")

    def test_filter_needs_stats_or_defaults(self) -> None:
        code, _out, err = self._run(["filter", "a.csv", "--out", "b.csv"])
        self.assertEqual(code, 2)
        self.assertIn("--stats", err)
        code, _out, _err = self._run(["filter", "a.csv", "--defaults", "--out", str((self.dir / "b.csv").resolve())])
        self.assertEqual(code, 0)
        name, argv = self._call()
        self.assertEqual(name, "CSVFilter.exe")
        self.assertIn("-defaults", argv)

    def test_collate_maps_the_list_and_the_outlier_filter(self) -> None:
        code, _out, _err = self._run(["collate", "--csvs", "a.csv;b.csv", "--average",
                                      "--outlier-stat", "FrameTime", "--outlier-threshold", "3",
                                      "--out", str((self.dir / "all.csv").resolve())])
        self.assertEqual(code, 0)
        name, argv = self._call()
        self.assertEqual(name, "CSVCollate.exe")
        self.assertIn("-avg", argv)
        self.assertEqual(argv[argv.index("-filterOutlierStat") + 1], "FrameTime")
        self.assertEqual(argv[argv.index("-filterOutlierThreshold") + 1], "3")
        self.assertEqual(argv[-2:], ["-o", str((self.dir / "all.csv").resolve())])

    def test_svg_needs_a_source_and_an_output(self) -> None:
        code, _out, err = self._run(["svg", "--stats", "FrameTime"])
        self.assertEqual(code, 2)
        self.assertIn("-csvs", err.replace("csv svg ", ""))
        code, _out, _err = self._run(["svg", "--csvs", "a.csv", "--stats", "FrameTime",
                                      "--out", str((self.dir / "g.svg").resolve()), "--update"])
        self.assertEqual(code, 0)
        name, argv = self._call()
        self.assertEqual(name, "CSVToSVG.exe")
        self.assertIn("-updatesvg", argv)
        self.assertEqual(argv[-2:], ["-o", str((self.dir / "g.svg").resolve())])

    def test_report_needs_an_input_and_an_output_directory(self) -> None:
        code, _out, _err = self._run(["report", "--csv", "a.csv", "--out", str((self.dir / "report").resolve()),
                                      "--type", "flythrough", "--summary-formats", "html,csv,json"])
        self.assertEqual(code, 0)
        name, argv = self._call()
        self.assertEqual(name, "PerfreportTool.exe")
        self.assertEqual(argv[argv.index("-reportType") + 1], "flythrough")
        self.assertEqual(argv[-2:], ["-o", str((self.dir / "report").resolve())])

    def test_regressions_needs_its_thresholds(self) -> None:
        code, _out, err = self._run(["regressions", "summary.csv", "--out", "out"])
        self.assertEqual(code, 2)
        self.assertIn("--thresholds", err)
        code, _out, _err = self._run(["regressions", "summary.csv", "--out", str((self.dir / "out").resolve()),
                                      "--thresholds", "t.json", "--test-name", "Forest"])
        self.assertEqual(code, 0)
        name, argv = self._call()
        self.assertEqual(name, "RegressionsReport.exe")
        self.assertEqual(argv[:2], ["-csvFile", str(Path("summary.csv").resolve())])
        self.assertEqual(argv[argv.index("-testName") + 1], "Forest")

    def test_a_tool_that_fails_is_our_exit_one_with_its_output_printed(self) -> None:
        self.patch.stop()
        with mock.patch.object(toolrun, "run_tool",
                               return_value=_fake_result(self.tools / "csvinfo.exe", exit_code=3,
                                                        stdout="", stderr="bad csv\n")):
            code, _out, err = self._run(["info", "a.csv"])
        self.assertEqual(code, 1)
        self.assertIn("bad csv", err)
        self.assertIn("exited 3", err)

    def test_no_engine_directory_is_skipped_with_the_hint(self) -> None:
        code, out, _err = self.run_cli(["csv", "info", "a.csv"])
        self.assertEqual(code, 2)
        self.assertIn("skipped: no engine directory", out)
        self.assertIn("--engine-dir", out)

    def test_a_tree_without_that_executable_is_skipped_and_names_it(self) -> None:
        (self.tools / "csvinfo.exe").unlink()
        code, out, _err = self._run(["info", "a.csv"])
        self.assertEqual(code, 2)
        self.assertIn("has no csvinfo.exe", out)


@unittest.skipUnless(REAL_ENGINE is not None and REAL_ENGINE.csvtools("csvinfo.exe") is not None,
                     "no engine tree with csvinfo here (set $UEI_ENGINE_DIR)")
class TestTheRealToolbox(UeiaTestCase):
    """The acceptance test: the engine's own `csvinfo` parses what `from-trace` writes."""

    def test_csvinfo_reads_our_synthesized_csv(self) -> None:
        assert REAL_ENGINE is not None, "the class only runs when a tree with csvinfo is here"
        capture = self.write_capture(_csv_trace())
        out = self.dir / "synthesized.csv"
        code, text, _err = self.run_cli(["csv", "from-trace", str(capture), "--out", str(out)])
        self.assertEqual(code, 0, text)
        json_path = self.dir / "info.json"
        code, out_text, err = self.run_cli([
            "csv", "info", str(out), "--json", str(json_path),
            "--engine-dir", str(REAL_ENGINE.root),
        ])
        self.assertEqual(code, 0, out_text + err)
        self.assertTrue(json_path.is_file(), "csvinfo -toJson wrote nothing")
        document = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertIn("GameThread/Game/FrameTime", json.dumps(document))


if __name__ == "__main__":
    unittest.main()
