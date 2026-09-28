"""The parse cache: identity, hits, misses, and the ways it must stay invisible."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest import mock

from testcase import UeiaTestCase

import cache
import shapes
from fixtures import demo_trace

MODEL = {"tool_version": shapes.TOOL_VERSION, "schema": [], "counts": {"events": 3}}


class TestIdentity(UeiaTestCase):
    def test_size_and_sha256(self) -> None:
        data = demo_trace()
        path = self.write_capture(data)
        identity = cache.capture_identity(path)
        self.assertEqual(identity["size"], len(data))
        self.assertEqual(identity["sha256"], hashlib.sha256(data).hexdigest().upper())

    def test_missing_file_raises(self) -> None:
        from shapes import UeiaError

        with self.assertRaises(UeiaError):
            cache.capture_identity(self.dir / "nope.utrace")


class TestStoreAndLoad(UeiaTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ.pop(shapes.ENV_NO_CACHE, None)
        self.path = self.write_capture(demo_trace())
        self.identity = cache.capture_identity(self.path)

    def test_round_trip(self) -> None:
        written = cache.store(self.path, self.identity, MODEL)
        self.assertEqual(written, cache.cache_path(self.path))
        self.assertTrue(written.exists())
        self.assertFalse(written.with_name(written.name + ".tmp").exists())
        self.assertEqual(cache.load(self.path, self.identity), MODEL)

    def test_cache_lives_beside_the_capture(self) -> None:
        written = cache.store(self.path, self.identity, MODEL)
        self.assertEqual(written.name, "fixture.utrace.ueiacache")
        self.assertEqual(written.parent, self.path.resolve().parent)

    def test_a_different_capture_is_a_miss(self) -> None:
        cache.store(self.path, self.identity, MODEL)
        other = self.write_capture(demo_trace() + b"\x00\x00\x00\x00", name="other.utrace")
        other_identity = cache.capture_identity(other)
        self.assertIsNone(cache.load(other, other_identity))

    def test_a_stale_tool_version_is_a_miss(self) -> None:
        cache.store(self.path, self.identity, MODEL)
        with mock.patch.object(cache, "TOOL_VERSION", "9.9.9"):
            self.assertIsNone(cache.load(self.path, self.identity))

    def test_a_stale_cache_format_is_a_miss(self) -> None:
        cache.store(self.path, self.identity, MODEL)
        with mock.patch.object(cache, "CACHE_FORMAT", 999):
            self.assertIsNone(cache.load(self.path, self.identity))

    def test_a_corrupt_cache_is_a_miss_not_an_error(self) -> None:
        path = cache.cache_path(self.path)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(cache.load(self.path, self.identity))

    def test_the_disable_flag_turns_both_ways_off(self) -> None:
        os.environ[shapes.ENV_NO_CACHE] = "1"
        written = cache.store(self.path, self.identity, MODEL)
        self.assertFalse(written.exists())
        os.environ.pop(shapes.ENV_NO_CACHE, None)
        cache.store(self.path, self.identity, MODEL)
        os.environ[shapes.ENV_NO_CACHE] = "1"
        self.assertIsNone(cache.load(self.path, self.identity))

    def test_status_and_clear(self) -> None:
        status = cache.status(self.path, self.identity)
        self.assertFalse(status["exists"])
        cache.store(self.path, self.identity, MODEL)
        status = cache.status(self.path, self.identity)
        self.assertTrue(status["exists"])
        self.assertTrue(status["matches"])
        self.assertGreater(int(status["bytes"]), 0)
        self.assertIsInstance(status["model"], dict)
        self.assertTrue(cache.clear(self.path))
        self.assertFalse(cache.clear(self.path))
        self.assertFalse(cache.cache_path(self.path).exists())

    def test_the_document_carries_the_identity(self) -> None:
        cache.store(self.path, self.identity, MODEL)
        document = json.loads(cache.cache_path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(document["capture_sha256"], self.identity["sha256"])
        self.assertEqual(document["capture_size"], self.identity["size"])
        self.assertEqual(document["model"], MODEL)


if __name__ == "__main__":
    unittest.main()
