"""Event framing: uid forms, serials, scopes, aux blocks, and where a walk stops."""

from __future__ import annotations

import struct
import unittest
from typing import Dict, Optional

from testcase import UeiaTestCase

import events
from fixtures import (
    UID_AUX_DATA,
    UID_SCOPE_ENTER,
    UID_SCOPE_ENTER_TB,
    UID_SCOPE_LEAVE,
    event,
    important_record,
    scope,
    timed_scope,
)
from shapes import EVENT_FLAG_MAYBE_HAS_AUX, EVENT_FLAG_NOSYNC, EventTypeRow


class FakeRegistry(object):
    """The registry the walker sees, spelled out per test."""

    def __init__(self, types: Dict[int, Dict[str, object]]) -> None:
        self.types = types

    def get(self, uid: int) -> Optional[EventTypeRow]:
        row = self.types.get(uid)
        if row is None:
            return None
        return row  # type: ignore[return-value]

    def _flags(self, uid: int) -> int:
        row = self.types.get(uid)
        if row is None:
            return 0
        flags = row.get("flags", 0)
        return flags if isinstance(flags, int) else 0

    def is_sync(self, uid: int) -> bool:
        return not (self._flags(uid) & EVENT_FLAG_NOSYNC)

    def has_aux(self, uid: int) -> bool:
        return bool(self._flags(uid) & EVENT_FLAG_MAYBE_HAS_AUX)

    def size(self, uid: int) -> int:
        row = self.types.get(uid)
        if row is None:
            return 0
        size = row.get("size", 0)
        return size if isinstance(size, int) else 0


def _counts() -> Dict[str, int]:
    return {
        "events": 0, "sync_events": 0, "scopes": 0, "aux_blocks": 0, "unknown_uid": 0,
    }


class TestDecode7Bit(UeiaTestCase):
    def test_single_byte(self) -> None:
        self.assertEqual(events.decode7bit(b"\x05", 0), (5, 1))

    def test_multi_byte(self) -> None:
        self.assertEqual(events.decode7bit(b"\x80\x01", 0), (128, 2))
        self.assertEqual(events.decode7bit(b"\xff\x7f", 0), (16383, 2))

    def test_truncated_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            events.decode7bit(b"\x80", 0)

    def test_value_longer_than_64_bits_raises(self) -> None:
        with self.assertRaises(ValueError):
            events.decode7bit(b"\xff" * 11, 0)


class TestImportantRecords(UeiaTestCase):
    def test_two_records(self) -> None:
        stream = important_record(7, b"ab") + important_record(9, b"cde")
        anomalies = []
        records = list(events.iter_important_records(stream, anomalies))
        self.assertEqual(records, [(7, 2, 4), (9, 3, 10)])
        self.assertEqual(anomalies, [])

    def test_truncated_tail_is_reported(self) -> None:
        stream = important_record(7, b"ab") + b"\x01\x00"
        anomalies = []
        records = list(events.iter_important_records(stream, anomalies))
        self.assertEqual(len(records), 1)
        self.assertEqual(anomalies[0][0], "truncated-record")

    def test_record_past_the_end_is_reported(self) -> None:
        stream = struct.pack("<HH", 7, 100) + b"ab"
        anomalies = []
        records = list(events.iter_important_records(stream, anomalies))
        self.assertEqual(records, [])
        self.assertEqual(anomalies[0][0], "truncated-record")


class TestRecordAux(UeiaTestCase):
    """Aux blocks inside important records: unshifted uids, a terminal per field."""

    def test_unshifted_uid_and_a_terminal_after_every_field(self) -> None:
        from fixtures import important_aux_block

        stream = important_aux_block(0, b"one") + important_aux_block(3, b"three")
        blocks = events.walk_record_aux(stream, 0, len(stream))
        self.assertEqual([index for index, _off, _size in blocks], [0, 3])
        self.assertEqual(stream[blocks[0][1]:blocks[0][1] + blocks[0][2]], b"one")
        self.assertEqual(stream[blocks[1][1]:blocks[1][1] + blocks[1][2]], b"three")

    def test_a_thread_stream_SHIFTED_uid_is_not_an_important_block(self) -> None:
        stream = bytes((UID_AUX_DATA << 1, 0, 0, 0)) + b"data"
        self.assertEqual(events.walk_record_aux(stream, 0, len(stream)), [])

    def test_a_block_past_the_record_end_stops_the_walk(self) -> None:
        from fixtures import important_aux_block

        stream = important_aux_block(0, b"one") + bytes((1, 0, 0xFF, 0xFF))
        blocks = events.walk_record_aux(stream, 0, len(stream))
        self.assertEqual(len(blocks), 1)

    def test_an_empty_record_has_no_blocks(self) -> None:
        self.assertEqual(events.walk_record_aux(b"", 0, 0), [])


