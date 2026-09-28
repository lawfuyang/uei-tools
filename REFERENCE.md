# REFERENCE.md — the `.utrace` format, decoded

The detail behind `README.md`: how an Unreal Insights capture is laid out, fact by fact. Every
statement below was read from the engine source (UE 5.8.3; the file map is §7) and **validated
against the corpus**: a packet walk of `editor-pie-1` consumes the file exactly, and its whole
schema — 51 event types in 2 Events packets (3,312 bytes decoded) — decodes cleanly. This is the
spec the parser (ROADMAP §1) implements, and these numbers are its golden targets.

## 1. Container

* 4-byte magic `2CRT` (little-endian; big-endian traces are refused by the engine's reader, as
  are legacy magics `TRC2` / `00000001`).
* `uint16` metadata size, then metadata fields as `[uint16 size | field-id << 8]` + bytes:
  field 0 = control port (1985 in the corpus; the data port is 1981), field 1 = session GUID
  (16 bytes), field 2 = trace GUID (16 bytes). Corpus: 40 bytes of metadata.
* `uint8` transport version **4** (`TidPacketSync`), `uint8` protocol version **7**.

## 2. Packets

The rest of the file is a flat sequence of packets, each `[uint16 PacketSize][uint16 ThreadId]`
(PacketSize counts its own 4-byte header):

* ThreadId bits: `0x8000` **EncodedMarker** — payload is an LZ4 *block* (no frame header), with
  a `uint16 DecodedSize` following the header; `0x4000` Verification (a debug build mode,
  +uint64 serial — not expected in normal captures).
* Special thread ids: `0` = **Events** (the schema stream), `1` = **Importants** (the
  important-event cache, re-broadcast on every connect), `2`..`0x3ff0` = real threads
  (`Bias` = 2), `0x3ffe` = PseudoImportants, `0x3fff` = **Sync** (sync points; 3 in the corpus).
