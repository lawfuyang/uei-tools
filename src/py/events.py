"""Framing: how events sit in a thread's byte stream, and how to walk them.

Two framings exist, and both were read off the engine's own reader:

* **Thread streams** (every real thread): the uid is shifted (`uid << 1`, one
  byte for well-known ids) with bit 0 set on the two-byte form; sync events
  carry a 24-bit serial after the uid; a MaybeHasAux event's strings and
  arrays follow its fixed payload as `[uid][field/size][uint16 size]` blocks,
  ended by an AuxDataTerminal byte.
* **Special streams** (Events, Importants): `[uint16 uid][uint16 size]` records
  with the uid *unshifted* -- the important cache is replayed ahead of the
  events that would otherwise describe it.

A walk that cannot trust its position stops (and says so) rather than guessing
past a corrupt record: everything after it would be a misread.
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Protocol, Tuple

from container import Anomaly
from shapes import (
    EventTypeRow,
    RawEvent,
    SCOPE_PLAIN_UIDS,
    SCOPE_TIMED_UIDS,
    SERIAL_MASK,
    UID_AUX_DATA,
    UID_AUX_DATA_TERMINAL,
    UID_USER,
)

AUX_FIELD_MASK = 0x1F
AUX_SIZE_SHIFT = 13

AUX_FIELD_INDEX_SIZE = 2
IMPORTANT_HEADER_SIZE = 4
SYNC_EVENT_SERIAL_SIZE = 3

#: A timestamped scope marker is eight bytes *in total*: the writer packs
#: `(cycles << 8) | uid << 1` into one uint64, so the uid byte and seven bytes
#: of timestamp are written together (`FScopedStampedLogScope::Deinit`).
SCOPE_TIMED_PAYLOAD_SIZE = 7


class RegistryLike(Protocol):
    """What the event walker needs to know about the capture's vocabulary."""

    def get(self, uid: int) -> Optional[EventTypeRow]:
        ...

    def is_sync(self, uid: int) -> bool:
        ...

    def has_aux(self, uid: int) -> bool:
        ...

    def size(self, uid: int) -> int:
        ...


def decode7bit(data: bytes, offset: int) -> Tuple[int, int]:
    """One little-endian 7-bits-per-byte varint, as `FTraceUtils::Encode7bit` writes."""
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("7-bit value runs past the end of its buffer")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("7-bit value is longer than 64 bits")


def iter_important_records(
    stream: bytes, anomalies: List[Anomaly]
) -> Iterator[Tuple[int, int, int]]:
    """Yield (uid, size, data offset) for every record of a special stream."""
    offset = 0
    length = len(stream)
    while offset < length:
        if offset + IMPORTANT_HEADER_SIZE > length:
            anomalies.append((
                "truncated-record", offset, length - offset,
                "%d trailing byte(s) cannot hold a record header" % (length - offset,),
            ))
            return
        uid = stream[offset] | (stream[offset + 1] << 8)
        size = stream[offset + 2] | (stream[offset + 3] << 8)
        if offset + IMPORTANT_HEADER_SIZE + size > length:
            anomalies.append((
                "truncated-record", offset, size,
                "record uid %d claims %d bytes, %d remain"
                % (uid, size, length - offset - IMPORTANT_HEADER_SIZE),
            ))
            return
        yield uid, size, offset + IMPORTANT_HEADER_SIZE
        offset += IMPORTANT_HEADER_SIZE + size


def _walk_aux(
    stream: bytes, offset: int, anomalies: List[Anomaly], counts: Dict[str, int]
) -> Tuple[int, List[Tuple[int, int, int]]]:
    """Read an event's aux blocks up to their terminal. Returns (offset, blocks)."""
    length = len(stream)
    blocks: List[Tuple[int, int, int]] = []
    while True:
        if offset >= length:
            anomalies.append((
                "missing-aux-terminal", offset, 0,
                "the stream ends inside an aux block list",
            ))
            return offset, blocks
        uid_byte = stream[offset]
        if uid_byte == (UID_AUX_DATA_TERMINAL << 1):
            return offset + 1, blocks
        if uid_byte != (UID_AUX_DATA << 1):
            anomalies.append((
                "bad-aux-block", offset, uid_byte,
                "expected an aux block or its terminal, found uid byte 0x%02x" % (uid_byte,),
            ))
            return offset, blocks
        if offset + 4 > length:
            anomalies.append((
                "truncated-aux-block", offset, 0, "the aux header runs past the stream",
            ))
            return offset, blocks
        field_index_size = stream[offset + 1]
        size = ((stream[offset + 2] | (stream[offset + 3] << 8)) << 3) | (
            field_index_size >> 5
        )
        data_offset = offset + 4
        if data_offset + size > length:
            anomalies.append((
                "truncated-aux-block", offset, size,
                "aux block claims %d bytes, %d remain" % (size, length - data_offset),
            ))
            return offset, blocks
        blocks.append((field_index_size & AUX_FIELD_MASK, data_offset, size))
        counts["aux_blocks"] += 1
        offset = data_offset + size


