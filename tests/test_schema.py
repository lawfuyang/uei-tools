"""The capture's own vocabulary: NewEvent records into a registry."""

from __future__ import annotations

import struct
import unittest
from typing import Dict, List, Tuple

from testcase import UeiaTestCase

import schema
from fixtures import (
    EVENT_FLAG_IMPORTANT,
    EVENT_FLAG_MAYBE_HAS_AUX,
    EVENT_FLAG_NOSYNC,
    important_record,
    new_event_record,
)
from shapes import EVENT_FLAG_NOSYNC as FLAG_NOSYNC


def _counts() -> Dict[str, int]:
    return {"new_events": 0, "redefined": 0}


class TestParseNewEvent(UeiaTestCase):
    def test_reads_fields_offsets_sizes_and_names(self) -> None:
        record = new_event_record(
            42,
            "CpuProfiler",
            "EventSpec",
            [("Id", "u32"), ("Name", "s"), ("File", "s"), ("Line", "u32")],
            flags=EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX | EVENT_FLAG_NOSYNC,
        )
        anomalies: List[Tuple[str, int, int, str]] = []
        # strip the [uid][size] record header to hand the body to the parser
        body_offset = 4
        size = struct.unpack_from("<H", record, 2)[0]
        row = schema.parse_new_event(record, body_offset, size, anomalies)
        assert row is not None
        self.assertEqual(row["uid"], 42)
        self.assertEqual(row["logger"], "CpuProfiler")
        self.assertEqual(row["name"], "EventSpec")
        self.assertEqual(row["full_name"], "CpuProfiler.EventSpec")
        self.assertEqual(row["flag_names"], "Important+MaybeHasAux+NoSync")
        self.assertEqual(row["size"], 8)  # the two u32 fields; strings carry none
        fields = row["fields"]
        self.assertEqual([field["name"] for field in fields], ["Id", "Name", "File", "Line"])
        self.assertEqual(fields[0]["offset"], 0)
        self.assertEqual(fields[0]["type_name"], "u32")
        self.assertEqual(fields[1]["offset"], 4)
        self.assertEqual(fields[1]["size"], 0)
        self.assertEqual(fields[1]["type_name"], "AnsiString")
        self.assertEqual(fields[3]["offset"], 4)
        self.assertEqual(anomalies, [])

    def test_wides_string_type_names(self) -> None:
        record = new_event_record(7, "Misc", "BookmarkSpec", [("FormatString", "ws"), ("Line", "i32")])
        size = struct.unpack_from("<H", record, 2)[0]
        row = schema.parse_new_event(record, 4, size, [])
        assert row is not None
        self.assertEqual(row["fields"][0]["type_name"], "WideString")
        self.assertEqual(row["fields"][1]["type_name"], "i32")

    def test_the_unwritten_descriptor_byte_is_ignored(self) -> None:
        record = bytearray(new_event_record(9, "Log", "Message", [("Value", "u32")]))
        # byte 1 of the first FNewEventField is garbage in real captures
        record[4 + 6 + 1] = 0x70
        size = struct.unpack_from("<H", record, 2)[0]
        anomalies: List[Tuple[str, int, int, str]] = []
        row = schema.parse_new_event(bytes(record), 4, size, anomalies)
        assert row is not None
        self.assertEqual(row["fields"][0]["name"], "Value")
        self.assertEqual(anomalies, [])

    def test_reference_and_definition_fields_take_their_size_from_the_type_byte(self) -> None:
        descriptors = bytearray()
        # reference (family 1): offset 0, ref uid 42, u32
        descriptors += struct.pack("<BBHHBB", 1, 0, 0, 42, 0x02, 4)
        # definition id (family 2): type byte lives in the last byte
        descriptors += struct.pack("<BBHHBB", 2, 0, 4, 0, 0, 0x02)
        names = b"RefX" + b"Type" + b"Name"  # logger, event, then the one named field
        body = bytearray(struct.pack("<HBBBB", 55, 2, EVENT_FLAG_NOSYNC, 4, 4))
        body += descriptors
        body += names
        record = important_record(0, bytes(body))
        size = struct.unpack_from("<H", record, 2)[0]
        row = schema.parse_new_event(record, 4, size, [])
        assert row is not None
        self.assertEqual(row["size"], 8)  # 4 + 4, both fixed
        self.assertEqual(row["fields"][0]["family_name"], "reference")
        self.assertEqual(row["fields"][0]["ref_uid"], 42)
        self.assertEqual(row["fields"][1]["family_name"], "definition-id")
        self.assertEqual(row["fields"][1]["name"], "DefinitionId")

    def test_record_smaller_than_its_header_is_reported(self) -> None:
        anomalies: List[Tuple[str, int, int, str]] = []
        row = schema.parse_new_event(b"\x00\x00\x01", 0, 3, anomalies)
        self.assertIsNone(row)
        self.assertEqual(anomalies[0][0], "bad-new-event")

    def test_names_past_the_record_are_reported(self) -> None:
        body = struct.pack("<HBBBB", 5, 0, 0, 40, 40) + b"short"
        anomalies: List[Tuple[str, int, int, str]] = []
        row = schema.parse_new_event(body, 0, len(body), anomalies)
        self.assertIsNone(row)
        self.assertEqual(anomalies[0][0], "bad-new-event")

    def test_zero_fields_is_a_valid_type(self) -> None:
        record = new_event_record(11, "$Trace", "ThreadGroupEnd", [])
        size = struct.unpack_from("<H", record, 2)[0]
        row = schema.parse_new_event(record, 4, size, [])
        assert row is not None
        self.assertEqual(row["fields"], [])
        self.assertEqual(row["size"], 0)


