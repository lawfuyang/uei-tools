# AGENTS.md

Personal, vibe-coded tools for offline Unreal Insights `.utrace` analysis. See `README.md` for
what they do; nothing here is a supported product. This repo is built in the flavor of
[rdc-tools](https://github.com/lawfuyang/rdc-tools) — where a rule below says "inherited", the
precedent and the scar tissue behind it are in that repo's `AGENTS.md`.

## The gates — run them after every change

Any change to `src/py/` or `tests/` is not finished until all three pass:

```powershell
python src\py\ueia.py lz4 --build       # step 0: bin/ueia_lz4.dll, built only when stale
python src\py\ueia.py selftest          # 530 tests, ~30 s, exit 0 pass / 1 fail / 2 bad option
npx --yes --offline pyright@1.1.414     # ~13 s; must print: 0 errors, 0 warnings
python src\py\ueia.py goldens --check   # the corpus: exit 0 matched / 1 a problem / 2 nothing to compare
```

**While iterating, `selftest --quick` is the inner loop**: it leaves out the 42 tests whose classes
are marked `corpus` (they read the registered captures, and they are ~25 s of the ~30), says how many
it left out, and runs the other 488 in about six seconds. The gate is still the full suite.

**Two things these commands must not do, both measured 2026-09-29** (the gates took *minutes*, which
is a bug in the gates, not a fact of life):

* **`pyright@latest` costs ~40 s of registry resolution on every run** (51 s measured, against 13 s
  for the same check with a pinned, cached version and `--offline`; pyright's own work is ~10 s). Pin
  the version and keep `--offline`; if the pinned version is not cached yet, run it once without
  `--offline` to fetch it.
* **A test class that reads a registered capture must set `corpus = True`** (`testcase.UeiaTestCase`
  documents it). With the cache off, every corpus test re-parsed its capture: `test_compare` alone
  parsed the editor capture four times (72 s) and the whole suite was **~4-5 minutes**. With the
  cache allowed for the corpus classes it is **~45 s**, and it cannot change an answer -- the cache
  is keyed by the capture's SHA-256 and the tool version. Hermetic tests keep it off, because a
  test must never pass on an answer a previous run left on disk.

Step 0 is the decoder's build, and it is part of the pipeline rather than advice: `lz4 --build`
hashes the recipe (both LZ4 sources and `CMakeLists.txt`, so a flag change counts) against the
stamp the build wrote beside the DLL, runs `cmake` only when the DLL is missing or stale, and
self-tests the result by decoding a block whose answer is known. A run with nothing to do costs
three file hashes and no compiler. Never commit `bin/` or `build/`, and never ship a second
decoder to avoid the build.

The build is a prerequisite of the *corpus* half, not of the hermetic one: without
`bin/ueia_lz4.dll` the tool refuses the 24,767 encoded packets rather than decoding them itself,
and the suite's real-library class **skips** (and says so in its count) while its logic half runs
against a stand-in C function. Report a skip; do not paper over it.

`selftest -v` prints per-test output and `selftest -k <text>` filters by test id. A change that
can move what a **command prints** is not finished until the corpus agrees: `goldens --write`
refreshes this machine's transcripts (gitignored — a transcript is the capture's own words) and
that diff is the review, and `goldens --check` exits **2** when no capture is present here,
which means "nothing compared", never "pass". Report the real numbers (test count, pyright's
error and warning counts), not "passes".

## Layout — so a new module has an obvious home

`src/py/`, one module per layer, each importing only *down* the layering, and **unprefixed**:
the entry point is `ueia.py`, so a module's name is its job, not its owner.

`shapes` (shapes, constants, errors) → `lz4`, `timing` and `gpu` (leaves; `lz4` is the ctypes
loader rather than a decoder of our own, `gpu` decodes the legacy `GpuProfiler` channel's
per-frame batches) → `container` (header + packet walk) → `streams` (per-thread streams)
→ `events` (framing: records, events, aux, scopes) → `schema` (the vocabulary) → `decode`
(values + the batch format) → `coverage` (cycle-interval arithmetic, and the per-frame occupancy
measurement every thread's timeline is folded into) → `model` (the session model, incl. the
per-frame work attribution, the occupancy and the coverage timelines) → `summary` (the budget,
percentiles and histograms a frame-time report is defined by) → `bottleneck` (the classification
on top of them: what bounds a frame) → `parallel` (the same measurements, asked who worked: solo
work, simultaneity, contention) → `calltree` (the nesting tree and **self** time, built from the
pairs in a second pass over one thread) → `sources` (the spec table's file:line, mapped into engine vs
project and cross-referenced with the engine's anti-pattern names) → `advice` (the rules over all of
the above: ranked findings with evidence and the next command) → `compare` (two models as named metrics, and the directional CI gate over them)
→ `tasks` (the task channel's
graph, its critical path, and the DOT/Mermaid exports) → `cache` (the parse cache) → `goldens`
(the corpus harness) → `commands` (the commands and their rendering) → `ueia.py` (the CLI, which
re-exports them all for scripts and tests).

Two names are deliberately *not* the obvious ones, and the reasons are measured:
`types.py` is impossible — the interpreter preloads the stdlib `types`, so `import types` would
never reach our file and our own `from types import ...` would fail — and `profile.py` would
shadow a stdlib module in a folder every test puts on `sys.path`. Hence `shapes.py` and
`timing.py`. A module may not be named after a stdlib module; check before renaming one.

`src/cpp/third_party/lz4/` is the vendored LZ4 v1.9.2 the root `CMakeLists.txt` builds as
`bin/ueia_lz4.dll` (both `bin/` and `build/` are gitignored — the DLL is a build artifact, so
nothing may depend on it being committed, and nothing may ship a second decoder). `tools/stamp_lz4.cmake`
writes `bin/ueia_lz4.build.json` beside it from the build itself: that stamp is what makes
"is this library current?" answerable, and the Python side (`lz4.build_state`) reads it rather
than trusting a timestamp.

`tests/` is the hermetic suite: `testcase.py` is the shared floor (paths, scratch dir, CLI
runner — not a test file, and discovery is pinned to `test_*.py` because of it), `fixtures.py`
builds valid traces in memory, and the LZ4 class that loads the real library skips without it.
`goldens/` holds the committed corpus identity plus this machine's gitignored transcripts.

## Tests come with the feature — every time

**Every implemented feature lands with a copious unit-test suite in the same change.** Every
command, every decode path, every analysis rule, every flag, every output format: if its tests
are not in `tests/` and green, the feature is not implemented and no doc may describe it as
such. Tests are never "a follow-up item" — the effort figures in `ROADMAP.md` assume the test
work is part of the item, and a change to `src/` that adds behaviour without tests is rejected
before anything else is looked at.

"Copious" is meant literally. A feature's suite covers, at minimum:

* the happy path, and the boundaries (first/last, empty, one element, the maximum);
* malformed and truncated inputs — a corrupt packet, a bad size, a half-written header must be
  **reported as such**, never crash, hang or silently drop the rest of the file;
* absent data — the lazy schema (an event type that never fired), a missing channel, an empty
  frame range — which is information, and must read as information;
* each variant of what the parser handles: every packet form (raw, LZ4-encoded, sync), every
  event header form (one-byte/two-byte uid, sync/NoSync, important), every field type the
  schema can name;
* determinism: the same input twice produces byte-identical output, and every JSON document
  validates against its schema.

The suite is **hermetic**: it needs no capture, no GPU, no engine directory and no network —
fixture traces are built in memory in `tests/`. Wrapped exes are stubbed with a fixture binary
for the same reason. Checks that *do* need this machine's captures or a real engine directory
are the goldens half, reported separately, where "not compared" is never "pass".

## Engine tooling: the flag, the variable, then a search that says what it picked

`--engine-dir <dir>` (or `$UEI_ENGINE_DIR`, and `engine.py` holds the contract) names the tree whose
tooling this repo uses — the CsvTools executables, the source tree for file:line mapping, and
`Build.version` for a report's meta. An **explicit flag that names a tree wins outright and nothing is
searched**: an instruction is never second-guessed. A **flag naming something which is not one** is
printed and then searched past (2026-09-29 — it used to end the command), and if the search finds
nothing it is still a usage error. A **variable** naming a non-tree is reported and ignored, because
an ambient setting must never change behaviour quietly.

Nothing named at all **searches this machine** (REFERENCE §18): the launcher's install manifests, the
source builds the engine registers under `HKCU\...\Unreal Engine\Builds`, and the usual install
directories on `C:`–`H:`, one or two levels deep and never a network drive — four trees in 0.05 s on
the machine this was written on. Two rules the code keeps, because a search can lie by omission:
**the winner is named with its version and its gaps** (`rank` prefers a complete tree over a newer
partial one, then the newest version compared as *numbers*, then the most tools, then the path), and
**`UEIA_NO_ENGINE_SCAN=1` switches it off** for callers whose output must not depend on what is
installed — the hermetic suite, and the corpus harness for every pinned command (auto-discovery
changed six transcripts the moment it landed: `sources` and `advice` map through whatever tree the
machine has). Commands that need tooling and find none report **skipped** (exit 2) with the hint —
the same "nothing to compare" rule as the corpus half. Every wrapped call records the exe's SHA-256 and
exact argv (`toolrun.py`), because the unwrapped tool is the authority and our output has to say which
build answered.

## Channels: absent is not zero

A capture carries only what it was recorded with (the channel inventory is REFERENCE §8): a trace
without `gpu` cannot answer a GPU question, one without `memtag`/`memalloc` cannot answer a memory
question, and one without `task` cannot show a critical path. An analysis whose channel is missing
reports **skipped** with the re-record line, never zero, never a default and never a guess — the
same rule as the corpus half's exit 2, one level down. Every analysis that lands says in its tests
what it does with its channel absent, and `coverage` (ROADMAP §1) is the one command that has to
get this right for all of them at once.

## Parallel work (the one place processes are used)

`--jobs N` spreads the *per-thread* model walk over N worker processes
(`model._parallel_shares`); `0`, the default, chooses for the machine: one process until the
decoded streams pass `_PARALLEL_MIN_BYTES`, then the box's cores capped at `_AUTO_WORKERS_MAX`
(past a handful nothing improves on a lopsided capture — REFERENCE §6 has the measured series).
Three rules come with it, and the first two are scars:

* **A worker function is module-level.** `spawn` sends the function by name and the arguments
  pickled, so a lambda, a closure or an unpicklable object cannot be a unit of work.
* **The pool may not fail silently.** `ProcessPoolExecutor` replaces a dead worker and retries the
  same unit *for ever* — an initializer that raised cost an hour of wall-clock on 2026-09-28 with
  nothing on stdout to say so. The shared context is therefore handed over pickled (a mistake then
  fails in this process, when the pool is built), and a unit with no result inside
  `_STALL_TIMEOUT` is reported as a stall — a no-progress budget, never a total one.
* **`--jobs` may not change the answer.** Shares meet in one `_merge_share`, in ascending tid order,
  and the suite pins serial ≡ parallel at the cache's own byte level.

A bug fix lands with a test that fails before the fix. Never weaken, skip or delete an
assertion to make a run pass; a test pinning behaviour that looks wrong is marked
`CHARACTERIZATION` and changed together with the code, saying so. And every reply reports the
suite's real numbers — test count, pass/fail, pyright errors — not "passes".

## Standing rules

* **Never run `git commit` or `git push` unless explicitly asked in that turn.**
* **The corpus is generic and a path is never committed.** A capture is a key
  (`editor-pie-1`, ...: platform then ascending size for a new key) plus a SHA-256; where this
  machine keeps the file is gitignored local state (`captures.local.json`). CSV captures and
  their `.csv.bin` forms are local state too. No doc, comment, test or golden writes a
  capture's path or file name — refer to it by key. A capture's own strings (log lines, object
  names) are its author's words: transcripts of them are published only with the author's
  say-so.
* **CsvTools are wrapped, never reimplemented.** Where an engine executable answers a question
  (statistics, filtering, splitting, collating, SVG graphs, performance/regression reports —
  README §2), the code calls that executable, parses its output, and records the exe's version
  and exact argv in our own output. Any command that needs engine-provided tooling takes the
  single `--engine-dir` flag (env `UEI_ENGINE_DIR`), which also supplies the source tree and
  `Build.version`; a missing engine dir makes those sections report *skipped* (exit-2
  convention), never a silent pass, and tests stub the exes with a fixture binary so the
  hermetic suite needs no engine. Subprocess rules: an argument list and never `shell=True`,
  stdout/stderr/exit code always captured, a timeout, a scratch working directory, and paths
  with spaces quoted correctly (both the corpus and a default engine install contain spaces —
  this bug would otherwise arrive on day one).
* **Evidence or silence.** Every claim — in a report, a golden, or a reply to the user — cites
  the frame/timer/thread/event id it came from and the command that reproduces it, and says
  which of *certain* (the file recorded it), *heuristic* (a name matched a pattern) or *unknown*
  (channel absent, decode missing) it is. An answer that ends with "here is what I could not
  determine, and how you could" beats a confident guess. A wrapped CsvTools output is cited by
  exe + version + argv, not swallowed.
* **Report the real numbers** (test count, pyright error/warning counts, measured timings with
  what they were measured against), not "passes". A measured number is quoted as a ratio or with
  its baseline.
* **Never weaken, skip or delete an assertion to make a run pass.** A bug fix needs a test that
  fails before it. Tests pinning behaviour that looks wrong are marked `CHARACTERIZATION` and
  changed together with the code, saying so.
* **The format facts are pinned to UE 5.8.3** — the local engine source tree (reached through
  the engine dir). When a binary-layout question comes up, read the engine source and cite the
  file — never guess a layout, never trust a blog. The file map (every one of those files was
  used to write `REFERENCE.md`) is in `REFERENCE.md` §7.
* **The vocabulary is in the file, not in the engine.** `.utrace` is self-describing: event,
  timer and channel names come from `NewEvent` records decoded at run time, and the schema is
  *lazy* — a type missing from a capture never fired in that session, which is information, not
  a decode failure (REFERENCE §4). Never hardcode event or timer names into the tool. (This is
  the deliberate inversion of rdc-tools' chunk-name rule, where names came from the RenderDoc
  tree; here the engine tree answers *layout* questions, the capture answers *vocabulary*
  ones.)
* **Text output is the contract.** Deterministic for a fixed input and tool version: sorted
  tables, no timestamps, no absolute paths in the prose. `--format json|markdown` may change the
  shape, never the answer. Every JSON document carries `schemaVersion` — including documents
  that embed a wrapped exe's output, which keeps the exe's own bytes intact under a typed
  envelope rather than reformatting them.
* **The cache stays invisible** — it may change speed, never output; keep tests hermetic
  (scratch dirs via env vars, like rdc-tools' `TempDirCase`). Add cache and corpus state to
  `.gitignore` as it appears. Anything a report needs must therefore be *in the model*: the
  per-frame work attribution and occupancy are written by the walk and cached, which is why a warm
  `summary`/`bottleneck` costs 0.52 s like any other command (REFERENCE §6, §10) — a second walk at
  report time would have been the slow path on every run. Changing what the model *means* means
  bumping `TOOL_VERSION` in the same change, or a cache built by the old meaning keeps answering with
  it (this happened during §11's development: the old walk's `wait_cycles` looked like a real
  finding for one command run).
* **A scope pair belongs to the frame its *cycle* falls in, never to "the frame that was open".**
  Frame markers and scope batches do **not** interleave in a `.utrace` — `game-pc-2` writes all
  771 of a thread's frames in the first 8% of its stream, the batches after them — so stream order
  answers a question nobody asked. The walk pairs the frames first and then attributes by window
  (`_pair_windows` + the cycle search), the way the engine's own analyser clips an event to a frame
  interval; the corpus is what caught this, and it is why the extra pass is not optional.
* **A sample says it is a sample.** The model keeps frame work only for the longest frames of each
  thread (`_FRAME_WORK_KEEP`), because a capture with 100,000 frames must not carry 100,000 of
  them to name its worst twenty. A report that lists a frame without one prints `-` and says so;
  it never presents the absence as an empty frame, and never re-derives the answer it dropped.
  The same rule covers the attribution's leftovers: a scope pair that could not be attributed is
  *counted* (`scope_pairs_spanning`, `scope_pairs_no_spec`, the unpaired ends and begins), and
  `verify` prints those counts — a gap in a report is a number, not silence.
* **A measurement is not a recommendation.** `ueia parallelism` reports what it measured (solo
  cycles, peaks, lock overlap) and marks every *ceiling* it derives as Amdahl on that measurement,
  because "this work had nobody beside it" is a fact and "this work could be spread" is not. Two
  consequences the code keeps: the ceiling is built on the **commonest** frame's thread count, never
  the maximum (one startup frame where 60 threads run at once would turn an idle machine into a full
  one), and an absent input stays absent — a capture records no core count, so oversubscription is
  "cannot be judged here", and a lock whose name the heuristic does not recognise is "invisible",
  never "no contention". The same shape as the bottleneck's rule about a missing GPU channel.
* **A second pass over the streams says what it cost.** `ueia self` is the one command that reads a
  thread's streams again — the model keeps only the longest frames per thread, so "this frame's
  self time" cannot come from the sample, and a tree per frame for every frame would dwarf the cache
  (6.74 MB for the corpus's editor capture). It therefore prints its own `pass` line (7.25 s over the
  game capture's tid 2, 17.48 s over the editor's, once per run) and is deliberately **not** in
  `goldens.PINNED_COMMANDS`: the harness pins commands that cost a fraction of a second, and the
  gate's runtime is a feature. It is paid **once per capture** -- the trees are stored with the model
  (`FrameRow.self_detail`) and every later run answers from them (4.86 s then 0.27 s on the game
  capture, 12.33 s then 0.38 s on the editor's, byte-identical both times), and the seconds go to
  stderr under `$UEI_PROGRESS`, never into the report: `cache.py`'s rule is that no command's output
  may say whether it was warm, and a report that cited the pass's seconds would change between two
  runs of the same command on the same capture. What keeps it honest instead is the fixture whose tree is
  hand-computable at 1 cycle = 1 microsecond, plus one real-capture invariant check — and, since
  2026-09-30, a **frame-by-frame comparison with the walk's own measurement** (`FrameRow.self_cycles`,
  credited during the parse by the same rule and the same promotion): two independent decodes of one
  capture must produce one answer, and the test says so on the game capture. Anything that changes how
  either side credits self time has to keep them equal.
* **Never sum inclusive cycles across scopes.** The model's per-frame work is inclusive (a scope
  counts its children), so adding it up per file, module or pattern produces a number no frame ever
  contained — measured: `Runtime/CoreUObject` sums to 306,239 s of a 332 s capture. Anything that  aggregates a group of timers takes **the biggest match inside each frame and sums those across
  frames** (`sources.Weights.presence`), which is bounded by the frames' own span and is a lower
  bound on the group's presence. A share that cannot exceed 100% is the point; the call tree
  (ROADMAP, self time) is what will make the other question answerable.
* **One machine, offline.** No network, no device, no live connections — see ROADMAP's not-list
  before proposing work that needs one.

## Python coding guidelines (inherited from rdc-tools, enforced from phase 1)

Target: **Python 3.8+, standard library only** — the one native piece is the vendored LZ4 DLL,
reached through `ctypes` and never through a third-party Python package — pyright `"standard"`
mode, zero errors and zero warnings.

* `from __future__ import annotations` at the top of every module; annotate every parameter and
  return. Modern annotation syntax is allowed *because* of that future import, but nothing that
  needs 3.9+/3.10+ at runtime.
* `TypedDict` for dict-shaped records, `Optional[T]` for nullables, `Sequence`/`Iterable` for
  read-only inputs. No `Any` without a comment saying why; no `# type: ignore` without a rule
  name and a reason.
* PEP 8, 4-space indent, ≤100 columns; PEP 257 docstrings (the module docstring **is** the CLI
  help — keep its `Usage:` block in sync with `main()`); `UPPER_SNAKE_CASE` constants,
  `snake_case` functions; no mutable default arguments; no bare `except:`.
* stdlib imports at the top sorted; optional third-party deps imported inside the function that
  needs them, with a `typings/` stub so pyright can resolve them.
* CLI parsing stays hand-rolled: no `argparse` rewrite of a dispatch that works; `main()` prints
  the module docstring for a missing/unknown command. Behaviour is the contract — quirks pinned
  by tests survive refactors unless changed deliberately, together, with a note.
* Layout: one module per layer under `src/py/`, each importing only *down* the layering
  (container → packets → events → schema → model → analysis → engine tools → CLI). A cycle
  breaks star-imports at import time; put shared helpers *below* the modules that need them.
  New behaviour needs tests in `tests/` in the same change; a parser fixture goes in-memory,
  not on disk.
* No new runtime dependencies without asking first.

## Conventions

* Docs: `README.md` (setup, corpus, CsvTools reuse, the playbook), `REFERENCE.md` (the capture
  format, decoded: container, packets, event streams, schema incl. the CSV Profiler's events,
  the engine file map, corpus measurements, §10's definitions of what a frame-time report
  means by a frame, a percentile, a hitch and "what ran in it", §11's bottleneck verdict, §12's
  task graph, §13's coverage timeline with the parallelism report it feeds and §14's source
  mapping), `ROADMAP.md` (build order,
  P-labels, scope, the not-list — landed items are removed, cross-references updated in the same
  change), `AGENTS.md` (this file).
* Nothing that belongs to one machine's working tree is committed — local state (the parse
  cache, `captures.local.json`, exported reports, generated SVGs, CSV captures) goes into
  `.gitignore` as it appears.