def walk_record_aux(stream: bytes, offset: int, end: int) -> List[Tuple[int, int, int]]:
    """Aux blocks inside a bounded record (an important event's own payload).

    Important records frame their aux blocks differently from thread streams,
    and the writer is the authority (`FImportantLogScope::FFieldSet`): the uid
    bytes are **unshifted** (`AuxData`=1, `AuxDataTerminal`=3), and a terminal
    follows **every** aux field rather than ending one list. The record's own
    size is the bound, so a terminal is skipped and the walk continues.
    """
    blocks: List[Tuple[int, int, int]] = []
    while offset + 4 <= end:
        if stream[offset] == UID_AUX_DATA_TERMINAL:
            offset += 1
            continue
        if stream[offset] != UID_AUX_DATA:
            return blocks
        field_index_size = stream[offset + 1]
        size = ((stream[offset + 2] | (stream[offset + 3] << 8)) << 3) | (
            field_index_size >> 5
        )
        data_offset = offset + 4
        if data_offset + size > end:
            return blocks
        blocks.append((field_index_size & AUX_FIELD_MASK, data_offset, size))
        offset = data_offset + size
    return blocks


def iter_thread_events(
    stream: bytes,
    tid: int,
    registry: RegistryLike,
    anomalies: List[Anomaly],
    counts: Dict[str, int],
) -> Iterator[RawEvent]:
    """Yield every framed event of one thread's stream, in stream order."""
    offset = 0
    length = len(stream)
    while offset < length:
        uid_byte = stream[offset]
        if uid_byte & 1:
            if offset + 2 > length:
                anomalies.append((
                    "truncated-event", offset, 0, "the two-byte uid runs past the stream",
                ))
                return
            uid = (stream[offset] | (stream[offset + 1] << 8)) >> 1
            offset += 2
        else:
            uid = uid_byte >> 1
            offset += 1

        serial: Optional[int] = None
        size = 0
        has_aux = False
        if uid in SCOPE_PLAIN_UIDS:
            counts["scopes"] += 1
            yield RawEvent(uid=uid, serial=None, offset=offset, size=0, aux=[], b_scope=True)
            continue
        if uid in SCOPE_TIMED_UIDS:
            counts["scopes"] += 1
            size = SCOPE_TIMED_PAYLOAD_SIZE
            if offset + size > length:
                anomalies.append((
                    "truncated-event", offset, size, "a timestamped scope runs past the stream",
                ))
                return
            yield RawEvent(uid=uid, serial=None, offset=offset, size=size, aux=[], b_scope=True)
            offset += size
            continue
        if uid < UID_USER:
            anomalies.append((
                "unknown-well-known-uid", offset, uid,
                "well-known uid %d has no framing this parser knows; stopping at offset %d"
                % (uid, offset),
            ))
            return

        row = registry.get(uid)
        if row is None:
            anomalies.append((
                "unknown-uid", offset, uid,
                "event uid %d is not declared by this capture; stopping at offset %d"
                % (uid, offset),
            ))
            counts["unknown_uid"] += 1
            return
        size = registry.size(uid)
        has_aux = registry.has_aux(uid)
        if registry.is_sync(uid):
            if offset + SYNC_EVENT_SERIAL_SIZE > length:
                anomalies.append((
                    "truncated-event", offset, 0, "the event's serial runs past the stream",
                ))
                return
            serial = (
                stream[offset] | (stream[offset + 1] << 8) | (stream[offset + 2] << 16)
            ) & SERIAL_MASK
            offset += SYNC_EVENT_SERIAL_SIZE
            counts["sync_events"] += 1

        if offset + size > length:
            anomalies.append((
                "truncated-event", offset, size,
                "event %d claims %d bytes, %d remain" % (uid, size, length - offset),
            ))
            return
        payload_offset = offset
        offset += size
        aux: List[Tuple[int, int, int]] = []
        if has_aux:
            offset, aux = _walk_aux(stream, offset, anomalies, counts)
        counts["events"] += 1
        yield RawEvent(
            uid=uid, serial=serial, offset=payload_offset, size=size, aux=aux, b_scope=False
        )
