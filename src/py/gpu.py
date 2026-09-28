"""The GPU profiler channel: the legacy `GpuProfiler` frames the older captures carry.

Unreal's *current* GPU channel emits one trace event per GPU work item. The channel the corpus
carries is the **legacy** one (removed from the engine in 5.6 as a writer, kept as a reader: UE
5.8.3 still ships `OldGpuProfilerTraceAnalysis.cpp` "for backward compatibility with old traces"),
which packs a whole rendered frame into one `GpuProfiler.Frame` event. That decoder is the
authority for the layout below, and this module follows it field for field:

* `GpuProfiler.EventSpec` (important): `uint32 EventType` plus a `WideString` **array** `Name` --
  the id-to-name map a frame's records resolve through. Ids are opaque (the writer that chose them
  no longer exists), so the map is the only way to name a GPU pass.
* `GpuProfiler.Frame` (MaybeHasAux): `uint64 CalibrationBias`, `uint64 TimestampBase`,
  `uint32 RenderingFrameNumber`, `uint8[] Data`.

`Data` is a varint-delta stream, not a struct array (`Utils.h`'s `Decode7bit`):

    packed = Decode7bit()                     # LEB128
    timestamp += packed >> 1                  # the delta is in microseconds
    if packed & 1:                            # a begin: its spec id follows as uint32 LE
        spec = uint32(4 bytes)
    else:                                     # an end: no payload
        ...

`busy_us` is the sum of the **outermost** event durations. Outermost intervals cannot overlap, so
that sum is the union: the microseconds the GPU spent executing traced work in that frame. A
nested pass is counted in `passes` as well (inclusive, like the CPU side), which is what makes
"which pass owns the GPU time" answerable. Nothing here interprets `CalibrationBias`: durations are
differences, so a constant bias cancels -- and the frame's `TimestampBase` (which the engine's
decoder uses to prime the delta chain) is kept as `base_us` for the alignment in `bottleneck.py`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from shapes import GpuFrameRow, GpuSpecRow

#: The logger and event names the legacy channel declares (`OldGpuProfilerTraceAnalysis.cpp`).
LOGGER = "GpuProfiler"
SPEC_EVENT = "GpuProfiler.EventSpec"
FRAME_EVENT = "GpuProfiler.Frame"

#: How many passes of a frame are kept for evidence (the biggest by inclusive microseconds).
PASS_KEEP = 6

#: Total microseconds a single frame's batch may claim before it is treated as corrupt. The
#: corpus's worst frame is 80 ms; a value that big means the deltas are being misread.
MAX_FRAME_US = 60 * 60 * 1000000


class Frame(NamedTuple):
    """One decoded `GpuProfiler.Frame`: what the GPU did, and what could not be read."""

    busy_us: int
    events: int
    depth: int
    unbalanced: int
    truncated: int
    passes: List[Tuple[int, int]]


def spec(values: Mapping[str, Any]) -> Optional[GpuSpecRow]:
    """One `GpuProfiler.EventSpec` record: its id and its name.

    The name arrives as an array of UTF-16 code units (the engine reads
    `GetArray<UTF16CHAR>("Name")`), so the bytes are decoded as UTF-16LE. A name that is not valid
    UTF-16 decodes with replacement characters rather than raising: a malformed *name* must not cost
    the capture every GPU timing it has.
    """
    raw_id = values.get("EventType")
    if not isinstance(raw_id, (int, float)):
        return None
    blob = values.get("Name")
    name = ""
    if isinstance(blob, (bytes, bytearray)):
        name = bytes(blob).decode("utf-16-le", "replace").rstrip("\x00")
    elif isinstance(blob, str):
        name = blob
    return GpuSpecRow(id=int(raw_id), name=name)


def _varint(blob: bytes, offset: int) -> Optional[Tuple[int, int]]:
    """LEB128 at `offset`, or None when the varint runs off the end."""
    value = 0
    shift = 0
    while True:
        if offset >= len(blob):
            return None
        byte = blob[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            return None


def decode_frame(blob: bytes, base_us: int) -> Frame:
    """Decode one frame's `Data` array: the layout the engine's own legacy decoder reads.

    Never raises: a truncated varint, a begin without its 4-byte spec id, an end with no begin and
    events left open at the end are all counted (`truncated`, `unbalanced`) and the readable part is
    returned, because a GPU frame that half-decodes is still evidence and a capture must not be lost
    to one bad batch.
    """
    timestamp = base_us
    stack: List[Tuple[int, int]] = []
    passes: Dict[int, int] = {}
    busy = 0
    events = 0
    depth = 0
    unbalanced = 0
    truncated = 0
    offset = 0
    while offset < len(blob):
        read = _varint(blob, offset)
        if read is None:
            truncated += 1
            break
        packed, offset = read
        timestamp += packed >> 1
        if packed & 1:
            if offset + 4 > len(blob):
                truncated += 1
                break
            spec_id = int.from_bytes(blob[offset:offset + 4], "little")
            offset += 4
            events += 1
            stack.append((spec_id, timestamp))
            depth = max(depth, len(stack))
        elif stack:
            spec_id, begin = stack.pop()
            if not stack:
                busy += timestamp - begin
            passes[spec_id] = passes.get(spec_id, 0) + (timestamp - begin)
        else:
            unbalanced += 1
    unbalanced += len(stack)
    if busy > MAX_FRAME_US:
        return Frame(busy_us=0, events=0, depth=0, unbalanced=unbalanced + 1, truncated=truncated,
                     passes=[])
    ordered = sorted(passes.items(), key=lambda item: (-item[1], item[0]))[:PASS_KEEP]
    return Frame(busy_us=busy, events=events, depth=depth, unbalanced=unbalanced,
                 truncated=truncated, passes=[(int(spec_id), int(micro))
                                              for spec_id, micro in ordered])


def frame_row(values: Mapping[str, Any], tid: int, data: Any) -> Optional[GpuFrameRow]:
    """One `GpuProfiler.Frame` record turned into the row the model keeps (None if unreadable)."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    number = values.get("RenderingFrameNumber")
    base = values.get("TimestampBase")
    decoded = decode_frame(bytes(data), int(base) if isinstance(base, (int, float)) else 0)
    return GpuFrameRow(
        tid=tid,
        number=int(number) if isinstance(number, (int, float)) else 0,
        base_us=int(base) if isinstance(base, (int, float)) else 0,
        busy_us=decoded.busy_us,
        events=decoded.events,
        depth=decoded.depth,
        unbalanced=decoded.unbalanced,
        truncated=decoded.truncated,
        passes=decoded.passes,
    )


def wall_seconds(base_us: int, anchor_us: int, scale: float, anchor_seconds: float) -> float:
    """A GPU timestamp placed on the session timeline: `anchor` plus the scaled difference.

    The two clocks are not the same clock: on the corpus the GPU timeline spans 215.175 s where the
    render thread's frames span 217.171 s, so a scale is measured rather than assumed (see
    `bottleneck.align_gpu`). The anchor is a single reference point -- a frame that is known to
    correspond -- and every other frame is placed relative to it.
    """
    return anchor_seconds + (base_us - anchor_us) * scale / 1000000.0


def specs_of(rows: Sequence[GpuSpecRow]) -> Dict[int, str]:
    """The id-to-name map, last declaration winning (the engine renames a spec in place)."""
    names: Dict[int, str] = {}
    for row in rows:
        names[int(row["id"])] = str(row["name"])
    return names
