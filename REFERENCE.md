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
  (`Bias` = 2), `0x3ffe` = **PseudoImportants** (the on-connect thread enumeration — thread
  names and groups — carried in *thread* framing rather than record framing), `0x3fff` =
  **Sync** (sync points; 3 in the corpus).
* Packets > 384 bytes and ≤ 4 KB (the writer's block size) *may* be LZ4-encoded. Corpus:
  138,830 raw + 24,767 encoded; average packet 217 bytes, max 3,756.
* A packet's decoded bytes are appended to that thread's byte stream; events never straddle
  packets.
* The decoder is **the C library** (`LZ4_decompress_safe`, from LZ4 v1.9.2 vendored under
  `src/cpp/third_party/lz4`), loaded with `ctypes` from `bin/ueia_lz4.dll` — the root
  `CMakeLists.txt` builds it — or, failing that, from a system library (`$UEI_LZ4_DLL` names one
  explicitly). The tool ships no decoder of its own, on purpose: one decoder means one answer,
  and a packet decoded by anything laxer is one every command downstream would read as fact.
  `ueia lz4` reports the state, `ueia lz4 --build` compiles it when it is missing or stale, and
  `tools/stamp_lz4.cmake` writes `bin/ueia_lz4.build.json` from the build itself, so "current" is
  a comparison of the recipe's SHA-256s — not of timestamps, which say nothing about what changed.

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
  `AuxDataTerminal` byte, for events the schema marks MaybeHasAux. A long string or array is
  written as **several segments, each with its own header and the same field index** (the
  writer splits at buffer boundaries) — a reader that takes only the last segment holds a
  truncated value, which is exactly how a CPU batch blob loses its final varint.
* **Important events** (the Events/Importants streams) use `[uint16 Uid][uint16 Size]` headers —
  self-contained sizes, because the important cache is replayed ahead of normal events on
  connect — and they frame their aux blocks **differently from thread streams**: the uid bytes
  are unshifted (`1` = AuxData, `3` = terminal) and a terminal follows *every* aux field, not
  one list. A "wide" string there is written one byte per character (the writer truncates each
  character), not UTF-16.
* **Scope markers**: `EnterScope`/`LeaveScope` are one byte; a timestamped marker
  (`EnterScope_TA`/`_TB` and their leaving twins) is **eight bytes in total** — the writer packs
  `(cycles << 8) | uid << 1` into one uint64, so the uid byte is the *first* of the eight and a
  reader that takes eight payload bytes past it walks out of step (which is what stopped 38
  streams early on the corpus's first parse).
* **Timers**: `EnterScope`/`LeaveScope` bytes bracket the event that names the timer; the CPU
  profiler's timer specs (`FCpuProfilerTrace::OutputEventType(Name, File, Line)`) are important
  events that carry **name + source file + line** — source locations are in the trace itself
  (4,879 of the corpus's 27,760 specs have one).
* **CPU timings** arrive in `CpuProfiler.EventBatchV2` blobs (V3 from UE 5.6 on). Every record
  is one varint `(cycle << 2) | flags`, followed — **on begin records only** — by the spec id
  as a second varint; an end record carries no id and is paired against that thread's own scope
  stack, so a reader that expects an id after every record eats the next record's cycle (which
  is what made 356 k batches look corrupt on the first parse). A cycle value *smaller* than the
  previous one is a delta against it. Bit 2 of the flags marks the coroutine forms V3 added,
  which carry a depth (and, when they begin, an id).

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

The packet layer: 163,600 packets total — 138,830 raw, 24,767 LZ4-encoded, 3 sync — walking to
exactly EOF (35,495,101 bytes), 49,857,025 bytes decoded over 107 streams. Highest-volume
threads by packets: tid 2 (16,567 — the tracing thread), tids 4/5/6 (~13–14k each), then a long
tail; 104 thread ids carry packets. The Events stream is 2 packets / 3,312 bytes and declares
**51 event types in 51 records, 0 redefinitions**; Importants is 2,491 packets.

The decoded model: 131 threads (122 named by the capture, in 8 groups), **960,142 events**,
154,973 scope markers, 19,251 sync events whose serial range is **gapless** (span 19,251, 0
missing — the strongest single check that the walker is in step), 837,357 CPU batches holding
**10,055,971 records**, 2,826 frame pairs (2 unpaired begins), 24 bookmarks joined to their
specs, 27,760 timer specs (4,879 with file:line), 182 counter specs, session duration 332.053 s.
`verify` reports 0 errors and 0 warnings. These are the numbers the parser's golden output must
reproduce (`goldens/labels/editor-pie-1.json` pins them).

Cost, measured in this working tree on this machine (2026-09-28, a cold `verify`):

| phase | serial | `--jobs 0` (the pool) |
|---|---|---|
| read the 34 MB file | 0.008 | 0.008 |
| container header | 0.000 | 0.000 |
| packets (163,600 headers) | 0.251 | 0.251 |
| streams (LZ4: 24,767 blocks, 34.8 MB → 49.9 MB) | 0.599 | 0.599 |
| model (the per-thread Python walk) | 9.560 | 4.140 |
| **cold full decode** | **11.34** | **5.22** |

A cached command is **0.49 s** and `goldens --check` ~15 s (its transcripts are the pinned commands'
real output, so it now runs a full `verify`).

Two histories are in that table. The LZ4 half is the C library (§2): with the pure-Python decoder
the same cold decode was **21.3 s**. And the walk is per-thread work, so `--jobs` spreads it over
processes — measured **9.56 / 5.41 / 4.14 / 4.16 / 4.15 s** at 1 / 2 / 4 / 8 / 12 workers. It stops
at the *biggest* thread rather than at the core count: tid 2 owns 53.4% of the corpus's 10,055,971
batch records, so the ceiling is that one thread, and `--jobs 0` (the default) caps its own choice
at 8 workers — past that nothing improves here, while a capture whose work is spread evenly gets
more of the box. The answer does not depend on `--jobs` at all: shares are merged by one function in
ascending tid order, byte-for-byte into the cache's own format.

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
| `Engine\Source\Runtime\TraceLog\Private\Trace\Field.cpp` | how aux data is segmented: a header per segment, each carrying the same field index |
| `Engine\Source\Runtime\TraceLog\Private\Trace\LZ4\` | the engine's own vendored LZ4 (v1.9.2 — the same version `src/cpp/third_party/lz4` carries) which compresses the packets §2 decodes |
| `Engine\Source\Runtime\TraceLog\Public\Trace\Detail\Important\ImportantLogScope.inl` | the important streams' own aux framing (unshifted uids, a terminal after every field) |
| `Engine\Source\Developer\TraceServices\Private\Analyzers\CpuProfilerTraceAnalysis.cpp` | the engine's own batch decoder: a begin record carries a spec id, an end record does not |
| `Engine\Source\Developer\TraceAnalysis\Private\Analysis\Engine.cpp` | the reader our parser mirrors: magic/metadata stages, packet transport, event parsing, the serial min-heap |
| `Engine\Source\Runtime\Core\Public\ProfilingDebugging\CpuProfilerTrace.h` | timer specs (name + file + line), the scope API |
| `Engine\Source\Runtime\Core\Public\ProfilingDebugging\CsvProfilerTrace.h` | the `CsvProfiler` channel's events (§5) |
| `Engine\Source\Developer\TraceServices\Private\Analyzers\CsvProfilerTraceAnalysis.cpp` | the engine's own analysis of that channel |
