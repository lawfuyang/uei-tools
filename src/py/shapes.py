"""Shapes, constants and errors shared by every layer of the tool.

This module is the bottom of the layering: it imports nothing from the other
modules, and everything may import it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Tuple, TypedDict

TOOL_NAME = "ueia"
#: 0.2.0 added `frame_work` to the model (the per-frame work attribution the summary reports).
#: 0.3.0 added the frame occupancy (`covered_cycles`/`wait_cycles`), the decoded GPU frames and the
#: GPU specs -- the bottleneck verdict's inputs. The cache is keyed by this string, so the bump is
#: what stops a model built by an older version, which has none of those, from being read back and
#: reported as a capture that had no occupancy and no GPU work.
TOOL_VERSION = "0.3.0"
CACHE_FORMAT = 1

MAGIC = b"2CRT"
MAGIC_LEGACY_METADATA = b"TRC2"
MAGIC_LEGACY_BE = b"ECRT"
MAGIC_LEGACY_RAW = b"TRCE"
TRANSPORT_TID_PACKET_SYNC = 4
PROTOCOL_CURRENT = 7

PACKET_HEADER_SIZE = 4
ENCODED_MARKER = 0x8000
VERIFICATION_MARKER = 0x4000
TID_MASK = 0x3FFF

TID_EVENTS = 0
TID_IMPORTANTS = 1
TID_BIAS = 2
TID_PSEUDO_IMPORTANTS = 0x3FFE
TID_SYNC = 0x3FFF

TID_NAMES: Dict[int, str] = {
    TID_EVENTS: "Events",
    TID_IMPORTANTS: "Importants",
    TID_PSEUDO_IMPORTANTS: "PseudoImportants",
    TID_SYNC: "Sync",
}

UID_NEW_EVENT = 0
UID_AUX_DATA = 1
UID_AUX_DATA_TERMINAL = 3
UID_SCOPE_ENTER = 4
UID_SCOPE_LEAVE = 5
UID_SCOPE_ENTER_TA = 6
UID_SCOPE_LEAVE_TA = 7
UID_SCOPE_ENTER_TB = 8
UID_SCOPE_LEAVE_TB = 9
UID_USER = 16

SCOPE_PLAIN_UIDS = frozenset((UID_SCOPE_ENTER, UID_SCOPE_LEAVE))
SCOPE_TIMED_UIDS = frozenset(
    (UID_SCOPE_ENTER_TA, UID_SCOPE_LEAVE_TA, UID_SCOPE_ENTER_TB, UID_SCOPE_LEAVE_TB)
)
UID_NAMES: Dict[int, str] = {
    UID_NEW_EVENT: "NewEvent",
    UID_AUX_DATA: "AuxData",
    UID_AUX_DATA_TERMINAL: "AuxDataTerminal",
    UID_SCOPE_ENTER: "EnterScope",
    UID_SCOPE_LEAVE: "LeaveScope",
    UID_SCOPE_ENTER_TA: "EnterScope_TA",
    UID_SCOPE_LEAVE_TA: "LeaveScope_TA",
    UID_SCOPE_ENTER_TB: "EnterScope_TB",
    UID_SCOPE_LEAVE_TB: "LeaveScope_TB",
}

EVENT_FLAG_IMPORTANT = 1 << 0
EVENT_FLAG_MAYBE_HAS_AUX = 1 << 1
EVENT_FLAG_NOSYNC = 1 << 2
EVENT_FLAG_DEFINITION = 1 << 3

FIELD_FAMILY_REGULAR = 0
FIELD_FAMILY_REFERENCE = 1
FIELD_FAMILY_DEFINITION_ID = 2
FIELD_FAMILY_NAMES: Dict[int, str] = {
    FIELD_FAMILY_REGULAR: "regular",
    FIELD_FAMILY_REFERENCE: "reference",
    FIELD_FAMILY_DEFINITION_ID: "definition-id",
}

FIELD_CATEGORY_MASK = 0xC0
FIELD_FLOAT = 0x40
FIELD_ARRAY = 0x80
FIELD_SPECIAL_MASK = 0x18
FIELD_STRING = 0x08
FIELD_SIGNED = 0x10
FIELD_SIZE_MASK = 0x03
FIELD_SIZE_SHIFT = 3

SERIAL_MASK = 0xFFFFFF

ENV_ENGINE_DIR = "UEI_ENGINE_DIR"
ENV_NO_CACHE = "UEI_NO_CACHE"
ENV_PROFILE = "UEI_PROFILE"
ENV_PROGRESS = "UEI_PROGRESS"


class UeiaError(Exception):
    """A user-facing failure: printed to stderr, exit code 1."""


class UsageError(UeiaError):
    """A bad command line: printed to stderr, exit code 2."""


class Lz4Error(UeiaError):
    """A malformed or undecodable LZ4 block."""

    def __init__(self, message: str, offset: Optional[int] = None) -> None:
        super().__init__(message)
        self.offset = offset


class ContainerError(UeiaError):
    """The file's container could not be read."""