class TestRegistry(UeiaTestCase):
    def test_builds_from_records_and_counts_them(self) -> None:
        stream = new_event_record(30, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        stream += new_event_record(31, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        anomalies: List[Tuple[str, int, int, str]] = []
        counts = _counts()
        registry = schema.build_registry(stream, anomalies, counts)
        self.assertEqual(registry.count(), 2)
        self.assertEqual(counts["new_events"], 2)
        self.assertEqual(registry.size(30), 9)
        self.assertTrue(registry.is_sync(30))
        self.assertFalse(registry.has_aux(30))
        self.assertEqual([row["uid"] for row in registry.rows()], [30, 31])
        self.assertEqual(registry.full_name(31), "Misc.EndFrame")
        self.assertEqual(anomalies, [])

    def test_nosync_events_are_not_sync(self) -> None:
        stream = new_event_record(30, "Misc", "Bookmark", [("Cycle", "u64")], flags=FLAG_NOSYNC)
        registry = schema.build_registry(stream, [], _counts())
        self.assertFalse(registry.is_sync(30))

    def test_a_non_new_event_record_is_reported(self) -> None:
        stream = new_event_record(30, "Misc", "BeginFrame", [])
        stream += important_record(66, b"channel announce bytes")
        anomalies: List[Tuple[str, int, int, str]] = []
        counts = _counts()
        registry = schema.build_registry(stream, anomalies, counts)
        self.assertEqual(registry.count(), 1)
        self.assertEqual(anomalies[0][0], "unexpected-events-record")

    def test_a_redefinition_keeps_the_last_and_is_counted(self) -> None:
        stream = new_event_record(30, "Misc", "First", [])
        stream += new_event_record(30, "Misc", "Second", [])
        anomalies: List[Tuple[str, int, int, str]] = []
        counts = _counts()
        registry = schema.build_registry(stream, anomalies, counts)
        self.assertEqual(registry.count(), 1)
        self.assertEqual(counts["new_events"], 2)
        self.assertEqual(counts["redefined"], 1)
        self.assertEqual(registry.get(30)["name"], "Second")  # type: ignore[index]
        self.assertEqual(anomalies, [])


if __name__ == "__main__":
    unittest.main()
