# The web UI

Server-rendered pages from the control plane (Quart + Jinja), made live with htmx 2.0.11, its SSE
extension and idiomorph, all vendored under `src/coverset/web/static/vendor/` (pinned, SRI-checked;
see the README there). No build step, no Node, no inline scripts. Code: `src/coverset/web/`
(blueprints), `templates/`, `static/app.css`, `static/keys.js` and `static/app.js`, and `render/` (the only modules
that produce trusted markup).

## Pages

| Route | What it shows |
|---|---|
| `/` | Dashboard: repositories, live runs (queued, running, awaiting approval), the last 20 runs with verdict chips, and a few numbers (runs, tests, median time to the first test result and to the first failure). |
| `/repos/<rid>` | The overview of the default branch's baselines (see [Repository overview](#repository-overview)), then pull requests with their latest run, default-branch runs, links to the map, tests and invariants. |
| `/repos/<rid>/prs/<n>` | The PR page (latest run for the PR head; `?run=` picks another run of the PR). `/runs/<run_id>/coverage` redirects here. |
| `/repos/<rid>/prs/<n>/functions/<func_key>` | Fragment: one "Needs attention" card's expanded content (loaded when the card opens). |
| `/runs/<run_id>` | Run page: timeline strip, shard Gantt chart, failures, "Why this order", every test. `?replay=1&speed=0.25` replays a stored run; `&freeze=<seconds>` stops the replay at that point of the run's own clock. |
| `/runs/<run_id>/events` | The run page's SSE stream (HTML regions as named events). |
| `/repos/<rid>/files/<path>` | Source view. `?run=` picks the run (default: the newest default-branch baseline), `?pr=<n>` uses the PR's latest run and marks changed lines, `?test=<test_id>` dims every line that test did not run (see [The test page](#the-test-page)), `?highlight=all\|missing` picks the highlight mode (see [View settings](#view-settings)). |
| `/repos/<rid>/files/<path>/lines/<n>/tests` | Fragment: the tests that ran one line (the gutter popover). |
| `/repos/<rid>/functions/<func_key>` | One function: its history over the last baselines (see [Function history](#function-history)), shape grid, flow ribbon, source with coverage (with the source view's **Highlight** toggle in the header, key `h`; its first state is the mode the layout resolved, `?highlight=` then the scoped `view.highlight`, so it agrees with the painted lines before scripts run), covering tests, invariants on it, AI finding. A parameter row without input classes says "never called by a test" only when no test ran a line of the body; a function whose tests ran it but recorded no argument shapes (a Rust bundle, a Python run without shape capture) says "called by N tests, no argument shapes recorded". |
| `/repos/<rid>/functions/<func_key>/shapes` | Fragment: the shape grid; with `&cls=<param>:<class>` one cell's detail. |
| `/repos/<rid>/map` | Treemap, `?mode=gap` (default) `\|lines\|inputs`, `?modules=thin` (default) `\|full\|hidden` (module-level code), `?layout=ordered` (default) `\|squarified`, `?path=` zooms into a directory or file, `?run=` (a PR run is drawn in its baseline's frame). A "largest gaps" table ranks called functions by gap, lists the functions no test called apart, and only counts what has nothing to compare (module-level code, functions that expect no input classes). |
| `/repos/<rid>/tests` | Every test (each nodeid links to its test page): runs, fail rate, flaky retries, p50/p95, last failure, quarantine toggle (`POST /repos/<rid>/tests/<test_id>/quarantine`, admin + CSRF; the answer swaps the row (`tests_row.html`, the same markup the page's single row loop writes) and, out of band, the "N quarantined" count). `?sort=risk\|slow\|name`. 500 rows a page (`?page=N`, previous and next links above and below the table) with a "show all N" link (`?all=1`); the header counts every test. A filter box under the sort switch ("Filter tests  /", `/` focuses it) hides the rows of the page that do not contain every typed word; `Enter` opens the first match, `Esc` clears it. |
| `/repos/<rid>/tests/<test_id>` | One test: identity, its result in `?run=` (default: the newest completed default-branch baseline), the last 12 results as squares, and its data flow in that run (see [The test page](#the-test-page)). `test_id` is the control database's test id (an id beyond 64 bits is a 404). |
| `/repos/<rid>/tests/by-nodeid?nodeid=&run=` | Redirects (302) to the test page of that nodeid, keeping `run`. Every nodeid in the UI links through the `test_link` macro: the tests table, the run page's failures and "All tests", the PR page's tests, the line popover, a shape cell's tests and the function page's covering tests. |
| `/repos/<rid>/invariants` | Every declared invariant: statement, status chip, the last 12 statuses, the coverage lens and per-verification evidence. `?run=<PR run>` adds the PR-vs-base comparison and "Invariants changed by this PR". |
| `/forge/outbox` | (Instance admins; the header links it, and Setup, only for them.) Every recorded forge call, newest first; sticky comments and check-run summaries rendered as GitHub-flavoured markdown under "Would be posted to GitHub (fake forge)". `?method=` filters. |
| `/setup` | GitHub App manifest flow (the App is stored in the database, encrypted; **Forget the App** deletes it), the credentials in force, token mode, sandbox settings. Plain HTML, no scripts. The "Create the GitHub App" button is disabled, with the reason, while the public URL is a loopback address, `COVERSET_SECRET_KEY` is unset or the environment holds GitHub credentials. |

Every UI route renders only its `main` block when the request carries `HX-Request: true` without
`HX-Boosted`, and the full layout otherwise (responses `Vary: HX-Request`). Unknown repos, runs,
functions and paths are 404; a source path is served only when the run's facts list it, and its text
comes from `git cat-file` on the bare mirror at the run's head commit. A 404 or 500 on a UI route is a
styled page in the layout that says what was not found (`/v1/*` keeps its JSON body); a fragment that
fails to load shows "Could not load this section (HTTP n)" where it would have gone.

### The PR page

1. **Verdict band** (sticky, full width, opaque): FAIL / PASS / PASS with gaps, the reason, then
   "first failure 1.0 s · test 1 of 100 (file order: 66th, ~1.7 s estimated from the first test)",
   patch coverage, changed
   functions with no test, "N input classes never exercised in changed functions" (distinct
   `param:class` per changed function, the same classes the cards count) with a muted "· M more in
   invariant targets" linking to the invariants page when functions the PR did not change, checked
   because invariants target them, have more; then invariants violated or untested, and the AI review
   source ("fixtures", always marked advisory). The sticky comment's headline uses the same split
   (`coverset.forge.comment.missing_counts`) and keeps the full list in its `<details>`.
2. **Needs attention**: one card per changed function, most severe first: an invariant on it is
   violated or a covering test failed, then no test calls it, then lines never ran, then expected
   input classes never passed in. Each card has the NEW/CHANGED badge (TRIVIAL/RENAMED/MOVED with
   "inherited"), test count, line and branch percentages, a mini shape strip (input classes only),
   invariant chips and the AI chip (labelled by source, advisory). An invariant appears on a card
   when it targets the function or when one of its linked tests covers it ("via test_x", a failing
   test first). Opening a card shows the diff through the coverage formatter (removed lines struck
   through, changed lines marked `+`), the shape grid, the flow ribbon, the top five covering tests
   and a suggested test with a Copy button (the AI review's when it has one, else one generated from
   the missing classes), headed "Suggested Hypothesis test" only when it uses `@given`. AI
   invariant claims read "AI suggests: `key` may be violated". The first card is open.
3. **Collapsed sections**: tests (failures first), invariants with their lens, indirect changes
   (deleted functions, class- and module-level edits) plus patch coverage per file, and
   "Invariants changed by this PR".

### The run page

* **Timeline**: webhook received (when there was one), queued, sandbox ready, first test, wave 1
  done, first failure, verdict, finished, in seconds from the webhook (else from queuing). A failed
  test reported before its retry confirmed it shows as a dashed "failure reported" tick until the
  confirmed first failure replaces it. On runs of the demo repository (`coverset-demo/ledger`),
  when `docs/numbers.json` has `t_cold_path_demo`, a grey ghost bar shows the demo app's measured
  cold path on the same axis, labelled "measured cold path, demo app: fresh sandbox, install, plain
  pytest", with a failure tick at `t_cold_path_demo_first_failure` when the run itself has a first
  failure; when it is more than four times the run's axis the bar is drawn broken and labelled "not
  to scale" instead of squashing every milestone into the first pixels. Other repositories get no
  bar: no cold path was measured for their suites (the bench suite's `t_cold_path` describes 1,200
  synthetic tests, not them). After a confirmed failure the head line also says where file order
  would have run it and when ("~3.3 s estimated from the first test", see
  [orchestrator.md](orchestrator.md#timeline)).
* **Shards**: one lane per sandbox, one block per test in the order it reported. A block spans
  from the previous report on its shard to its own (the first test of a shard gets the lane's
  median block width, since its report also carries pytest's start-up). Blue = passed, vermilion
  with a heavier edge = failed, dotted yellow = flaky (failed, then passed on retry), grey =
  skipped or xfail, hollow dashed = tests still queued. Blocks are at least 6 px wide (failures
  10 px, drawn last); a lane whose blocks would overlap at that width is laid out in run order (each
  block right after the previous one, never before its own time) and its label says "in run
  order". A full-height vermilion rule marks "first failure t s" (or "failure reported" before
  confirmation) across all lanes.
* **Why this order**: the tests with a risk signal (score above 0, or in wave 1; at most 20) in
  priority order, each a stacked bar of its score components with its one-line reason; up to
  three failing tests are pinned on top with "ran 1st; file order would have run it 66th" and how a
  tie on the score was broken; one muted row sums up the rest ("96 more tests with no risk signal,
  then shortest p50 first", or "then in file order" when that is what happened). Component colours
  are a fixed categorical set clear of the coverage blue, the never-ran vermilion and the status
  green and red. Durations read in ms like the tests page. While replaying, a failing test is
  pinned only once the replay has reached its failure.
* **Failures**: expandable tracebacks (one element per test id; the page opens the first, stream
  frames never re-open one, and a reader's open/closed choice survives every morph); whether a
  retry confirmed the failure, its rank, whether it also fails on the base branch. The head says
  what the scores were computed against ("scored against `1a2b3c4d` (merge-base)"); the cost line
  is hidden for the local executor.
* An untrusted run awaiting approval shows the Approve run button to admins
  (`POST /runs/<id>/approve`).

### The test page

`/repos/<rid>/tests/<test_id>?run=` (`web/testpage.py`, `templates/test.html`). The run is chosen
like the other pages: `?run=` (a run of this repository, else 404), default the newest completed
default-branch baseline; each history square opens the page for its run.

* **Identity**: the nodeid, its file, the markers and whether it is property-based (`@given`),
  read from the test's source at the run's head commit (module `pytestmark`, class `pytestmark`
  and decorators of the test function; a parametrize id may itself hold `::`, so the name is what
  lies between the file and the first `[`). When the run's facts do not list the test's file the
  markers read "not read" and property-based "unknown", since the plugin's collected markers are
  not stored. Then the invariants that link it in this run with their status, and the rolling
  statistics. The heading is the test's function with its parametrize id (`test_x[a::b]`).
* **In this run**: outcome, duration, attempts (each one listed when retried), the flaky flag, rank
  and shard, and the failure text from the run's `test` events (already redacted; only this test's
  events are read, newest first). Under it the
  last 12 results as squares in the invariants page's style, oldest first, with a glyph and a
  title each (✓ passed, ✕ failed, ! error, ≈ flaky, – skipped); the selected run is ringed.
* **Data flow**: an SVG (`render/flow.py`, the ribbon's visual grammar) with four columns: the
  test → in-repo callers in between ("via", only when the shape observations show a call came
  through another function) → the functions it called → their parameters, each labelled with how
  many input classes this test carried. Ribbon width = sampled calls; a hairline = one call (or a
  function the tracer never sampled for this test, whose body still ran). A `call_shapes` row
  aggregates every test of a caller, so a row's samples are shared evenly among its tests; one call
  passes all its parameters, so a ribbon into a function or a "via" carries the samples of its
  busiest parameter, not their sum, and a ribbon into a parameter that parameter's own. The shade
  steps with the classes carried (1, 2, 3–4, 5+). Only the 40 functions with the most sampled calls
  get a bar (the rest share one "+N more functions" bar, and a note under the chart says so) and a
  function fans out to at most 12 parameters ("+N more parameters"); the table below lists every
  function. Every bar is a link: the test to its file's source view, the functions and parameters
  to the function page.
* **Functions it called**: per file, the functions whose body the test ran (from `test_lines` ∩
  function bodies, `facts/queries.py` `test_touched`, ordered by file then `def` line), "body
  lines run 5 of 7", the line ranges (each a link into the source view), module-level lines, and
  per parameter two strips drawn with `render_strip`: **this test** (filled = a class this test
  passed in; hollow dashed = a class no test ever passed in; classes only other tests reached are
  left out and counted "N more by other tests") and **any test** (the function's whole picture),
  with the redacted example this test passed when it was the example's test.
* The source-view links carry `&test=<id>`: the source view then dims every line that test did not
  run, says "lines run by <test>: N of M statements" in the bar (with "not in this run" when the
  test has no result in the selected run), and keeps `test=` on its previous / next file links;
  "show every test" drops it. A test of another repository is a 404.
* Every string that comes from a bundle (the test id, paths, function and parameter names,
  examples, the failure text, and the flow SVG's labels and titles) shows its bidirectional control
  characters as visible `<U+202E>` markers instead of applying them (the static report marks them
  the same way; the PR comment strips them). The test and source pages do; the tests list, the run
  and PR pages still print test ids through the shared `nodeid` filter unmarked.

### Repository overview

The top section of `/repos/<rid>` (`web/repos.py` `repo_overview`, `templates/repo.html`), read
from the facts store, never from the control database's totals. A *baseline* here is a finalized
run of the default branch with no PR number (`runs_dim`); the overview uses the last 12, and the
latest one is the run it describes (it links to its run page).

* **Tiles** (the dashboard's stat style), from `baseline_trend`'s newest point: statements, lines
  run % with "182 of 202", branches taken % with "31 of 38", functions called by a test / callables,
  input classes observed / expected with the %, and tests (distinct node ids without `<import>`).
* **Trend**: one sparkline per measure, lines run % (solid) and input-class % (dashed), with the
  baseline count and how they spread ("6 baselines over 3 min, newest 2026-10-12 01:50 UTC" within
  two days, "12 baselines 2026-10-01 → 2026-10-11" when further apart). Each sparkline fits its own range: line % sits near 90 and
  input-class % near 35, and on one shared axis both would flatten. Under it, the change from the
  previous baseline in percentage points and in classes observed and expected.
* **Since the previous baseline** (`class_changes`): per function present in both baselines, the
  classes **missing now** (− in the miss colour, hollow dashed chips) and **no longer missing** (+
  in the ran colour, filled chips), followed by a muted "vs `d0e6be8c`, inferred from the stored
  gaps": only never-observed classes are stored, so a chip's title says "missing in X, not missing
  in Y", never "observed". Classes are named only when the function's signature digest
  (`sig_hash`) and expected class count are the same in both baselines; otherwise the row says
  "expects 10 → 12 classes: classes not compared" (or "its signature changed: classes not
  compared") and names none. A function no test passes anything to any more reads "lost N classes
  (no test passes it anything now)". Then "lines no longer run" with the body lines that ran before
  and not now (bodies whose hash did not change), and a note when a test of the function hit the
  sample cap in the latest baseline (a missing class may only have gone unsampled). Each function
  links to its function page. With no change it says "No input class lost or gained and no body
  line newly uncovered since `d0e6be8c`"; the first baseline says there is nothing to compare yet.
  The line under the trend gives the change in points, or "n/a" where either baseline has no
  percentage.
* **Gap map**: the Gap-mode treemap of the latest baseline at 520 × 220 with its labels hidden and
  the Gap legend under it; every tile links to the coverage map (a directory or file zoomed in, a
  function's tile to its file), and the heading links to the whole map. Tiles that would be smaller
  than 64 px² are merged per file ("12 small functions") or per directory ("40 small files") into
  one tile of the same area, coloured by their statement-weighted numbers, so a large repository
  ships a few dozen tiles instead of tens of thousands (the demo's tiles are all far larger). The
  hover line above it shows a tile's numbers at once, as on the map (see the data-flow overlay).
* **Largest gaps** (`top_gaps`, top 8): called functions with the most input classes no test passed
  in, with "16 of 18", the body's line % and up to six class names as hollow chips ("+N more").
* Without a finalized baseline the section says so and the PR and run lists follow as before.

The dashboard's repository table gains a "Latest baseline" column: a line % sparkline and an
input-class % sparkline (160 px each, last value labelled) over the same baselines. The data comes
from a context processor in `web/repos.py` that only runs for the dashboard's endpoint.

**Sparklines** (`render/sparkline.py`): 160–240 × 36 px, one or two series on one axis (0–100,
fitted to the data with at least 10 points of range), 2 px lines, the second series dashed with a
hollow end dot, the last value of each labelled at the right in text ink (pushed apart when the two
would collide), `None` values break the line (a lone value is a dot), an optional dotted reference
rule with its own title and an optional tick under the current point. Every point has a
full-height hover column whose `<title>` names the run and each series' value; the SVG is
`role="img"` with an `aria-label` that summarises each series ("main baselines: lines run from 79%
to 90.1% over 6 baselines"). Lines are Okabe-Ito blue (`--spark-1`, `#0072B2` / dark `#3D9AD6`);
input classes are pink (`--spark-2`, `#D075AB` / dark `#C96FA4`), a pair that passes the dataviz
palette validator in both modes, and the dash carries the distinction without colour.

### Function history

A "History" panel on the function page above the shape grid and source (`web/files.py`
`function_history_view`, `templates/function.html`), from `function_history` over the default
branch's last 12 baselines up to the run the page shows (created at or before it), so viewing any
run, an old baseline outside the newest 12 included, shows the runs that led to it and it last,
never later ones. When that run is not a baseline (a PR run), it is appended as the last point (its
square set a little apart) and labelled "this run (<branch>)". Classes and modules have no panel
(their history would only say "not in this run").

* The heading's aside repeats the selected run's numbers and the window ("input classes 8 of 10 ·
  lines 100% · 6 baselines of `main` up to this one", or "... before this run, and this run").
* One sparkline with two series on one axis: body lines % (solid) and input-class % (dashed),
  with a key, gaps where the function did not exist yet and a tick under the selected run.
* One square per run in the test page's history style, oldest first: a **filled** square (ran
  tint, blue edge) shows the number of classes observed that run, `–` (solid outline) means the
  function existed but no input classes were recorded (no test passed it anything), and a
  **dotted** square with `·` means the function is not in that run. Each title reads like "baseline
  77e37dc6 · 2026-10-12 01:50 UTC · 8 of 10 input classes · lines 100% · 22 tests"; the selected
  run is ringed and every square of a run that has the function links to the page for that run.
* Under the strip, a class missing at the selected run that the nearest comparable earlier run did
  not miss reads "`email:idn` missing now, not missing in run d0e6be8c (date)", naming the newest
  comparable earlier run that did not miss it (with the sample-cap caveat when the selected run hit
  the cap, a PR run's included), and a muted line says it is inferred from the stored gaps. Only
  never-observed classes are stored, so only runs with the same expected class count and signature
  digest are comparable; the others are skipped.

## The data-flow overlay

**Input classes** are named blocks of a parameter's input space (`email:multi-label-domain`,
`weights:zero-element`). The catalogue expects a class only when the data or the annotation makes
it relevant: an email-shaped string was seen, so the email sub-classes are expected.

* **Shape grid**: one row per parameter (signature order) plus `return`; one square per expected
  class, grouped by family (str, email, quantity, number, container, elements, order, ...). A
  **filled** square is a class some test passed in; its shade steps with log10 of the samples
  (1, 10, 100, 1000+), with a blue edge so even the lightest step stands out. A **hollow dashed**
  square is a class no test ever passed in. The `return` row shows only what the function was seen
  returning (filled, "returned in N sampled calls"): a return value is an output, so it is never
  hollow, never suggests a strategy and never counts. The header reads "Input classes 8 of 10 ·
  lines 100% · branches 100%" and, like the hollow note and the cards' strips, counts inputs only. Hollow squares are labelled
  "never exercised" only when no test of the function hit the tracer's sample cap; otherwise the
  wording is "not observed in N sampled calls (K tests hit the sample cap)", because sampling may
  have missed it. A hollow square means "never observed", not "buggy". Clicking (or Enter on) a
  square shows the tests that passed that class in with a redacted example value, or for a hollow
  square the suggested Hypothesis strategy.
* **Flow ribbon**: three columns, test files → in-repo callers ("called from the test" for direct
  calls) → parameters, from the tracer's caller keys. Ribbon width is sampled calls; the shade
  steps with how many distinct input classes those calls carried (1, 2, 3-4, 5+). Node bars are
  thin and caller labels sit above their bar. Grids and ribbons scale to their column.
* **Inlay chips**: under the `def` line of every function that expects input classes, always
  visible and wrapped inside the code viewport: `email: str  plain 14 · multi-label-domain 0 · ...`.
  Filled chips are observed classes with their sample counts, dashed chips are classes never
  exercised. Specific families (email, quantity, number, elements, order, ...) are shown when a
  parameter has them, else every class; whatever that leaves out is one "+K more (names)" chip,
  dashed when one of them was never exercised, so the chips always add up to the header's count.
  The "function" link sits at the right edge of the code viewport.
* **Treemap Gap mode**: directory → file → function (module-level code is the `<module>` leaf),
  area = statements. Gap = line coverage % minus input-class coverage % on one hue, in five bins
  with round edges (0-20-40-60-80-100) whose first bin is already a visible step: the stronger the
  colour, the more of the function ran under tests that never tried whole kinds of input. Hatched
  tiles were never called; grey tiles expect no input classes (functions without parameters,
  properties); module-level code is an outlined tile in the surface colour, dashed when it is drawn
  reduced (see [Module-level code on the map](#module-level-code-on-the-map)). Lines and Input
  classes modes use a blue ramp with the same bins. Tile labels are dark on the light steps and light
  on the dark ones (at least 4.5:1). The SVG is a group of links, each with its own `aria-label`.
  The layout is ordered (see [A map that holds still](#a-map-that-holds-still)).
* **Hover line**: a status line sits right-aligned directly above the map (and above the overview's
  thumbnail), muted "hover a tile" until a tile is hovered or focused with the keyboard; then it
  shows that tile's whole text at once: `src/ledger/billing/refund.py::refund · 14 statements ·
  lines 86% · input classes 40% · gap 46 points`, "never called by a test" for a function no test
  called, "module-level code: no input classes", and for a directory or file its path, statements
  and line %. The text is the tile's `aria-label`, carried in `data-info` (the native `<title>`
  tooltip stays); `app.js` (and `report.js` for the report) fills the line by event delegation
  (`mouseover`/`focusin` on the map, back to the placeholder on `mouseout`/`focusout`), so no
  inline handler is needed. The line is `aria-hidden`: a screen reader already reads the focused
  tile's label.

## The source view

* **Summary line** in the bar, after the path: `lines 28.5% (294 of 1030) · branches 61.2% (30 of
  49) · input classes 12 of 68 (17.6%) · gap 11 pts · 1 test`. Lines are the measured statements
  some test or import ran; branches are the file's possible branch arcs and those the run took
  (the facts store's `file_cov` counts; left out when the file has no branch); input classes are
  the expected classes of the file's called functions and how many some test passed in; the gap is
  line % minus input-class % (never below 0), the number the map's Gap mode colours by. A file
  whose called functions expect no input class says "no input classes" and shows no gap. Tests
  are the tests (not import time) that ran a line of the file. Each piece has hover text ("736
  never ran", "37 lines took one branch only", "56 input classes never exercised"); the line wraps
  between pieces, never inside one, so it stays readable at phone width.
* **Syntax colours**: Python (`.py`, `.pyi`), TypeScript (`.ts`, `.mts`, `.cts`, `.tsx`) and
  JavaScript (`.js`, `.mjs`, `.cjs`, `.jsx`) are highlighted with their Pygments lexers, anything
  else is plain text (still one block per line). Keywords are violet, builtins, types and
  decorators teal, strings green, numbers plum, comments and docstrings grey italic; function and
  class names at their definition are bold ink (the TypeScript lexer does not tell a definition
  name apart, so there they stay ink); operators and punctuation stay ink. The palette is muted and
  avoids the coverage hues, so the blue / vermilion / dotted-yellow bars and tints, the `+` diff
  column and the lens stay the loudest marks. Every token colour keeps at least 4.5:1 on the
  surface and on the never-ran and partial tints in both themes (light: keyword `#6a3d9a` 7.6:1,
  builtin `#1d6464` 6.9, string `#2f6b3a` 6.4, number `#8f3a6e` 7.0, comment `#5d6670` 5.8 on
  white, 5.0 or more on the tints; dark: `#b9a3dc` 7.9, `#86c0bb` 8.6, `#a3c795` 9.4, `#dca5c2`
  8.6, `#8f99a3` 6.1 on `#15191e`, 4.7 or more on the tints). Excluded, removed and dimmed (lens)
  lines drop the token colours and recede as plain text. Token spans carry Pygments' classes with
  a `tk-` prefix (`tk-k`, `tk-s2`), so a token class never collides with the page's own classes.
* **Multi-line statements** are painted as one block. coverage.py reports a statement at its first
  line, so a parenthesised import, a call with its arguments on their own lines, a dict literal, a
  backslash continuation or a triple-quoted string used to colour only its first line. Every
  continuation line now takes the state of its statement's first line with the `cont` class: the
  same bar colour, a lighter tint, no gutter count and no "never ran" note (the first line keeps
  those), so a never-run 8-line import reads as one vermilion block. For Python the continuation
  lines are found with `tokenize`, by the rule coverage.py builds its `_multiline` map with (the
  lines up to the `NEWLINE` token that ends the logical line: inside brackets, after a backslash,
  inside a multi-line string, blank and comment-only lines among them). A compound statement's
  header ends at its colon, so `def f(\n  a,\n  b\n):` covers the header lines only, never the
  body. For TypeScript and JavaScript a bracket-depth scanner marks the lines that start while a
  `(`, `[` or object-literal `{` is open, and the inner lines of a template literal; a block `{`
  (after `)`, `=>`, `else`, a class name, or at a statement's start) ends the chain, so a callback
  body passed as an argument keeps its own statements (strings and comments are skipped; a regex
  literal or `${...}` nesting can mislead the scanner). A line coverage measures on its own always
  keeps its own state. A partial branch stays on the first line (its continuation lines read as
  ran). The diff column (`chg`) stays per physical line; the lens follows the statement, so a
  continuation line is lit when the test ran its first line.

## Keys

One table drives the keys on every page: `KEYS` in `static/keys.js`, shared with the static report,
plus the keys `app.js` binds for the live pages' own content (`tests/web/test_keys_table.py` checks
that the help dialog, this table and the scripts agree). `?` opens the help dialog, which lists only the keys of the page it was
opened on (a row whose kind of content is missing, say the source keys on the map, is hidden). Keys
are ignored while typing in a form field and with Ctrl, Alt or Cmd held. A filter or search box
takes `Enter` (open the first match) and `Esc` (clear it).

| Key | Where | Action |
|---|---|---|
| `?` | everywhere | This help: the keys of this page |
| `Esc` | everywhere | Close the help, the tests popover or the go-to hint |
| `/` | pages with a search or filter box | Focus it (the source view's file list filter, the tests page's filter; the report's search) |
| `g o` | everywhere | Go to the repository overview |
| `g t` | everywhere | Go to the tests page |
| `g m` | everywhere | Go to the coverage map |
| `g e` | when the economy page exists | Go to the economy page |
| `g i` | everywhere | Go to the invariants page |
| `r` | pages that show a run | Open that run's page (the map and the source view show the newest baseline unless `?run=` picks one) |
| `j` / `k` | source view | Next / previous line that never ran or took only one branch (a multi-line statement's continuation lines are skipped: they belong to its first line) |
| `n` / `N` | source view | Next / previous function with an input class no test passed in |
| `t` | source view | Tests that ran the current line |
| `i` | source view | Show or hide the current function's shape grid |
| `h` | source view, function page | Highlight every measured line, or only what never ran or took one branch |
| `.` | source view, function page | Show or hide the input-class chips under def lines |
| `[` / `]` | source view | Previous / next file |
| `m` | coverage map | Next colour mode: Gap, Lines, Input classes |
| `M` | coverage map | Next module-level code weight: full, thin, hidden |

`g` waits 1.5 s for its letter and says so in a small hint at the bottom right ("g … o t m i"). The
go-to targets are never URLs in the script: they are the links in the help dialog (`data-go` on
each; the economy row wraps the economy feature's own nav include, so it exists exactly when that
page does) and, for `r`, the run link the page itself carries (`data-go="r"`): the source view,
the map, the function page and the test page (the run they show), the PR page ("run page"), the
repository page ("Latest baseline run") and the dashboard (the newest live run, else the newest
recent run). The economy include is exactly one anchor with no `id` (the help dialog copies it
into its row, and an `id` would then be on the page twice).

## View settings

How a page paints coverage is a view setting with four agreed keys, resolved per reader by the
scoped settings (`user > repo > org > instance > default`, `coverset/settings_scopes.py`; a user may
override every one):

| Key | Values (default first) | Query parameter | Key | What it changes |
|---|---|---|---|---|
| `view.highlight` | `all`, `missing` | `?highlight=` (source view, function page) | `h` | `all` paints every measured line; `missing` keeps the vermilion and dotted-yellow treatment and drops the blue bar of executed lines to a faint gutter mark, so what never ran is all that stands out |
| `view.map_mode` | `gap`, `lines`, `inputs` | `?mode=` (map) | `m` | The map's colour |
| `view.map_module_weight` | `thin`, `full`, `hidden` | `?modules=` (map, repository overview) | `M` | How module-level code shows on the map and the overview's thumbnail |
| `view.theme` | `auto`, `light`, `dark` | | header button | Colour scheme (a click on the header button beats the setting in this browser, see below) |

Each page resolves them in this order: the **query parameter**, then what somebody **chose in the
scoped settings** (`view_prefs`; a value still at its built-in default counts as unset unless the
settings layer says where each value came from, through a `sources` map), then the reader's own
choice in **this browser** (`localStorage`: `coverset-highlight`, `coverset-map`,
`coverset-theme`, `coverset-chips`; the map choice also in the `coverset-map` cookie, below), then
the **default**. The server writes its answer and where
it came from into `<meta name="coverset-view" data-highlight="missing"
data-highlight-from="query|prefs|default" ...>` before `app.js`, a synchronous script in the head:
it sets `data-highlight` (and `data-theme`, `data-chips`) on `<html>` before the body is parsed, so
the first paint is already in the right mode (no flash of the other one). A server choice also sits
on the source frame (`<div class="src-frame" data-highlight="missing">`), so it holds without
script. The map is drawn on the server, so a reader's map mode and module weight (from `m`, `M` or
the switches) also go into a small first-party cookie, `coverset-map=<mode>.<modules>` (`SameSite=Lax`,
a year; the server accepts only the listed values). The map page and the repository overview read it
after the query parameter and the scoped settings (source `reader`), so the first request already
draws the reader's map and the overview's Gap thumbnail follows the same module weight: no second
render, no reload. (A browser that saved a choice before the cookie existed gets the cookie written
from `localStorage` on its next page, and reloads that one map once.)

The **theme** is the one view setting a reader changes in the header rather than by a key: a click
cycles from the theme on screen (auto → light → dark), and it beats `view.theme` from the scoped
settings in this browser. The script stores the pick together with the settings value it overrode
(`coverset-theme-over`), so the pick holds until somebody changes the setting; a new setting value
wins again until the reader clicks once more.

Toggling with `h` (or the "Highlight" button in the source bar, `aria-pressed`, both labels stacked in
one grid cell so the button never changes width) flips one attribute, stores the choice, and adds
`highlight=` to the links the page built (`data-keep-view`: previous / next file, the file list) and
to the address, so the next page and a reload paint the same. An explicit `?highlight=` is kept by
the server in the same links.

### Missing coverage as regions

A never-run block reads as one region, not confetti: the blank, comment-only and other stateless
lines (`else:`, a docstring) **between** statements that never ran get the `gap` class and the
block's vermilion bar and lighter tint (`render/source.py` `gap_lines`: a maximal run of lines whose
statements all never ran, with the stateless lines inside it; a partial, executed or excluded line
ends it, and stateless lines at its edges stay out). Continuation lines of a multi-line statement
already take their statement's state (see [The source view](#the-source-view)). Both modes paint
gap blocks; `missing` mode is where they matter most. A range render (the function page) looks just
past its range to find the block it sits in.

### Module-level code on the map

Imports, constants and `def`/`class` lines run when a module is imported, so module-level code
reads as covered whatever the tests do: drawn at full size, a file of constants or a large
`__init__.py` is a big blue (or "no gap") tile that says nothing about the tests, and it inflates the
covered area. `view.map_module_weight` decides how it shows (`render/treemap.py` `weigh_modules`):

* **thin** (default): each `<module>` leaf gets the smaller of its statements and 10% of its file's
  function statements (at least 1), drawn with a dashed outline, its hover text saying "shown
  reduced: drawn as 6 of 40 statements", and the legend lists "module-level code shown reduced"
  (its hover text gives the reason above). A file of nothing but module-level code keeps a sliver.
* **full**: area = statements, as before.
* **hidden**: module leaves are left out of the layout; a file's area is its function statements
  and its hover text still reports "module-level code: 40 statements, 100% ran (hidden from the
  map)". A file of nothing but module-level code disappears.

The numbers in every hover text and in the "Largest gaps" table stay the real statement counts; only
areas change. The repository overview's Gap thumbnail follows the same setting (`?modules=` there
too).

### A map that holds still

The map uses an **ordered** layout by default (`render/treemap.py` `ordered_split`): siblings keep
a fixed order (directories and files by path, a file's functions in source order with module-level
code first, path as the tie-break); the list is cut where the running weight is closest to half and
the rectangle across its longer side in proportion, recursively. Squarified layouts (`?layout=
squarified`) sort by size, so a function that grows a little can swap places with one across the
map. Measured as the share of function tiles whose centre moves more than 5% of the map (Euclidean,
in coordinates scaled to the map's width and height; `centre_moves`) at 1200 × 620 with thin module
weight:

| Comparison | Squarified | Ordered |
|---|---|---|
| The demo's 5 pairs of consecutive baselines (each adds functions and files: 15 → 53 tiles) | 62.0% (111 of 179) | 56.4% (101 of 179) |
| PR #1 (adds `refund.py` and a function) against the latest baseline | 71.7% (38 of 53) | 22.6% (12 of 53, anchored: all in `billing/`) |
| PR #2 (changes one function's body) against the latest baseline | 11.3% (6 of 53) | 0% (anchored) |
| A real 4,015-tile repository, ±10% statements on 10% of its functions (5 trials) | 3.2% | 0.1% |
| The same, a new 10-statement function in 20 of its files (3 trials) | 21.6% | 2.8% |

On the demo's baselines most tiles move with either layout: between consecutive baselines the
repository grows by whole files, and a map that keeps proportional areas has to make room. When the
sizes change a little (the usual commit) the ordered layout barely moves.

**A PR run is drawn in its baseline's frame** (`anchor_to`): every directory, file and function the
repository's latest baseline has keeps the baseline's area, whatever its children add up to now; a
function the PR adds takes a share of its file's area. The map of a PR run is therefore the
baseline's picture with the PR's colours, and only the inside of files or directories the PR changed
moves. The page says "layout from baseline `77e37dc`: the baseline's rectangles, this run's colours"
(linking to the baseline's map) whenever that changes any size.

## Speed

Pages stay light: no per-line or per-tile event listener (everything is delegated from `document`),
no layout thrash (the scripts read geometry only on a key press; `j`/`k` find the line at the top
of the viewport by a binary search), and nothing that moves once painted: fixed gutter columns, the
highlight toggle sized for both labels, the theme button sized for its longest label, the
status line above a map reserving its height, and the lens menu of the report at a fixed width.

* **Source lines** skip their style, layout and paint until they are near the viewport:
  `content-visibility: auto` with `contain-intrinsic-size: auto 0px auto 20px` (every line is one
  20 px row, so the scroll height and `#L` anchors are exact before a line is first drawn). The sticky
  `def` lines stay real so they can stick. The width of the block is set from the first paint by an
  invisible, zero-height copy of the widest line (its text and its branch note, first in
  `.src-lines`, `aria-hidden`): without it an off-screen line counts with its placeholder width, and
  the horizontal scroll width grew each time a long line below the fold was first scrolled into view. Rows of tables are left alone (skipped rows would let
  column widths change while scrolling); a page marks whole panels below the fold `.lazy-panel`
  instead (the report's file, gap and test tables).
* The report's data-flow lens builds its menu from the page's data block the first time it is used,
  instead of shipping one `<option>` per test (4,261 of them, 610 KB, on the largest file below).

Measured with the static report of a real 4,015-function Flask code base (`coverset report` on its
`.coverset` bundle; Chromium headless shell, 1440 × 900, median of 7 loads from `file://`, the same
stylesheet and line markup the live source view uses; before and after measured back to back):

| Page | Before | After |
|---|---|---|
| `spreadsheets.py`, 2,205 lines: first contentful paint / DOMContentLoaded / longest task | 104 / 120 ms / 77 ms | 68 / 80 ms / none over 50 ms |
| `sample.py`, 1,623 lines, 4,261 tests touch it: FCP / DOMContentLoaded / longest task | 140 / 185 ms / 103 ms | 68 / 75 ms / none over 50 ms |
| The report's index, 11 MB, 4,520 tiles per map, 5,081 test rows: DOMContentLoaded / longest task | 602 ms / 207 ms | 239 ms / none over 50 ms |

Cumulative layout shift is 0 on all of them. On the server, rendering `spreadsheets.py` (Pygments
lexing included) takes 63 ms; from the per-process lexing cache 3.8 ms (3.2 ms before gap blocks).
The function page and the PR page's cards render the function's hunk from the same cache, so a
file is lexed once per process, not once per card. Pages with thousands of links that differ in one
value (the map's tiles, the tests page's rows) build each URL from a prefix made once per page
(`repos.url_builder`, the same strings `url_for` returns), and the tests page writes its rows in one
template loop, 500 to a page.

## Theming and accessibility

* `:root { color-scheme: light dark }` and every colour token defined once with `light-dark()`;
  the header's theme button cycles auto → light → dark from the theme on screen, stored in
  `localStorage` (`coverset-theme`) and applied as `<html data-theme>` before first paint; a click
  beats `view.theme` from the scoped settings in this browser (see "View settings").
* Colour is reserved for data. Coverage uses Okabe-Ito: **blue** `#0072B2` = ran, **vermilion**
  `#D55E00` = never ran (with a heavier edge), **yellow** `#F0E442` = took one branch (dotted
  edge). Status chips always pair colour with a symbol and a word (✕ FAIL, ✓ PASS, dashed ✓ PASS
  with gaps); invariant chips likewise (✓ ✕ ? ≈ …), and so do the invariant history squares (a
  glyph and a title each, captioned "oldest → newest"). The coverage lens reads "linked tests: N ·
  never reached:" followed by dashed chips, without the invariant's key repeated. Score components
  use their own categorical colours, never the coverage or status hues, and so do the source view's
  muted syntax colours (`--syn-*`, see [The source view](#the-source-view)). `--muted` text keeps
  at least 4.5:1 on the page background.
* System font stacks; tabular numerals everywhere; visible `:focus-visible` rings; shape-grid
  squares and gutter buttons are keyboard reachable; charts carry `<title>` hover text and the
  treemap and timeline have text equivalents (the gaps table, a hidden milestone list).
* Print: navigation and buttons hidden, sticky elements static, colours kept.

## Live updates

One `sse-connect` per page (the run page, for runs that are not finished or when replaying), with
named events `head`, `timeline`, `gantt`, `failures` and a final `done` that closes the
connection (`sse-close="done"`). Each event's data is the region re-rendered on the server and is
swapped with `hx-swap="morph:innerHTML"`, so the DOM is patched, not replaced. Live streams poll
`run_events` every 0.5 s (the helpers and limits are shared with the `/v1` stream in
`web/api.py`), re-render at most every 0.15 s, send `: ping` every 15 s, carry `id: <seq>` for
`Last-Event-ID` resume and end after 15 minutes (the browser reconnects). Replays re-emit the stored
events with their original spacing divided by `speed` (pauses longer than 5 s compressed).

## Rendering safety

Jinja autoescaping is on and templates never use `|safe`: only `web/render/*` builds `Markup`, and
every value it interpolates is escaped (attributes by hand, since Pygments leaves quotes in token
text alone). URLs pass `safe_href` (same-site paths and http(s) only). Markdown goes through
markdown-it-py with `html=False`, images off and links limited to http(s); the sticky comment's
fixed `<details><summary>` skeleton is rebuilt from whole lines and its hidden marker dropped.
`tests/web/test_render_xss.py` feeds `<script>`, `"onmouseover=` and `javascript:` to every
renderer, including `</span>` and `"onmouseover=` inside string and docstring tokens of every lexer
and a hostile qualname in a treemap tile's `data-info`. The test page's flow renderer (`render/flow.py`) and the page itself (hostile nodeids, failure text and example reprs) are tested the same way in `tests/web/test_web_testpage.py`. The CSP (`script-src 'self'`) forbids inline and eval'd script, so the htmx config sets
`allowEval: false` and no `hx-on` or trigger filters are used. Pages need the admin token whenever
one is configured; the quarantine toggle and approval are POSTs carrying the CSRF token from
`hx-headers` on `<body>` (or a hidden form field).

## Screenshots (for `scripts/screenshots.py`)

Start the demo, which seeds the repository, runs the history and both PRs, and serves:
`COVERSET_DATA_DIR=<dir> uv run coverset demo --executor local --port 8787`. Use a 1440×900
viewport, device scale 1, and set the colour scheme per shot (`color_scheme="light"|"dark"` on the
Playwright context).

**Ready flag**: every page sets `document.body.dataset.ready` to `"1"` once the page has loaded
and htmx has no request in flight (it drops back to `"0"` while a request runs; a request is
settled by `htmx:afterRequest` only, which htmx fires after errors, aborts and timeouts too). Wait with
`page.wait_for_selector('body[data-ready="1"]', state="attached")`. Do not use
`page.wait_for_function("...")` with a string: the CSP forbids eval and the call fails. `/setup`
has no scripts and is rendered with `data-ready="1"`.

**Run ids**: take them from the database (`psql` on `COVERSET_DATABASE_URL`), e.g.
`SELECT r.id FROM runs r JOIN pull_requests p ON p.id = r.pr_id WHERE p.number = 1 ORDER BY r.created_at DESC LIMIT 1`,
or follow the "run page" link on `/repos/1/prs/1`. The demo repo is id 1.

| Shot | URL | Notes |
|---|---|---|
| `pr.jpg` (hero) | `/repos/1/prs/2` | PASS with gaps; the Test tax card (within threshold, the new tests with the lowest leverage) fills the viewport under the head, and the "Needs attention" heading starts at the bottom edge (its first card, the changed `check_if_new_user_signup_allowed`, is open below the fold). `/repos/1/prs/1` is the red, fast story. |
| `run-live.jpg` | `/runs/<PR 1 run>?replay=1&speed=0.25&freeze=<F>` with **F = timeline.verdict + 0.1** | F is the run's `timeline.verdict` in seconds from `timeline.webhook_received` (else `timeline.queued`), plus 0.1, rounded to 2 decimals (the demo's PR 1 lands near 1.1). Wait for `[data-replay="frozen"]` (attached; about 5 s at ¼ speed): FAIL posted, the first-failure rule across the lanes, the failing test pinned in "Why this order", lanes partly filled, queued tests hollow. |
| `why-order.jpg` | `/runs/<PR 1 run>` | Clip to `#why-order` (the panel with its heading); the failing test is pinned "ran 1st; file order would have run it 66th · tied at 8.0 with 2 others; …" and one muted row sums up the tests with no risk signal. |
| `shapes.jpg` | `/repos/1/functions/<validate_email func_key>?run=<PR 2 run>` | The History panel on top (six baselines and the PR run as the last square), then the shape grid (two hollow email classes) and flow ribbon; the Hypothesis box is below the fold. The func_key is in the card links on `/repos/1/prs/2` (`/prs/2/functions/<key>`). |
| `source.jpg` | `/repos/1/files/src/ledger/durations/parse.py?run=<PR 2 run>` | Chips under `def parse_duration`; then click `#L18 button.gt` and wait for `#line-tests-body .test-list` to show the tests popover. Shot at 1440×1000 and clipped just under the last test row that is fully visible in the popover (the popover scrolls inside a capped height, so its own bottom edge would cut a row in half): at 900 px the popover opens above line 18 and covers the chips. |
| `map.jpg` | `/repos/1/map` | Gap mode is the default. Full-page shot clipped just above the "Largest gaps" table, so the whole treemap is in it. The pointer is parked at (1, 1) after every navigation: Playwright keeps it where the source shot's click left it, and a treemap tile under it would show its hover outline. |
| `comment.jpg` | `/forge/outbox?method=upsert_pr_comment` | The rendered sticky comments. |
| `repo.jpg` | `/repos/1` | The repository overview: tiles, the two trend sparklines over the six baselines, what changed since the previous baseline (`merge_intervals` gained `intervals:single`; the demo's sample cap can add a capped function), the Gap map thumbnail and the largest gaps. Full page clipped just under the overview section (`#overview`). |
| `tests.jpg` | `/repos/1/tests` | The tests page, riskiest first: the sort switch, the filter box under it and the first rows of the table (PR #1's failing test and the flaky clock-skew test on top). Viewport shot. |
| `test.jpg` | `/repos/1/tests/by-nodeid?nodeid=tests/test_refund.py::test_refund_partial&run=<PR 1 run>` | Follows the 302 to `/repos/1/tests/<id>?run=<PR 1 run>`. Wait for `body[data-ready="1"]` and `#test-flow svg.flow-test`. PR #1's failing test: failed, `cents_conserved` violated, the data flow test → `refund` → `allocate_cents` and both functions' strips. Full page at 1440 wide (about 1330 px tall). |

Times on pages are relative to `settings.now()`, so `COVERSET_FAKE_NOW=2026-10-11T14:00:00Z` pins
"3 min ago" texts for stable shots. Set it for the serving process only after the demo data
exists (runs executed under a pinned clock would get zero-length timelines).