class PacketError(UeiaError):
    """The packet layer could not be walked to the end of the file."""


class FieldRow(TypedDict):
    """One field of one event type, as the capture's own schema declares it."""

    index: int
    name: str
    family: int
    family_name: str
    offset: int
    size: int
    type_byte: int
    type_name: str
    ref_uid: int


class EventTypeRow(TypedDict):
    """One NewEvent record: the file's vocabulary, decoded."""

    uid: int
    flags: int
    flag_names: str
    logger: str
    name: str
    full_name: str
    size: int
    fields: List[FieldRow]
    field_names: Dict[str, int]


class MetadataField(NamedTuple):
    """One field of the container's metadata block."""

    field_id: int
    data: bytes


class ContainerHeader(NamedTuple):
    """What the container's own header says about the file."""

    magic: bytes
    metadata_size: int
    metadata: List[MetadataField]
    transport_version: int
    protocol_version: int
    first_packet_offset: int
    warnings: List[str]


@dataclass(frozen=True)
class PacketRow(object):
    """One packet of the stream, as its 4-byte header describes it.

    A dataclass rather than a NamedTuple: a field called `index` would shadow
    `tuple.index`, which the type checker rejects (and rightly so).
    """

    index: int
    offset: int
    size: int
    tid: int
    form: str
    decoded_size: int


class RawEvent(NamedTuple):
    """One event of one thread's stream, framed but not yet interpreted."""

    uid: int
    serial: Optional[int]
    offset: int
    size: int
    aux: List[Tuple[int, int, int]]
    b_scope: bool


class TimerRow(TypedDict):
    """One CpuProfiler timer spec."""

    id: int
    name: str
    file: str
    line: int


class ThreadRow(TypedDict):
    """One thread: what the packet layer and the stream saw of it.

    Every key is always present. `""` for a name or a group, and `0` for a
    system id, cycle or end cycle, mean the capture did not say -- a thread
    with no timing events has no cycles, and that is what zero records.
    """

    tid: int
    name: str
    system_id: int
    group: str
    packets: int
    bytes: int
    events: int
    sync_events: int
    batches: int
    batch_records: int
    first_cycle: int
    last_cycle: int
    end_cycle: int


class FrameRow(TypedDict):
    """One frame: a BeginFrame/EndFrame pair of one frame type on one thread.

    `covered_cycles` and `wait_cycles` are the frame's **occupancy** -- the cycles of this frame's
    window the frame's own thread spent inside a `CpuProfiler` scope (any depth, overlapping scopes
    merged), and the part of that spent inside a scope whose name reads as a wait
    (`model.WAIT_NAME_MARKERS`: `WaitForTasks`, `WaitForGPU`, ...). Coverage is what answers "was
    this thread doing anything during this frame", and the wait split is what stops a thread that is
    *waiting* from reading as a thread that is *working* -- the corpus's render thread is inside
    scopes for 97% of its frames, and 99.7% of that is `WaitForTasks`.

    Both are None when the capture declared no timer specs: nothing could be attributed, which is
    **not** the same as zero coverage, and a report must be able to tell those apart.
    """

    index: int
    type: int
    tid: int
    begin_cycle: int
    end_cycle: int
    covered_cycles: Optional[int]
    wait_cycles: Optional[int]


class FrameWorkRow(TypedDict):
    """What one frame was spent on: the scope pairs attributed to it, biggest first.

    The walk keeps this only for the **longest frames of each thread** (`model._FRAME_WORK_KEEP`),
    because the frame-time report asks about the tail -- a capture with a hundred thousand frames
    must not put a hundred thousand of these in the cache to answer a question about its worst
    twenty. So this is a sample by construction, and a command that lists a frame without one says
    so rather than reporting an empty frame.

    `cycles` is the frame's own duration (`end_cycle - begin_cycle`); each item is a timer spec id
    and the **inclusive** cycles attributed to it -- a scope that contains another counts its
    children's time too, which is what "this timer owns the frame" means. The times of the items in
    one frame can therefore add up to more than the frame, and a pair that began before the frame
    is counted (`scope_pairs_spanning`) rather than attributed.
    """

    tid: int
    type: int
    begin_cycle: int
    end_cycle: int
    cycles: int
    pairs: int
    items: List[Tuple[int, int]]


