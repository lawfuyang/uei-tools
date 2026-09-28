"""The capture's own vocabulary: NewEvent records, decoded into a registry.

A trace describes its events in the file itself. Each NewEvent record names a
logger, an event, the event's flags, and one descriptor per field -- offset,
size, a type byte and a name. The registry built here is what turns a uid in a
thread stream back into `CpuProfiler.EventSpec(Id, Name, File, Line)`.

Two facts the decoder must respect, both measured on the corpus:

* **The schema is lazy.** A type only appears once its first event fires, so a
  capture's vocabulary is what actually happened in that session.
* **Byte 1 of each field descriptor is not written by the writer** (a union's
  unused byte); the corpus holds arbitrary values there. Never validate it.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from container import Anomaly
from events import iter_important_records
from shapes import (
    EVENT_FLAG_MAYBE_HAS_AUX,
    EVENT_FLAG_NOSYNC,
    FIELD_FAMILY_DEFINITION_ID,
    FIELD_FAMILY_NAMES,
    FIELD_FAMILY_REFERENCE,
    UID_NEW_EVENT,
    EventTypeRow,
    FieldRow,
    field_size,
    field_type_name,
    flag_names,
)

NEW_EVENT_HEADER_SIZE = 6
NEW_EVENT_FIELD_SIZE = 8
DEFINITION_ID_FIELD_NAME = "DefinitionId"


def _read_field_descriptor(
    data: bytes, base: int
) -> Tuple[int, int, int, int, int, int]:
    """(family, offset, size, type byte, name size, ref uid) of one descriptor.

    The `size` is the field's fixed byte size in the payload; strings and
    arrays carry their data as aux blocks and declare 0 here. Reference and
    definition-id fields take their size from the type byte, as the engine's
    own reader does.
    """
    family = data[base]
    offset = data[base + 2] | (data[base + 3] << 8)
    if family == FIELD_FAMILY_REFERENCE:
        ref_uid = data[base + 4] | (data[base + 5] << 8)
        type_byte = data[base + 6]
        return family, offset, field_size(type_byte), type_byte, 0, ref_uid
    if family == FIELD_FAMILY_DEFINITION_ID:
        type_byte = data[base + 7]
        return family, offset, field_size(type_byte), type_byte, 0, 0
    size = data[base + 4] | (data[base + 5] << 8)
    type_byte = data[base + 6]
    name_size = data[base + 7]
    return family, offset, size, type_byte, name_size, 0


def parse_new_event(
    data: bytes, offset: int, size: int, anomalies: List[Anomaly]
) -> Optional[EventTypeRow]:
    """One NewEvent record into an event type row, or None when malformed."""
    end = offset + size
    if size < NEW_EVENT_HEADER_SIZE or end > len(data):
        anomalies.append((
            "bad-new-event", offset, size, "NewEvent record is smaller than its header",
        ))
        return None
    uid, field_count, flags, logger_size, event_size = struct.unpack_from(
        "<HBBBB", data, offset
    )
    fields_offset = offset + NEW_EVENT_HEADER_SIZE
    names_offset = fields_offset + NEW_EVENT_FIELD_SIZE * field_count
    if names_offset + logger_size + event_size > end:
        anomalies.append((
            "bad-new-event", offset, size, "NewEvent names run past the record",
        ))
        return None

    fields: List[FieldRow] = []
    field_names: Dict[str, int] = {}
    total_size = 0
    name_cursor = names_offset + logger_size + event_size
    for index in range(field_count):
        base = fields_offset + index * NEW_EVENT_FIELD_SIZE
        family, field_offset, fixed_size, type_byte, name_size, ref_uid = (
            _read_field_descriptor(data, base)
        )
        if family == FIELD_FAMILY_DEFINITION_ID:
            name = DEFINITION_ID_FIELD_NAME
        else:
            if name_cursor + name_size > end:
                anomalies.append((
                    "bad-new-event", offset, size, "a field name runs past the record",
                ))
                return None
            name = bytes(data[name_cursor:name_cursor + name_size]).decode("utf-8", "replace")
            name_cursor += name_size
        fields.append(FieldRow(
            index=index,
            name=name,
            family=family,
            family_name=FIELD_FAMILY_NAMES.get(family, str(family)),
            offset=field_offset,
            size=fixed_size,
            type_byte=type_byte,
            type_name=field_type_name(type_byte),
            ref_uid=ref_uid,
        ))
        field_names[name] = index
        total_size += fixed_size

    logger = bytes(data[names_offset:names_offset + logger_size]).decode("utf-8", "replace")
    name = bytes(
        data[names_offset + logger_size:names_offset + logger_size + event_size]
    ).decode("utf-8", "replace")
    return EventTypeRow(
        uid=uid,
        flags=flags,
        flag_names=flag_names(flags),
        logger=logger,
        name=name,
        full_name=logger + "." + name,
        size=total_size,
        fields=fields,
        field_names=field_names,
    )


class SchemaRegistry(object):
    """The decoded vocabulary: uid -> event type row.

    The engine's own reader keeps the *last* definition of a uid (a reconnect
    re-describes every event type), and so does this one.
    """

    def __init__(self) -> None:
        self._types: Dict[int, EventTypeRow] = {}

    def add(self, row: EventTypeRow) -> None:
        self._types[row["uid"]] = row

    def get(self, uid: int) -> Optional[EventTypeRow]:
        return self._types.get(uid)

    def is_sync(self, uid: int) -> bool:
        row = self._types.get(uid)
        return row is not None and not (row["flags"] & EVENT_FLAG_NOSYNC)

    def has_aux(self, uid: int) -> bool:
        row = self._types.get(uid)
        return row is not None and bool(row["flags"] & EVENT_FLAG_MAYBE_HAS_AUX)

    def size(self, uid: int) -> int:
        row = self._types.get(uid)
        return 0 if row is None else int(row["size"])

    def rows(self) -> List[EventTypeRow]:
        """Every type, ascending by uid (file order is a stack, not a sort)."""
        return [self._types[uid] for uid in sorted(self._types)]

    def count(self) -> int:
        return len(self._types)

    def full_name(self, uid: int) -> str:
        row = self._types.get(uid)
        if row is None:
            return "uid %d" % (uid,)
        return str(row["full_name"])


def build_registry(
    events_stream: bytes, anomalies: List[Anomaly], counts: Dict[str, int]
) -> SchemaRegistry:
    """Read every NewEvent record of the Events stream into a registry."""
    registry = SchemaRegistry()
    for uid, size, offset in iter_important_records(events_stream, anomalies):
        if uid != UID_NEW_EVENT:
            anomalies.append((
                "unexpected-events-record", offset, uid,
                "uid %d on the Events stream is not a NewEvent record" % (uid,),
            ))
            continue
        row = parse_new_event(events_stream, offset, size, anomalies)
        if row is None:
            continue
        counts["new_events"] += 1
        if registry.get(row["uid"]) is not None:
            counts["redefined"] += 1
        registry.add(row)
    return registry
