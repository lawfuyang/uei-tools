"""Building valid `.utrace` bytes in memory: what every hermetic test stands on.

The fixtures follow the format REFERENCE.md documents, written from the same
source the parser reads: the 2CRT header, the packet layer (raw, LZ4 and sync
forms), NewEvent schema records, and thread events with every uid form and aux
block shape. An LZ4 fixture block is emitted as literals only (a legal block
whose last sequence has no match), so no compressor is needed to test the
decompressor.
"""

from __future__ import annotations

import struct
from typing import Dict, Optional, Sequence, Tuple

MAGIC = b"2CRT"
ENCODED_MARKER = 0x8000
TID_EVENTS = 0
TID_IMPORTANTS = 1
TID_SYNC = 0x3FFF
TID_PSEUDO_IMPORTANTS = 0x3FFE

UID_NEW_EVENT = 0
UID_AUX_DATA = 1
UID_AUX_DATA_TERMINAL = 3
UID_SCOPE_ENTER = 4
UID_SCOPE_LEAVE = 5
UID_SCOPE_ENTER_TA = 6
UID_SCOPE_LEAVE_TA = 7
UID_SCOPE_ENTER_TB = 8
UID_SCOPE_LEAVE_TB = 9

EVENT_FLAG_IMPORTANT = 1
EVENT_FLAG_MAYBE_HAS_AUX = 2
EVENT_FLAG_NOSYNC = 4
EVENT_FLAG_DEFINITION = 8

#: kind -> (type byte, fixed size in the payload; strings and arrays have none)
FIELD_KINDS: Dict[str, Tuple[int, int]] = {
    "u8": (0x00, 1),
    "u16": (0x01, 2),
    "u32": (0x02, 4),
    "u64": (0x03, 8),
    "i8": (0x10, 1),
    "i16": (0x11, 2),
    "i32": (0x12, 4),
    "i64": (0x13, 8),
    "f32": (0x42, 4),
    "f64": (0x43, 8),
    "s": (0x88, 0),
    "ws": (0x89, 0),
    "arr": (0x80, 0),
}

DEFAULT_METADATA: Tuple[Tuple[int, bytes], ...] = (
    (0, struct.pack("<H", 1985)),
    (1, bytes(range(16))),
    (2, bytes(range(16, 32))),
)


def pack(kind: str, value: float) -> bytes:
    """One field's payload bytes, by its fixture kind.

    `value` is typed as a float because every numeric kind is one -- an int is
    accepted and packed exactly (no float conversion happens: `int(value)`
    returns the int it was given).
    """
    if kind == "u8":
        return struct.pack("<B", int(value))
    if kind == "u16":
        return struct.pack("<H", int(value))
    if kind == "u32":
        return struct.pack("<I", int(value))
    if kind == "u64":
        return struct.pack("<Q", int(value))
    if kind == "i8":
        return struct.pack("<b", int(value))
    if kind == "i16":
        return struct.pack("<h", int(value))
    if kind == "i32":
        return struct.pack("<i", int(value))
    if kind == "i64":
        return struct.pack("<q", int(value))
    if kind == "f32":
        return struct.pack("<f", float(value))
    if kind == "f64":
        return struct.pack("<d", float(value))
    raise AssertionError("kind %r has no fixed payload" % (kind,))


def metadata_block(fields: Sequence[Tuple[int, bytes]] = DEFAULT_METADATA) -> bytes:
    """The handshake's metadata block: uint16 size, then (size|id<<8) + bytes."""
    body = bytearray()
    for field_id, data in fields:
        body += struct.pack("<H", len(data) | (field_id << 8))
        body += data
    return struct.pack("<H", len(body)) + bytes(body)


def lz4_literal_block(data: bytes) -> bytes:
    """A legal LZ4 block holding `data` as literals only (no match sequence)."""
    out = bytearray()
    length = len(data)
    if length < 15:
        out.append(length << 4)
    else:
        out.append(0xF0)
        remaining = length - 15
        while remaining >= 255:
            out.append(255)
            remaining -= 255
        out.append(remaining)
    out += data
    return bytes(out)