class GpuSpecRow(TypedDict):
    """One GPU timer spec: the id a frame's batch refers to, and the name it stands for.

    The legacy `GpuProfiler` channel (the one the corpus carries, and the one UE 5.8.3 still
    decodes for old traces -- `OldGpuProfilerTraceAnalysis.cpp`) names its events through
    `GpuProfiler.EventSpec`, an important event whose `EventType` is an opaque uint32 id.
    """

    id: int
    name: str


class GpuFrameRow(TypedDict):
    """One rendered frame as the GPU saw it: how long the GPU was busy, and on what.

    `busy_us` is the sum of the frame batch's **outermost** GPU event durations -- outermost spans
    cannot overlap, so their sum is the union: the time the GPU was executing traced work in that
    frame. `passes` is the same idea per spec, biggest first, and `depth` is how deeply the batch
    nested (a sanity number: a wrong decoder leaves events open, which `unbalanced` counts).

    `unbalanced` and `truncated` are the row's own honesty: a batch whose events do not close, whose
    varint runs off the end, or which claims an implausible duration (`gpu.MAX_FRAME_US`) has no
    usable busy time, and `bottleneck` ignores exactly the rows whose counters are not zero rather
    than reading them as a fast frame.
    """

    tid: int
    number: int
    base_us: int
    busy_us: int
    events: int
    depth: int
    unbalanced: int
    truncated: int
    passes: List[Tuple[int, int]]


class BookmarkRow(TypedDict):
    """One bookmark instance, joined to its spec by the point it names."""

    cycle: int
    point: int
    name: str
    file: str
    line: int


class CounterSpecRow(TypedDict):
    """One counters spec."""

    id: int
    type: int
    display_hint: int
    name: str


class CsvStatRow(TypedDict):
    """One CsvProfiler stat definition."""

    id: int
    category: int
    name: str
    kind: str


def field_is_string(type_byte: int) -> bool:
    """True when a field's type byte names an AnsiString or WideString."""
    return (type_byte & FIELD_SPECIAL_MASK) == FIELD_STRING


def field_is_array(type_byte: int) -> bool:
    """True when a field's type byte names an array (strings included)."""
    return bool(type_byte & FIELD_ARRAY)


def field_is_float(type_byte: int) -> bool:
    """True when a field's type byte names a float."""
    return (type_byte & FIELD_CATEGORY_MASK) == FIELD_FLOAT


def field_size(type_byte: int) -> int:
    """The fixed byte size a field's type byte declares (1, 2, 4 or 8)."""
    return 1 << (type_byte & FIELD_SIZE_MASK)


def field_type_name(type_byte: int) -> str:
    """A stable, human name for a field's type byte (octal bitfields)."""
    if field_is_string(type_byte):
        return "AnsiString" if (type_byte & FIELD_SIZE_MASK) == 0 else "WideString"
    if field_is_array(type_byte):
        return "Array"
    if field_is_float(type_byte):
        return "f32" if (type_byte & FIELD_SIZE_MASK) == 2 else "f64"
    signed = bool(type_byte & FIELD_SIGNED)
    size = field_size(type_byte)
    if type_byte == 0:
        return "bool"
    if signed:
        return "i" + str(size * 8)
    return "u" + str(size * 8)


def flag_names(flags: int) -> str:
    """The schema flags of an event type, spelled out."""
    names: List[str] = []
    if flags & EVENT_FLAG_IMPORTANT:
        names.append("Important")
    if flags & EVENT_FLAG_MAYBE_HAS_AUX:
        names.append("MaybeHasAux")
    if flags & EVENT_FLAG_NOSYNC:
        names.append("NoSync")
    if flags & EVENT_FLAG_DEFINITION:
        names.append("Definition")
    return "+".join(names) if names else "none"


def env_flag(name: str) -> bool:
    """True when an environment variable is set to something truthy."""
    value = os.environ.get(name, "")
    return value not in ("", "0", "false", "False")
