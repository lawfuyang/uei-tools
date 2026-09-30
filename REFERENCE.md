# REFERENCE.md — the `.utrace` format, decoded

The detail behind `README.md`: how an Unreal Insights capture is laid out, fact by fact. Every
statement below was read from the engine source (UE 5.8.3; the file map is §7) and **validated
against the corpus**: a packet walk of `editor-pie-1` consumes the file exactly, and its whole
schema — 51 event types in 2 Events packets (3,312 bytes decoded) — decodes cleanly. This is the
spec the parser implements, and these numbers are its golden targets.

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

## 5. The CSV Profiler's events (the `CsvProfiler` logger, on the `counters` channel)

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
with the CSV profiler *registered* but no CSV capture active (README §2; a capture that has both is
still wanted). The engine's own reader of these events is
`TraceServices\Private\Analyzers\CsvProfilerTraceAnalysis.cpp`, feeding the
`CsvProfilerProvider` model.

**What `csv from-trace` writes, and why it is that shape.** The file the CsvTools executables read
is defined by `CsvStats.ReadCSVFromLines` (read 2026-09-28), and our writer is built to pass it:

* the **first line is the column header**, `EVENTS,<series names>` -- the first column is the events
  column and is named `EVENTS`, whatever the series are called;
* **one line per frame**, each `<events>,<value>,<value>,...`, where the events column holds
  `name##seconds` entries joined by `;` (a `,` or `;` inside a name becomes `.`), and the values
  follow the header's order;
* the **last line is metadata** (`[Key],Value` pairs, keys lower-cased by the reader): ours carries
  `[EventTimestamps]`, `[FramesFrom]`, `[SynthesizedBy]` and `[Source]` -- never a machine path;
* **numbers** follow the engine's own `FCsvWriterHelper` formatting: `%.0f` when integral, `%.6f`
  below 0.1, `%.4f` otherwise; a series with no value in a frame is **`0`**, never blank;
* **series names** follow the engine's writer: `<Thread>/<Category>/<Stat>` for a timed stat and
  `<Category>/<Stat>` for a custom one (`ECsvCustomStatOp {Set, Min, Max, Accumulate}` combines the
  repeats inside a frame, and a timed stat's cycles become **milliseconds** through the session's
  `cycle_frequency`);
* **frames** are the trace's own `Misc.BeginFrame`/`Misc.EndFrame` pairs of the thread that emitted
  the values, because the CSV Profiler's frame counter is not in the trace -- and the metadata says
  so. A value before the first frame is counted as dropped, and the command reports the count.

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

Cost, measured in this working tree on this machine (2026-09-29, a cold `parse`, frame work
attribution, occupancy, the GPU frame decode and the coverage timelines with their per-frame
measurement included — §10, §11, §13):

| phase | serial | `--jobs 0` (the pool) |
|---|---|---|
| read the 34 MB file | 0.008 | 0.011 |
| container header | 0.000 | 0.000 |
| packets (163,600 headers) | 0.251 | 0.290 |
| streams (LZ4: 24,767 blocks, 34.8 MB → 49.9 MB) | 0.589 | 0.629 |
| model (the per-thread Python walk, all of the above) | 15.493 | 7.931 |
| **cold full decode** | **16.34** | **8.86** |

A cached command is **0.32-0.33 s** (`summary`, `bottleneck` and `parallelism` included: everything
they report is in the cache, never re-derived from the streams), the editor capture's cache is
**6.74 MB** (3.35 MB before the coverage measurement landed) and `goldens --check` ~40 s over the
three registered captures, whose transcripts are the pinned commands' real output. What a warm
command spends its time on, measured 2026-09-29: **0.17 s interpreter and imports** (nothing this
tool can avoid), **0.03 s** hashing the capture and **0.02 s** parsing the cache, and -- before the
lazy packet walk below -- **0.25 s walking 163,600 packet headers on every command**, including the
seven that never look at the table. That is why `CaptureView.packets` is a property: `info`,
`packets`, `schema` and `verify` walk it, the model-based commands never pay for it.