def packet(tid: int, payload: bytes, encoded: bool = False) -> bytes:
    """One TID packet (header + payload), LZ4-encoded on request."""
    body = payload
    if encoded:
        compressed = lz4_literal_block(payload)
        body = struct.pack("<H", len(payload)) + compressed
        header = struct.pack("<HH", 4 + len(body), ENCODED_MARKER | tid)
        return header + body
    header = struct.pack("<HH", 4 + len(body), tid)
    return header + body


def sync_packet() -> bytes:
    """A sync packet: a bare header on the sync thread id."""
    return struct.pack("<HH", 4, TID_SYNC)


def build_trace(
    events_stream: bytes = b"",
    importants_stream: bytes = b"",
    threads: Optional[Dict[int, bytes]] = None,
    protocol: int = 7,
    transport: int = 4,
    encode_events: bool = False,
    packet_chunk: int = 4096,
    metadata: Sequence[Tuple[int, bytes]] = DEFAULT_METADATA,
) -> bytes:
    """A whole capture: 2CRT, metadata, versions, then its packets."""
    out = bytearray()
    out += MAGIC
    out += metadata_block(metadata)
    out += bytes((transport, protocol))
    if events_stream:
        out += packet(TID_EVENTS, events_stream, encoded=encode_events)
    if importants_stream:
        out += packet(TID_IMPORTANTS, importants_stream)
    sources: Dict[int, bytes] = threads or {}
    for tid in sorted(sources):
        stream = sources[tid]
        for start in range(0, len(stream), packet_chunk):
            out += packet(tid, stream[start:start + packet_chunk])
    return bytes(out)


def important_record(uid: int, payload: bytes) -> bytes:
    """One record of a special stream: [uint16 uid][uint16 size][payload]."""
    return struct.pack("<HH", uid, len(payload)) + payload


def new_event_record(
    event_uid: int,
    logger: str,
    name: str,
    fields: Sequence[Tuple[str, str]] = (),
    flags: int = 0,
) -> bytes:
    """A NewEvent record describing one event type, als the writer builds it."""
    descriptors = bytearray()
    names = bytearray(logger.encode("ascii") + name.encode("ascii"))
    offset = 0
    for field_name, kind in fields:
        type_byte, fixed_size = FIELD_KINDS[kind]
        encoded_name = field_name.encode("ascii")
        descriptors += struct.pack(
            "<BBHHBB", 0, 0, offset, fixed_size, type_byte, len(encoded_name)
        )
        names += encoded_name
        offset += fixed_size
    body = bytearray(
        struct.pack(
            "<HBBBB", event_uid, len(fields), flags, len(logger), len(name)
        )
    )
    body += descriptors
    body += names
    if len(body) % 2:
        body += b"\x00"
    return important_record(UID_NEW_EVENT, bytes(body))


def aux_block(field_index: int, data: bytes) -> bytes:
    """One aux block of a thread-stream event: shifted uid, no terminal."""
    field_index_size = (field_index & 0x1F) | ((len(data) & 0x07) << 5)
    return bytes((UID_AUX_DATA << 1, field_index_size)) + struct.pack("<H", len(data) >> 3) + data


def important_aux_block(field_index: int, data: bytes) -> bytes:
    """One aux block as an important record writes it: unshifted uid, terminal after."""
    field_index_size = (field_index & 0x1F) | ((len(data) & 0x07) << 5)
    return (
        bytes((UID_AUX_DATA, field_index_size))
        + struct.pack("<H", len(data) >> 3)
        + data
        + bytes((UID_AUX_DATA_TERMINAL,))
    )


