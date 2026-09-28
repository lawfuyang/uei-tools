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

The commands in §4 are the contract the tools will be built to; the build order, priorities and
effort figures live in `ROADMAP.md`.

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
| Python 3.8+ | 3.11.3 tested; standard library only, plus the optional `lz4` decoder (ROADMAP §1) |
| A `.utrace` capture | the corpus, §1.1 — analysis is offline; nothing connects anywhere |
| **Unreal Engine directory** | passed as `--engine-dir <dir>` (or `UEI_ENGINE_DIR`). It is used for three things: the **CsvTools executables** under `<engine-dir>\Engine\Binaries\DotNET\CsvTools` (§2), the engine **source tree** under `<engine-dir>\Engine\Source` (module/source mapping; optional), and the engine **version** from `<engine-dir>\Engine\Build\Build.version`, which stamps every report's `meta` block. Without it, the features that need it report **skipped** — never silently pass. |
| A CSV Profiler capture | optional: a `.csv` (or `.csv.bin`) written by the CSV Profiler, or a `.utrace` that carries the channel (see §2) — either can drive the CsvTools pipeline |
| `lz4` (optional) | the corpus capture holds 24,767 LZ4-encoded packets; without the module, those packets are refused with the fix in the message rather than a traceback (ROADMAP §1) |

The capture format — container, packets, event streams, schema, the engine files that define
each, and the measured numbers of the corpus — is documented in `REFERENCE.md`, not here.

## 1.1 The corpus

| key | size | SHA-256 |
|---|---|---|
| `editor-pie-1` | 35,495,101 bytes (33.9 MB) | `50E7FF09857B36292336D42AA93DB2219BBBF68DF5F94E705A121B5EEF69F954` |

An editor Play-In-Editor session on PC, protocol 7 (the current format), 51 event types in its
schema. The corpus rules are inherited from rdc-tools wholesale:

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

## 2. CsvTools — the engine's CSV toolbox, reused and never reimplemented

The engine ships its whole CSV Profiler toolchain as self-contained .NET executables under
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
is passed through as-is, and a trace whose `CsvProfiler` channel holds data is synthesized into a
CSV Profiler-format `.csv` first — the channel carries the stat definitions
(`RegisterCategory`, `DefineDeclaredStat`, `DefineInlineStat`) and, when a CSV capture was
running, the per-frame values too (`BeginStat`/`EndStat`/`CustomStat`/`Event`/`Metadata`, see
`REFERENCE.md`). The corpus capture has the definitions but no per-frame CSV events — a capture
with a CSV capture running is wanted (ROADMAP §2).

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

## 4. The intended surface (`ROADMAP.md` is the build order)

One entry point, rdc-tools style: `python src\py\ueia.py <command> ...` (plain text by default,
`--format json|markdown` where tabular; stdout is the contract). Commands that need
engine-provided tooling take `--engine-dir` (or `UEI_ENGINE_DIR`).

| Command | What it will answer |
|---|---|
| `analyze --trace X [--engine-dir D] --sections summary,bottlenecks,hitches,... --format json` | the composed report: frame stats, thread utilisation, hot timers (incl/excl), bottleneck classification with confidence, hitch list |
| `compare --trace-a A --trace-b B` | before/after A/B on cached analyses |
| `explain --trace X --timer "FName::Tick" --frame-range 1200-1250` | the deep dive on one timer/frame range |
| `csv info\|split\|convert\|filter\|collate\|svg\|report\|regressions <csv> --engine-dir D` | the §2 executables, wrapped 1:1, their outputs folded into ours (and available raw) |
| `csv from-trace X -o capture.csv --engine-dir D` | a CSV Profiler-format CSV synthesized from a trace's `CsvProfiler` channel, ready for the exes above |
| `export --trace X --what events,jsonl` | raw/intermediate data for further agent processing |

Output philosophy: every report carries a `meta` block (tool version, input hashes, engine and
protocol version detected, engine-dir path as configured, analysis duration, limitations, and
the list of wrapped executables with their versions and argv); every finding carries `id`,
`severity`, `confidence`, `category`, `evidence` (timer names, frame numbers, percentages) and
`suggested_actions`. JSON is primary and schema-versioned; Markdown is the summary.

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
  schema (incl. the `CsvProfiler` channel), the corpus measurements, and the engine files that
  define each.
* **`ROADMAP.md`** — the build order with P-labels and effort figures, the language decision,
  and what is deliberately *not* on the list.
* **`AGENTS.md`** — the rules for an AI agent changing this repo: the standing contracts (incl.
  the CsvTools/engine-dir contract), the engine-source map pointer, the coding guidelines, the
  corpus and evidence rules.