Three histories are in that table. The LZ4 half is the C library (§2): with the pure-Python decoder
the same cold decode was **21.3 s**. The walk is per-thread work, so `--jobs` spreads it over
processes — measured 2026-09-29 on the same capture, with the coverage work in place:
**17.25 / 11.27 / 9.39 / 9.48 s** at 1 / 2 / 4 / 8 workers. It stops helping at four workers because
two of its parts do not spread: tid 2 (53.4% of the corpus's 10,055,971 batch records) is one unit
however many processes there are, and the frame measurement (§13) runs once, in the parent, over
every thread's timeline. `--jobs 0` (the default) caps its own choice at 8, while a capture whose
work is spread evenly gets more of the box. The answer does not depend on `--jobs` at all: shares
are merged by one function in ascending tid order, byte-for-byte into the cache's own format.

**What the attribution and occupancy cost.** Walking tid 2 alone — 5.49 M of those records, the
biggest single unit — takes **3.28 s with nothing attributed and 4.24 s with the frame work and the
occupancy (+29%)**, which is the ~1 s the pool shows on the critical thread; the cold total went
5.97 s (work only, before the occupancy) → 6.98 s with everything. Two cheaper shapes were measured
and rejected on the way: accumulating into a dict per frame (**+23%** against +19% for the locals)
and writing every count into the walk's `counts` dict as it goes; the loop keeps its totals, its
running coverage and its counters in locals and flushes them once per frame window, and since
2026-09-29 it also keeps the per-frame totals in a **list** (`totals[spec] += span`, an index
instead of two hashes for 2.5 M pairs) and answers the **disjoint** coverage case first. The cost is
paid once per capture, by the parse that fills the cache; a warm `summary`/`bottleneck` is 0.26/0.29 s
like any other cached command, which is the whole reason the occupancy lives in the model rather than
being recomputed by the report.

**What the coverage timeline and the frame measurement add** (§13). The timeline is a second union
over the same records, and the measurement folds the corpus's 779,431 intervals into its 2,826
frames. Measured 2026-09-29: walking every thread with the timeline costs **12.7 s** against
**11.5 s** without it (serial, +1.2 s), and the folding costs **2.40 s** — once, in the parent,
whatever `--jobs` says, because it needs every thread's timeline at the same time and that is what
makes it the parse's fixed part. A cold decode therefore went 5.69 → 8.86 s over the pool and
12.39 → 16.34 s serial, the cache 3.35 → 6.74 MB, and it is paid by the parse that fills the cache
once: `parallelism` itself is 0.33 s, like any other cached command. One change came out of
measuring it: the folding subtracts a thread's wait intervals **per frame** instead of subtracting
two long lists once (3.91 → 2.40 s), and the coverage union is a merge rather than a concurrency
sweep, because nothing asks the union how many threads were inside it.

**Where the remaining seconds are, and what was tried.** A profile of the serial parse (51.2 s under
`cProfile`, ~3× real time) put `decode7bit` at the top with 15.08 M calls, then the walk's own loop,
then `decode_batch` (837,357 batches, 10,055,971 records), then `iter_thread_events` (2.23 M events)
and the aux framing. Five changes came out of it, all in the same week and all pinned by the corpus
transcripts (which did *not* change):

| change | why it was there |
|---|---|
| `decode7bit`'s one-byte fast path | the commonest varint by far is a 1-byte delta or spec id, and the function was the tool's hottest |
| the varint fast path **inlined** into `decode_batch` | 15.08 M calls became ~1 M: the call overhead was half of what the decoder cost |
| one memoised schema lookup per uid in `iter_thread_events` | it asked `size`, `has_aux` and `is_sync` per event: three method calls on 2.23 M events |
| values decoded **only** for the event types the walk handles | ~1 M events were paying for a value dict and an aux walk the model has nothing to say about |
| `CaptureView.packets` is a **property** | 0.25 s of packet walking on every command, including the seven that never look at the table |

Measured on the corpus: a cold parse **6.93 → 5.69 s** (-18%), serial **15.2 → 12.4 s** (-18%), and
the model-based warm commands **~0.50 → 0.23-0.29 s** (-45%). Three shapes were **measured and
rejected** rather than assumed: threading the LZ4 decode (the per-block Python holds the GIL — 0.42 s
serial against 1.00 s on 8 threads), a faster `packets --limit 0` (its 1.08 s is the 0.30 s walk and
12.8 MB of text, not Python overhead), and a pickle cache (the JSON parse is 0.022 s, so it would have
traded the plain-text cache's safety for nothing measurable). The lesson the changes cost: the
coverage merge's "disjoint" shortcut first shipped **wrong** — it moved the region's left edge and
double-counted later overlaps — and the corpus's own verdicts (`game-pc-2`: 770 bounded frames where
9 are real) caught it inside one run, because the transcripts pin the answers and not just the code.

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
| `Engine\Source\Runtime\Core\Public\ProfilingDebugging\CpuProfilerTrace.h` | timer specs (name + file + line), the scope API |
| `Engine\Source\Runtime\Core\Public\ProfilingDebugging\CsvProfilerTrace.h` | the CSV Profiler's events — they ride the `counters` channel (§5) |
| `Engine\Source\Developer\TraceAnalysis\Private\Analysis\Engine.cpp` | the reader our parser mirrors: magic/metadata stages, packet transport, event parsing, the serial min-heap |
| `Engine\Source\Developer\TraceServices\Private\Analyzers\CpuProfilerTraceAnalysis.cpp` | the engine's own batch decoder: a begin record carries a spec id, an end record does not (§3, §10) |
| `Engine\Source\Developer\TraceServices\Private\Analyzers\CsvProfilerTraceAnalysis.cpp` | the engine's own analysis of that channel |

## 8. The channels, and what each one lets you answer

A capture carries only what it was recorded with (`-trace=<id>,<id>...`; the macros that declare a
channel are `UE_TRACE_CHANNEL*` in `Runtime\TraceLog\Public\Trace\Trace.h`). This is the inventory
of what the engine can emit — gathered from the tree on 2026-09-28, grouped by the question it
answers, with the engine analyser to mirror for field-level truth. **An absent channel is not a
zero value: it is a question the capture cannot answer**, and the tool says so (§18).

| Channel | Carries | Answers | Engine analyser to mirror |
|---|---|---|---|
| `cpu` | CPU scopes on every thread (`CpuProfiler`) | which work owns the frame: the call tree, per-thread timelines, self time | `CpuProfilerTraceAnalysis.cpp` |
| `gpu` | GPU timings, breadcrumbs, queue sync (`GpuProfiler`) | per-pass cost, GPU-bound confirmation, CPU↔GPU waits | `GpuProfilerTraceAnalysis.cpp` (and `OldGpuProfiler…` for the pre-5 format); the current channel is §19 |
| `frame` | frame durations per frame type | frame boundaries without `Misc.BeginFrame` | `MiscTraceAnalysis.cpp` |
| `bookmark` | low-frequency markers (boot, level load) | ✓ used: 24 bookmarks joined in the corpus | `BookmarksTraceAnalysis.cpp` |
| `region` | thread-agnostic timespans | "what happened inside this region" | `MiscTraceAnalysis.cpp` |
| `screenshot` | embedded screenshots | line a finding up with what was on screen | `MiscTraceAnalysis.cpp` |
| `log` | log messages | ✓ used: counts + specs | `LogTraceAnalysis.cpp` |
| `counters` | numeric counters over time; **the CSV Profiler's events ride here** | stat/counter series, CSV profiling | `CountersTraceAnalysis.cpp`, `CsvProfilerTraceAnalysis.cpp` |
| `stats` | `DECLARE_STATS_GROUP` stats as counters | the numbers a `stat` HUD shows, per frame | `StatsTraceAnalysis.cpp` |
| `memtag` | LLM tag memory values | memory budgets per tag over time | `MemoryAnalysis.cpp` |
| `memalloc` | allocations (size, lifetime) | churn, top sites, growth that never returns | `AllocationsAnalysis.cpp` |
| `callstack` | allocation callstacks (spec + frames) | allocation sites — *by module+offset* without symbols | `CallstacksAnalysis.cpp` |
| `module` | loaded modules (base, size, path) | module grouping; the input symbolication needs | `ModuleAnalysis.cpp` |
| `task` | task-graph lifecycle and dependencies | critical path, parallelism, waits | `TasksAnalysis.cpp` |
| `loadtime`, `asset` | package/request-group load timing | load trees, slowest packages and assets | `LoadTimeTraceAnalysis.cpp` |
| `file` | platform-file open/close/read/write | IO wait vs CPU work | `PlatformFileTraceAnalysis.cpp` |
| `iostore` | I/O dispatcher, chunk loading | streaming detail behind the file channel | (IoStore provider) |
| `assetmetadata` | asset metadata records | asset identity for a report | `MetadataAnalysis.cpp` (metadata provider) |
| `metadata` | scoped key/value metadata | build/changelist/platform/device context | `MetadataAnalysis.cpp` |
| `net` | connections, packets, objects | network replication cost | `NetTraceAnalyzer.cpp` |
| `object`, `objectproperties` | UObject lifecycle, property changes | object lifetime; allocation attribution | `ObjectTraceAnalysis.cpp` |
| `slate` | Slate UI timing | UI cost per frame (read by the Insights UI; no TraceServices analyser) | — |
| `rdg`, `rendercommands`, `rhicommands` | render-graph and command streams | render-thread depth (Read by the RenderGraph Insights plugin) | — |
| `audio`, `audio.mixer` | audio mixer events | audio cost per frame (Audio Insights plugin) | — |
| `cook`, `save`, `http`, `rac`, `mass`, `animation` | editor/cook, save, HTTP, race detector, Mass, animation | long tail: detected and named, not analysed yet | `CookAnalysis.cpp`, `VerseTraceAnalysis.cpp`, … |

Capture presets the engine ships (`TraceAuxiliary.cpp`): `-trace=Default` is
`cpu,gpu,frame,log,bookmark,screenshot,region`, and `-trace=Memory` is
`memtag,memalloc,callstack,module` (read-only). `-trace=Memory_Light` is `memtag,memalloc`.

What the registered captures actually carry — checked against their own schemas on 2026-09-28, which
corrected this file:

| Capture | Carries | Does **not** carry |
|---|---|---|
| `editor-pie-1` (editor PIE) | `cpu` scopes, `frame`, `log`, `bookmark`, `counters`, `region`, the **legacy** `GpuProfiler` channel (§11: 1,410 rendered frames, 37 named passes) and `Memory.MemoryScope` (42,029 events, a `Tag i32` scope) | `task`, `memalloc`/`callstack`/`module`, `loadtime`, `screenshot` |
| `game-pc-2` (game) | the same CPU channels, `Memory.MemoryScope` (38,595), CSV/stat *definitions* | everything GPU (no `GpuProfiler` at all), `task`, allocations, `loadtime` |
| `viewer-pc-3` | `cpu` scopes, counters, log | **any frame pair**, GPU, `task` |

So the GPU and memory items do not start from nothing after all; what was still missing for a full
GPU answer — the *current* channel's queue semantics (waits, fences, breadcrumbs; §11 decodes the
legacy per-frame form) — is decoded now (§19), and for memory an allocations channel rather than a
tag scope.

## 9. Engine programs that read traces (what we wrap, and what we do not)

| Program | Where | Editor build needed? | What it gives us |
|---|---|---|---|
| `TraceQuery` | `Engine\Source\Programs\TraceQuery` | **no** | runs the engine's own analysers (CPU profiler, counters, memory, regions, log) and emits **JSON** — the strongest offline cross-check of our parser |
| `TraceAnalyzer` | `Engine\Source\Programs\TraceAnalyzer` | **no** | trace → text dump; the tool for decoding a channel we have not parsed yet |
| `TraceTrimmer` | `Engine\Source\Programs\TraceTrimmer` | **no** | trims/rewrites traces (smaller fixtures, shareable captures) |
| `UnrealInsights.exe` headless | `Engine\Source\Programs\UnrealInsights` | **yes** (not shipped prebuilt here) | `-NoUI -AutoQuit -ExecOnAnalysisCompleteCmd="…"` with `TimingInsights.ExportTimers/ExportThreads/ExportTimingEvents/ExportTimerStatistics/ExportTimerCallees/ExportCounters/ExportCounterValues` and `MemoryInsights.ExportAllocs`; a response file (`@=file.rsp`) runs several. CSV/TSV/TXT out |
| CsvTools | `Engine\Binaries\DotNET\CsvTools` (prebuilt here) | no | the CSV Profiler chain (README §2) |
| `NetworkProfiler.exe` | `Engine\Binaries\DotNET` (prebuilt) | no | the *legacy* network profiler format, **not** `.utrace`: out of scope, listed so nobody re-derives it |
| `AutomatedPerfTesting` plugin, Gauntlet (`RunInsightsTests.cs`) | `Engine\Plugins\Performance`, `Programs\AutomationTool` | n/a | the engine's own scripted-perf-CI machinery: we consume the `.utrace`/`.csv` it produces, never the runner |

Also prebuilt in this checkout and worth a look later: `iostore_analysis.exe`,
`AnalysisTabUtils.exe` (`Engine\Binaries\Win64`).

## 10. The frame-time summary (`summary`) — what its words mean

