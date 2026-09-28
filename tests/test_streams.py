"""The packet layer decoded: per-thread streams, LZ4 routing, and failures."""

from __future__ import annotations

import struct
import unittest
from unittest import mock

from testcase import UeiaTestCase

import container
import lz4
import streams
from fixtures import MAGIC, build_trace, lz4_literal_block, metadata_block, packet, sync_packet


def _walk(data: bytes):
    header = container.parse_header(data)
    return container.walk_packets(data, header)


class TestAssemble(UeiaTestCase):
    def test_raw_and_lz4_packets_land_in_their_streams(self) -> None:
        """What this pins is the *assembly*: which packets are decoded, and with what.

        The decoder is stood in for, so the test needs no built DLL and can assert the call the
        assembler makes -- the block's bytes and the size the packet header declares. The real
        library's half is `test_lz4`'s business (and the corpus's).
        """
        data = MAGIC + metadata_block() + bytes((4, 7))
        data += packet(2, b"abcd")
        data += packet(3, b"efgh", encoded=True)
        data += sync_packet()
        rows, walk_anomalies = _walk(data)
        self.assertEqual(walk_anomalies, [])
        with mock.patch.object(lz4, "decompress_block", return_value=b"efgh") as decoder:
            stream_set = streams.assemble(data, rows)
        decoder.assert_called_once_with(lz4_literal_block(b"efgh"), 4)
        self.assertEqual(stream_set.streams[2], b"abcd")
        self.assertEqual(stream_set.streams[3], b"efgh")
        self.assertEqual(stream_set.counts["raw"], 1)
        self.assertEqual(stream_set.counts["lz4"], 1)
        self.assertEqual(stream_set.counts["sync"], 1)
        self.assertEqual(stream_set.bytes_per_tid[2], 4)
        self.assertEqual(stream_set.packets_per_tid[3], 1)
        self.assertEqual(stream_set.anomalies, [])

    def test_packets_of_one_thread_are_concatenated_in_order(self) -> None:
        data = MAGIC + metadata_block() + bytes((4, 7))
        data += packet(2, b"first")
        data += packet(2, b"second")
        rows, _ = _walk(data)
        stream_set = streams.assemble(data, rows)
        self.assertEqual(stream_set.streams[2], b"firstsecond")
        self.assertEqual(stream_set.packets_per_tid[2], 2)

    def test_a_bad_lz4_block_is_an_anomaly_and_not_a_stream(self) -> None:
        data = MAGIC + metadata_block() + bytes((4, 7))
        # claims five literals, carries one: the block cannot decode
        bad = struct.pack("<H", 5) + bytes((0x50, 0x61))
        data += struct.pack("<HH", 4 + len(bad), 0x8000 | 2) + bad
        rows, _ = _walk(data)
        stream_set = streams.assemble(data, rows)
        self.assertNotIn(2, stream_set.streams)
        self.assertEqual(stream_set.anomalies[0][0], "lz4-decode-error")
        self.assertEqual(stream_set.counts["lz4"], 1)

    def test_sync_packets_are_counted_but_never_streamed(self) -> None:
        data = MAGIC + metadata_block() + bytes((4, 7)) + sync_packet()
        rows, _ = _walk(data)
        stream_set = streams.assemble(data, rows)
        self.assertEqual(stream_set.streams, {})
        self.assertEqual(stream_set.counts["sync"], 1)

    def test_special_streams_and_thread_tids(self) -> None:
        data = build_trace(
            events_stream=b"events",
            importants_stream=b"importants",
            threads={2: b"\x00", 9: b"\x01"},
        )
        rows, _ = _walk(data)
        stream_set = streams.assemble(data, rows)
        self.assertEqual(streams.special_stream(stream_set, 0), b"events")
        self.assertEqual(streams.special_stream(stream_set, 1), b"importants")
        self.assertEqual(streams.special_stream(stream_set, 77), b"")
        self.assertEqual(streams.thread_tids(stream_set), [2, 9])


if __name__ == "__main__":
    unittest.main()