def event(
    uid: int,
    payload: bytes = b"",
    serial: Optional[int] = None,
    aux: Sequence[Tuple[int, bytes]] = (),
    maybe_aux: bool = False,
) -> bytes:
    """One thread-stream event, framed exactly as the walker expects it."""
    out = bytearray()
    if uid < 64:
        out.append(uid << 1)
    else:
        out += struct.pack("<H", (uid << 1) | 1)
    if serial is not None:
        out += struct.pack("<I", serial & 0xFFFFFF)[:3]
    out += payload
    for field_index, data in aux:
        out += aux_block(field_index, data)
    if maybe_aux:
        out.append(UID_AUX_DATA_TERMINAL << 1)
    return bytes(out)


def varint(value: int) -> bytes:
    """One little-endian 7-bits-per-byte varint, as the batch format packs them."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def demo_trace() -> bytes:
    """A small but complete capture: a timer, a frame pair, a batch, a log line."""
    schema = (
        new_event_record(16, "$Trace", "NewTrace",
                         [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                         flags=EVENT_FLAG_NOSYNC)
        + new_event_record(17, "$Trace", "ThreadInfo", [("ThreadId", "u32"), ("Name", "s")],
                           flags=EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX | EVENT_FLAG_NOSYNC)
        + new_event_record(20, "CpuProfiler", "EventSpec",
                           [("Id", "u32"), ("Name", "s"), ("File", "s"), ("Line", "u32")],
                           flags=EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX | EVENT_FLAG_NOSYNC)
        + new_event_record(21, "CpuProfiler", "EventBatchV2", [("Data", "arr")],
                           flags=EVENT_FLAG_NOSYNC | EVENT_FLAG_MAYBE_HAS_AUX)
        + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(30, "Logging", "LogMessage", [("LogPoint", "u64"), ("Cycle", "u64")])
    )
    importants = (
        important_record(16, pack("u64", 1000000) + pack("u64", 1000000))
        + important_record(17, pack("u32", 2) + important_aux_block(1, b"Game"))
        # the fixed fields first (Id, Line), then the strings as aux blocks
        + important_record(20, pack("u32", 7) + pack("u32", 91)
                           + important_aux_block(1, b"Tick")
                           + important_aux_block(2, b"Game.cpp"))
    )
    batch = varint((1400000 << 2) | 1) + varint(7)
    stream = (
        event(22, pack("u64", 1100000) + pack("u8", 0), serial=5)
        + event(23, pack("u64", 1300000) + pack("u8", 0), serial=6)
        + event(21, b"", aux=[(0, batch)], maybe_aux=True)
        + event(30, pack("u64", 1) + pack("u64", 1250000), serial=7)
    )
    return build_trace(events_stream=schema, importants_stream=importants, threads={2: stream})


def work_schema() -> bytes:
    """The vocabulary `work_stream` needs: two timer specs and the frame pair events."""
    return (
        new_event_record(16, "$Trace", "NewTrace",
                         [("StartCycle", "u64"), ("CycleFrequency", "u64")],
                         flags=EVENT_FLAG_NOSYNC)
        + new_event_record(17, "$Trace", "ThreadInfo", [("ThreadId", "u32"), ("Name", "s")],
                           flags=EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX
                           | EVENT_FLAG_NOSYNC)
        + new_event_record(20, "CpuProfiler", "EventSpec",
                           [("Id", "u32"), ("Name", "s"), ("File", "s"), ("Line", "u32")],
                           flags=EVENT_FLAG_IMPORTANT | EVENT_FLAG_MAYBE_HAS_AUX
                           | EVENT_FLAG_NOSYNC)
        + new_event_record(21, "CpuProfiler", "EventBatchV2", [("Data", "arr")],
                           flags=EVENT_FLAG_NOSYNC | EVENT_FLAG_MAYBE_HAS_AUX)
        + new_event_record(22, "Misc", "BeginFrame", [("Cycle", "u64"), ("FrameType", "u8")])
        + new_event_record(23, "Misc", "EndFrame", [("Cycle", "u64"), ("FrameType", "u8")])
    )


def work_stream() -> bytes:
    """One thread's frames and the timer scopes inside them, as `work_trace`'s capture has them."""
    first = (
        varint((1002000 << 2) | 1) + varint(8)      # FrameTime begins at 1,002,000
        + varint((8000 << 2) | 0)                   # ends 8,000 cycles later
        + varint((2000 << 2) | 1) + varint(9)       # WaitForTasks begins at 1,012,000
        + varint((6000 << 2) | 0)                   # ends 6,000 cycles later -- the wait split
    )
    second = (
        varint((1025000 << 2) | 1) + varint(7)      # Tick begins at 1,025,000
        + varint((5000 << 2) | 1) + varint(8)       # FrameTime inside it, at 1,030,000
        + varint((20000 << 2) | 0)                  # FrameTime ends at 1,050,000
        + varint((20000 << 2) | 0)                  # Tick ends at 1,070,000
    )
    return (
        # the frame events are sync events (the capture declares them without NoSync), so each
        # carries a serial: the walker reads three bytes for it before the payload
        event(22, pack("u64", 1000000) + pack("u8", 0), serial=1)
        + event(21, b"", aux=[(0, first)], maybe_aux=True)
        + event(23, pack("u64", 1020000) + pack("u8", 0), serial=2)
        + event(22, pack("u64", 1020000) + pack("u8", 0), serial=3)
        + event(21, b"", aux=[(0, second)], maybe_aux=True)
        + event(23, pack("u64", 1080000) + pack("u8", 0), serial=4)
        + event(22, pack("u64", 1080000) + pack("u8", 0), serial=5)
        + event(23, pack("u64", 1088000) + pack("u8", 0), serial=6)
        + event(22, pack("u64", 1088000) + pack("u8", 0), serial=7)
        + event(23, pack("u64", 1120000) + pack("u8", 0), serial=8)
    )


def work_importants() -> bytes:
    """The specs and thread names `work_stream` refers to."""
    return (
        important_record(16, pack("u64", 1000000) + pack("u64", 1000000))
        + important_record(17, pack("u32", 2) + important_aux_block(1, b"GameThread"))
        + important_record(20, pack("u32", 7) + pack("u32", 91)
                           + important_aux_block(1, b"Tick")
                           + important_aux_block(2, b"Game.cpp"))
        + important_record(20, pack("u32", 8) + pack("u32", 92)
                           + important_aux_block(1, b"FrameTime")
                           + important_aux_block(2, b"Game.cpp"))
        + important_record(20, pack("u32", 9) + pack("u32", 42)
                           + important_aux_block(1, b"WaitForTasks")
                           + important_aux_block(2, b"TaskGraph.cpp"))
    )


def work_trace() -> bytes:
    """A capture whose frames have timer work in them: four frames, one of them a hitch.

    Every number is round on purpose (the frequency is 1,000,000, so 1 cycle is 1 microsecond, and
    the frames start at cycle 1,000,000, so a frame's `at` is its own offset): the frame spans are
    20, 60, 8 and 32 ms, the first holds 8 ms of `FrameTime` and then 6 ms of `WaitForTasks` (so its
    occupancy is 14 ms, 6 of them waiting), and the second holds a nested pair -- 45 ms of `Tick`
    containing 20 ms of `FrameTime`. What that implies for a 60 FPS budget (three frames over it,
    one hitch, p50 20 ms, three histogram bins filled) is pinned by hand in `test_summary`; the
    third and fourth frames carry no work at all, so a report has to say so rather than leave a
    hole.
    """
    return build_trace(
        events_stream=work_schema(), importants_stream=work_importants(),
        threads={2: work_stream()},
    )


def scope(uid: int) -> bytes:
    """A plain EnterScope/LeaveScope marker (one byte, no payload)."""
    return bytes((uid << 1,))


def timed_scope(uid: int, stamp: int) -> bytes:
    """A timestamped scope marker: eight bytes, the uid byte plus seven of stamp.

    The writer packs `(stamp << 8) | uid << 1` into one uint64, so the wire form
    is the uid byte first and the timestamp's low seven bytes after it.
    """
    return struct.pack("<Q", (stamp << 8) | (uid << 1))
