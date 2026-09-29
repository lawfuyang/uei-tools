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
with the CSV profiler *registered* but no CSV capture active (README §2; ROADMAP §11 wants a
capture that has both). The engine's own reader of these events is
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
attribution, occupancy and the GPU frame decode included — §10, §11):

| phase | serial | `--jobs 0` (the pool) |
|---|---|---|
| read the 34 MB file | 0.008 | 0.011 |
| container header | 0.000 | 0.000 |
| packets (163,600 headers) | 0.250 | 0.254 |
| streams (LZ4: 24,767 blocks, 34.8 MB → 49.9 MB) | 0.651 | 0.671 |
| model (the per-thread Python walk, all of the above) | 11.485 | 4.752 |
| **cold full decode** | **12.39** | **5.69** |

A cached command is **0.23-0.29 s** (`summary` and `bottleneck` included: everything they report is
in the cache, never re-derived from the streams) and `goldens --check` ~20 s over the three
registered captures, whose transcripts are the pinned commands' real output. What a warm command
spends its time on, measured 2026-09-29: **0.17 s interpreter and imports** (nothing this tool can
avoid), **0.03 s** hashing the capture and **0.02 s** parsing the 3.35 MB cache, and -- before the
lazy packet walk below -- **0.25 s walking 163,600 packet headers on every command**, including the
seven that never look at the table. That is why `CaptureView.packets` is a property: `info`,
`packets`, `schema` and `verify` walk it, the model-based commands never pay for it.

Three histories are in that table. The LZ4 half is the C library (§2): with the pure-Python decoder
the same cold decode was **21.3 s**. The walk is per-thread work, so `--jobs` spreads it over
processes — measured 2026-09-29 on the same capture: **12.99 / 7.54 / 5.99 / 6.00 / 5.99 / 6.04 /
6.02 / 6.10 s** at 1 / 2 / 4 / 6 / 8 / 10 / 12 / 16 workers. It stops at the *biggest* thread rather
than at the core count: tid 2 owns 53.4% of the corpus's 10,055,971 batch records, so the ceiling is
that one thread and the curve is flat from 4 workers on (8 workers was 4.14 s before the occupancy
landed, on the same shape of curve). `--jobs 0` (the default) caps its own choice at 8, while a
capture whose work is spread evenly gets more of the box. The answer does not depend on `--jobs` at
all: shares are merged by one function in ascending tid order, byte-for-byte into the cache's own
format.

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
zero value: it is a question the capture cannot answer**, and the tool says so (ROADMAP §7).

| Channel | Carries | Answers | Engine analyser to mirror |
|---|---|---|---|
| `cpu` | CPU scopes on every thread (`CpuProfiler`) | which work owns the frame: the call tree, per-thread timelines, self time | `CpuProfilerTraceAnalysis.cpp` |
| `gpu` | GPU timings, breadcrumbs, queue sync (`GpuProfiler`) | per-pass cost, GPU-bound confirmation, CPU↔GPU waits | `GpuProfilerTraceAnalysis.cpp` (and `OldGpuProfiler…` for the pre-5 format) |
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

So the GPU and memory items do not start from nothing after all; what is still missing for a full
GPU answer is the *current* channel's queue semantics (waits, fences, breadcrumbs — §11 decodes the
legacy per-frame form), and for memory an allocations channel rather than a tag scope.

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
frame), not the current one (one event per GPU work item): the writer was removed in UE 5.6, and UE
5.8.3 ships only the reader (`OldGpuProfilerTraceAnalysis.cpp`, "maintained for backward
compatibility with old traces"). That reader is the layout's authority, and `gpu.py` follows it field
for field:

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
