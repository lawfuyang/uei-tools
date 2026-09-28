"""The packet layer decoded: one byte stream per thread id.

A packet's payload is appended to its thread's stream (LZ4-decoding the
EncodedMarker packets on the way); events never straddle packets, so the
streams this module produces are what the event layer reads. This is the
expensive half of a cold parse -- the parse cache exists to avoid repeating it.
"""

from __future__ import annotations

import struct
from typing import Dict, List, NamedTuple, Tuple

import lz4
from container import Anomaly
from shapes import (
    Lz4Error,
    PACKET_HEADER_SIZE,
    PacketRow,
    TID_EVENTS,
    TID_IMPORTANTS,
    TID_PSEUDO_IMPORTANTS,
    TID_SYNC,
)


class StreamSet(NamedTuple):
    """Every thread's decoded byte stream, plus what decoding cost or failed."""

    streams: Dict[int, bytes]
    bytes_per_tid: Dict[int, int]
    packets_per_tid: Dict[int, int]
    counts: Dict[str, int]
    anomalies: List[Anomaly]


def _decode_errors_payload(
    data: bytes, packet: PacketRow
) -> Tuple[bytes, str]:
    """Decode one packet's payload. Returns (bytes, error-message-or-empty)."""
    start = packet.offset + PACKET_HEADER_SIZE
    end = packet.offset + packet.size
    if packet.form == "sync":
        return b"", ""
    if packet.form == "lz4":
        decoded_size = struct.unpack_from("<H", data, start)[0]
        compressed = bytes(data[start + 2:end])
        try:
            return lz4.decompress_block(compressed, decoded_size), ""
        except Lz4Error as exc:
            return b"", str(exc)
    return bytes(data[start:end]), ""


def assemble(data: bytes, packets: List[PacketRow]) -> StreamSet:
    """Append every packet's decoded payload to its thread's stream."""
    streams: Dict[int, bytearray] = {}
    bytes_per_tid: Dict[int, int] = {}
    packets_per_tid: Dict[int, int] = {}
    counts: Dict[str, int] = {
        "packets": 0, "raw": 0, "lz4": 0, "sync": 0, "other": 0,
        "lz4_bytes": 0, "lz4_decoded_bytes": 0,
    }
    anomalies: List[Anomaly] = []

    for packet in packets:
        counts["packets"] += 1
        if packet.form == "sync":
            counts["sync"] += 1
            continue
        if packet.form == "lz4":
            counts["lz4"] += 1
            counts["lz4_bytes"] += packet.size - PACKET_HEADER_SIZE - 2
            counts["lz4_decoded_bytes"] += packet.decoded_size
        elif packet.form == "raw":
            counts["raw"] += 1
        else:
            counts["other"] += 1

        payload, error = _decode_errors_payload(data, packet)
        if error:
            anomalies.append((
                "lz4-decode-error", packet.offset, packet.size,
                "packet %d (tid %d): %s" % (packet.index, packet.tid, error),
            ))
            continue
        stream = streams.get(packet.tid)
        if stream is None:
            stream = bytearray()
            streams[packet.tid] = stream
            packets_per_tid[packet.tid] = 0
            bytes_per_tid[packet.tid] = 0
        stream += payload
        packets_per_tid[packet.tid] += 1
        bytes_per_tid[packet.tid] += len(payload)

    frozen: Dict[int, bytes] = {tid: bytes(stream) for tid, stream in streams.items()}
    return StreamSet(
        streams=frozen,
        bytes_per_tid=bytes_per_tid,
        packets_per_tid=packets_per_tid,
        counts=counts,
        anomalies=anomalies,
    )


def special_stream(stream_set: StreamSet, tid: int) -> bytes:
    """One special stream's bytes (b"" when the capture has none)."""
    return stream_set.streams.get(tid, b"")


def thread_tids(stream_set: StreamSet) -> List[int]:
    """Real thread ids (the ones between the special ids), ascending."""
    return sorted(
        tid for tid in stream_set.streams
        if tid not in (TID_EVENTS, TID_IMPORTANTS, TID_PSEUDO_IMPORTANTS, TID_SYNC)
        and 2 <= tid < 0x3FF0
    )
