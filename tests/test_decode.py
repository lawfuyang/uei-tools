"""Interpreting payloads: typed values, strings from aux, and the batch format."""

from __future__ import annotations

import struct
import unittest
from typing import List, Tuple, cast

from testcase import UeiaTestCase

import decode
from fixtures import FIELD_KINDS, new_event_record
from shapes import RawEvent
import schema


def _row_for(fields: List[Tuple[str, str]], uid: int = 42):
    record = new_event_record(uid, "Test", "Event", fields)
    size = struct.unpack_from("<H", record, 2)[0]
    row = schema.parse_new_event(record, 4, size, [])
    assert row is not None
    return row


class TestReadValue(UeiaTestCase):
    def test_every_fixed_kind(self) -> None:
        checks = [
            ("u16", 0xFFFF, 0xFFFF),
            ("u32", 0xFFFFFFFF, 0xFFFFFFFF),
            ("u64", 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF),
            ("i8", -1, -1),
            ("i16", -2, -2),
            ("i32", -3, -3),
            ("i64", -4, -4),
        ]
        for kind, value, expected in checks:
            type_byte = FIELD_KINDS[kind][0]
            packed = _pack(kind, value)
            self.assertEqual(decode.read_value(packed, 0, type_byte), expected, kind)

    def test_floats(self) -> None:
        f32 = decode.read_value(_pack("f32", 1.5), 0, FIELD_KINDS["f32"][0])
        self.assertAlmostEqual(cast(float, f32), 1.5, places=6)
        self.assertEqual(decode.read_value(_pack("f64", -2.25), 0, FIELD_KINDS["f64"][0]), -2.25)

    def test_a_zero_type_byte_reads_the_raw_byte(self) -> None:
        # Bool and uint8 are the same byte in the protocol: the wire value wins
        self.assertEqual(decode.read_value(b"\x00", 0, 0x00), 0)
        self.assertEqual(decode.read_value(b"\x01", 0, 0x00), 1)
        self.assertEqual(decode.read_value(b"\x08", 0, 0x00), 8)


def _pack(kind: str, value: float) -> bytes:
    from fixtures import pack

    return pack(kind, value)


class TestEventValues(UeiaTestCase):
    def test_fixed_fields_and_aux_strings_are_named(self) -> None:
        row = _row_for([("Id", "u32"), ("Name", "s")])
        payload = _pack("u32", 7)
        stream = payload + b"the name"
        event = RawEvent(
            uid=42, serial=None, offset=0, size=len(payload),
            aux=[(1, len(payload), 8)], b_scope=False,
        )
        values = decode.event_values(row, stream, event)
        self.assertEqual(values["Id"], 7)
        self.assertEqual(values["Name"], "the name")

    def test_wide_strings_decode_from_utf16(self) -> None:
        row = _row_for([("Name", "ws")])
        text = "wide".encode("utf-16-le")
        stream = text
        event = RawEvent(uid=42, serial=None, offset=0, size=0, aux=[(0, 0, len(text))], b_scope=False)
        values = decode.event_values(row, stream, event)
        self.assertEqual(values["Name"], "wide")

    def test_a_string_decodes_by_its_declared_type_not_its_contents(self) -> None:
        """A declared WideString is UTF-16 even when the bytes look like text, and vice versa.

        The engine's important writer memcpy's UTF-16 for a field declared `WideString`
        (`FFieldSet<…, WideString>::Impl`) and truncates to the low byte for one declared
        `AnsiString` fed wide literals -- the decode follows the declaration, which is what
        `Diagnostics.Session2`'s build version and `Misc.BookmarkSpec`'s format string (both
        genuinely wide, both mangled before 2026-09-30) need.
        """
        wide_row = _row_for([("Name", "ws")])
        event = RawEvent(uid=42, serial=None, offset=0, size=0, aux=[(0, 0, 8)], b_scope=False)
        utf16 = "wide".encode("utf-16-le")
        self.assertEqual(decode.event_values(wide_row, utf16, event)["Name"], "wide")
        narrow_row = _row_for([("Name", "s")])
        self.assertEqual(decode.event_values(narrow_row, utf16, event)["Name"],
                         "w\x00i\x00d\x00e",
                         "narrow bytes are not reinterpreted as UTF-16: the low bytes are the text")

    def test_missing_aux_leaves_the_field_absent(self) -> None:
        row = _row_for([("Name", "s")])
        event = RawEvent(uid=42, serial=None, offset=0, size=0, aux=[], b_scope=False)
        values = decode.event_values(row, b"", event)
        self.assertNotIn("Name", values)

    def test_array_fields_come_back_as_bytes(self) -> None:
        row = _row_for([("Data", "arr")])
        event = RawEvent(uid=42, serial=None, offset=0, size=0, aux=[(0, 0, 3)], b_scope=False)
        values = decode.event_values(row, b"\x01\x02\x03", event)
        self.assertEqual(values["Data"], b"\x01\x02\x03")

    def test_value_helpers(self) -> None:
        self.assertEqual(decode.value_int({"A": 5}, "A"), 5)
        self.assertIsNone(decode.value_int({"A": "text"}, "A"))
        self.assertIsNone(decode.value_int({}, "A"))
        self.assertEqual(decode.value_str({"A": "x"}, "A"), "x")
        self.assertEqual(decode.value_str({}, "A"), "")


