"""The container header and the packet walk, over fixture captures."""

from __future__ import annotations

import struct
import unittest

from testcase import UeiaTestCase

import container
from fixtures import (
    MAGIC,
    build_trace,
    metadata_block,
    packet,
    sync_packet,
)
from shapes import ContainerError


class TestHeader(UeiaTestCase):
    def test_reads_the_shape_the_writer_writes(self) -> None:
        data = build_trace(threads={2: b"\x00"})
        header = container.parse_header(data)
        self.assertEqual(header.magic, MAGIC)
        self.assertEqual(header.transport_version, 4)
        self.assertEqual(header.protocol_version, 7)
        expected_offset = 4 + len(metadata_block()) + 2
        self.assertEqual(header.first_packet_offset, expected_offset)
        self.assertEqual(header.warnings, [])

    def test_metadata_summary_names_the_three_fields(self) -> None:
        header = container.parse_header(build_trace(threads={2: b"\x00"}))
        summary = container.metadata_summary(header)
        self.assertEqual(summary["control port"], "1985")
        self.assertEqual(summary["session"], bytes(range(16)).hex())
        self.assertEqual(summary["trace"], bytes(range(16, 32)).hex())

    def test_legacy_trc2_magic_is_read_with_a_warning(self) -> None:
        data = build_trace(threads={2: b"\x00"})
        legacy = b"TRC2" + data[4:]
        header = container.parse_header(legacy)
        self.assertEqual(header.magic, b"TRC2")
        self.assertTrue(header.warnings)

    def test_big_endian_magic_is_refused(self) -> None:
        with self.assertRaises(ContainerError):
            container.parse_header(b"ECRT" + b"\x00" * 32)

    def test_legacy_raw_magic_is_refused(self) -> None:
        with self.assertRaises(ContainerError):
            container.parse_header(b"TRCE" + b"\x00" * 32)

    def test_garbage_magic_is_refused(self) -> None:
        with self.assertRaises(ContainerError):
            container.parse_header(b"NOPE" + b"\x00" * 32)

    def test_tiny_file_is_refused(self) -> None:
        with self.assertRaises(ContainerError):
            container.parse_header(b"2CRT\x00")

    def test_metadata_block_past_the_end_is_refused(self) -> None:
        data = MAGIC + struct.pack("<H", 1000) + b"\x00" * 8
        with self.assertRaises(ContainerError):
            container.parse_header(data)

    def test_missing_version_pair_is_refused(self) -> None:
        data = MAGIC + metadata_block()
        with self.assertRaises(ContainerError):
            container.parse_header(data)

    def test_metadata_field_overrun_is_a_warning(self) -> None:
        # A field that claims more bytes than the block holds: reported, not fatal.
        body = struct.pack("<H", 200 | (7 << 8)) + b"ab"
        data = MAGIC + struct.pack("<H", len(body)) + body + bytes((4, 7))
        header = container.parse_header(data)
        self.assertTrue(header.warnings)
        self.assertEqual(header.metadata, [])


class TestPacketWalk(UeiaTestCase):
    def test_walks_raw_lz4_and_sync_packets(self) -> None:
        payload = b"payload bytes"
        data = MAGIC + metadata_block() + bytes((4, 7))
        data += packet(2, payload)
        data += packet(3, payload, encoded=True)
        data += sync_packet()
        header = container.parse_header(data)
        rows, anomalies = container.walk_packets(data, header)
        self.assertEqual(anomalies, [])
        self.assertEqual([row.form for row in rows], ["raw", "lz4", "sync"])
        self.assertEqual(rows[0].decoded_size, len(payload))
        self.assertEqual(rows[1].decoded_size, len(payload))
        self.assertEqual(rows[1].size, 4 + 2 + len(payload) + 1)  # header+decoded size+block
        self.assertEqual(rows[2].decoded_size, 0)
        self.assertEqual(rows[0].tid, 2)
        self.assertEqual(rows[1].tid, 3)
        self.assertEqual(rows[0].index, 0)
        self.assertEqual(rows[-1].offset + rows[-1].size, len(data))

    def test_packet_size_of_four_is_not_an_anomaly(self) -> None:
        data = MAGIC + metadata_block() + bytes((4, 7)) + sync_packet()
        header = container.parse_header(data)
        rows, anomalies = container.walk_packets(data, header)
        self.assertEqual(len(rows), 1)
        self.assertEqual(anomalies, [])

    def test_size_smaller_than_the_header_stops_the_walk(self) -> None:
        data = MAGIC + metadata_block() + bytes((4, 7))
        data += struct.pack("<HH", 3, 2) + b"\x00" * 8
        header = container.parse_header(data)
        rows, anomalies = container.walk_packets(data, header)
        self.assertEqual(rows, [])
        self.assertEqual(anomalies[0][0], "bad-packet-size")

    def test_packet_past_the_end_stops_the_walk(self) -> None:
        data = MAGIC + metadata_block() + bytes((4, 7))
        data += struct.pack("<HH", 400, 2) + b"\x00" * 8
        header = container.parse_header(data)
        rows, anomalies = container.walk_packets(data, header)
        self.assertEqual(rows, [])
        self.assertEqual(anomalies[0][0], "truncated-packet")

    def test_trailing_bytes_too_short_for_a_header(self) -> None:
        data = MAGIC + metadata_block() + bytes((4, 7)) + b"\x00\x01\x02"
        header = container.parse_header(data)
        rows, anomalies = container.walk_packets(data, header)
        self.assertEqual(rows, [])
        self.assertEqual(anomalies[0][0], "truncated-packet-header")

    def test_encoded_packet_without_its_decoded_size(self) -> None:
        data = MAGIC + metadata_block() + bytes((4, 7))
        data += struct.pack("<HH", 5, 0x8000 | 4) + b"\x00"
        header = container.parse_header(data)
        rows, anomalies = container.walk_packets(data, header)
        self.assertEqual(rows, [])
        self.assertEqual(anomalies[0][0], "truncated-packet")

    def test_tid_name_covers_the_specials(self) -> None:
        self.assertEqual(container.tid_name(0), "Events")
        self.assertEqual(container.tid_name(1), "Importants")
        self.assertEqual(container.tid_name(0x3FFE), "PseudoImportants")
        self.assertEqual(container.tid_name(0x3FFF), "Sync")
        self.assertEqual(container.tid_name(7), "7")


if __name__ == "__main__":
    unittest.main()
