# uei-tools — offline Unreal Insights `.utrace` analysers

Headless, agent-first analysis of Unreal Engine trace captures. The sibling of
[rdc-tools](https://github.com/lawfuyang/rdc-tools), built in the same flavor: a pure CLI that
ingests what the engine already writes (`.utrace` files, CSV Profiler captures), needs no UI, no
engine build, no GPU and no network, and produces deterministic, structured output an AI agent
can parse and act on without staring at a timeline.

It turns Unreal Insights captures into **actionable, structured intelligence** — frame stats,
thread utilisation, hot timers, CPU/GPU bottleneck classification, hitch detection, parallelism
opportunities, source-mapped recommendations — as versioned JSON (the machine interface) and
Markdown (the human summary), with every finding carrying its evidence. Where the engine already
ships a tool for a question (the **CsvTools** executables, §2), this repo *wraps* it instead of
reimplementing it, and folds its output into ours.

The parser core is implemented and its commands are §4; what is still planned — the CsvTools
bridge (§2), the summary layer and the analyses on top of it — is in `ROADMAP.md`.

> ## Vibe coded — use at your own risk
>
> This repo follows the rdc-tools house rules: every feature is **vibe coded**, written ad-hoc,
> for me, by me and my agents. It is not a product, not a library, not supported, and not
> reviewed. Heuristics and hard-coded assumptions will be load-bearing throughout. **Use at your
> own risk** — validate anything you plan to rely on against the capture you are actually
> analysing.

## 1. Requirements and setup

| Requirement | Notes |
|---|---|
| Python 3.8+ | 3.11.3 tested; standard library only |
| CMake + a C compiler | once per checkout: `python src\py\ueia.py lz4 --build` compiles `bin/ueia_lz4.dll` — the LZ4 decoder for the encoded packets (24,767 of them in the corpus) — from the source vendored under `src/cpp/third_party/lz4`, and does nothing when it is already current. Underneath it is `cmake -S . -B build` then `cmake --build build --config Release` |
| A `.utrace` capture | the corpus, §1.1 — analysis is offline; nothing connects anywhere |
| **Unreal Engine directory** | passed as `--engine-dir <dir>` (or `UEI_ENGINE_DIR`). It is used for three things: the **CsvTools executables** under `<engine-dir>\Engine\Binaries\DotNET\CsvTools` (§2), the engine **source tree** under `<engine-dir>\Engine\Source` (module/source mapping; optional), and the engine **version** from `<engine-dir>\Engine\Build\Build.version`, which stamps every report's `meta` block. Name one and it is used as given; name nothing (or name something that is not an engine tree) and the machine is **searched** — the launcher's own install list, the source builds the engine registers, and the usual install directories — with the tree that gives the most picked and printed (REFERENCE §18). Without one anywhere, the features that need it report **skipped** — never silently pass. |
| A CSV Profiler capture | optional: a `.csv` (or `.csv.bin`) written by the CSV Profiler, or a `.utrace` that carries the channel (see §2) — either can drive the CsvTools pipeline |
| The LZ4 decoder | `bin/ueia_lz4.dll` (the build above), else whatever the system has (`$UEI_LZ4_DLL` names one explicitly: `lz4.dll`, `liblz4.so.1`, `liblz4.so`, `liblz4.dylib` in that order). There is **no decoder of our own** on purpose — one decoder means one answer — so a machine without one is a refusal that names the build command, never a silent fallback. `lz4` (below) reports which library answered, whether it matches the recipe that built it, and decodes a block whose answer is known |

The capture format — container, packets, event streams, schema, the engine files that define
each, and the measured numbers of the corpus — is documented in `REFERENCE.md`, not here.

## 1.1 The corpus

| key | size | SHA-256 |
|---|---|---|
| `editor-pie-1` | 35,495,101 bytes (33.9 MB) | `50E7FF09857B36292336D42AA93DB2219BBBF68DF5F94E705A121B5EEF69F954` |
| `game-pc-2` | 13,018,825 bytes (12.4 MB) | `C431814A9821D4B188266C4A887A03E662C53130A1AD542A341717E6D3168B1B` |
| `viewer-pc-3` | 465,564 bytes (0.44 MB) | `51615FD1142D63800F622DD4BAA8723F57A87AF098F2C45FE961776F702F1CDE` |

An editor Play-In-Editor session on PC, protocol 7 (the current format), 51 event types in its
schema; a game session (`game-pc-2`, 35 types, no GPU channel) and a small viewer session
(`viewer-pc-3`, 25 types, no frame pairs at all) — three different shapes of capture, and
`goldens --check` runs the pinned commands over all of them (REFERENCE §8 has what each carries).
The corpus rules are inherited from rdc-tools wholesale:

* A capture is a **key + SHA-256**, never a committed path. Where this machine keeps the file is
  gitignored local state (`captures.local.json`, once the harness exists). No doc, comment or
  test writes a capture's path or file name — refer to it by its key.
* A trace's **own strings** (log text, object names, user names) are the capture's words: a
  transcript of them is published only with the author's say-so.
* More captures come from running the editor or game with
  `-trace=cpu,gpu,frame,frameall,log,bookmark,counters` (or the Trace UI / console commands),
  saved from Unreal Insights, or written directly with `-TraceFile=...`. The engine's relay
  store keeps its captures under `%LOCALAPPDATA%`; the corpus proper starts with hand-placed
  files. A **CSV Profiler capture** (`-csvcapture`, `CsvProfiler.Start`, ...) is wanted too —
  see §2 for why.

## 2. CsvTools — the engine's CSV toolbox, reused and never reimplemented (the `csv` family)

The `csv` family implements this contract: one subcommand per executable plus `from-trace`, each a
thin invocation with the exe's own stdout as the answer. **How we call them, everywhere:** our flags
map 1:1 onto the exe's own spelling (`csv info --json F` → `-toJson F`), every path is passed as an
argument (never through a shell), the exe runs in a scratch directory so *its* temporary files never
land in this repo, the tool's stdout is ours only to pass through, and the exe's identity
(SHA-256 + size) and the exact argv go to **stderr** as the call's evidence. Exit codes: **0** the
tool ran, **1** it failed (its own output is printed), **2** *skipped* — no engine directory, or a
tree without that executable, with the hint. The engine ships its whole CSV Profiler toolchain as
self-contained .NET executables under
`<engine-dir>\Engine\Binaries\DotNET\CsvTools`. They are **the** answer for CSV statistics,
filtering, splitting, collating, graphs and performance reports, and this repo's rule is:
**wrap the executable, parse its output, cite it — never write our own version of what it
already answers.** Every CSV feature below is therefore a thin, tested invocation with the
exe's stdout/stderr and output files folded into our report, recording the exe version and the
exact argv in our output (the SVGs these tools produce embed their own commandline for the same
reason; ours must too).

| Executable | What it answers | How we call it |
|---|---|---|
| `csvinfo.exe` | a CSV's shape and numbers: sample count, metadata, events, stat averages/min/max/totals, per-stat values — **with `-toJson <file>` for machine consumption** | `csvinfo.exe <capture.csv> [-showAverages] [-showMin] [-showMax] [-showTotals] [-showAllStats] [-showEvents] [-statFilters "LLM/*,FrameTime"] [-toJson <out.json>] [-quiet]` — the JSON is parsed straight into our report |
| `CSVSplit.exe` | one CSV per distinct value of a stat — e.g. per map/level/test | `CSVSplit.exe -csv <in.csv> -splitStat <statName> [-o <out.csv>] [-delay N] [-virtualEvents]` |
| `CsvConvert.exe` | text ⇄ `.csv.bin` (the CSV Profiler's compressed form), metadata edits, integrity check | `CsvConvert.exe [-in <f>] -outFormat csv\|bin\|csvNoMetadata [-binCompress 0\|1\|2] [-o <f>] [-verify] [-force\|-inPlace] [-setMetadata key=value;...]` — also the tool that keeps a `.csv.bin` corpus usable |
| `CSVFilter.exe` | one CSV with only the wanted columns | `CSVFilter.exe -csv <in.csv> -stats <names\|wildcards> [-defaults] -o <out.csv>` |
| `CSVCollate.exe` | many CSVs → one table (optionally per-frame averaged, outlier-filtered, metadata-filtered) | `CSVCollate.exe -csvs "a.csv;b.csv"\|-csvDir <dir> [-searchPattern *.csv] [-recurse] [-avg] [-filterOutlierStat S -filterOutlierThreshold N] [-metadataFilter k=v] [-startEvent E] -o <out.csv>` |
| `CSVToSVG.exe` | the SVG graph renderer (frames on x, stats in the legend; large style-arg surface) | `CSVToSVG.exe (-csvs <f> \| -csv <f> \| -csvDir <dir>) -stats <list> -o <out.svg> [style args...]`; `-updatesvg` regenerates from the embedded commandline |
| `PerfreportTool.exe` | the full performance report bundle: HTML pages + graphs + summary tables (csv/json) from one capture or a whole folder, driven by report/graph XML | `PerfreportTool.exe (-csv <f> \| -csvDir <dir> \| -csvList ...) -o <outdir> [-reportType flythrough\|playthrough\|playthroughmemory] [-graphXML ReportGraphs.xml] [-reportXML ReportTypes.xml] [-summaryTableOutputFormats html,csv,json]` — it batches `CSVToSVG` internally |
| `RegressionsReport.exe` | a threshold-based regression report between a summary CSV and thresholds, as HTML + a JSON dump | `RegressionsReport.exe -csvFile <summary.csv> -o <outdir> -thresholds <thresholds.json> [-base <base.html>] [-dumpContents <out.json>] [-testName <name>]` |

Two facts to keep in mind: **`CsvStats` is the library behind most of these** (shipped as
`CsvStats.dll`; the binaries folder has no `CsvStats.exe` here — `csvinfo` is its CLI face), and
the executables are self-contained .NET — no install, no SDK, just the engine directory.

**Where the CSV comes from.** The CSV Profiler writes `.csv` / `.csv.bin` captures directly, and
*also* emits its channel into a `.utrace` when tracing is on. Our tool accepts both: a CSV file
is passed through as-is, and a trace whose CSV Profiler events hold data is synthesized into a
CSV Profiler-format `.csv` first — the channel carries the stat definitions
(`RegisterCategory`, `DefineDeclaredStat`, `DefineInlineStat`) and, when a CSV capture was
running, the per-frame values too (`BeginStat`/`EndStat`/`CustomStat`/`Event`/`Metadata`, see
`REFERENCE.md`). The corpus capture has the definitions but no per-frame CSV events — a capture
with a CSV capture running is wanted (the `from-trace` bridge above has the writer; the corpus has
the reader's format but no capture that exercises it end to end).

## 3. What the engine already answers (and this tool does not rebuild)

The rdc-tools rule applies: **do not re-derive what the engine's own tooling hands over —
validate against it instead.**

* **Headless export** (Epic's own functional-test recipe): `UnrealInsights.exe
  -OpenTraceFile="<trace>" -AutoQuit -NoUI -ExecOnAnalysisCompleteCmd="TimingInsights.<ExportCmd>
  <out.csv>" -log`, with `ExportThreads`, `ExportTimers`, `ExportTimingEvents`,
  `ExportTimerStatistics`, `ExportTimerCallees`. Those CSVs are the ground truth for our tables
  once an engine build exists on this machine; until then that half of the check reports *not
  compared* (the rdc-tools exit-code convention) rather than "pass".
* The engine's own stack, by module: `Engine\Source\Runtime\TraceLog` (the writer),
  `Engine\Source\Developer\TraceAnalysis` (the reader our parser mirrors),
  `Engine\Source\Developer\TraceServices` (the analyzer/provider framework, incl. the
  `CsvProfiler` provider), `Engine\Source\Developer\TraceInsights` + `TraceInsightsFrontend`
  (the Insights UI and its `TimingExporter`), `Engine\Source\Programs\UnrealTraceServer` (the
  relay/store), `Programs\TraceAnalyzer` (a minimal `convert-to-text`),
  `Programs\CSVTools` (the §2 executables' source), `TraceTrimmer`, `TraceQuery`.

What none of those give, and what this repo exists for: a **composable, agent-facing CLI over
the file itself** — structured findings, ranked recommendations, A/B comparisons,
machine-readable schemas, one engine-dir flag that reaches every engine-provided tool — with no
session, no device and no human in front of a timeline.

## 4. The commands

One entry point, rdc-tools style: `python src\py\ueia.py <command> <capture.utrace> [args]`.
Plain text by default; the row commands take `--format table|csv|markdown` (in the non-table
forms stdout is the table alone and the prose moves to stderr, and every cap applies in all
three). Commands that need engine-provided tooling take `--engine-dir` (or `UEI_ENGINE_DIR`) — and
when neither names a usable tree, one is searched for on the machine and named in the output
(REFERENCE §18); `UEI_NO_ENGINE_SCAN=1` switches that search off, which is what the test suite does.

| Command | What it answers |
|---|---|
| `info <capture>` | the file's own account of itself: magic, versions, metadata block, packet counts, bytes decoded, and whether the walk ends exactly at EOF |
| `packets <capture> [--limit N] [--tid N]` | the packet table: index, offset, size, decoded size, thread, form (raw/lz4/sync) |
| `schema <capture> [--filter TEXT] [--limit N]` | the capture's own vocabulary: uid, flags, `Logger.Event`, each field with its type, and how many events of that type fired on thread streams |
| `threads <capture>` | every thread: name and group as the capture itself names them, packets, bytes, events, batches, batch records, first/last cycle (on a cache miss it builds the model, so `--jobs` applies) |
| `timers <capture> [--filter TEXT] [--limit N]` | the CPU profiler's timer specs: id, name, file:line |
| `frames <capture> [--limit N]` | `Misc.BeginFrame`/`EndFrame` pairs per thread and frame type, in cycles and seconds since the trace started |
| `tasks <capture> [--limit N] [--graph dot\|mermaid\|json]` | the task graph and the longest dependency chain through it: how many tasks and edges the capture carries, the chain's total executing time with every step's duration, thread and frame, the per-thread waiting spans, and (`--graph`) the graph itself for a viewer or a PR comment. Exit 2 when the capture carries no `TaskTrace` events, with the re-record line — never an empty path |
| `bottleneck <capture> [--budget FPS \| --budget-ms MS] [--tid N] [--limit N]` | what bounds a frame: the **game thread**, the **render thread**, the **GPU**, or none of them. Every verdict is a measurement against the budget (the thread's own non-wait scope coverage, the sibling thread's coverage inside the same frame, the GPU's busy time when the capture carries the legacy GPU channel), with the evidence that decided it and the reasons it cannot decide. A capture with no GPU channel gets its CPU finding *plus* "the GPU side is unknown here", never a bare CPU-bound claim. Exit 0 classified / 2 nothing to judge (no frame pairs, no cycle frequency, no scopes) |
| `parallelism <capture> [--budget FPS \| --budget-ms MS] [--tid N] [--limit N]` | was the work spread? Per thread: busy / waiting / lock-named cycles inside the frame series' own frames, the frame thread's **solo** work (measured overlap, not a dependency claim), the most threads working at once, lock overlap and the timers that own a quarter of the frames the model keeps — every ceiling Amdahl on a measure and labelled a heuristic, every absence (no core count, a lock nobody named) said out loud. Exit 0 reported / 2 nothing to measure (no frames, no cycle frequency, no scopes) |
| `self <capture> [--tid N] [--frame INDEX] [--limit N] [--depth N]` | a frame's **call tree**: inclusive and **self** time per timer, the callee expansion at every level, top-N by self, and any frame — not only the ones the model keeps. A scope is present in every frame it overlaps, clipped, so a tree of intervals is built from the pairs' own nesting; siblings of one timer merge with a call count. The one command that reads a thread's streams a **second** time (self time must not be a bounded sample), so it prints what the pass cost. Exit 0 a tree was built / 2 no timer specs, no frame pair for that thread, or no such `--frame` |
| `compare <before> <after> [--threshold PCT] [--baseline FILE] [--save FILE]` | **the CI gate**: two captures reduced to the same metrics and differenced **section by section** (frames, work, occupancy, sources, tasks). A candidate worse than `--threshold` (10% by default) on **p99, p95, mean or the hitch count** exits 1 so a job can branch on it; improvements are **logged, never gated**; a capture compared with itself is an **empty diff**. `--save FILE` writes a rolling baseline, `--baseline FILE` compares against one, `--format markdown` is the PR-comment shape and `--format json` the machine form (`ueia.compare/1`) |
| `advice <capture> [--budget FPS \| --budget-ms MS] [--skip IDS] [--limit N]` | **what to do next**: rule-based findings over every report above — budget/tail/hitches, the bottleneck verdict, one timer owning a quarter of the frames, workers idle while the frame thread works, synchronous loads, a frame full of async loading, missing channels — ranked by impact → confidence → effort, each with its evidence, its file:line and **the next command to run**. `--format json` is the schema-versioned machine form (`ueia.advice/1`, with a `meta` block); `--skip id,id` drops rules |
| `sources <capture> [--engine-dir DIR] [--filter TEXT] [--limit N]` | where the timers live: the trace's own file:line per spec, classified into engine vs project (source, engine plugin, project plugin) and module **by the shape of the path** — the recorded paths belong to the machine that recorded it — weighted by the work the model keeps for the longest frames, and cross-referenced with the engine's anti-pattern names (`tick`, `sync-load`, `object-churn`, `gc`, `serialize`, `wait`). `--engine-dir` is optional and only *checks* how many of those paths exist in the tree at hand. Exit 0 reported / 2 nothing to map (no specs, or none with a file:line) |
| `summary <capture> [--budget FPS \| --budget-ms MS] [--tid N] [--limit N]` | is this capture fast? The frame-time **distribution** (mean/min/p50/p95/p99/max + a histogram) against an explicit budget (60 FPS by default) with a verdict, a **hitch count**, the one-line bottleneck verdict above, and the frames that break the budget — worst first, each naming the timers that ran in it. Exit 0 reported / 2 nothing to time (no frame pairs, or no cycle frequency) — being over budget is the report, not a failure |
| `verify <capture> [--jobs N]` | walks everything and reports what does not add up: packet and stream anomalies, schema redefinitions, serial gaps, unpaired frames, unknown bookmark points. **Exit 1** when anything error-level was found |
| `parse <capture> [--jobs N]` | builds (and caches) the session model; prints what it holds |
| `cache <capture> [--clear]` | the parse cache beside a capture: status, or remove it |
| `csv <subcommand> ...` | the engine's CSV toolbox, wrapped (§2): `info` (a CSV's shape and numbers, `--json FILE` for the machine form), `split`, `convert`, `filter`, `collate`, `svg`, `report`, `regressions`, and `from-trace` — synthesize the CSV Profiler `.csv` from a capture's own CSV events, the bridge no engine exe offers. `--engine-dir DIR` or `$UEI_ENGINE_DIR`; exit **2** when there is no engine tree, never a silent pass |
| `lz4 [--build] [--force]` | the LZ4 decoder: which library answered, its version, whether it matches the recipe that built it, and a decode of a known block as proof it works. `--build` compiles `bin/ueia_lz4.dll` when it is missing or stale (`--force` rebuilds either way). Exit 0 usable and current / 1 stale or broken / 2 nothing to decode with |
| `selftest [-v] [-k PATTERN]` | the hermetic unit-test suite — no capture, no engine directory, no network; ~45 s over 517 tests. Exit 0 pass / 1 fail / 2 bad option |
| `goldens [--check\|--write] [--capture KEY] [--only CMD[,CMD]] [-v]` | the corpus: re-runs the pinned commands over the captures this machine has and compares. `--capture` limits it to one capture, `--only` to named commands (iterating on one transcript, and the suite's own harness tests). Exit 0 matched / 1 a problem / 2 nothing to compare |

The parse cache sits beside the capture, keyed by its SHA-256 and the tool version, and never
changes an answer — only the seconds a command takes: measured on the corpus (2026-09-29), a cold
full decode is 8.86 s (the LZ4 half, in the C library, is 0.63 s of it, the per-thread walk runs
over worker processes, attributing each frame's work and occupancy costs ~1 s and measuring every
thread's occupancy inside every frame — the coverage timelines §4's `parallelism` reads — 2.4 s more)
and a cached command **0.32-0.33 s** — of which 0.17 s is interpreter startup, and the rest is
hashing the capture (0.03 s) and parsing the cache (0.02 s), because the packet table is only walked
by the commands that print it. `--jobs N` sets how many processes that walk may use — any command
that has to build the model — and `--jobs 0` (the default) chooses for the machine. It cannot change
a byte of the output; the suite pins serial ≡ parallel.

Planned (see `ROADMAP.md`): a `coverage` command that says whether a capture can answer a question at
all, GPU / memory / loading / stats-channel analyses, and the CI gate extended to more captures with
rolling baselines.

Output philosophy for those: every report carries a `meta` block (tool version, input hashes,
engine and protocol version detected, engine-dir path as configured, analysis duration,
limitations, and the list of wrapped executables with their versions and argv); every finding
carries `id`, `severity`, `confidence`, `category`, `evidence` (timer names, frame numbers,
percentages) and `suggested_actions`. JSON is the machine interface and schema-versioned;
Markdown is the summary.

## 5. The playbook (agent rules, inherited from rdc-tools)

* **Tests come with the feature** — every implemented feature (command, decode path, analysis
  rule, flag, output format) lands with a copious hermetic unit-test suite in the *same*
  change; a command in §4 exists only when its tests do. Checks that need this machine's
  captures or a real engine directory are the goldens half, and report "not compared" without
  them — never "pass".
* **Wrap, don't reimplement** — where an engine executable answers a question, it is called and
  cited (exe, version, argv); a missing engine directory makes those sections report *skipped*,
  never pass silently.
* **Offline first** — every answer the file can give costs sub-seconds and is deterministic; no
  command ever stands up a device or a session, and no command touches the network.
* **Extract to files, not to a terminal** — reports and exports survive the process that made
  them and can be re-read, grepped, diffed.
* **Evidence or silence** — every claim cites the frame/timer/thread/event id it came from and
  the command that reproduces it; distinguish *certain* (the file recorded it) from *heuristic*
  (a name matched a pattern) from *unknown* (channel absent, decode missing). State what could
  not be determined, and how it could.
* **Determinism** — fixed input + fixed tool version ⇒ byte-identical text output (sorted
  tables, no timestamps, no absolute paths). `--format` changes the shape, never the answer.
* **The cache stays invisible** — caching may change speed, never output.

## Where the rest of the manual lives

* **`REFERENCE.md`** — the capture format, decoded: the container, packets, event streams, the
  schema (incl. the CSV Profiler's events), the corpus measurements, and the engine files that
  define each.
* **`ROADMAP.md`** — the build order with P-labels and effort figures, the language decision,
  and what is deliberately *not* on the list.
* **`AGENTS.md`** — the rules for an AI agent changing this repo: the standing contracts (incl.
  the CsvTools/engine-dir contract), the engine-source map pointer, the coding guidelines, the
  corpus and evidence rules.