class TestDecodeBatch(UeiaTestCase):
    def test_begin_records_carry_a_spec_id_and_end_records_do_not(self) -> None:
        blob = _varint((100 << 2) | 1) + _varint(5) + _varint((120 << 2) | 0)
        records, coroutine, error = decode.decode_batch(blob)
        self.assertEqual(records, [(100, 5, True), (120, None, False)])
        self.assertEqual(coroutine, 0)
        self.assertEqual(error, "")

    def test_coroutine_records_are_counted_and_stepped_over(self) -> None:
        # a coroutine begin carries two varints (id, depth), an end one (depth)
        blob = _varint((100 << 2) | 3) + _varint(9) + _varint(2) + _varint((120 << 2) | 2) + _varint(1)
        records, coroutine, error = decode.decode_batch(blob)
        self.assertEqual(records, [(100, None, True), (120, None, False)])
        self.assertEqual(coroutine, 2)
        self.assertEqual(error, "")

    def test_the_varint_reader_answers_the_one_byte_form(self) -> None:
        """The hot path: 15.08 M calls on the corpus, most of them a single byte.

        A one-byte varint is its own value, and the reader has to keep doing that for 0x00 and 0x7F
        (the boundary before the continuation bit) as well as for the multi-byte form.
        """
        self.assertEqual(decode.decode7bit(b"\x00", 0), (0, 1))
        self.assertEqual(decode.decode7bit(b"\x7f", 0), (127, 1))
        self.assertEqual(decode.decode7bit(b"\x80\x01", 0), (128, 2))
        self.assertEqual(decode.decode7bit(b"\xff\xff\x03", 0), (65535, 3))
        with self.assertRaises(ValueError):
            decode.decode7bit(b"\x80", 0)
        with self.assertRaises(ValueError):
            decode.decode7bit(b"", 0)

    def test_truncated_record_is_reported_but_keeps_earlier_ones(self) -> None:
        blob = _varint((100 << 2) | 1) + _varint(5) + b"\x80"
        records, _coroutine, error = decode.decode_batch(blob)
        self.assertEqual(records, [(100, 5, True)])
        self.assertTrue(error)

    def test_a_begin_without_its_spec_id_is_truncated(self) -> None:
        blob = _varint((32 << 2) | 1)  # a begin record whose spec id never comes
        records, _coroutine, error = decode.decode_batch(blob)
        self.assertEqual(records, [])
        self.assertTrue(error)

    def test_empty_blob(self) -> None:
        self.assertEqual(decode.decode_batch(b""), ([], 0, ""))


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


if __name__ == "__main__":
    unittest.main()