The practice this layer answers to (ROADMAP, "What professional performance work looks like") is
blunt about the shape of the answer: **the distribution, never the average** — "a 16.6 ms average
with 50 ms spikes feels terrible" — against an explicit target, with a count of the frames that miss
it and the timers that own them. Every word in that sentence is defined here.

* **A frame** is one `Misc.BeginFrame`/`Misc.EndFrame` pair of one thread and frame type, and its
  time is `end_cycle - begin_cycle` divided by the capture's `session.cycle_frequency`. The engine's
  own frame track draws the same pair. The corpus has 1,413 pairs on tid 2 (frame type 0) and 1,413
  more on tid 98 (type 1), spanning 217.2 s of the capture's 332.1 s.
* **One series, never a pool of them.** A report judges the *busiest* (thread, frame type), and
  `--tid` names another on purpose: the corpus's game and render threads are different clocks, and
  averaging them into one distribution would describe neither. Cycles are not milliseconds without a
  cycle frequency, so a capture that has none cannot be summarised at all (exit 2, not a guess).
* **Percentiles are nearest-rank**: p_q of n values is the value at rank `ceil(q*n)` in sorted
  order — a real frame's time, never an interpolation between two frames that never happened, which
  is what a CI gate means by "p99 10% worse than the baseline". The corpus: mean 153.702 ms, min
  4.708, **p50 33.414, p95 333.408, p99 334.328, max 121,484.003** — the shape is an editor session
  (frames clustered at 33 ms and at 333 ms, plus one 121-second stall), and the mean belongs to no
  frame at all. The mean is printed, and it is never the verdict.
* **A hitch is `HITCH_FACTOR` (2) budgets**: 33.333 ms at 60 FPS — the threshold the CI literature
  counts — so the count is budget-relative (`--budget 30` makes a hitch a frame over 66.667 ms).
  `--budget FPS` and `--budget-ms MS` are two spellings of one thing: giving both is a usage error
  rather than one of them winning, and neither means 60 FPS, which the report prints.
* **The histogram's bins double from half the budget** (`budget/2`, `budget`, `2 x budget`, …, at
  most 16, the last one open-ended), because a linear axis would put every frame of a
  hitch-ridden capture in its first bin.
* **What ran in a frame** comes from the batch records (§3): the walk pairs each begin with its
  end — the wire carries a spec id only on the begin — and attributes the pair's cycles to the frame
  whose span holds the pair's **end cycle**, clipped to that frame (§11 has why it is the cycle and
  not the stream order). The item's cycles are that clipped **inclusive** time (a scope that
  contains another counts its children too, clipped to the same window), so the report's percentages
  can add up past 100% — which is the point: the timer that owns the frame reads as one number.
* **What could not be attributed is counted, never guessed.** Six model counters carry it, and
  `verify` prints them whenever a capture has frames:
  `scope_pairs` (attributed, a declared spec), `scope_pairs_spanning` (clipped at the frame's
  begin), `scope_pairs_no_spec` (a V3 coroutine record, which has no spec id on the wire, or an id
  the capture never declared), `scope_pairs_outside` (ended in no frame window at all),
  `scope_ends_unpaired`, `scope_begins_unpaired`.
* **The work kept is a sample by construction**: the **16 longest frames of each thread**, up to 6
  timers each (`model._FRAME_WORK_KEEP`, `_FRAME_WORK_TOP`), live in the model — so `summary` on a
  warm cache costs what any other cached command costs (0.51 s, §6) and never re-walks the file. A
  table row for a frame whose work was not kept prints `-`, and the prose says why.
* **The cross-check this owes.** The bar set for this layer is that our numbers agree with
  `TimingInsights.ExportTimerStatistics` where the columns overlap. That export needs a *built*
  UnrealInsights and this checkout has none, so the check is **not run here** — what is stated
  instead is the definitional overlap: a frame time is the BeginFrame/EndFrame span the engine's
  frame track draws, and a timer is a `CpuProfiler` batch scope named by the capture's own spec
  table, which is what that export enumerates. When an engine build exists on the machine, that
  export is the comparison to run (README §3).

## 11. The bottleneck verdict, and the GPU channel it is measured against

`ueia bottleneck` answers the practice's first question — game thread, render thread, GPU, or none
of them — and `summary` prints its one-line verdict. Three things it needs, and what each is here:

