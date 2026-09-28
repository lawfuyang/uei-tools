"""The LZ4 block decoder: our contract with the C library, and the library itself.

Two halves, split the way rdc-tools splits them. The logic -- what we ask the library for and
what we make of its answer -- is tested against a stand-in C function, so it needs no DLL and
runs on any machine. The library itself gets one class that **skips** when there is none to load:
`bin/ueia_lz4.dll` is a build artifact, not something a fresh checkout has, and a skip is
reported rather than hidden.

The stand-in has the real signature and writes through the destination pointer, because that is
the part of the call a pure-Python fake would quietly skip: the bytes have to come back out of the
buffer we allocated, not out of the fake's own return value.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import struct
import subprocess
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

from testcase import UeiaTestCase

import lz4
from fixtures import lz4_literal_block
from shapes import Lz4Error


def _writer(payload: bytes) -> Tuple[Callable[..., int], List[Tuple[int, int]]]:
    """A stand-in `LZ4_decompress_safe` that produces `payload`, and its call log."""
    calls: List[Tuple[int, int]] = []

    def fake(src: ctypes.c_void_p, dst: ctypes.c_void_p, src_size: int, dst_capacity: int) -> int:
        calls.append((src_size, dst_capacity))
        if len(payload) > dst_capacity:
            return -1
        ctypes.memmove(dst, payload, len(payload))
        return len(payload)

    return fake, calls


def _match_block(literals: bytes, offset: int, match_len: int, tail: bytes) -> bytes:
    """One raw LZ4 block: a literal run, a match, and the literal run a block must end with.

    The shape is dictated by what a *safe* decoder accepts, measured against the vendored one
    rather than guessed: the stored match nibble cannot be 0 (so `match_len` is at least 5), a
    block must end with enough literals (`LASTLITERALS`), and the match has to sit far enough from
    the end for the decoder's lookahead to stay inside the block (`MFLIMIT`). Both runs are capped
    at 14 bytes so no length extension is needed.
    """
    if not 5 <= match_len <= 18:
        raise ValueError("match_len must be 5..18: a stored nibble of 0 is refused, 15 extends")
    if not 5 <= len(tail) <= 14:
        raise ValueError("tail must be 5..14 bytes: the block must end with literals")
    if len(literals) > 14:
        raise ValueError("literals must be 0..14 bytes (no length extension here)")
    if not 1 <= offset <= 0xFFFF:
        raise ValueError("offset must be 1..65535")
    out = bytearray([(len(literals) << 4) | (match_len - 4)])
    out += literals
    out += struct.pack("<H", offset)
    out.append(len(tail) << 4)
    out += tail
    return bytes(out)


class TestTheCall(UeiaTestCase):
    """What `decompress_block` asks for, and what it makes of the answer."""

    def test_it_asks_for_exactly_the_declared_size_and_returns_what_was_written(self) -> None:
        fake, calls = _writer(b"decoded bytes")
        with mock.patch.object(lz4, "_c_function", return_value=fake):
            out = lz4.decompress_block(b"compressed", 13)
        self.assertEqual(out, b"decoded bytes")
        self.assertEqual(calls, [(10, 13)])

    def test_a_refusal_from_the_library_is_an_error_naming_both_sizes(self) -> None:
        def refuse(src: object, dst: object, src_size: int, dst_capacity: int) -> int:
            return -3

        with mock.patch.object(lz4, "_c_function", return_value=refuse):
            with self.assertRaises(Lz4Error) as caught:
                lz4.decompress_block(b"compressed", 13)
        self.assertIn("refused", str(caught.exception))
        self.assertIn("10", str(caught.exception))
        self.assertIn("13", str(caught.exception))

    def test_a_short_result_is_an_error_rather_than_a_short_answer(self) -> None:
        fake, _calls = _writer(b"short")
        with mock.patch.object(lz4, "_c_function", return_value=fake):
            with self.assertRaises(Lz4Error) as caught:
                lz4.decompress_block(b"compressed", 9)
        self.assertIn("decoded 5 bytes where the packet claims 9", str(caught.exception))

    def test_without_a_library_the_refusal_says_how_to_get_one(self) -> None:
        with mock.patch.object(lz4, "_c_function", return_value=None):
            with self.assertRaises(Lz4Error) as caught:
                lz4.decompress_block(b"compressed", 13)
        message = str(caught.exception)
        self.assertIn("ueia_lz4.dll", message)
        self.assertIn("lz4 --build", message)
        self.assertIn("UEI_LZ4_DLL", message)

    def test_the_named_library_comes_first_and_the_repos_build_second(self) -> None:
        with mock.patch.dict(os.environ, {"UEI_LZ4_DLL": os.path.join("X:", "named", "lz4.dll")}):
            candidates = lz4._candidates()
        self.assertEqual(candidates[0], os.path.join("X:", "named", "lz4.dll"))
        self.assertTrue(candidates[1].endswith(os.path.join("bin", "ueia_lz4.dll")))
        self.assertIn("lz4.dll", candidates)

    def test_without_the_variable_the_repos_build_is_first(self) -> None:
        os.environ.pop("UEI_LZ4_DLL", None)
        candidates = lz4._candidates()
        self.assertTrue(candidates[0].endswith(os.path.join("bin", "ueia_lz4.dll")))

    def test_an_empty_block_decodes_to_nothing(self) -> None:
        fake, calls = _writer(b"")
        with mock.patch.object(lz4, "_c_function", return_value=fake):
            self.assertEqual(lz4.decompress_block(b"", 0), b"")
        self.assertEqual(calls, [(0, 0)])


@unittest.skipUnless(lz4.available(), "no LZ4 library to load (build bin/ueia_lz4.dll)")
class TestTheLibrary(UeiaTestCase):
    """The library itself rather than a stand-in: `bin/ueia_lz4.dll`, or a system liblz4."""

    def test_a_literal_only_block_decodes_to_the_size_it_declares(self) -> None:
        payload = bytes(range(256)) * 4
        self.assertEqual(lz4.decompress_block(lz4_literal_block(payload), len(payload)), payload)

    def test_a_literal_run_walks_the_255_chain(self) -> None:
        payload = bytes((index * 7) & 0xFF for index in range(1000))
        self.assertEqual(lz4.decompress_block(lz4_literal_block(payload), len(payload)), payload)

    def test_a_match_copies_what_came_before(self) -> None:
        # "abcdef", then a match at offset 6 copying 8 bytes from the start: "abcdefab"
        tail = b"tail-tail-tail"
        block = _match_block(b"abcdef", offset=6, match_len=8, tail=tail)
        expected = b"abcdef" + b"abcdefab" + tail
        self.assertEqual(lz4.decompress_block(block, len(expected)), expected)

    def test_an_overlapping_match_repeats_a_pattern(self) -> None:
        # offset 1 with a match longer than it: LZ4's run-length case
        tail = b"tail-tail-tail"
        block = _match_block(b"x", offset=1, match_len=12, tail=tail)
        expected = b"x" + b"x" * 12 + tail
        self.assertEqual(lz4.decompress_block(block, len(expected)), expected)

    def test_an_offset_reaching_past_the_start_is_refused(self) -> None:
        tail = b"tail-tail-tail"
        block = _match_block(b"abc", offset=999, match_len=6, tail=tail)
        with self.assertRaises(Lz4Error) as caught:
            lz4.decompress_block(block, 3 + 6 + len(tail))
        self.assertIn("refused", str(caught.exception))

    def test_a_block_that_does_not_decode_to_the_declared_size_is_refused(self) -> None:
        payload = b"ten bytes!"
        block = lz4_literal_block(payload)
        with self.assertRaises(Lz4Error) as caught:
            lz4.decompress_block(block, len(payload) + 1)
        self.assertIn("decoded 10 bytes where the packet claims 11", str(caught.exception))

    def test_a_truncated_block_is_refused(self) -> None:
        payload = bytes(range(64))
        block = lz4_literal_block(payload)
        with self.assertRaises(Lz4Error):
            lz4.decompress_block(block[:-4], len(payload))

    def test_the_library_passes_the_checks_own_self_test(self) -> None:
        """`self_test`'s hand-built block against the real decoder: the check must not lie.

        The block is written out by hand in `lz4.py` rather than taken from the fixtures, so this
        is the test that says that block is *valid LZ4* and its expected answer is right.
        """
        ok, detail = lz4.self_test()
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "12 bytes decoded exactly")


def _tree(case: "UeiaTestCase", sources: Optional[Dict[str, bytes]] = None,
          dll: bytes = b"a fake library", stamp: Any = "built") -> Path:
    """A fixture repository root: the recipe files, a library, and (usually) a stamp for them."""
    root = case.dir / "tree"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "src" / "cpp" / "third_party" / "lz4").mkdir(parents=True, exist_ok=True)
    contents = sources if sources is not None else {
        "CMakeLists.txt": b"add_library(ueia_lz4 SHARED lz4.c)\n",
        "src/cpp/third_party/lz4/lz4.c": b"/* lz4 */\n",
        "src/cpp/third_party/lz4/lz4.h": b"/* lz4.h */\n",
    }
    for name, data in contents.items():
        (root / name).write_bytes(data)
    (root / "bin" / "ueia_lz4.dll").write_bytes(dll)
    if stamp == "built":
        stamp = {
            "stamp_format": 1,
            "library": "LZ4 v1.9.2",
            "flags": "LZ4_DLL_EXPORT=1 LZ4_FAST_DEC_LOOP=1",
            "sources": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                        for name in lz4.RECIPE_SOURCES},
            "dll_sha256": hashlib.sha256(dll).hexdigest(),
            "dll_size": len(dll),
        }
    if stamp is not None:
        (root / "bin" / "ueia_lz4.build.json").write_text(json.dumps(stamp), encoding="utf-8")
    return root


def _state(**overrides: Any) -> Dict[str, Any]:
    """A build state with everything current, for the tests that care about one field."""
    state: Dict[str, Any] = {
        "dll": str(lz4.dll_path()),
        "exists": True,
        "stamped": True,
        "stamp": None,
        "dll_current": True,
        "ours": True,
        "library": str(lz4.dll_path()),
        "version": "1.9.2",
        "current": True,
        "problems": [],
    }
    state.update(overrides)
    return state


def _runner(calls: List[Tuple[List[str], Optional[str]]], code: int = 0, stderr: str = "",
            after: Any = None) -> Callable[..., Any]:
    """A stand-in for `subprocess.run` that records the command lines it is given."""
    def run(argv: List[str], **kwargs: Any) -> Any:
        calls.append((list(argv), kwargs.get("cwd")))
        if after is not None:
            after()
        return subprocess.CompletedProcess(argv, code, "", stderr)
    return run


class TestTheRecipeCheck(UeiaTestCase):
    """What makes a built library current: the stamp, and the sources it names."""

    def test_a_matching_stamp_is_current_and_a_fixture_tree_is_never_the_library_in_use(self) -> None:
        state = lz4.build_state(_tree(self))
        self.assertTrue(state["exists"])
        self.assertTrue(state["stamped"])
        self.assertTrue(state["dll_current"])
        self.assertEqual(state["problems"], [])
        self.assertFalse(state["ours"], "the library this process loads is this repo's, not the fixture")

    def test_a_changed_source_makes_it_stale_and_says_which(self) -> None:
        root = _tree(self)
        (root / "src" / "cpp" / "third_party" / "lz4" / "lz4.c").write_bytes(b"/* changed */\n")
        state = lz4.build_state(root)
        self.assertEqual(state["problems"], ["src/cpp/third_party/lz4/lz4.c changed since the build"])
        self.assertFalse(state["dll_current"])

    def test_a_source_that_is_gone_is_named(self) -> None:
        root = _tree(self)
        (root / "src" / "cpp" / "third_party" / "lz4" / "lz4.h").unlink()
        self.assertEqual(lz4.build_state(root)["problems"],
                         ["src/cpp/third_party/lz4/lz4.h is gone"])

    def test_a_stamp_that_does_not_cover_every_source_is_not_a_stamp(self) -> None:
        root = _tree(self)
        stamp = json.loads((root / "bin" / "ueia_lz4.build.json").read_text(encoding="utf-8"))
        del stamp["sources"]["src/cpp/third_party/lz4/lz4.h"]
        (root / "bin" / "ueia_lz4.build.json").write_text(json.dumps(stamp), encoding="utf-8")
        self.assertEqual(lz4.build_state(root)["problems"],
                         ["the stamp does not cover src/cpp/third_party/lz4/lz4.h"])

    def test_a_stamp_from_another_format_is_refused(self) -> None:
        root = _tree(self)
        stamp = json.loads((root / "bin" / "ueia_lz4.build.json").read_text(encoding="utf-8"))
        stamp["stamp_format"] = 2
        (root / "bin" / "ueia_lz4.build.json").write_text(json.dumps(stamp), encoding="utf-8")
        self.assertEqual(lz4.build_state(root)["problems"][0], "the stamp is format 2, not 1")

    def test_a_dll_that_is_not_the_one_the_stamp_describes_is_stale(self) -> None:
        root = _tree(self)
        (root / "bin" / "ueia_lz4.dll").write_bytes(b"replaced behind the stamp's back")
        self.assertIn("the DLL is not the one the stamp describes", lz4.build_state(root)["problems"])

    def test_a_dll_with_no_stamp_beside_it_says_so(self) -> None:
        root = _tree(self, stamp=None)
        problems = lz4.build_state(root)["problems"]
        self.assertEqual(len(problems), 1)
        self.assertIn("there is no build stamp beside it", problems[0])
        self.assertFalse(lz4.build_state(root)["dll_current"])

    def test_an_unreadable_stamp_is_a_problem_rather_than_a_crash(self) -> None:
        root = _tree(self)
        (root / "bin" / "ueia_lz4.build.json").write_text("{not json", encoding="utf-8")
        self.assertIn("the build stamp is unreadable", lz4.build_state(root)["problems"][0])

    @unittest.skipUnless(lz4.stamp_path().is_file(), "bin/ueia_lz4.build.json is not built here")
    def test_the_python_recipe_list_matches_what_the_build_stamps(self) -> None:
        """The two halves of the check are written in different languages; they must agree."""
        stamp = json.loads(lz4.stamp_path().read_text(encoding="utf-8"))
        self.assertEqual(sorted(stamp["sources"]), sorted(lz4.RECIPE_SOURCES))
        self.assertIn("LZ4_FAST_DEC_LOOP=1", stamp["flags"])

    @unittest.skipUnless(lz4.build_state()["dll_current"], "bin/ueia_lz4.dll is not current here")
    def test_the_repos_own_library_is_current(self) -> None:
        state = lz4.build_state()
        self.assertTrue(state["ours"])
        self.assertTrue(state["current"])


class TestTheBuild(UeiaTestCase):
    """`build()`: idempotent when it can be, explicit when it cannot."""

    def test_a_current_dll_is_not_rebuilt(self) -> None:
        calls: List[Tuple[List[str], Optional[str]]] = []
        code, lines = lz4.build(run=_runner(calls), root=_tree(self))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [], "nothing to do must not run a compiler")
        self.assertIn("state    : current, nothing to build", lines)

    def test_a_stale_dll_is_configured_then_built(self) -> None:
        calls: List[Tuple[List[str], Optional[str]]] = []
        root = _tree(self)
        (root / "src" / "cpp" / "third_party" / "lz4" / "lz4.c").write_bytes(b"/* changed */\n")

        def restamp() -> None:
            stamp = json.loads((root / "bin" / "ueia_lz4.build.json").read_text(encoding="utf-8"))
            stamp["sources"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                for name in lz4.RECIPE_SOURCES}
            (root / "bin" / "ueia_lz4.build.json").write_text(json.dumps(stamp), encoding="utf-8")

        with mock.patch.object(lz4, "self_test", return_value=(True, "12 bytes decoded exactly")):
            code, lines = lz4.build(run=_runner(calls, after=restamp), root=root)
        self.assertEqual(code, 0)
        self.assertEqual([argv for argv, _cwd in calls], [
            ["cmake", "-S", ".", "-B", "build"],
            ["cmake", "--build", "build", "--config", "Release"],
        ])
        self.assertEqual([cwd for _argv, cwd in calls], [str(root), str(root)])
        self.assertIn("state    : stale (src/cpp/third_party/lz4/lz4.c changed since the build)", lines)
        self.assertTrue(any(line.startswith("built    : ") for line in lines))

    def test_a_failing_build_reports_the_output_and_the_exit_code(self) -> None:
        calls: List[Tuple[List[str], Optional[str]]] = []
        root = _tree(self, stamp=None)
        code, lines = lz4.build(run=_runner(calls, code=1, stderr="nope: no compiler\n"), root=root)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1, "a configure that failed must not be followed by the build")
        self.assertIn("stderr   : nope: no compiler", lines)
        self.assertTrue(any(line.startswith("error    : cmake -S . -B build exited 1") for line in lines))

    def test_a_missing_cmake_is_reported_rather_than_raised(self) -> None:
        def missing(argv: List[str], **kwargs: Any) -> Any:
            raise OSError("no such file: cmake")

        code, lines = lz4.build(run=missing, root=_tree(self, stamp=None))
        self.assertEqual(code, 1)
        self.assertIn("error    : could not run cmake (no such file: cmake)", lines)

    def test_force_rebuilds_even_when_it_is_current(self) -> None:
        calls: List[Tuple[List[str], Optional[str]]] = []
        with mock.patch.object(lz4, "self_test", return_value=(True, "12 bytes decoded exactly")):
            code, lines = lz4.build(force=True, run=_runner(calls), root=_tree(self))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("state    : current, rebuilt because --force said so", lines)

    def test_a_build_that_leaves_it_stale_is_a_failure(self) -> None:
        code, lines = lz4.build(run=_runner([], code=0), root=_tree(self, stamp=None))
        self.assertEqual(code, 1)
        self.assertTrue(any(line.startswith("error    : the build ran but the result is not current")
                            for line in lines), lines)


class TestTheCommand(UeiaTestCase):
    """`ueia lz4`: the verdicts, and the exit codes the pipeline reads."""

    def test_a_stale_library_is_a_problem_and_points_at_the_build(self) -> None:
        with mock.patch.object(lz4, "build_state", return_value=_state(current=False, dll_current=False)), \
                mock.patch.object(lz4, "self_test", return_value=(True, "12 bytes decoded exactly")):
            code, out, _err = self.run_cli(["lz4"])
        self.assertEqual(code, 1)
        self.assertIn("state    : stale -- run `python src\\py\\ueia.py lz4 --build`", out)
        self.assertIn("self-test: 12 bytes decoded exactly", out)

    def test_nothing_to_decode_with_exits_two(self) -> None:
        with mock.patch.object(lz4, "build_state", return_value=_state(
                exists=False, stamped=False, dll_current=False, ours=False, library=None,
                version=None, current=False)), \
                mock.patch.object(lz4, "self_test", return_value=(False, "no library to decode with")):
            code, out, _err = self.run_cli(["lz4"])
        self.assertEqual(code, 2)
        self.assertIn("library  : none found", out)
        self.assertIn("hint     : run `python src\\py\\ueia.py lz4 --build`", out)

    def test_a_library_this_repo_did_not_build_is_usable_and_says_so(self) -> None:
        with mock.patch.object(lz4, "build_state", return_value=_state(
                ours=False, library="/usr/lib/liblz4.so.1", version="1.9.4", current=False)), \
                mock.patch.object(lz4, "self_test", return_value=(True, "12 bytes decoded exactly")):
            code, out, _err = self.run_cli(["lz4"])
        self.assertEqual(code, 0)
        self.assertIn("origin   : not this repository's build", out)
        self.assertIn("state    : usable (a library this repository did not build)", out)

    def test_a_library_that_does_not_decode_is_a_problem(self) -> None:
        with mock.patch.object(lz4, "build_state", return_value=_state()), \
                mock.patch.object(lz4, "self_test",
                                  return_value=(False, "the library decoded b'x', not b'y'")):
            code, out, _err = self.run_cli(["lz4"])
        self.assertEqual(code, 1)
        self.assertIn("self-test: the library decoded", out)

    def test_build_that_fails_reports_the_build_and_exits_one(self) -> None:
        with mock.patch.object(lz4, "build", return_value=(1, ["state    : stale (x)",
                                                               "error    : cmake exited 1"])):
            code, out, _err = self.run_cli(["lz4", "--build"])
        self.assertEqual(code, 1)
        self.assertIn("error    : cmake exited 1", out)

    def test_a_bad_flag_is_a_usage_error(self) -> None:
        code, _out, err = self.run_cli(["lz4", "--rebuild"])
        self.assertEqual(code, 2)
        self.assertIn("--rebuild", err)

    @unittest.skipUnless(lz4.build_state()["current"], "bin/ueia_lz4.dll is not current here")
    def test_the_repos_own_library_reports_current(self) -> None:
        code, out, _err = self.run_cli(["lz4"])
        self.assertEqual(code, 0)
        self.assertIn("origin   : this repository's build", out)
        self.assertIn("state    : current", out)
        self.assertIn("self-test: 12 bytes decoded exactly", out)
        self.assertIn("recipe   : LZ4_DLL_EXPORT=1 LZ4_FAST_DEC_LOOP=1", out)


if __name__ == "__main__":
    unittest.main()
