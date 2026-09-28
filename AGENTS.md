# AGENTS.md

Personal, vibe-coded tools for offline Unreal Insights `.utrace` analysis. See `README.md` for
what they do; nothing here is a supported product. This repo is built in the flavor of
[rdc-tools](https://github.com/lawfuyang/rdc-tools) — where a rule below says "inherited", the
precedent and the scar tissue behind it are in that repo's `AGENTS.md`.

## The gates do not exist yet

ROADMAP §1 lands them **with** the parser, not after: the hermetic `selftest` suite, a clean
`npx --yes pyright@latest` (0 errors, 0 warnings), and the goldens corpus harness. Until then
the standing rules below are the whole contract; from then on, a change to `src/` is not
finished until all three pass. A change that can move what a **command prints** is not finished
until the corpus agrees (`goldens --check`, with rdc-tools' exit-2-means-not-compared convention
for machines without the captures).

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
  `.gitignore` as it appears.
* **One machine, offline.** No network, no device, no live connections — see ROADMAP's not-list
  before proposing work that needs one.

## Python coding guidelines (inherited from rdc-tools, enforced from phase 1)

Target: **Python 3.8+, standard library only** (plus the optional, lazily-imported `lz4`),
pyright `"standard"` mode, zero errors and zero warnings.

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
  format, decoded: container, packets, event streams, schema incl. the `CsvProfiler` channel,
  the engine file map, corpus measurements), `ROADMAP.md` (build order, P-labels, scope, the
  not-list — landed items are removed, cross-references updated in the same change),
  `AGENTS.md` (this file).
* Nothing that belongs to one machine's working tree is committed — local state (the parse
  cache, `captures.local.json`, exported reports, generated SVGs, CSV captures) goes into
  `.gitignore` as it appears.
