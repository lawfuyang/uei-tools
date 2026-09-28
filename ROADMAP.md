# uei-tools — build roadmap / TODO

**Status (phase 0, 2026-09-28):** this file, `README.md`, `REFERENCE.md`, `AGENTS.md`, and one
corpus capture (`editor-pie-1`, 33.9 MB, SHA-256 `50E7...F954`). **Nothing is implemented yet** —
this file is the whole build order.

Landed items are *removed* rather than ticked off, and the remaining sections renumbered with
their cross-references updated in the same change (the
[rdc-tools](https://github.com/lawfuyang/rdc-tools) rule).

**Every item below lands with its tests** (the rule in `AGENTS.md`, "Tests come with the
feature"): the effort figures include the unit-test work, and a phase is not done — and is not
removed from this file — until its hermetic suite covers it, copiously. An item whose tests do
not exist has not landed; an item whose tests are weak is still open.

Legend: **P0** = do next / unblocks everything · **P1** = high value, moderate effort ·
**P2** = useful, opportunistic · **P3** = nice-to-have. Effort figures are for one agent-day.

## The language decision (2026-09-28)

**Python 3.8+, stdlib only, pyright "standard", hermetic unittest, goldens corpus — the exact
rdc-tools stack.** Reasons, in order:

1. The project brief says "in the same flavor as rdc-tools", and the flavor there is not just
   philosophy — it is the working stack: a pure Python analyser over `mmap` walks 631 MB–1.5 GB
   capture streams in seconds. A 33.9 MB `.utrace` is well inside that envelope.
2. This machine has Python 3.11.3 and **no Rust toolchain** (checked 2026-09-28: `cargo` not
   installed). A language we cannot build is not a choice.
3. The one genuinely hot spot — LZ4 packet decode (24,767 encoded packets in the corpus) — has
   the same answer rdc-tools found: an optional/lazy native decoder (`python-lz4`, or the same
   CMake-built-DLL trick), not a rewrite.

The initial research recommends Rust for the core parser, and that recommendation is
**recorded, not discarded**: the trigger to revisit is *measured* — once §1 lands, if a
full-pass query on the real corpus takes long enough to hurt the interactive agent loop (say
>30 s uncached), a native core gets added **beside** the Python CLI (a decoder DLL, the
rdc-tools pattern), not instead of it. A measurement moves this decision; taste does not.

## Scope: one machine (inherited from rdc-tools)

A `.utrace` or CSV Profiler capture on this machine (the corpus), the offline Python, and — for
everything the engine already answers — **an engine directory** (`--engine-dir` /
`UEI_ENGINE_DIR`): its CsvTools executables and, optionally, its source tree. Work that needs a
live tracing connection, a device, the network, or an engine *build* is out of scope and named
in "what is deliberately *not* on this list".

---

## 1. P0 — the parser core (~2–3 d)

The phase that makes everything else possible, landing **with** its gates (AGENTS.md):

* Container: magic/metadata/versions parse (REFERENCE §1), the packet walk (REFERENCE §2) —
  sync points recorded, EncodedMarker packets routed to the LZ4 decoder (optional dep, lazy
  import, graceful "install lz4" refusal when absent, exactly rdc-tools' zstandard rule).
* Per-thread event stream reader: uid decoding (one/two-byte, shift), 24-bit serials, aux-data
  blocks + terminal, `EnterScope`/`LeaveScope`/`_TA`/`_TB` (REFERENCE §3).
* Schema registry from `NewEvent` records — the file's own vocabulary, never hardcoded; respect
  the lazy schema and the unwritten `FNewEventField` byte (REFERENCE §4).
* Decode of the core runtime events the summary layer needs: `$Trace` (`NewTrace`, thread
  info/groups, `ThreadTiming`), `CpuProfiler` (timer specs with file+line, scope/batch events),
  `Misc` frames, `Log`, bookmarks, counters, and the **`CsvProfiler` channel** (REFERENCE §5).
* Global merge by serial (min-heap over threads), serial-gap reporting bounded by Sync packets.
* **Parse cache** beside the capture (keyed by capture SHA-256 + tool version) so repeat queries
  are fast; the cache never changes output.
* Gates built in the same change: `selftest` (hermetic, fixture traces generated in-memory),
  `npx --yes pyright@latest` clean, and the goldens harness over the corpus (transcripts keyed
  by capture key + SHA-256, paths redacted).
* **Validation targets**: `editor-pie-1` walks to EOF with exactly the measured numbers
  (REFERENCE §6); event and timer counts agree with UnrealInsights' own
  `TimingInsights.ExportTimers` CSV **when an engine build exists here** — until then that half
  reports *not compared* (the rdc-tools exit-code convention), never "pass".

## 2. P0 — CsvTools integration and the `--engine-dir` contract (~1–2 d)

The engine already ships the CSV Profiler toolbox as .NET executables under
`<engine-dir>\Engine\Binaries\DotNET\CsvTools` — **we wrap them, we do not reimplement them**
(README §2 lists each exe, its arguments and how we use it). This phase makes that real:

* **The flag, from this point forward.** `--engine-dir <dir>` (env `UEI_ENGINE_DIR`) is the one
  way any command finds engine-provided tooling: the CsvTools exes, the source tree for
  module/source mapping, and `Engine\Build\Build.version` for the report's `meta`. Resolution
  order, validation (the exes exist and run), and the *skipped* convention when it is absent
  (never a silent pass) are all part of this item.
* **A wrapper module.** One place that runs an exe with `subprocess` — argument list, never
  `shell=True`; stdout/stderr/exit code always captured; a timeout; scratch working directory;
  paths with spaces quoted correctly (the corpus path has spaces; so does the engine's, by
  default, so this bug would otherwise arrive immediately). Every wrapped call records the
  exe's version and exact argv in our output.
* **The CLI surface**, 1:1 with the exes plus the trace bridge: `csv info` (`csvinfo -toJson`,
  parsed), `csv split`, `csv convert` (text ⇄ `.csv.bin`), `csv filter`, `csv collate`,
  `csv svg` (`CSVToSVG`), `csv report` (`PerfreportTool`, summary tables as csv/json), and
  `csv regressions` (`RegressionsReport`).
* **`csv from-trace`** — synthesize a CSV Profiler-format `.csv` from a trace's `CsvProfiler`
  channel, so a capture (not just a CSV file) can drive the whole pipeline. The corpus has the
  definitions but no per-frame values (REFERENCE §5), so **obtain a capture with a CSV capture
  running**, or a raw `.csv`/`.csv.bin` pair, and make it corpus key two.
* **Outputs fold into ours**: the report bundle gains a CSV section — `csvinfo`'s JSON summary,
  `PerfreportTool`'s summary tables, the generated SVGs — each stamped with the exe version and
  argv it came from. Raw pass-through is available too (the wrapped output *is* the answer).
* **Gates**: hermetic tests stub the exes (a fake exe fixture), so CI-shaped runs need no engine
  dir; the real-exe half reports *not compared* (exit 2 convention) when there is none.

## 3. P0 — the summary layer (~1–2 d)

Session overview (duration, frame count, frame-time stats: avg/min/max/1%/0.1%), thread
utilisation, top-N timers by inclusive/exclusive time, per-thread and per-category breakdown,
frame-time histogram + hitch detection. The `TimingInsights.ExportTimerStatistics` columns are
the minimum bar — our tables must agree with them where they overlap.

## 4. P1 — bottleneck classification (~1 d)

CPU-bound / GPU-bound / mixed, per-frame and aggregate, with confidence and the ranked "why is
this frame slow" evidence lines (game thread vs render thread vs RHI vs GPU queue, from the
trace's own frame and timer data).

## 5. P1 — critical path / task graph (~2–3 d)

TaskGraph / ParallelFor / async timing events → dependency graph, critical path (longest chain),
hot paths on it, exported as DOT/Mermaid + JSON. This is the flagship analysis and the one with
the least prior art to validate against — its claims carry confidence scores. Note: the corpus
has no TaskGraph events (REFERENCE §6), so a capture with them is needed before this item can
be tested against anything real.

## 6. P1 — parallelism findings (~1 d)

Serial regions on the game thread with no dependencies, ParallelFor candidates, lock-contention
and oversubscription signals; each with estimated speedup (heuristic, labelled as such).

## 7. P2 — source mapping (~1 d)

Timer → file:line is already in the trace (REFERENCE §3); map into engine-vs-game by module
using the engine tree (from `--engine-dir`) and the project source, and cross-reference known UE
anti-patterns (heavy Tick work, sync loads during hitches...). The engine tree is optional:
without it, report "module unknown" rather than guessing.

## 8. P2 — recommendations engine + rules (~2 d)

Rule-based findings (YAML/JSON, droppable by the user) producing the ranked
impact/difficulty/confidence list; the anti-pattern library starts small and grows from real
corpus sessions — and can cite CsvTools outputs as evidence where they answer.

## 9. P2 — compare (~1 d)

A/B of two cached analyses (before/after a change, or two configurations), section-scoped diffs
with the same evidence rules. CSV captures compare through `RegressionsReport` /
`PerfreportTool` bulk mode where that is the engine's answer. The self-compare of one trace
against itself must produce an empty diff (the rdc-tools A/B honesty check).

## 10. P3 — the long tail

`explain` deep-dive mode on one timer/frame range; raw JSONL export for further agent
processing; agent-facing schema docs; counter trends as text; anomaly detection.

---

## What is deliberately *not* on this list

Each left off on scope or on the rdc-tools rule ("don't rebuild what the engine already
answers"), each with its reason so the reasoning survives:

* **A reimplementation of any CsvTools functionality** — stats, filtering, splitting, collating,
  SVG graphs, HTML/regression reports: the engine's executables answer all of it (README §2), so
  we call them and cite them. A feature CsvTools answers is *wrapped*, never rewritten in
  Python. (The one thing we write ourselves is the trace→CSV bridge, which no engine exe
  does.)
* **Anything UnrealInsights already exports** — the `TimingInsights.Export*` family (README §3)
  answers threads/timers/timing-events/statistics exactly. We *consume and validate against*
  those CSVs; we do not re-implement the Insights UI's every table.
* **Live tracing / the relay protocol** — connecting to a running process or driving the trace
  store is a capture-time concern; this tool reads files, offline, forever.
* **PDB / symbol-server resolution** — the trace already carries timer names with file+line, and
  callstacks only when the capture has them; anything beyond that belongs to the engine's
  tooling.
* **Standing up a device or replaying frames** — `.utrace` analysis never needs a GPU, and this
  repo will not grow a replay driver; there is nothing to replay.
* **A GUI, a timeline, a picture** — the tool's whole point is that an agent does not need them
  (the CsvTools SVGs/HTML we produce are *artefacts* for a human or an agent's later look, not
  an interface we build).
* **Network anything** — no fetches, no telemetry; the only optional dependency (lz4) is local,
  and the CsvTools exes run offline.