* Packets > 384 bytes and ≤ 4 KB (the writer's block size) *may* be LZ4-encoded. Corpus:
  138,830 raw + 24,767 encoded; average packet 217 bytes, max 3,756.
* A packet's decoded bytes are appended to that thread's byte stream; events never straddle
  packets.

## 3. Per-thread event streams

* **Uid encoding**: well-known events are one byte (`uid << 1`); user events are `uint16` with
  bit 0 set (`Flag_TwoByteUid`, value = `uid >> 1`). Well-known uids (protocol 7):
  `NewEvent`=0, `AuxData`=1, `AuxDataTerminal`=3, `EnterScope`=4, `LeaveScope`=5, plus the
  `_TA`/`_TB` timestamped scope variants new in protocol 7 (absolute, and relative to a
  BaseTimestamp — the corpus carries `$Trace.ThreadTiming(BaseTimestamp)`). User uids start
  at 16.
* **Serial**: sync events carry a 24-bit serial after the uid (`uint16 SerialLow` + `uint8
  SerialHigh`). Serials are global (one atomic counter in the writer); the reader merges all
  thread streams into one order by serial (min-heap), detects gaps, and uses the Sync packets to
  bound how long to wait. NoSync events carry none.
* **Aux data**: strings and arrays are not inline — they follow the event's fixed payload as
  `FAuxHeader` blocks (`[uid byte][field-index/size][uint16 size]` + bytes), ended by an
  `AuxDataTerminal` byte, for events the schema marks MaybeHasAux.
* **Important events** (the Events/Importants streams) use `[uint16 Uid][uint16 Size]` headers —
  self-contained sizes, because the important cache is replayed ahead of normal events on
  connect.
* **Timers**: `EnterScope`/`LeaveScope` bytes bracket the event that names the timer; the CPU
  profiler's timer specs (`FCpuProfilerTrace::OutputEventType(Name, File, Line)`) are important
  events that carry **name + source file + line** — source locations are in the trace itself.

## 4. The schema — the file's own vocabulary

`NewEvent` records on the Events stream describe every event type the capture contains: logger
name (`CpuProfiler`, `Misc`, `$Trace`, ...), event name, flags (Important / MaybeHasAux / NoSync
/ Definition), and per-field descriptors: family (regular / reference / definition-id), offset,
size, a type byte, and a name. The type byte is octal bitfields: category (integer / float /
array), pow-2 size (8/16/32/64), specials (string, signed) — `0x02` is `uint32`, `0x88` is an
`AnsiString`, and so on.

Three measured facts the decoder must respect:

* **The schema is lazy.** A type is registered when its first event *fires* (plus specs
  registered at startup), so a capture's vocabulary is what actually happened in that session —
  an event type missing from the schema never fired. `editor-pie-1` has no TaskGraph events and
  no per-frame CSV events, which is information, not a decode failure.
* **Byte 1 of each `FNewEventField` is not written by the writer** (the union's `Unused` byte):
  the corpus contains garbage there (`0x70`, `0x61`, ...). Read family from byte 0, offset/size
  as `uint16`s at bytes 2/4, type and name-size at bytes 6/7 — and never validate byte 1 as
  zero, or the parse will reject a valid file.
* **The engine tree answers *layout* questions; the capture answers *vocabulary* ones.** Event
  and timer names are decoded from these records, never hardcoded.

## 5. The `CsvProfiler` channel

The CSV Profiler emits its data into the trace under the `CsvProfiler` logger
(`ProfilingDebugging/CsvProfilerTrace.h`), which is what makes a `.utrace` a valid input for the
CsvTools pipeline (README §2):

* **Definitions** (Important events): `RegisterCategory(Index, Name)` —
  `DefineDeclaredStat(StatId, CategoryIndex, Name)` — `DefineInlineStat(StatId, CategoryIndex,
  Name)` (plus an exclusive variant).
* **Per-frame values** (only when a CSV capture was running): `BeginStat`/`EndStat`,
  `BeginExclusiveStat`/`EndExclusiveStat` (cycles), `CustomStat(Value, OpType, Cycles)`,
  `Event(Text, CategoryIndex, Cycles)` (frame markers like level loads), plus
  `BeginCapture`/`EndCapture` (render/RHI thread ids, default wait stat) and `Metadata(Key,
  Value)`.

Corpus state: the definitions are present, the per-frame events are not — `editor-pie-1` ran
with the CSV profiler *registered* but no CSV capture active (README §2, ROADMAP §2 want a
capture that has both). The engine's own reader of this channel is
`TraceServices\Private\Analyzers\CsvProfilerTraceAnalysis.cpp`, feeding the
`CsvProfilerProvider` model.

## 6. What the corpus measured (2026-09-28)

163,600 packets total: 138,830 raw, 24,767 LZ4-encoded, 3 sync. Highest-volume threads by
packets: tid 2 (16,567 — the tracing thread), tids 4/5/6 (~13–14k each), then a long tail. The
Importants stream: 2,491 packets. The schema: 51 event types = 2 Events packets / 3,312 decoded
bytes (both packets decode with a from-scratch LZ4 block decoder — good evidence for §2). These
are the numbers the parser's golden output must reproduce.

## 7. Where the format is defined (engine source tree)

Paths are relative to the engine source tree root.

| Engine file | What it defines |
|---|---|
| `Engine\Source\Runtime\TraceLog\Private\Trace\Writer.cpp` | the file handshake (`2CRT`), the connect prologue, important-cache re-broadcast, sync packets |
| `Engine\Source\Runtime\TraceLog\Public\Trace\Detail\Transport.h` | the packet header, thread-id bits, special tids |
| `...\Detail\Protocols\Protocol0.h` | the field-type byte |
| `...\Detail\Protocols\Protocol5.h` | the event headers, the aux header |
| `...\Detail\Protocols\Protocol6.h` | the schema record (`FNewEventEvent`) |
| `...\Detail\Protocols\Protocol7.h` | the well-known uids, incl. the timestamped scopes |
| `...\Trace\Config.h` | block size, protocol selection |
| `Engine\Source\Runtime\TraceLog\Private\Trace\EventNode.cpp` | how schema records are written (incl. the unwritten `Unused` byte) |
| `Engine\Source\Developer\TraceAnalysis\Private\Analysis\Engine.cpp` | the reader our parser mirrors: magic/metadata stages, packet transport, event parsing, the serial min-heap |
| `Engine\Source\Runtime\Core\Public\ProfilingDebugging\CpuProfilerTrace.h` | timer specs (name + file + line), the scope API |
| `Engine\Source\Runtime\Core\Public\ProfilingDebugging\CsvProfilerTrace.h` | the `CsvProfiler` channel's events (§5) |
| `Engine\Source\Developer\TraceServices\Private\Analyzers\CsvProfilerTraceAnalysis.cpp` | the engine's own analysis of that channel |
