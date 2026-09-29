# uei-tools — build roadmap / TODO

**Status (2026-09-29):** the parser core, the CsvTools integration, the summary layer, the
**bottleneck verdict**, the **task graph with its critical path**, the **parallelism findings**, the
**source mapping**, the **recommendations engine**, the **call tree and self time** and
**`compare` with its CI gate** have landed and left this file — `src/py/` is
covered by **517 hermetic unit tests** (`selftest`; the engine-toolbox half skips, loudly, on a machine without an engine tree),
clean under Pyright (0 errors, 0 warnings), and verified against the **three registered captures**
(`editor-pie-1`, `game-pc-2`, `viewer-pc-3`): `goldens --check` compares the pinned commands' *real*
output over all of them and matches — `summary`, `bottleneck`, `tasks`, `parallelism`, `sources` and
`advice` included (and `compare` exercised by hand: the corpus harness pins one capture per command,
and a comparison takes two), with the corpus's histogram, its 121-second frame, the frame-rate cap that explains 1094 of
its unexplained frames, two answers about parallelism (**93.0%** of the editor's frame-thread work ran
with no other thread working, **69.6%** of the game's did, REFERENCE §13) and where those frames were
spent: the editor's biggest located timer is `RenderingFrame` in `LaunchEngineLoop.cpp` and its
synchronous loads are 42.9% of the kept frames, while the game capture's 1,352 located specs are
engine code with 83.2% of the kept frames inside `WaitForTask`-style scopes (REFERENCE §14). `verify`
reports 0 errors over 163,600 packets / 960,142 events / 10,055,971 batch records with a gapless
serial range. The LZ4 decoder is the C library this repo vendors and builds (`ueia lz4 --build`, gate
step 0), the per-thread walk runs over worker processes (`--jobs`), attributes each frame's work to
its timers by **cycle window**, measures each thread's occupancy (work vs wait) and folds every
thread's coverage timeline into every frame, and a cold full decode of the corpus is **8.86 s**
(REFERENCE §6; the coverage timeline and the frame measurement are ~3.6 s of it, §13). The CSV toolbox
is wrapped behind `ueia csv` (`--engine-dir` / `$UEI_ENGINE_DIR`), with the one bridge no engine exe
offers — `csv from-trace` — writing the format the engine's own readers parse; `csv info` on a
synthesized file is the acceptance test. The command list is in `README.md` §4; the format facts are
in `REFERENCE.md` — §10 for what a frame-time report means by a frame, a percentile, a hitch and "what
ran in it", §11 for the bottleneck verdict and the legacy GPU channel it is measured against, §12 for
the task graph, §13 for the coverage timeline and the parallelism report it feeds, §14 for the source
mapping and what a timer's weight means, §15 for the recommendations engine and its machine form, §16
for the A/B gate and the baseline it ratchets, §17 for the call tree and self time.

Landed items are *removed* rather than ticked off, and the remaining sections renumbered with
their cross-references updated in the same change (the
[rdc-tools](https://github.com/lawfuyang/rdc-tools) rule).

**Every item below lands with its tests** (the rule in `AGENTS.md`, "Tests come with the
feature"): the effort figures include the unit-test work, and a phase is not done — and is not
removed from this file — until its hermetic suite covers it, copiously. An item whose tests do
not exist has not landed; an item whose tests are weak is still open.

Legend: **P0** = do next / unblocks everything · **P1** = high value, moderate effort ·
**P2** = useful, opportunistic · **P3** = nice-to-have. Effort figures are for one agent-day.

## What professional performance work looks like (researched 2026-09-28)

Three sources define the practice this file is aimed at: Intel's GPA *Game Optimization
Methodology* (the canonical loop), Epic's *Introduction to Performance Profiling and
Configuration* (what the engine expects you to look at), and Bugnet's *How to Test Game
Performance Regression in CI* (what a perf gate must be to be trusted).

**The loop** (Intel), in order: *set the goal and the test system* — genre and target hardware
(Steam's hardware survey is the proxy), a CPU and GPU from the same era and price band, a
dedicated machine; *find the scene that is slow* and capture it reproducibly; **decide CPU-,
GPU- or display-bound before drilling anywhere**; rank the frame's contributors; drill to the
root cause with a call tree and a timeline; fix; re-measure; and **stop when the goal is met** —
optimization is unbounded, so the budget is the stopping condition.

**The numbers the practice runs on.** Epic's targets are 30/60/120 FPS and "consistency matters
as much as the number": a hitch is felt where an average is not. The CI literature is blunter —
a **16.6 ms average with 50 ms spikes feels terrible** — so a gate tracks mean/p95/**p99** and a
**hitch count** (frames over 33 ms), and fails a build when **p99 is >10% worse** than a rolling
baseline (the average of the last 10 main-branch runs, committed in-repo), with improvements
logged rather than gated so the baseline ratchets down instead of drifting up. Captures for that
come from a scripted scene battery (each ~30 s, **5 s of warm-up discarded**, one scene per
subsystem) on a machine that is *not* a shared CI runner, because a noisy neighbour can double
frame times. Epic adds the overhead rule: profiling costs performance, so hitting the target
*while profiling* means you have surpassed it.

**What that asks of this tool**, beyond reporting what is in the file: say whether a capture can
answer a question at all; answer with the distribution rather than the average; attribute the
tail to named work; and turn two captures into a verdict a build can be blocked on. Where the
practice's inputs do not exist in a trace — GPU hardware counters, cache misses, driver
internals — the honest answer is "out of reach here", never a number.

Sources: [Intel GPA — Game Optimization
Methodology](https://www.intel.com/content/www/us/en/docs/gpa/user-guide/2024-4/game-optimization-methodology.html) ·
[Epic — Introduction to Performance Profiling and
Configuration](https://dev.epicgames.com/documentation/unreal-engine/introduction-to-performance-profiling-and-configuration-in-unreal-engine) ·
[Bugnet — How to Test Game Performance Regression in
CI](https://bugnet.io/blog/how-to-test-game-performance-regression-in-ci)

### Where each practice lands in this file

| The practice's step | The data it needs | In a `.utrace` | Lands in |
|---|---|---|---|
| Budget verdict (30/60/120 FPS) | per-frame times | `Misc.BeginFrame`/`EndFrame` pairs, `session.cycle_frequency` | landed: `ueia summary` (REFERENCE §10) |
| Hitch hunting | the tail, not the mean | the frame-time distribution + what ran in the worst frames | landed: `ueia summary` (REFERENCE §10) |
| CPU / GPU / display-bound | CPU frame time vs GPU busy | game/render/RHI thread scopes + the `gpu` channel | landed: `ueia bottleneck` (REFERENCE §11), with the per-pass/queue detail in §2 |
| CPU root cause | inclusive vs self time, call tree | the timer scopes whose nesting is already decoded | landed: `ueia self` (REFERENCE §17) |
| Which code, which module | file:line per timer, engine vs project | the spec table's own `file`/`line` | landed: `ueia sources` (REFERENCE §14) |
| Threading / parallelism | occupancy, waits, critical path | batch records, `task`, thread-idle scopes | landed: `ueia parallelism` (REFERENCE §13) and the chain in `ueia tasks` (§12) |
| GPU root cause | per-pass and per-draw cost | `gpu` channel (`GpuProfiler`) | §2 |
| Memory questions | LLM tags, allocations, sites | `memtag`, `memalloc`, `callstack`, `module` | §3 |
| Streaming hitches | load trees, IO waits | `loadtime`, `asset`, `file`, `iostore` | §4 |
| The engine's own stat numbers | `stat` values per frame | `stats`, `counters` (+ CSV values via `csv from-trace`) | §5 |
| Comparable captures | scene hygiene, channels, run-to-run noise | capture metadata, channel coverage, variance | §1 |
| Build-to-build gating | p95/p99 + hitch count, rolling baseline | two analyses of the same scene | landed: `ueia compare` (REFERENCE §16) |
| "So what do I do?" | ranked, evidenced actions | all of the above | landed: `ueia advice` (REFERENCE §15) |

## The language decision (2026-09-28, kept honest by a measurement)

**Python 3.8+, stdlib only, pyright "standard", hermetic unittest, goldens corpus — the exact
rdc-tools stack.** Reasons, in order:

1. The project brief says "in the same flavor as rdc-tools", and the flavor there is not just
   philosophy — it is the working stack: a pure Python analyser over `mmap` walks 631 MB–1.5 GB
   capture streams in seconds. A 33.9 MB `.utrace` is well inside that envelope.
2. This machine has Python 3.11.3 and **no Rust toolchain** (checked 2026-09-28: `cargo` not
   installed). A language we cannot build is not a choice.
3. The one genuinely hot spot — LZ4 packet decode (24,767 encoded packets in the corpus) — is
   answered by **the C library, built from source this repo vendors** (`bin/ueia_lz4.dll`, the
   exact rdc-tools DLL pattern: `src/cpp/third_party/lz4`, `ctypes`, `$UEI_LZ4_DLL` to point at a
   system liblz4). The Python side never grows a decoder of its own: one decoder means one answer.
4. The remaining hot spot — the Python walk over the decoded streams (960 k events, 10.1 M batch
   records) — is **per-thread work, and runs over worker processes**: `--jobs N`, the pool's own
   choice by default, with the merge pinned so the answer cannot depend on it. It bounds itself by
   the biggest thread rather than by the core count (the corpus's tid 2 is 53.4% of the records),
   which is measured, documented, and *accepted* instead of papered over.

The initial research recommends Rust for the core parser, and that recommendation is
**recorded, not discarded**: the trigger to revisit was a measured one — a full-pass query on
the real corpus taking long enough to hurt the interactive agent loop (say >30 s uncached).
Measured 2026-09-28 with the parser landed, the C decoder in place and the walk spread over
processes: a **cold full decode is 5.2 s** (34 MB capture; LZ4 is 0.60 s of it, the walk 4.1 s
over 8 workers, from 9.6 s in one process), and a **warm cached command is 0.49 s**. The trigger
did not fire, and the phase table says where the remaining time actually is: the batch-record
walk, which is now parallel rather than slow — a native rewrite would buy the ~4 s of Python
recording, not the 0.6 s of decoding. It stays as the stated bar for a full pass that must be
faster, rather than a matter of taste.

### Python, C++, or the engine's own tools? (asked 2026-09-28, answered here)

**Every analysis stays in Python; native code is only ever *wrapped*, and only where the engine
already answers.** The data is parsed once (REFERENCE §1–§5) and everything above it is
aggregation, which is what Python is for; the single native piece we own is the LZ4 DLL.
What the engine's tree offers instead (found in this checkout, 2026-09-28 — REFERENCE §9 lists the
files):

* **`TraceQuery`** (`Engine\Source\Programs\TraceQuery`, console, **no editor build needed**)
  drives the engine's own analysers (CPU profiler, counters, memory, regions, log) and emits
  **JSON**. That makes it the strongest independent check available offline: the corpus gate can
  run it on the same capture and compare its per-thread / per-timer numbers with ours, which is
  evidence no hermetic fixture can give.
* **`TraceAnalyzer`** (`Engine\Source\Programs\TraceAnalyzer`, also editor-free) dumps a trace as
  text — the tool to reach for when decoding a channel we have not parsed yet.
* **`TraceTrimmer`** rewrites traces; useful for cutting corpus captures down to shareable sizes.
* **`UnrealInsights.exe -NoUI -AutoQuit -ExecOnAnalysisCompleteCmd="TimingInsights.Export*"`
  / `MemoryInsights.ExportAllocs`** exports timers, threads, timing events, timer statistics,
  callees and counter values as CSV/TSV. It needs a *built* Insights (this checkout has no
  prebuilt `UnrealInsights.exe`), so it stays a cross-check where it exists, and our tables must
  agree with its columns where they overlap.
* **`CsvTools`** (README §2) and, out of scope, `NetworkProfiler.exe` (a legacy format, not
  `.utrace`).
  The `AutomatedPerfTesting` plugin and Gauntlet's `RunInsightsTests.cs` are the engine's own
  perf-CI machinery: we consume their `.utrace`/`.csv` artefacts and never reimplement the runner.

Two honest ceilings, stated rather than engineered around: **symbolication** (pdb/psym resolution
is engine tooling — we group by module and offset, and wrap the engine tool when symbols matter)
and **hardware counters** (GPU counters, cache misses, driver internals live in PIX/Nsight/
Superluminal/VTune/ETW, not in a trace).

## Scope: one machine (inherited from rdc-tools)

A `.utrace` or CSV Profiler capture on this machine (the corpus), the offline Python, and — for
everything the engine already answers — **an engine directory** (`--engine-dir` /
`UEI_ENGINE_DIR`): its CsvTools executables, its trace programs, and, optionally, its source tree.
Work that needs a live tracing connection, a device, the network, or an engine *build* is out of
scope and named in "what is deliberately *not* on this list".

---

## 1. P1 — coverage and capture hygiene (`coverage`) (~1 d)

The practice's first two steps, as one command: **which channels this capture carries**, which
analyses each one enables (REFERENCE §8 has the per-capture table this command would print, and what
it took to get it right — the corpus was documented as GPU-less for a while), what is *missing*
(`task`, `memalloc`, `loadtime`, ...) with the exact re-record line
(`-trace=cpu,gpu,frame,log,bookmark,region,screenshot` is the engine's `Default` preset,
`-trace=Memory` for allocations), the capture's metadata (build, changelist, configuration, platform
— from the trace itself), and its hygiene: how many frames, how long, whether a warm-up window
should be trimmed, and how noisy the distribution is (run-to-run variance when several captures are
given). Every analysis that needs an absent channel then reports **skipped** (the exit-2 convention,
per channel), never zero.

## 2. P2 — GPU passes and the queue (~2 d)

The `gpu` channel (`GpuProfiler`): per-pass and per-queue timings, the per-frame GPU total, top
passes with their evidence, and queue-vs-CPU synchronisation (where the game thread waits on the
GPU). The frame-level half is already there — `bottleneck` decodes the **legacy** channel the
corpus carries, names the passes and reports the GPU's busy time per frame (REFERENCE §11) — so what
this item adds is the *current* channel's shape: per-queue timelines (`GpuProfiler.QueueSpec`,
`EventBeginWork`/`EventEndWork`/`EventWait`, absolute uint64 GPU timestamps) and the breadcrumbs.
`game-pc-2` has no GPU channel at all, so the queue half still wants a capture recorded with
one.

## 3. P2 — memory (~2–3 d)

The `memtag` (LLM tag values), `memalloc` (allocations, sizes, lifetimes) and `callstack`/
`module` channels: peaks and growth per frame and per region, allocation churn, the top sites by
allocation size — grouped by **module + offset**, since symbolication is engine tooling
(Python-vs-C++ section above) — leaks expressed as growth that does not come back, and the
memory-into-hitch correlation (a full LLM tag list is also what tells a budget story: texture
pool, audio, physics). The engine's own implementation to mirror field by field is
`TraceServices\Private\Analyzers\MemoryAnalysis.cpp` / `AllocationsAnalysis.cpp` (REFERENCE §8).
Both registered traces carry `Memory.MemoryScope` (the tag scopes: 42,029 in the editor session,
38,595 in the game one), so the tag half can start on the captures already here; the allocations
half (`memalloc`, `callstack`, `module`) still wants one recorded with `-trace=Memory`.

## 4. P2 — loading and streaming (~2 d)

The `loadtime`, `asset`, `file` and `iostore` channels: load-time trees per request group,
the slowest packages and assets, IO wait vs CPU work, and — the money shot — **hitch frames
correlated with load/streaming scopes** ("the hitch is a synchronous load in `GameThread`").
Mirrors `LoadTimeTraceAnalysis.cpp` and `PlatformFileTraceAnalysis.cpp` (REFERENCE §8).

## 5. P2 — stats and counters as time series (~1–2 d)

The `stats` and `counters` channels carry the same numbers a `stat` HUD shows (and the CSV
Profiler's values, which ride `counters`): per-frame series per stat, trends, worst frames per
stat, and per-stat budgets. This is also the bridge into the CsvTools chain: a capture with CSV
values can be exported as a CSV Profiler `.csv` (`csv from-trace`) and driven through the CsvTools
report chain (README §2). The corpus carries counters and CSV *definitions* but no CSV values, so
a second capture — recorded with a CSV capture running — is what this item and the `from-trace`
acceptance test both want.

## 6. P3 — the long tail

`explain` deep-dive mode on one timer/frame range; raw JSONL export for further agent
processing; agent-facing schema docs; anomaly detection across frames; `region`/`screenshot`/
`annotation` channels (markers a human can line a finding up with); `object`/`animation`/
`slate`/`audio`/`rdg` channels as they become interesting; callstack grouping by module even
without symbols.

---

## What is deliberately *not* on this list

Each left off on scope or on the rdc-tools rule ("don't rebuild what the engine already
answers"), each with its reason so the reasoning survives:

* **A reimplementation of any CsvTools functionality** — stats, filtering, splitting, collating,
  SVG graphs, HTML/regression reports: the engine's executables answer all of it (README §2), so
  we call them and cite them. A feature CsvTools answers is *wrapped*, never rewritten in
  Python. (The one thing we write ourselves is the trace→CSV bridge, which no engine exe does.)
* **A reimplementation of the engine's trace analysers** — `MemoryAnalysis`, `TasksAnalysis`,
  `LoadTimeTraceAnalysis`, `NetTraceAnalyzer`, `CsvProfilerTraceAnalysis` and the rest
  (REFERENCE §8): we read the same channels, mirror their field layouts, and build our own model,
  but where an engine tool can export the same numbers (TraceQuery, the Insights `Export*`
  commands) we **cross-check against it** instead of claiming authority.
* **Anything UnrealInsights already exports** — the `TimingInsights.Export*` family (README §3)
  answers threads/timers/timing-events/statistics/callees/counters exactly. We *consume and
  validate against* those CSVs; we do not re-implement the Insights UI's every table.
* **A perf-CI runner** — scripted scene playback, device deployment, artefact collection and
  retries are Gauntlet / `AutomatedPerfTesting` / `RunInsightsTests.cs` (REFERENCE §9). We consume
  the `.utrace` and `.csv` those produce and provide the *verdict* a build is blocked on; we do
  not stand up the harness, and we do not pretend a shared CI runner's timings are comparable.
* **Symbolication / PDB resolution** — the engine resolves symbols (DbgHelp, breakpad `.psym`,
  RAD `.sym`) via its own providers and symbol search paths; a trace without matching symbols can
  only be grouped by module and offset. We group, and wrap the engine tool when names matter.
* **Hardware counters and driver internals** — GPU counters, cache misses, occupancy, shader
  stalls: PIX, Nsight, Superluminal, VTune, ETW/Perfetto and console devkits answer those, not a
  `.utrace`. The tool says so when a question needs them.
* **Live tracing / the relay protocol** — connecting to a running process or driving the trace
  store is a capture-time concern; this tool reads files, offline, forever.
* **Standing up a device or replaying frames** — `.utrace` analysis never needs a GPU, and this
  repo will not grow a replay driver; there is nothing to replay.
* **A GUI, a timeline, a picture** — the tool's whole point is that an agent does not need them
  (the CsvTools SVGs/HTML we produce are *artefacts* for a human or an agent's later look, not
  an interface we build).
* **Network anything** — no fetches, no telemetry; the only native code is the LZ4 DLL this repo
  and the CsvTools exes run offline.