**1. A frame's own occupancy.** Cycles of a frame's window that the frame's thread spent inside a
scope, **merged** (overlapping scopes count once) and split into work and wait. The split is what
makes the measure usable: on the corpus the render thread is inside a scope for 97% of its frames
and inside `WaitForTasks`/`WaitUntilTasksComplete` for 99.7% of *that*, so without it every render
frame would read as "render-thread bound". A scope counts as a wait when its name says so
(`model.WAIT_NAME_MARKERS`, the engine's own `WaitFor*` naming) — **heuristic**, and labelled as such
wherever it is used.

Crucially, a scope pair belongs to a frame by **cycle**, not by stream order: the frame markers and
the scope batches do not interleave in a `.utrace` (the game capture (`game-pc-2`) flushes all 771 game
frames in the first 8% of the thread's stream, the batches after them; on `editor-pie-1` whole
ranges carry one and not the other), so "the frame that was open when the pair was read" is no frame
at all. The walk therefore pairs the frames first (`_pair_windows`, a header-only pass) and then
attributes every pair to the window whose span holds the pair's **end** cycle, clipped to it — the
same thing the engine's own `FrameStatsHelper` does when it clips an event to a frame interval. The
counters say what that cost: `scope_pairs` (attributions with a declared spec),
`scope_pairs_spanning` (clipped at the frame's begin — 2,838 on the corpus), `scope_pairs_no_spec`,
`scope_pairs_unframed` (ended in no window at all — which on the corpus is 2.2 M pairs, almost all of
them on threads that have no frames: 104 thread ids carry packets, 2 carry frames),
`scope_ends_unpaired`, `scope_begins_unpaired`.

Measured on the corpus: the game thread is inside scopes for 8% of a frame (p50) and the render
thread 97% (89% of that in waits) — against the game capture's game thread at 99% (its
`FlushRenderingCommands`/`GameThreadWaitForTask` frames are the ones that read as waits).

**2. The other frame series of the capture.** A game frame and the render frame beside it are one
frame of the pipeline, so each judged frame is matched to the frames of other threads that **overlap
it** in cycles, and the busiest of them is the one a verdict names. Thread roles (`game`, `render`,
`rhi`) come from the capture's own thread names (`GameThread`, `RenderThread 0`, `RHIThread`) —
**heuristic** again, and each report line says so.

**3. The GPU.** The corpus carries the **legacy** `GpuProfiler` channel (one event per rendered
frame), not the current one (one event per GPU work item — that decode is §19, and it is the second
way this verdict reaches a GPU answer): the legacy writer was removed in UE 5.6, and UE 5.8.3 ships
only the reader (`OldGpuProfilerTraceAnalysis.cpp`, "maintained for backward compatibility with old
traces"). That reader is the layout's authority, and `gpu.py` follows it field for field:

| Event | Fields | Meaning |
|---|---|---|
| `GpuProfiler.EventSpec` | `uint32 EventType`, `WideString[] Name` | the id → name map (37 specs on the corpus: `SlateUI`, `Basepass`, `Prepass`, `TAA`, `NaniteEditor`, …) |
| `GpuProfiler.Frame` | `uint64 CalibrationBias`, `uint64 TimestampBase`, `uint32 RenderingFrameNumber`, `uint8[] Data` | one rendered frame |

`Data` is a varint-delta stream (`Utils.h`'s `Decode7bit`), not a struct array: `packed =
Decode7bit(); timestamp += packed >> 1; if (packed & 1)` a begin follows, whose spec id is the next
4 bytes little-endian, else an end. `busy_us` is the sum of the **outermost** spans — outermost
spans cannot overlap, so that is the union: the microseconds the GPU spent executing traced work.
It is a *duration*, so `CalibrationBias` cancels and is never interpreted; `TimestampBase` is kept
for the alignment.

**The alignment is measured, not assumed.** The GPU clock is not the CPU clock: on the corpus the GPU
timeline spans 215.175 s where the render thread's frames span 217.171 s, so the scale between them
is fitted (1.0094) and each GPU frame is placed inside the window its scaled time falls in — 1,409 of
1,410 land inside one. Fewer than half, or a scale outside 1 ± 0.25: the alignment is **refused**, and
the report says the GPU side is unknown, because an unplaced GPU number is not evidence about a frame.

**The decision tree is the engine's** (`Engine/Private/ChartCreation.cpp:1325-1349`, "if frame time is
greater than our target then we are bounded by something", and `DynamicResolution.cpp:280-290`): over
budget and the frame's thread worked ≥ budget → **game**-bound; else a partner thread did → **render**;
else the GPU was busy ≥ budget → **GPU**-bound; else **unexplained** (and when most unexplained frames
sit within 5% of a multiple of 1/60, 1/30, 1/120 or 1/90 s, a **frame-rate/display cap** note, marked
heuristic). A capture with no GPU channel never gets a bare "CPU-bound": the finding comes with "the
GPU side is unknown here" and the re-record line.

What the corpus says, with those rules (pinned as transcripts):

| capture | verdict |
|---|---|
| `editor-pie-1` | 61 of 1413 frames game-thread bound; 1320 unexplained, **1094 of them pinned to a display period** (an editor throttled to ~3 FPS: p50 frame 33.4 ms, GPU p50 0.25 ms); the 121.5 s PIE frame is game-bound (118,999.8 ms of `UEditorEngine::StartPlayInEditorSession` inclusive inside it) |
| `game-pc-2` | 9 of 771 frames bound (1 game, 8 render); 761 unexplained with **no GPU channel to check against** — its game thread is inside wait-shaped scopes (`FlushRenderingCommands`, `GameThreadWaitForTask`) for its whole 905 ms frames, so "waiting on something this capture cannot show" is the honest answer |
| `viewer-pc-3` | no `Misc.BeginFrame` pairs at all: exit 2, never zero |

## 12. The task graph, and the longest chain through it

`ueia tasks` answers what a timeline cannot: *which* work a slow frame was waiting on. The channel is
`TaskTrace` (the command line token is `task`; the engine ships a `TaskGraph` preset for
`-trace=cpu,gpu,frame,log,bookmark,screenshot,region,task`), and it is **task-centric** rather than
per-thread: `FTaskBase` writes its events from whichever thread it is on, so the model collects the
events per thread and builds the graph once, after the merge (`tasks.build_graph`).

Every event is flag-less and carries `uint64 Timestamp` = `FPlatformTime::Cycles64()` -- a *global*
counter, which is why timestamps from different threads are comparable here when the trace's own
event times are not (`TaskTrace.cpp:14-73`):

| event | fields | meaning |
|---|---|---|
| `Created` | `Timestamp, TaskId, TaskSize` | the task object exists |
| `Launched` | `..., DebugName, Tracked, ThreadToExecuteOn, TaskSize` | its name, and where it may run |
| `Scheduled` | `Timestamp, TaskId` | prerequisites met, it is queued |
| `SubsequentAdded` | `Timestamp, TaskId, SubsequentId` | completing the first unlocks the second -- the **only** dependency record |
| `Started`/`Finished` | `Timestamp, TaskId` | the body runs: this pair is a task's **duration** |
| `Completed`/`Destroyed` | `Timestamp, TaskId` | nested tasks done; the object freed |
| `WaitingStarted`/`WaitingFinished` | `Timestamp[, uint64[] Tasks]` | the *recording* thread blocks, waiting for those tasks |

A task's life is four intervals and only one of them is work (`TasksProfiler.cpp:601-676`):
`Launched→Scheduled` is waiting for prerequisites, `Scheduled→Started` is queued, **`Started→Finished`
is executing** (the duration everything ranks by), and `Finished→Completed` is waiting for nested
tasks. `ThreadToExecuteOn` is the engine's own `ENamedThreads` packing
(`TaskGraphInterfaces.h:56-108`): the low byte names the thread, the high bits its queue and
priorities.

**The critical path is the engine's arithmetic**, from the Insights task-graph profiler
(`TaskGraphProfilerManager.cpp:759-760, 807-808`): walk the prerequisite edges and take, at every
branch, the chain whose **sum of executing durations** is longest -- `MaxChainDuration +
(Finished - Started)`, recursively. It adds no waiting and no gaps: what it answers is "if this chain
could not be shorter, the frame could not be faster", and a gap between two tasks of the chain is a
scheduling artefact, drawn as an edge rather than summed. Our implementation walks it with an
explicit stack (a deep graph cannot exhaust the interpreter's) and a three-colour mark, and a cycle
in those edges -- which the writer could not have meant -- is **counted and its edge ignored**
(`task_counts["ignored_edges"]`), never followed.

What the model keeps is bounded and says so: the chain is always complete (it is a chain, not a
graph), while the task table keeps the `TASK_KEEP` longest and the graph the `EDGE_KEEP` edges among
them, with `task_counts["dropped"]` counting what did not fit -- a capture that ran a million tasks
must not put a million rows in the cache.

Two things a real `task` capture will confirm and this machine cannot, because **none of the three
registered captures carries the channel** (`tasks` exits 2 with the re-record line on all three):
whether `TaskTrace.Launched` arrives with `MaybeHasAux` in its schema record (the engine's macro
declares no flags, while every corpus event with a string field carries the flag -- if it is not
written, the task *names* would need the aux read by another rule), and whether an editor or game
capture's task count fits the table. The protocol above is pinned to the engine source; the
fields, events and arithmetic are not guesses.

## 13. The coverage timeline, and the parallelism report (`parallelism`)

The practice's question after "what bounds this frame" is **"was the work spread?"** — Intel's loop
puts *decide CPU/GPU/display-bound* before any drilling, and the CPU half of that decision is about
occupancy: one thread working while the machine idles is a different problem from a saturated pool.
`ueia parallelism` answers it from measurements the walk takes for every thread, not from scope
names:

* **Coverage**, per thread, is the union of the cycles it spent inside `CpuProfiler` scopes. The
  walk records it as merged intervals — one per **outermost** span, because a nested scope closes
  inside its parent, so the outermost ones *are* the union — and the same for the spans whose name
  reads as a **wait** (`WaitForTasks` and friends) and as a **lock**. On the corpus's editor capture
  that is 769,875 busy intervals, 240,201 wait intervals and 36,509 lock intervals across 131
  threads; the game capture's RHI thread alone needs more than the cap below.
* **`model.span_kind`** decides the split from the name: `"wait"` anywhere in it is a wait, and the
  lock words (`lock`, `mutex`, `critical`, `semaphore`) are matched against the name's **camel-case
  words**, not as a substring — `AllocateHeapBlock` is in the corpus's timer table, and a
  case-insensitive `"lock" in name` reads it as one. 97 of the editor capture's 27,760 specs read as
  locks, 6 of the game capture's 2,784.
* **The timelines are bounded and the bound is counted.** A thread may keep
  `coverage.SPAN_KEEP` (262,144) intervals per set; past that the walk *merges* what it sees into the
  interval before it, which overstates coverage rather than dropping it — the safe direction for a
  parallelism claim — and counts the spans it swallowed (`counts["spans_coarsened"]`, and
  `thread_spans[].coarsened` so a table can name the row). The corpus's worst *uncoarsened* thread
  needs 120,846; the game capture's RHI thread exceeds the cap by 32,649 spans.
* **The timelines do not go in the cache.** They are packed (`coverage.pack`, u64) because a share is
  pickled between processes, and they are dropped after the measurement: what the model keeps is the
  answer (`thread_spans`, `frame_occupancy`), which is bounded by threads and frames rather than by
  spans. Those rows are what the cache grew for: the editor capture's 2,826 frame rows are **3.6 MB**
  of its **6.74 MB** cache (measured: 3.62 MB of a 7.44 MB pretty-printed document, against 3.40 MB
  for everything else the model holds).

**The measurement**: `coverage.measure_frames` folds every thread's timeline into every frame the
capture recorded (`Misc.BeginFrame`/`EndFrame` pairs, of every thread — a worker pool has no frames
of its own, so its occupancy only means something against somebody else's). Each frame row carries:

| column | what it is |
|---|---|
| `threads[]` | per thread: `busy_cycles`, `wait_cycles`, `lock_cycles` inside that window |
| `solo_cycles` | the frame's own thread **working** (busy minus wait) with no *other* thread working at the same cycle |
| `others_work_cycles` | the union of the other threads' work in the window (their overlaps counted once) |
| `all_cycles` | the union of every thread's coverage, waiting included, so `span - all_cycles` is time no thread was inside any scope at all |
| `peak_workers` | the most threads working at the same cycle — **work**, not coverage: a thread inside `WaitForTasks` is not one of them |
| `contended_cycles` | cycles two or more threads spent inside lock-named scopes together |

`solo_cycles` is the number the report's argument rests on, and it is deliberately *overlap*, not
dependency: two scopes that merely touch are not an overlap (`sweep` orders an end before a begin at
the same cycle), and the report says in the same line that a scope beside another is not a dependency
— what it does *not* say is that the solo work could have been spread.

**What the report prints, and what it refuses to claim.** The table is per thread over the series'
own span (busy/wait/lock percentages, plus why a row is worth reading); the prose carries the
occupancy split, the solo share, the frames that miss the budget with their own solo share, the
histogram of `peak_workers`, the lock overlap and the candidates. Three rules are visible in the
wording:

* **A ceiling is a heuristic.** The Amdahl figure is computed from the *measured* solo share over the
  thread count a frame usually has working, printed as a *ceiling* ("would cut the work to 25.0%"),
  never as a prediction.
* **The typical frame, not the best one.** The thread count in that ceiling is the **commonest**
  `peak_workers`, not the maximum: one startup frame where sixty threads run at once would otherwise
  turn "the machine was idle" into "the machine was full". The maximum is still printed, named as
  such.
* **No core count, no oversubscription verdict.** A `.utrace` does not record how many cores the
  machine had, so the report says so and stops there rather than guessing from the thread count.
  Contention is the same shape: it is the overlap of the names that read as locks, and a lock called
  something else is invisible — said out loud, because "not measured" is not "none".

**What the corpus shows** (`ueia parallelism --budget 60`, pinned as golden transcripts):

| capture | series | solo work | peak workers (commonest / most) | locks | verdict |
|---|---|---|---|---|---|
| `editor-pie-1` | tid 2, 1,413 frames, 217.200 s | **93.0%** (201.325 s of 216.493 s) | 5 / 60 | 42 frames, 0.162 ms | an editor thread working alone: its render and RHI threads are inside scopes 100% of the frames and *waiting* 98.5% / 97.0% of that |
| `game-pc-2` | tid 2, 771 frames, 40.121 s | **69.6%** (1.257 s of 1.806 s) | 23 / 63 | none | a game spreading its work: the other threads do 24.9% of the span's work beside a frame thread that is itself waiting 95.5% of the time |
| `viewer-pc-3` | none | — | — | — | **exit 2**: no `Misc.BeginFrame` pair to measure against |

The editor capture's candidate line names `UEditorEngine::StartPlayInEditorSession` (90% of the 16
frames the model keeps work for), which is the honest shape of that answer: a **sample** of the
thread's worst frames (`model._FRAME_WORK_KEEP`) plus a **name heuristic** that skips scopes already
called `Parallel*`/`Task*`/`Async*`, both said in the line itself. What a capture cannot show is
whether that scope *could* be split — which is why the ceiling, not the candidate, is the part of
this report a reader is meant to act on.

Cost: §6 — the timeline is ~1.2 s of the walk and the folding is 2.40 s of a cold parse, once per
capture; a warm `parallelism` is 0.33 s.

## 14. Source mapping (`sources`) — where a timer lives, and what its weight means

Every `CpuProfiler.EventSpec` carries a name, a file and a line (§3), and on the corpus 4,879 of
27,760 specs carry all three. `ueia sources` turns those strings into the answers a reader acts on —
*engine or project, which module, which file, and how much of the measured work it held* — and
cross-references the engine's own anti-pattern names. Three things define it, and the first two are
about not pretending:

* **The path is classified by its shape, never by this machine.** The strings are the *recording*
  machine's absolute paths (`D:\TikiStarMain_TMR\UnrealEngine\Engine\Source\Runtime\...`), which no
  tree on this machine contains. What is portable is the structure the engine itself enforces:
  `Engine` followed by `Source` or `Plugins` is the engine root (the *first* such pair — a module
  called `Engine` must not shadow it); `<root>\Engine\Source\<Group>\<Module>\Public|Private\...` is
  engine code; `<root>\Engine\Plugins\<Category>\<Plugin>\Source\<Module>\...` an engine plugin;
  `<project>\Plugins\<...>\Source\<Module>\...` a project plugin; a path with no `Source` segment at
  all — a bare file name, which is what a stripped build records — is **unknown**, and the report
  says unknown rather than guessing. Matching is case-insensitive; the spelling printed is the
  capture's own.
* **The engine tree only ever *checks*.** `--engine-dir` (or `$UEI_ENGINE_DIR`, `engine.py`) resolves
  the recorded engine paths against a tree and reports how many exist: on this machine **2,734 of the
  editor capture's 3,605** engine paths are in the 5.8.3 tree — a revision signal (871 are not, and
  the capture was recorded from another build) and never a requirement, because the mapping above
  needs no tree. A flag naming something that is not an engine tree is a usage error; the mapping
  without a tree says so in its own line.
* **A group's weight is a *presence*, not a sum.** The cycles come from `model["frame_work"]`: the
  biggest few timers of each of the longest frames of each thread (`_FRAME_WORK_KEEP`,
  `_FRAME_WORK_TOP`), **inclusive**, so a scope and its nested children both count the same cycles.
  Summing those per file or module is arithmetic and not an answer — measured on the corpus,
  `Runtime/CoreUObject` sums to **306,239 s of a 332 s capture**. So a file, a module and a pattern
  are all weighted by **the biggest matching timer inside each frame, summed over the frames**:
  bounded by the frames' own span (the walk clips a pair to the window it ended in), so the share
  stays a share, and it is a **lower bound** on the group's presence rather than an overstatement.
  The corpus: the biggest located timer is **96.6%** of the kept frames' span, and the mapping covers
  17.6% of the specs — the ones with a location own almost all of the measured work.

The anti-pattern rules are name rules, and the report calls them heuristics where it prints them:

| rule | names it matches | the guidance it is standing in for |
|---|---|---|
| `tick` | `tick` anywhere in the name | per-frame work in `Tick`: the engine's own "do less every frame" advice |
| `sync-load` | `StaticLoadObject`, `LoadObject`, `LoadPackage`, `FlushAsyncLoading`, `SynchronousLoading`, `LoadMap`, `GetOrLoad` | loading on the calling thread — the hitch that is not a hitch to profile, it is a decision |
| `object-churn` | `NewObject`, `SpawnActor`, `ConstructObject`, `CreateDefaultSubobject`, `DuplicateObject` | constructing objects and actors inside a frame |
| `gc` | `CollectGarbage`, `GarbageCollect`, `GCLock`, `IncrementalPurge` | garbage collection in a frame |
| `serialize` | `Serialize` | asset serialization on the calling thread |
| `wait` | `WaitForTasks`, `WaitFor` | a frame waiting — the same finding §13 reaches from the occupancy side |

A rule is only printed when at least `PATTERN_MIN_SPECS` (2) specs match it: one `Tick` timer is a
timer, not a pattern. Each finding carries the cycles of its biggest match in each frame, how many of
the kept frames are over budget, and the file:line of its biggest match, so the reader has somewhere
to go. What the corpus shows (editor capture, `--budget 60`): `wait` 48 specs at **49.5%** of the kept
frames with 17 over budget (`WaitForTasks`, `Runtime/Core/Private/Async/TaskGraph.cpp:734`);
`sync-load` 8 specs at **42.9%** (`StaticLoadObjectInternal`, `.../UObjectGlobals.cpp:1370`); `tick`
231 specs but **0.8%**, because the frames the model keeps for this capture are its loading stall and
not its steady state — a fact about the sample, said where it is printed. In the game capture the
whole picture is the opposite one: 1,352 of its located specs are engine code and none is project
source (a packaged build's scopes are the engine's), 83.2% of the kept frames are inside
`WaitForTask`-style scopes, and 31 of 32 are over budget — the same verdict §11's bottleneck report
reaches from the timing side, reached here from the names.

Cost: nothing measurable. This report adds no model field and reads no new bytes — the specs' file
and line were already in the cache (§3) — so a warm `sources` is the same 0.3 s as any other cached
command, and the cache version did not change for it.

## 15. The recommendations engine (`advice`) — rules, ranking, and the machine form

The practice's last question is *"so what do I do?"*, and `ueia advice` answers it as **rules over the
measurements the reports above already make** — never as new measurements. Each finding carries
`severity` (impact), `effort` (what the fix costs), `confidence` (`certain` / `heuristic` /
`unknown`, the vocabulary the rest of this repo uses), the evidence it was derived from, the
`file:line` where the trace has one, and **the next command to run**. The list is ranked
**severity → confidence → effort → id**, and the order is documented because a reader will disagree
with it: impact first, a certain finding before a heuristic one of the same impact, cheap fixes
before expensive ones.

The rules, and what each one is standing in for (the practice's own checklist):

| rule | fires when | category |
|---|---|---|
| `budget-default` | no `--budget` was given: the report ran against 60 FPS | budget |
| `no-frames` / `no-frequency` | no `Misc.BeginFrame` pair, or no cycle frequency: the frame questions cannot be answered | data |
| `frame-cap` | ≥25% of the frames sit within 5% of a **small** (≤4) multiple of a display period — the throttle shape | frames |
| `over-budget` / `hitches` / `tail-vs-mean` / `thin-sample` | the distribution itself: shares that miss the budget, hitches over `2 x budget`, p99 ≥ 2 x p50, or too few frames to have percentiles | frames |
| `bound-thread` / `bound-unexplained` | the bottleneck verdict: which thread owns the frames, or that **nothing this capture recorded** explains them | cpu |
| `one-timer` | one timer owns ≥ 25% of the frames the model keeps (`parallel`'s candidates), with its file:line | cpu |
| `workers-idle` | ≥ 80% of the frame thread's work had no other thread working beside it | parallelism |
| `sync-load` / `async-loading` | a named synchronous load with a share, or an over-budget frame whose kept timers are ≥ 25% async-loading names | streaming |
| `gpu-unknown` / `task-unknown` / `csv-no-values` | a channel that is absent (or registered with no values recorded): the re-record line, said as a finding | channels |
| `attribution-gaps` / `anomalies` | what the parse itself could not attribute or read cleanly: every number above is then a floor | data |

**`frame-cap` is stricter than `bottleneck.cap_note` on purpose**: a 905 ms frame is within 5% of 54
display periods, so *any* slow frame is "a multiple of a period" if the multiple may grow with the
frame. The rule accepts multiples of at most 4 (a genuine 30/60/120 FPS throttle) and names the
period and the multiple it found. On the corpus that is the difference between the editor capture
(55% of its frames on `2 x 16.667 ms`: a finding) and the game capture (905 ms frames: not one).

The machine form is `--format json`: `{"schema": "ueia.advice/1", "meta": {...}, "findings": [...]}`,
schema-versioned, with the `meta` block of README §4 — tool version, the capture's SHA-256 and size,
the session, the budget and whether it was given, the engine directory as configured, the analysis
duration, the **limitations** (every analysis that could not run, and why), and `executables` as an
empty list because this report wraps no engine program: stated rather than omitted, so "none" and
"not asked" read differently. `--skip id,id` drops rules; an unknown id is a usage error rather than
a filter that quietly matched nothing.

`advice` is the one report that **always exits 0**: it is the command that has something to say about
every capture, including "this capture carries no frame pair" — which is its first finding, and the
most severe one.

## 16. The A/B gate (`compare`), and the baseline it ratchets

The question a change asks is *did this make it better or worse?*, and `ueia compare` answers it by
reducing two cached analyses to the same named metrics and differencing them **directionally**: every
metric knows whether lower is better, so `frames.count` going up is not a regression and
`occupancy.threads_working` going *down* is not an improvement. Nothing new is measured — both sides
come from the same parser and the same definitions, and a comparison never mixes a fresh measurement
with a cached one.

The sections, in report order: **frames** (count, mean, p50, p95, p99, max, over-budget, hitches),
**work** (attributed scope pairs, the share that could not be attributed, timelines the cap merged),
**occupancy** (the frame thread's solo share, the others' work share, the threads working in the
commonest frame, contended frames), **sources** (specs with a file:line, distinct files) and **tasks**
(the graph's size and the critical path, when the channel is there). A metric present on one side
only is reported and **not differenced**, and a metric whose baseline is zero has no ratio: the count
is printed, and nothing is gated on it.

**The gate is the practice's own list.** Only `frames.mean_ms`, `frames.p95_ms`, `frames.p99_ms` and
`frames.hitches` can fail the run (`compare.GATED`), and only when the candidate is worse by more
than `--threshold` — 10% by default, which is the practice's "p99 more than 10% worse".
Improvements over the same threshold are **logged, never gated**: a gate that fails on improvement is
a gate nobody keeps. The exit codes are the CI interface: **0** nothing worse than the threshold (or
a baseline written), **1** the gate failed, **2** a usage error or nothing comparable.

**A self-comparison is an empty diff** — the A/B honesty check rdc-tools carries: comparing a capture
with itself (or with its own saved baseline) moves no metric, breaches nothing and improves nothing,
and the report says so in words. It is asserted on the fixture, on two identical captures and on the
corpus capture against itself.

**The rolling baseline** is the same schema, one side filled in: `--save FILE` writes
`ueia.compare/1` with every metric's baseline value and the `meta` block, and `--baseline FILE`
compares a capture against one. A baseline committed under `goldens/` can therefore ratchet down as
the tool's own numbers improve, instead of drifting up unnoticed; a file that is not a
`ueia.compare/1` document is a usage error naming what is wrong with it, never a silently empty
comparison.

The two machine-facing forms are `--format json` (`ueia.compare/1`: `meta`, `verdict` with the
threshold and the gated list, `breached`, `improved` and every metric with both sides and its delta)
and `--format markdown`, which is the PR-comment shape: the verdict, a table of what moved with a
`worse`/`better` column, what could not be compared, and a footer naming the schema and the gated
metrics. The artefacts are deliberately compatible with Gauntlet/`AutomatedPerfTesting` runs — their
`.utrace` is what we read and their `.csv` is what `ueia csv` wraps — and their *runner* is not
reimplemented here (§9). `compare` takes two captures, so the corpus harness (README §4) cannot pin
it: its transcripts pin one capture per command. It is exercised by hand against the registered
captures instead, and by the self-diff test over `editor-pie-1`.

## 17. The call tree and self time (`self`)

The CPU root-cause step. `summary` answers *which timer owns this frame* from the inclusive cycles the
walk keeps — enough to say "timer X owns 31% of frame 4121", but a scope's inclusive time includes
everything it calls, so the timer to blame is often one level down; and the frame someone asks about
is rarely one of the sixteen per thread the model keeps (`model._FRAME_WORK_KEEP`). `ueia self`
builds the **nesting tree** instead: inclusive and **self** time per timer, the callee expansion at
every level, top-N by self, and *any* frame of any thread.

**The two questions the item left open, answered by measurement and by what the cache is for:**

* **Self time must not come from the bounded sample.** The sample exists so a 100,000-frame capture
  does not carry 100,000 trees; the point of this report is a frame nobody kept. So the tree is a
  **second pass over that thread's streams** — seconds, paid only when this command runs, never by a
  parse, and printed in the report's own `pass` line (`7.25 s` over `game-pc-2`'s tid 2, `17.48 s`
  over `editor-pie-1`'s: 3,079,153 and 2,743,404 scope pairs).
* **The tree *is* in the cache now, but only for the frames this command reports on** (`FrameRow.self_detail`:
  the worst few by self time per thread, `_SELF_DETAIL_KEEP`, plus any frame a run names with
  `--frame`). A tree per frame for every frame of every thread would dwarf the model (6.74 MB for the
  editor capture, §13), so this is a bounded sample of the trees that get asked for -- and `self` says
  on every run which frame it served and what the model keeps, so the reader is never guessing
  whether the report came out of the cache. The first run over a capture pays for the pass; every run
  after it reads the stored detail, and **the report is rendered from the stored detail either way**,
  which is what makes a warm run and a cold run print the same lines (`cache.py`: no command's output
  may say whether it was warm; `test_calltree` runs `self` twice on a capture and compares byte for
  byte).

**How a pair is placed: the model's own rule, one frame per pair.** A scope belongs to the frame that
holds its **end** (the first frame with `end >= cycle`, the test `model`'s cursor makes) and is
clipped at that frame's begin when it started earlier — the situation the walk counts as
`scope_pairs_spanning`. Its children *in that frame* are the pairs that closed inside it there, so a
tree of intervals whose depth came from the wire; `self` is the clip minus its children's clips, the
frame's self time is the sum over its roots, and the two agree by construction (`test_calltree` pins
that on the fixture and on a real capture). Siblings that name the same timer **merge**: one node with
`calls` counting them, because "called 12 times, 40 ms of it not in a callee" is what a reader wants,
not twelve identical rows.

One consequence is not obvious and cost two attempts on the corpus (2026-09-29). A scope that *spans*
many frames belongs to none of them but the last, so the pairs that closed inside the earlier frames
while it was open have **no parent in those frames** and are promoted to **roots** there — by the
promotion at the close of the spanning pair, and by `weigh` for a pair still open when a frame is
weighed. A pair's own framing must not gate that promotion: the corpus's long engine scopes end
*after the last frame*, and a promotion inside the "is this pair framed" branch never ran for them, so
every frame read 0.000 ms while the pairs were being attributed correctly all along (the instrumented
`weigh` showed `attributed=3108`, `roots=0`).

This rule is the model's, deliberately, and it used to differ: the pass credited a scope to every
frame it *overlapped* (§13's `scope_pairs_spanning` counted the same situation the other way). One
rule everywhere means `self`, `summary` and `bottleneck` cannot answer the same question two ways —
and it is also why the pass got faster, since the per-pair clipping it needed is gone (`editor-pie-1`:
**17.48 → 9.64 s**).

**What it counts rather than invents.** An end with no begin, a begin that never closed, a pair
outside every frame — each is a number in the `pairing` line, never a guessed pair (the editor
capture has 5 unclosed begins out of 2,743,404 pairs; the game capture none).

**The fixture, and what it caught.** `fixtures.calltree_stream` is three frames of one thread with
every number hand-computable at 1 cycle = 1 microsecond, including the case that a naive ranking gets
wrong: F1 is the **longest** frame (140 ms) and F2 the worst by **self** (80 ms against F1's 30 ms),
so a report that ranks by duration picks F1. It also carries a scope that begins before F0 and closes
inside it (only its last 10 ms are in F0), a scope that straddles F1 into F2 (10 ms in each tree,
merged with F2's own 70 ms sibling), two merged `FrameTime` siblings, one end with no begin, and one
begin that never closes. Three arithmetic bugs were found by that fixture before any capture was
read: roots held once per frame the scope rooted in (a straddling scope was counted twice), a frame's
list was summed across frames (siblings from another frame merged into this tree), and the first
(monotonic-cursor) frame lookup lost the frames a pair *began* in when a long scope closed after a
short one inside it.

**What the corpus shows** (measured 2026-09-29; the frames are the ones with the most self time):

| capture | frame | inside scopes | **self** | the tree's shape |
|---|---|---|---|---|
| `game-pc-2` (tid 2) | #52 | 816.212 ms | **815.384 ms** | three roots, and `WinPumpMessages 815.133 / 815.133 ms x1` is almost all of it: the worst frame in the capture is the engine pumping its own message loop — an answer `summary` cannot give |
| `game-pc-2` worst three | #52, #60, #36 | | 815.384 / 177.750 / 164.073 ms | the three are 8.2 s / 1.8 s / 1.7 s frames: the hitch frames, self time and all |
| `editor-pie-1` (tid 2) | #2 | 1,411.885 ms | **1,092.902 ms** | roots are the engine's own frame plumbing — `Bv.OnBeginFrame_Kick`, `FRenderCommandPipe_StartRecording`, `FStats::AdvanceFrame` — so most of the frame is the tool's own bookkeeping |
| `editor-pie-1` worst three | #2, #2350, #2352 | | 1,092.902 / 1,070.537 / 1,054.345 ms | |

**Cost, and why it is not a golden transcript.** The pass is per thread and per run: **3.89 s** over
the game capture's tid 2 and **9.64 s** over the editor's, on top of the streams assembly the command
needs (~0.3 s of it is the cached model the frame list comes from). Both fell when the rule became
the model's — 7.25 s and 17.48 s before it — because the per-pair clipping the overlap rule needed is
gone; what is left is the decode the format forces (each thread's records are delta-encoded, so no
pass can skip any of them). It is paid **once per capture**, because the trees it builds are stored
in the model (`FrameRow.self_detail`): 4.86 s on this capture's first `self` run and 0.27 s on the
second, byte-identical output (12.33 s to 0.38 s on the editor capture, measured 2026-09-30).
Pinning it in the corpus harness would
add that to `goldens --check` for every registered capture, so it is **not** in
`goldens.PINNED_COMMANDS` — a decision, not an oversight: the harness pins commands whose cost is a
fraction of a second, and the suite covers this one hermetically (the fixture, hand-computed) plus
one real-capture invariant check.

**Self time is measured twice, and the two must agree.** The walk credits every frame's self time
during the parse (`FrameRow.self_cycles`): the same rule as this pass (a pair belongs to the frame
holding its end, clipped at that frame's begin), and the same promotion — a pair still open when a
window is flushed releases what it holds for that frame, because the children it collected there are
that frame's roots. That promotion forced an ordering that is easy to get wrong and was: the flush
must run **before the pair is popped**. A scope that closes in the *gap* between two frames (or after
the last one) is not in any frame, so it credits nothing itself — but its slot held its children's own
time, and popping it first threw that time away. The corpus showed it as `covered=8980608` at the
flush of the game capture's frame 0 with `sum(child_own)=0`, and the fix is what makes the walk's
numbers match this pass's: `test_calltree` compares them **frame by frame** on the real capture, and
they agree to the millisecond (815.384 / 177.750 / 164.073 ms as the worst three).

What the cached number is for, and what it is not: it is **one integer per frame**, so `self` names the
worst frames without decoding anything again, and the *reading* of the streams that builds their trees
happens **once per capture** — those trees are then stored in the model (above) and every later run
renders from them. Measured 2026-09-30 on the game capture: 4.86 s for the first run, **0.27 s** for
the second, byte-identical output.

**What it cannot say.** Whether a scope *should* be split (that is `parallelism`'s measured ceiling,
§13), what a timer whose name never reached the table is (`spec N`), and anything about a frame with
no pair inside it (the tree is empty and the `pairing` line says the counts). Exit 0 a tree was built
/ 2 the capture cannot answer — no `CpuProfiler` timer specs, no `Misc.BeginFrame` pair for the
thread, or a `--frame` that thread does not have.

## 18. Finding an engine tree (`--engine-dir`, `$UEI_ENGINE_DIR`, and the search)

Three consumers need an engine tree (§2's CSV toolbox, §14's source mapping, every report's version
stamp) and one module owns the question (`engine.py`). What a tree has to hold — the **inventory** —
is the list of things this repo can actually use:

| Part | Where | Needed for |
|---|---|---|
| the CsvTools executables | `Engine\Binaries\DotNET\CsvTools` | the CSV toolbox (`csv`, README §2) — all eight this repo wraps (`csvinfo`, `CSVSplit`, `CsvConvert`, `CSVFilter`, `CSVCollate`, `CSVToSVG`, `PerfreportTool`, `RegressionsReport`) |
| `Build.version` | `Engine\Build\Build.version` | every report's `meta` block, and the version the search ranks by |
| the source tree | `Engine\Source` | module/source mapping (§14) — optional, and its absence is reported as "module unknown", never as zero |

**Resolution order: the flag, the environment, then a search.** An explicit `--engine-dir` that names
a tree is used as given and *nothing is searched* — an instruction is never second-guessed. A flag
that names something which is **not** one is said out loud and then searched past (2026-09-29: it used
to end the command; a typo is a mistake worth reporting, not a reason to refuse work that a tree on
this machine can do) — and if the search finds nothing, the flag's mistake is still a usage error
(exit 2). `$UEI_ENGINE_DIR` is reported and *ignored* when it names a non-tree, because an ambient
setting must not change what a command does quietly. Nothing anywhere is legal: the commands that need
engine tooling report **skipped** with the hint.

**What the search is.** Three sources, no directory walk:

* **the launcher's own install list** — `%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests\*.item`,
  one JSON per install, read for its `InstallLocation`. A file that cannot be read is skipped: a
  search may find nothing, never fail;
* **the source builds the engine registers** — `HKCU\Software\Epic Games\Unreal Engine\Builds`, which
  is where a source build lists itself so it can be run without a launcher entry;
* **the usual install directories** on `C:`–`H:` — `<drive>\Epic Games\UE_*`,
  `<drive>\Program Files\Epic Games\UE_*`, `<drive>\UE_*`, `<drive>\UnrealEngine*`,
  `<drive>\Unreal Engine\UE_*` and friends (`INSTALL_SUBDIRS`), one or two levels deep. Network drives
  are never probed: a disconnected one can block for seconds, which is not a *quick* scan.

Measured on the machine this was written on (2026-09-29): **four engine trees in 0.05 s** — two at
5.8.3 (both complete), a project-local 5.7.0, and a 5.4.4 missing `RegressionsReport.exe` — of which
the launcher list and the registry between them found all four and the directory globs found none,
which is why the first two are in the search at all.

**What is picked, and what is printed.** `rank` orders the candidates: a **complete** tree before a
partial one (a 5.8 install with no CsvTools cannot run `csv` at all, while a complete 5.6 can run
every part of this repo), then the **newest version** — compared as numbers, because `UE_5.10` is
newer than `UE_5.9` and a string comparison says the opposite — then how many tools are there, then
the path, so the answer is stable run to run. The version comes from `Build.version`, falling back to
the directory name (`UE_5.8` is exactly how a launcher install is named). Whatever wins, the report
says so:

```
scanned  : C:\Workspace\UnrealEngine_Code_Inner (5.8.3, 8 of 8 CsvTools, source tree present)
           -- 4 engine tree(s) on this machine
```

and a winner with gaps names them (`scanned  : it lacks 1 CsvTools executable(s) (RegressionsReport.exe)`),
because a partial tree chosen silently would be a lie of omission. There is no per-consumer choice: the
tree that gives the most is used for everything, and what it lacks is printed.

**The search is switched off with `UEIA_NO_ENGINE_SCAN=1`** (`shapes.ENV_NO_ENGINE_SCAN`). Two callers
use it, for the same reason — output must not depend on what is installed on the machine running it:
the hermetic suite sets it, and the corpus harness passes it to every pinned command, because `sources`
and `advice` map file:line through whatever tree the machine has. With auto-discovery landed but that
switch absent, six transcripts (sources and advice, all three captures) differed immediately: the pins
record the *no engine tree* behaviour, which is the deterministic half, and discovery is covered by its
own tests (`test_engine.TestTheSearch`).

## 18. What a capture carries, and what it can answer (`coverage`)

The practice's first two steps as one command: *set the goal and the test system*, and know what the
file in your hand can be asked. Every trace declares its own **channel registry** (each row with an
`is_enabled` flag), so the file says what was recorded without decoding an event -- and an absent
channel is **not** a zero value, it is a question the capture cannot answer (SS8). `ueia coverage`
turns that into a verdict per analysis, names what is missing, and quotes the `-trace=` line that
would have recorded it.

**Two things stop this being a table lookup.** The registry lists every channel the *engine* knows,
so "declared" and "recorded" are different answers; and the registry can be wrong. On the corpus,
four channels (`bookmark`, `counters`, `log`, `stats`) carry events while their own registry rows say
they were off -- the report prints that disagreement rather than quietly preferring one side. So
wherever the tool models a channel, **the model's events decide**: a row claiming `cpu` is not enough
when the schema holds no timer specs, and a capture whose registry is silent is still answerable when
the model is full of events. For a channel nothing models yet (`memalloc`, `loadtime`, ...) the
registry is the only word there is, and the row says so.

**What it prints.** The channels recorded against the channels declared; the analyses that cannot run
(`skipped`, never `0`), each with the channels it needed; the capture's metadata out of the trace
itself (build, changelist, configuration, platform, project, length, threads, events); and the
hygiene the practice asks for before anyone trusts a number -- frames and their distribution per
thread (median, p95, longest), whether the first frame looks like a **warm-up** to trim (measured
against that series' own median, so a capture whose frames are all slow does not read as one with a
warm-up), and, when a second capture of the same scene is given, the run-to-run spread of the
medians.

**The re-record lines** are two, because they are two recordings: the CPU-side channels come from the
engine's `Default` preset (`-trace=cpu,gpu,frame,log,bookmark,region,screenshot`) and allocations,
LLM tags and callstacks come from `-trace=Memory`, which a capture has to be recorded with on purpose.
A capture missing both gets both lines.

Exit codes: 0 whenever the capture has a registry or any event at all -- "this capture cannot answer
that" *is* the answer here -- and 2 when the file has neither, so there is nothing to report on.

## 19. The current GPU channel: queue timelines, passes, and the `gpu` report

The engine has **two** GPU channel shapes on one `gpu` channel id. The corpus carries the *legacy*
one (§11: a whole rendered frame packed into one `GpuProfiler.Frame` event). UE 5.6 removed that
writer and replaced it with the **current** one — `Runtime\RHI\Private\GpuProfilerTrace.cpp` writes
one trace event per GPU work item, and `TraceServices\Private\Analyzers\GpuProfilerTraceAnalysis.cpp`
(the reader this module mirrors rule for rule) consumes them. What the current channel declares:

| Event | Fields | Notes |
|---|---|---|
| `GpuProfiler.Init` (Important) | `uint8 Version` | 2 in this checkout; read into the session |
| `GpuProfiler.QueueSpec` (Important) | `uint32 QueueId`, `WideString TypeString` | `GPU = id>>8`, `Index = id>>16` (both `& 0xFF`), `Type = id & 0xFF` |
| `GpuProfiler.EventFrameBoundary` | `uint32 QueueId`, `uint32 FrameNumber` | the queue's own rendered-frame numbers |
| `GpuProfiler.EventBeginWork` / `EventEndWork` | `QueueId`, `uint64 GPUTimestampTOP` / `GPUTimestampBOP`; the begin adds `uint64 CPUTimestamp` | the work brackets; the CPU timestamp is the submission |
| `GpuProfiler.EventWait` | `QueueId`, `uint64 StartTime`, `uint64 EndTime` | a self-contained span: the queue idle waiting for a fence |
| `GpuProfiler.EventBreadcrumbSpec` (Important) | `uint32 SpecId`, `WideString StaticName`, `WideString NameFormat`, `uint8[] FieldNames` | the id → name map; `FieldNames` is a CBOR blob, counted not decoded |
| `GpuProfiler.EventBeginBreadcrumb` / `EventEndBreadcrumb` | `SpecId`, `QueueId`, `uint64 GPUTimestampTOP` / `GPUTimestampBOP`; the begin adds `uint8[] Metadata` (CBOR) | the *named* spans — the current channel's "passes" |
| `GpuProfiler.EventStats` | `QueueId`, `uint32 NumDraws`, `uint32 NumPrimitives` | accumulated between frame boundaries |
| `GpuProfiler.SignalFence` / `WaitFence` | `QueueId`, `uint64 CPUTimestamp`, `uint64 Value`; the wait adds `uint32 QueueToWaitForId` | who signalled, and which queue waited on whom |

The wire rules the reader pins, and ours follow:

* **One clock.** The platform RHI translates GPU timestamps into the CPU clock domain before the
  profiler sees them (the calibration the legacy channel carried is gone — the header says so), and
  the engine reader converts them with the *session's* clock (`Analysis\Engine.cpp`'s
  `FEventTime::AsSeconds(ts)`, the same conversion CPU cycles get). We place the spans with the
  session's own base and frequency — and then **check** the placement: fewer than half the work
  spans landing inside a frame window, or a timeline that overlaps none, is refused, and the
  per-frame numbers read as unknown while the queue totals stand.
* **Two stacks per queue.** Breadcrumbs and work bracket independently (the reader keeps a stack
  per kind), so a breadcrumb can nest inside a work span without disturbing either pairing.
* **A timestamp of 0 is "could not be determined"** and the reader skips the event; we skip it and
  count it (`gpu_zero_timestamps`). Interleaved/reversed timestamps, negative durations, unpaired
  brackets and breadcrumb begins with no spec are warnings on the reader's console — here they are
  counters a report can quote (`gpu_out_of_order`, `gpu_negative_durations`, `gpu_work_unpaired`,
  `gpu_breadcrumb_no_spec`), because a count survives where a console does not.
* **`busy_us` and `wait_us` are unions** of each queue's intervals — outermost work spans do not
  overlap, so their union is the time the queue was executing traced work, and a nested span is
  counted once. The kept intervals are capped per queue and kind (`gpu.QUEUE_SPAN_KEEP`); past the
  cap the tail folds into the last interval — the union is then *overstated* where it happened,
  which `gpu_spans_coarsened` counts. Named passes are capped by inclusive time
  (`gpu.QUEUE_PASS_KEEP`), the fold counted in `gpu_passes_dropped`.
* **Wide strings on the importants stream are UTF-16.** `QueueSpec.TypeString` and the breadcrumb
  spec's names are declared `WideString`, and the engine's important writer `memcpy`s the UTF-16
  (`ImportantLogScope.inl`'s `FFieldSet<…, WideString>`) — where a field is declared `AnsiString`
  and fed wide literals (the CPU profiler's spec names), the same writer truncates each character
  to its low byte. Decoding by the *declared* type (2026-09-30) is what unmangled
  `Diagnostics.Session2`'s build version and `Misc.BookmarkSpec`'s format string on real captures.

**What `ueia gpu` reports** (`--table queues|passes|frames`): per queue — name, busy/wait unions,
submit-to-start lag (mean, max, and the count of submits whose CPU timestamp lands *after* the GPU
start: counted, never reinterpreted), draw counts and frame boundaries; per pass — calls, inclusive
time, the biggest single span and the second it ran; per frame — the GPU busy and wait time inside
each window of the judged series, worst first, the same number `bottleneck` spends. The fences name
the cross-queue blocking (`queue 2 waited on queue 0, 3 time(s)`). A capture carrying the legacy
channel instead gets its state named and a pointer to `bottleneck`, which judges frames; a capture
carrying neither exits 2 with the `-trace=` line. Breadcrumbs are conditionally compiled into the
engine (`WITH_RHI_BREADCRUMBS`), so work without a single named pass is its own sentence, not an
error.

On this machine's corpus the current channel is absent from all three captures (the editor session
carries the legacy one; the other two carry no GPU data at all), so the transcripts pin the absent
halves — the queue decode itself is pinned by the hermetic fixtures, hand-computed microsecond by
microsecond (`test_gpu`, `test_queues`), and a capture recorded with a 5.6+ engine is what the
queue half still wants against the real world.
