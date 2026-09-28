"""The container: the file's own header, and the packet walk over what follows.

Nothing here decompresses anything, so `info` and `packets` are served from
this layer alone -- the expensive half (LZ4 + per-thread streams) lives one
module up, in streams.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Tuple

from shapes import (
    ContainerError,
    ContainerHeader,
    ENCODED_MARKER,
    MAGIC,
    MAGIC_LEGACY_BE,
    MAGIC_LEGACY_METADATA,
    MAGIC_LEGACY_RAW,
    MetadataField,
    PACKET_HEADER_SIZE,
    PacketRow,
    TID_MASK,
    TID_NAMES,
    VERIFICATION_MARKER,
)

Anomaly = Tuple[str, int, int, str]


def _anomaly(kind: str, offset: int, value: int, message: str) -> Anomaly:
    return (kind, offset, value, message)


def metadata_summary(header: ContainerHeader) -> Dict[str, str]:
    """The metadata block spelled out: control port, session and trace guid."""
    out: Dict[str, str] = {}
    for field in header.metadata:
        if field.field_id == 0 and len(field.data) == 2:
            out["control port"] = str(struct.unpack("<H", field.data)[0])
        elif field.field_id == 1:
            out["session"] = field.data.hex()
        elif field.field_id == 2:
            out["trace"] = field.data.hex()
        else:
            out["field %d" % field.field_id] = field.data.hex()
    return out


def parse_header(data: bytes) -> ContainerHeader:
    """Read the file's magic, metadata block and version pair."""
    if len(data) < 12:
        raise ContainerError("the file is too small to be a trace (%d bytes)" % (len(data),))
    magic = bytes(data[0:4])
    warnings: List[str] = []
    offset = 4
    metadata: List[MetadataField] = []
    if magic == MAGIC:
        pass
    elif magic == MAGIC_LEGACY_METADATA:
        warnings.append("legacy magic TRC2 (pre-2CRT trace); reading it anyway")
    elif magic == MAGIC_LEGACY_BE:
        raise ContainerError("big-endian traces are not supported (magic ECRT)")
    elif magic == MAGIC_LEGACY_RAW:
        raise ContainerError(
            "legacy raw transport traces (magic TRCE) are not supported: no metadata block"
        )
    else:
        raise ContainerError(
            "not a trace file: magic %r (expected %r)" % (magic, MAGIC)
        )

    if magic != MAGIC_LEGACY_RAW:
        if offset + 2 > len(data):
            raise ContainerError("the file ends before its metadata size")
        metadata_size = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        end = offset + metadata_size
        if end > len(data):
            raise ContainerError(
                "the metadata block claims %d bytes past the end of the file" % (metadata_size,)
            )
        while offset + 2 <= end:
            packed = struct.unpack_from("<H", data, offset)[0]
            field_id = packed >> 8
            size = packed & 0xFF
            offset += 2
            if offset + size > end:
                warnings.append("metadata field %d overruns the block" % (field_id,))
                break
            metadata.append(MetadataField(field_id, bytes(data[offset:offset + size])))
            offset += size
        if offset != end:
            warnings.append("metadata block has trailing bytes")
            offset = end
    else:
        metadata_size = 0

    if offset + 2 > len(data):
        raise ContainerError("the file ends before its transport and protocol versions")
    transport_version = data[offset]
    protocol_version = data[offset + 1]
    offset += 2
    return ContainerHeader(
        magic=magic,
        metadata_size=metadata_size,
        metadata=metadata,
        transport_version=transport_version,
        protocol_version=protocol_version,
        first_packet_offset=offset,
        warnings=warnings,
    )


def walk_packets(
    data: bytes, header: ContainerHeader
) -> Tuple[List[PacketRow], List[Anomaly]]:
    """Walk every packet header to the end of the file, collecting anomalies.

    A packet that cannot be trusted (a size smaller than its own header, a
    payload past the end of the file) stops the walk and is reported rather
    than guessed past: everything after it would be a misread.
    """
    rows: List[PacketRow] = []
    anomalies: List[Anomaly] = []
    offset = header.first_packet_offset
    size = len(data)
    while offset < size:
        if offset + PACKET_HEADER_SIZE > size:
            anomalies.append(_anomaly(
                "truncated-packet-header", offset, size - offset,
                "%d trailing byte(s) cannot hold a packet header" % (size - offset,),
            ))
            return rows, anomalies
        packet_size, tid_raw = struct.unpack_from("<HH", data, offset)
        if packet_size < PACKET_HEADER_SIZE:
            anomalies.append(_anomaly(
                "bad-packet-size", offset, packet_size,
                "packet claims %d bytes, fewer than its own header" % (packet_size,),
            ))
            return rows, anomalies
        if offset + packet_size > size:
            anomalies.append(_anomaly(
                "truncated-packet", offset, packet_size,
                "packet claims %d bytes, %d remain" % (packet_size, size - offset),
            ))
            return rows, anomalies
        tid = tid_raw & TID_MASK
        form = "raw"
        decoded_size = packet_size - PACKET_HEADER_SIZE
        if tid == 0x3FFF:
            form = "sync"
            decoded_size = 0
        elif tid_raw & ENCODED_MARKER:
            form = "lz4"
            if packet_size < PACKET_HEADER_SIZE + 2:
                anomalies.append(_anomaly(
                    "truncated-packet", offset, packet_size,
                    "encoded packet has no decoded-size field",
                ))
                return rows, anomalies
            decoded_size = struct.unpack_from("<H", data, offset + PACKET_HEADER_SIZE)[0]
        elif tid_raw & VERIFICATION_MARKER:
            form = "verify"
        rows.append(PacketRow(
            index=len(rows),
            offset=offset,
            size=packet_size,
            tid=tid,
            form=form,
            decoded_size=decoded_size,
        ))
        offset += packet_size
    return rows, anomalies


def tid_name(tid: int) -> str:
    """The display name of a thread id: its special name, or the number."""
    return TID_NAMES.get(tid, str(tid))