class TestThreadEvents(UeiaTestCase):
    def test_one_byte_uid_without_serial(self) -> None:
        registry = FakeRegistry({20: {"flags": EVENT_FLAG_NOSYNC, "size": 2}})
        stream = event(20, struct.pack("<H", 7))
        anomalies: list = []
        counts = _counts()
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, counts))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].uid, 20)
        self.assertIsNone(decoded[0].serial)
        self.assertEqual(stream[decoded[0].offset], 7)
        self.assertEqual(counts["events"], 1)
        self.assertEqual(anomalies, [])

    def test_two_byte_uid_with_serial(self) -> None:
        registry = FakeRegistry({300: {"flags": 0, "size": 1}})
        stream = event(300, b"\x09", serial=0x123456)
        anomalies: list = []
        counts = _counts()
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, counts))
        self.assertEqual(decoded[0].uid, 300)
        self.assertEqual(decoded[0].serial, 0x123456)
        self.assertEqual(counts["sync_events"], 1)

    def test_serials_are_24_bit(self) -> None:
        registry = FakeRegistry({300: {"flags": 0, "size": 0}})
        stream = event(300, serial=0xABCDEF)
        decoded = list(events.iter_thread_events(stream, 2, registry, [], _counts()))
        self.assertEqual(decoded[0].serial, 0xABCDEF)

    def test_plain_and_timestamped_scopes(self) -> None:
        stream = scope(UID_SCOPE_ENTER) + scope(UID_SCOPE_LEAVE) + timed_scope(UID_SCOPE_ENTER_TB, 42)
        anomalies: list = []
        counts = _counts()
        decoded = list(events.iter_thread_events(stream, 2, FakeRegistry({}), anomalies, counts))
        self.assertEqual([item.uid for item in decoded], [UID_SCOPE_ENTER, UID_SCOPE_LEAVE, UID_SCOPE_ENTER_TB])
        self.assertTrue(all(item.b_scope for item in decoded))
        self.assertEqual(decoded[2].size, 7)  # eight bytes in total, uid included
        self.assertEqual(counts["scopes"], 3)
        self.assertEqual(anomalies, [])

    def test_a_timed_scope_leaves_the_next_event_in_step(self) -> None:
        registry = FakeRegistry({20: {"flags": EVENT_FLAG_NOSYNC, "size": 1}})
        stream = timed_scope(UID_SCOPE_ENTER_TB, 0x123456789A) + event(20, b"\x07")
        anomalies: list = []
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, _counts()))
        self.assertEqual([item.uid for item in decoded], [UID_SCOPE_ENTER_TB, 20])
        self.assertEqual(stream[decoded[0].offset:decoded[0].offset + 7], b"\x9a\x78\x56\x34\x12\x00\x00")
        self.assertEqual(anomalies, [])

    def test_aux_block_is_found_and_walked_past(self) -> None:
        registry = FakeRegistry({
            21: {"flags": EVENT_FLAG_MAYBE_HAS_AUX | EVENT_FLAG_NOSYNC, "size": 4},
            22: {"flags": EVENT_FLAG_NOSYNC, "size": 1},
        })
        stream = event(21, struct.pack("<I", 5), aux=[(1, b"name")], maybe_aux=True)
        stream += event(22, b"\x07")
        anomalies: list = []
        counts = _counts()
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, counts))
        self.assertEqual([item.uid for item in decoded], [21, 22])
        self.assertEqual(len(decoded[0].aux), 1)
        field_index, offset, size = decoded[0].aux[0]
        self.assertEqual((field_index, size), (1, 4))
        self.assertEqual(stream[offset:offset + size], b"name")
        self.assertEqual(counts["aux_blocks"], 1)
        self.assertEqual(anomalies, [])

    def test_missing_aux_terminal_is_reported(self) -> None:
        registry = FakeRegistry({21: {"flags": EVENT_FLAG_MAYBE_HAS_AUX | EVENT_FLAG_NOSYNC, "size": 0}})
        stream = event(21, b"", aux=[(0, b"x")])
        anomalies: list = []
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, _counts()))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(anomalies[0][0], "missing-aux-terminal")

    def test_stray_aux_byte_is_reported(self) -> None:
        registry = FakeRegistry({21: {"flags": EVENT_FLAG_MAYBE_HAS_AUX | EVENT_FLAG_NOSYNC, "size": 0}})
        stream = bytes((21 << 1,)) + bytes((UID_AUX_DATA << 1, 0, 0, 0)) + b"\xEE\x00"
        anomalies: list = []
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, _counts()))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(anomalies[0][0], "bad-aux-block")

    def test_unknown_uid_stops_the_walk(self) -> None:
        registry = FakeRegistry({})
        stream = event(20) + event(20)
        anomalies: list = []
        counts = _counts()
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, counts))
        self.assertEqual(decoded, [])
        self.assertEqual(anomalies[0][0], "unknown-uid")
        self.assertEqual(counts["unknown_uid"], 1)

    def test_unknown_well_known_uid_stops_the_walk(self) -> None:
        stream = bytes((2 << 1,)) + event(20)
        anomalies: list = []
        decoded = list(events.iter_thread_events(stream, 2, FakeRegistry({}), anomalies, _counts()))
        self.assertEqual(decoded, [])
        self.assertEqual(anomalies[0][0], "unknown-well-known-uid")

    def test_truncated_payload_stops_the_walk(self) -> None:
        registry = FakeRegistry({20: {"flags": EVENT_FLAG_NOSYNC, "size": 8}})
        stream = event(20, b"\x00\x00")
        anomalies: list = []
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, _counts()))
        self.assertEqual(decoded, [])
        self.assertEqual(anomalies[0][0], "truncated-event")

    def test_truncated_serial_stops_the_walk(self) -> None:
        registry = FakeRegistry({300: {"flags": 0, "size": 0}})
        stream = struct.pack("<H", (300 << 1) | 1) + b"\x01"
        anomalies: list = []
        decoded = list(events.iter_thread_events(stream, 2, registry, anomalies, _counts()))
        self.assertEqual(decoded, [])
        self.assertEqual(anomalies[0][0], "truncated-event")


if __name__ == "__main__":
    unittest.main()
