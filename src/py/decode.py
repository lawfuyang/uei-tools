"""Interpreting event payloads: schema-driven values, and the batch format.

Everything here reads values through the capture's own field descriptors --
offset, size and type byte -- so a field that a newer engine moved, resized or
renamed still decodes, and a field that is not there reads as absent rather
than as a guess.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from events import decode7bit
from shapes import (
    EventTypeRow,
    FIELD_FAMILY_REGULAR,
    FIELD_SIGNED,
    RawEvent,
    field_is_array,
    field_is_float,
    field_is_string,
    field_size,
)

_INT_FORMATS = {1: "b", 2: "h", 4: "i", 8: "q"}
_UINT_FORMATS = {1: "B", 2: "H", 4: "I", 8: "Q"}
_FLOAT_FORMATS = {4: "f", 8: "d"}


def read_value(stream: bytes, offset: int, type_byte: int) -> object:
    """One fixed-size field's value, by its type byte.

    A zero type byte is `Bool` to the engine's own naming, but `bool` and
    `uint8` are the same byte in the protocol -- the wire value is the number,
    so that is what comes back, and a caller that wants a bool asks for one.
    """
    size = field_size(type_byte)
    if field_is_float(type_byte):
        return struct.unpack_from("<" + _FLOAT_FORMATS[size], stream, offset)[0]
    if type_byte & FIELD_SIGNED:
        return struct.unpack_from("<" + _INT_FORMATS[size], stream, offset)[0]
    return struct.unpack_from("<" + _UINT_FORMATS[size], stream, offset)[0]


def decode_string(stream: bytes, offset: int, size: int, wide: bool) -> str:
    """One string field's value out of its aux block (no terminator is stored)."""
    data = stream[offset:offset + size]
    if wide:
        return data.decode("utf-16-le", "replace").rstrip("\x00")
    return data.decode("utf-8", "replace").rstrip("\x00")


def event_values(
    row: EventTypeRow, stream: bytes, event: RawEvent, wide_as_bytes: bool = False
) -> Dict[str, object]:
    """Every regular field of one event, named, out of payload and aux blocks.

    Strings and arrays come from the aux blocks the walker recorded, matched by
    field index. A long string or array is written as several segments, **each
    with its own header and the same field index** (the writer splits at buffer
    boundaries), so all of a field's segments are concatenated in order -- the
    last one alone is a truncated value, which is how a batch blob loses its
    final varint. Fixed values are read at the descriptor's offset; the
    reference and definition-id families are not interpreted here.
    """
    values: Dict[str, object] = {}
    segments: Dict[int, List[Tuple[int, int, int]]] = {}
    for block in event.aux:
        segments.setdefault(block[0], []).append(block)
    for field in row["fields"]:
        if field["family"] != FIELD_FAMILY_REGULAR:
            continue
        type_byte = int(field["type_byte"])
        if field_is_string(type_byte):
            blocks = segments.get(int(field["index"]))
            if not blocks:
                continue
            # an important record writes a "wide" string one byte per character
            wide = (type_byte & 0x03) != 0 and not wide_as_bytes
            if len(blocks) == 1:
                values[field["name"]] = decode_string(stream, blocks[0][1], blocks[0][2], wide)
            else:
                joined = b"".join(stream[off:off + size] for _index, off, size in blocks)
                values[field["name"]] = decode_string(joined, 0, len(joined), wide)
        elif field_is_array(type_byte):
            blocks = segments.get(int(field["index"]))
            if not blocks:
                values[field["name"]] = b""
            elif len(blocks) == 1:
                values[field["name"]] = stream[blocks[0][1]:blocks[0][1] + blocks[0][2]]
            else:
                values[field["name"]] = b"".join(
                    stream[off:off + size] for _index, off, size in blocks
                )
        else:
            offset = event.offset + int(field["offset"])
            if offset + field_size(type_byte) > event.offset + event.size:
                continue
            values[field["name"]] = read_value(stream, offset, type_byte)
    return values


def value_int(values: Dict[str, object], name: str) -> Optional[int]:
    """A named integer field, or None when the capture does not carry it."""
    value = values.get(name)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def value_str(values: Dict[str, object], name: str) -> str:
    """A named string field, or "" when the capture does not carry it."""
    value = values.get(name)
    return value if isinstance(value, str) else ""


def decode_batch(blob: bytes) -> Tuple[List[Tuple[int, Optional[int], bool]], int, str]:
    """One CpuProfiler EventBatch blob, as the engine's own analyzer reads it.

    Every record starts with `(cycle << 2) | flags`, one 7-bit varint. A
    **begin** record (bit 0 set) carries the spec id as a second varint; an
    **end** record carries nothing more -- the engine pairs it against that
    thread's own scope stack, so there is no id on the wire. Bit 1 marks the
    coroutine forms V3 added, which carry a depth (and, when they begin, an
    id); this parser steps over those without interpreting them.

    Returns (records, coroutine_records, error), a record being
    (cycle-value-or-absolute, spec id or None, is-begin).
    """
    records: List[Tuple[int, Optional[int], bool]] = []
    coroutine_records = 0
    offset = 0
    length = len(blob)
    # The loop below runs once per record of every batch in a capture -- 10,055,971 times on the
    # corpus -- and `decode7bit` was the single hottest function in the tool (15.08 M calls, 8.7 s of
    # a profiled serial parse). Its one-byte form is the commonest by far (a delta, a spec id), so
    # that case is decoded here, inline, without a call at all; anything longer falls through to it.
    append = records.append
    d7 = decode7bit
    end_record = (0, None, False)
    while offset < length:
        packed = blob[offset]
        if packed < 0x80:
            offset += 1
        else:
            try:
                packed, offset = d7(blob, offset)
            except ValueError as exc:
                return records, coroutine_records, str(exc)
        is_begin = bool(packed & 1)
        if packed & 2:
            coroutine_records += 1
            try:
                if is_begin:
                    _coroutine_id, offset = d7(blob, offset)
                _depth, offset = d7(blob, offset)
            except ValueError as exc:
                return records, coroutine_records, str(exc)
            append((packed >> 2, None, is_begin))
            continue
        if is_begin:
            if offset >= length:
                return records, coroutine_records, "7-bit value runs past the end of its buffer"
            spec_id = blob[offset]
            if spec_id < 0x80:
                offset += 1
            else:
                try:
                    spec_id, offset = d7(blob, offset)
                except ValueError as exc:
                    return records, coroutine_records, str(exc)
            append((packed >> 2, spec_id, True))
        else:
            end_record = (packed >> 2, None, False)
            append(end_record)
    return records, coroutine_records, ""
