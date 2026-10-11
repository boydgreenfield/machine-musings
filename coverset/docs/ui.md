# The web UI

Server-rendered pages from the control plane (Quart + Jinja), made live with htmx 2.0.11, its SSE
extension and idiomorph, all vendored under `src/coverset/web/static/vendor/` (pinned, SRI-checked;
see the README there). No build step, no Node, no inline scripts. Code: `src/coverset/web/`
(blueprints), `templates/`, `static/app.css` and `static/app.js`, and `render/` (the only modules
that produce trusted markup).

## Pages

| Route | What it shows |
|---|---|
| `/` | Dashboard: repositories, live runs (queued, running, awaiting approval), the last 20 runs with verdict chips, and a few numbers (runs, tests, median time to the first test result and to the first failure). |
| `/repos/<rid>` | Pull requests with their latest run, default-branch runs, links to the map, tests and invariants. |
| `/repos/<rid>/prs/<n>` | The PR page (latest run for the PR head; `?run=` picks another run of the PR). `/runs/<run_id>/coverage` redirects here. |
| `/repos/<rid>/prs/<n>/functions/<func_key>` | Fragment: one "Needs attention" card's expanded content (loaded when the card opens). |
| `/runs/<run_id>` | Run page: timeline strip, shard Gantt chart, failures, "Why this order", every test. `?replay=1&speed=0.25` replays a stored run; `&freeze=<seconds>` stops the replay at that point of the run's own clock. |
| `/runs/<run_id>/events` | The run page's SSE stream (HTML regions as named events). |
| `/repos/<rid>/files/<path>` | Source view. `?run=` picks the run (default: the newest default-branch baseline), `?pr=<n>` uses the PR's latest run and marks changed lines. |
| `/repos/<rid>/files/<path>/lines/<n>/tests` | Fragment: the tests that ran one line (the gutter popover). |
| `/repos/<rid>/functions/<func_key>` | One function: shape grid, flow ribbon, source with coverage, covering tests, invariants on it, AI finding. |
| `/repos/<rid>/functions/<func_key>/shapes` | Fragment: the shape grid; with `&cls=<param>:<class>` one cell's detail. |
| `/repos/<rid>/map` | Treemap, `?mode=gap` (default) `\|lines\|inputs`, `?path=` zooms into a directory or file, `?run=`. A "largest gaps" table shows the same data. |
| `/repos/<rid>/tests` | Every test: runs, fail rate, flaky retries, p50/p95, last failure, quarantine toggle (`POST /repos/<rid>/tests/<test_id>/quarantine`, admin + CSRF). `?sort=risk\|slow\|name`. |
| `/repos/<rid>/invariants` | Every declared invariant: statement, status chip, the last 12 statuses, the coverage lens and per-verification evidence. `?run=<PR run>` adds the PR-vs-base comparison and "Invariants changed by this PR". |
| `/forge/outbox` | Every recorded forge call, newest first; sticky comments and check-run summaries rendered as GitHub-flavoured markdown under "Would be posted to GitHub (fake forge)". `?method=` filters. |
| `/setup` | GitHub App manifest flow, token mode, sandbox settings. Plain HTML, no scripts. |

Every UI route renders only its `main` block when the request carries `HX-Request: true` without
`HX-Boosted`, and the full layout otherwise (responses `Vary: HX-Request`). Unknown repos, runs,
functions and paths are 404; a source path is served only when the run's facts list it, and its text
comes from `git cat-file` on the bare mirror at the run's head commit.

### The PR page

1. **Verdict band** (sticky): FAIL / PASS / PASS with gaps, the reason, then "first failure 1.0 s ·
   test 1 of 100 (file order: 66th, ~1.7 s est.)", patch coverage, changed functions with no test,
   input classes never exercised, invariants violated or untested, and the AI review source
   ("fixtures", always marked advisory).
2. **Needs attention**: one card per changed function, most severe first: an invariant on it is
   violated or a covering test failed, then no test calls it, then lines never ran, then expected
   input classes never passed in. Each card has the NEW/CHANGED badge (TRIVIAL/RENAMED/MOVED with
   "inherited"), test count, line and branch percentages, a mini shape strip, invariant chips and
   the AI chip (labelled by source, advisory). Opening a card shows the diff through the coverage
   formatter (removed lines struck through, changed lines marked `+`), the shape grid, the flow
   ribbon, the top five covering tests and a suggested Hypothesis test with a Copy button (the AI
   review's when it has one, else one generated from the missing classes). The first card is open.
3. **Collapsed sections**: tests (failures first), invariants with their lens, indirect changes
   (deleted functions, class- and module-level edits) plus patch coverage per file, and
   "Invariants changed by this PR".

### The run page

* **Timeline**: webhook received (when there was one), queued, sandbox ready, first test, wave 1
  done, first failure, verdict, finished, in seconds from the webhook (else from queuing). When
  `docs/numbers.json` has `t_cold_path` a grey ghost bar shows the measured cold path on the same
  axis; without the key no bar is drawn.
* **Shards**: one lane per sandbox, one block per test in the order it reported. A block spans
  from the previous report on its shard to its own (the first test of a shard gets the lane's
  median block width, since its report also carries pytest's start-up). Blue = passed, vermilion
  with a heavier edge = failed, dotted yellow = flaky (failed, then passed on retry), grey =
  skipped or xfail, hollow dashed = tests still queued.
* **Why this order**: the first 20 tests by priority, each a stacked bar of its score components
  (fixed legend colours) with its one-line reason; up to three failing tests are pinned on top with
  "ran 1st; file order would have run it 66th".
* **Failures**: expandable tracebacks; whether a retry confirmed the failure, its rank, whether it
  also fails on the base branch.
* An untrusted run awaiting approval shows the Approve run button to admins
  (`POST /runs/<id>/approve`).

## The data-flow overlay

**Input classes** are named blocks of a parameter's input space (`email:multi-label-domain`,
`weights:zero-element`). The catalogue expects a class only when the data or the annotation makes
it relevant: an email-shaped string was seen, so the email sub-classes are expected.

* **Shape grid**: one row per parameter (signature order) plus `return`; one square per expected
  class, grouped by family (str, email, quantity, number, container, elements, order, ...). A
  **filled** square is a class some test passed in; its shade steps with log10 of the samples
  (1, 10, 100, 1000+). A **hollow dashed** square is a class no test ever passed in. The header
  reads "Input classes 8 of 10 · lines 100% · branches 100%". Hollow squares are labelled
  "never exercised" only when no test of the function hit the tracer's sample cap; otherwise the
  wording is "not observed in N sampled calls (K tests hit the sample cap)", because sampling may
  have missed it. A hollow square means "never observed", not "buggy". Clicking (or Enter on) a
  square shows the tests that passed that class in with a redacted example value, or for a hollow
  square the suggested Hypothesis strategy.
* **Flow ribbon**: three columns, test files → in-repo callers ("called from the test" for direct
  calls) → parameters, from the tracer's caller keys. Ribbon width is sampled calls; the shade
  steps with how many distinct input classes those calls carried (1, 2, 3-4, 5+).
* **Inlay chips**: under every `def` line in the source view, always visible:
  `email: str  plain 14 · multi-label-domain 0 · ...`. Filled chips are observed classes with their
  sample counts, dashed chips are classes never passed in. Specific families (email, quantity,
  number, elements, order, ...) are shown when a parameter has them, else every class.
* **Treemap Gap mode**: directory → file → function (module-level code is the `<module>` leaf),
  area = statements. Gap = line coverage % minus input-class coverage % on one hue: the stronger
  the colour, the more of the function ran under tests that never tried whole kinds of input.
  Hatched tiles were never called; grey tiles expect no input classes (module code, functions
  without parameters). Lines and Input classes modes use the blue "ran" ramp.

## Source view keys

| Key | Action |
|---|---|
| `j` / `k` | Next / previous line that never ran or took only one branch |
| `n` / `N` | Next / previous function with an input class no test passed in |
| `t` | Tests that ran the current line |
| `i` | Show or hide the current function's shape grid |
| `[` / `]` | Previous / next file |
| `?` | Keys help |

Keys are ignored while typing
in a form field and with Ctrl, Alt or Cmd held.

## Theming and accessibility

* `:root { color-scheme: light dark }` and every colour token defined once with `light-dark()`;
  the header's theme button cycles auto → light → dark, stored in `localStorage`
  (`coverset-theme`) and applied as `<html data-theme>` before first paint.
* Colour is reserved for data. Coverage uses Okabe-Ito: **blue** `#0072B2` = ran, **vermilion**
  `#D55E00` = never ran (with a heavier edge), **yellow** `#F0E442` = took one branch (dotted
  edge). Status chips always pair colour with a symbol and a word (✕ FAIL, ✓ PASS, dashed ✓ PASS
  with gaps); invariant chips likewise (✓ ✕ ? ≈ …).
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
renderer. The CSP (`script-src 'self'`) forbids inline and eval'd script, so the htmx config sets
`allowEval: false` and no `hx-on` or trigger filters are used. Pages need the admin token whenever
one is configured; the quarantine toggle and approval are POSTs carrying the CSRF token from
`hx-headers` on `<body>` (or a hidden form field).

## Screenshots (for `scripts/screenshots.py`)

Start the demo, which seeds the repository, runs the history and both PRs, and serves:
`COVERSET_DATA_DIR=<dir> uv run coverset demo --executor local --port 8787`. Use a 1440×900
viewport, device scale 1, and set the colour scheme per shot (`color_scheme="light"|"dark"` on the
Playwright context).

**Ready flag**: every page sets `document.body.dataset.ready` to `"1"` once the page has loaded
and htmx has no request in flight (it drops back to `"0"` while a request runs). Wait with
`page.wait_for_selector('body[data-ready="1"]', state="attached")`. Do not use
`page.wait_for_function("...")` with a string: the CSP forbids eval and the call fails. `/setup`
has no scripts and is rendered with `data-ready="1"`.

**Run ids**: take them from the database (`<dir>/coverset.db`), e.g.
`SELECT r.id FROM runs r JOIN pull_requests p ON p.id = r.pr_id WHERE p.number = 1 ORDER BY r.created_at DESC LIMIT 1`,
or follow the "run page" link on `/repos/1/prs/1`. The demo repo is id 1.

| Shot | URL | Notes |
|---|---|---|
| `pr.jpg` (hero) | `/repos/1/prs/2` | PASS with gaps; the first card (the changed `check_if_new_user_signup_allowed`) is open. `/repos/1/prs/1` is the red, fast story. |
| `run-live.jpg` | `/runs/<PR 1 run>?replay=1&speed=0.25&freeze=1.0` | Wait for `[data-replay="frozen"]` (about 4-5 s at ¼ speed): FAIL already posted, lanes partly filled, queued tests hollow. |
| `why-order.jpg` | `/runs/<PR 1 run>` | Scroll to the "Why this order" panel (or clip to it); the failing test is pinned "ran 1st; file order would have run it 66th". |
| `shapes.jpg` | `/repos/1/functions/<validate_email func_key>?run=<PR 2 run>` | Shape grid (two hollow email classes) and flow ribbon. The func_key is in the card links on `/repos/1/prs/2` (`/prs/2/functions/<key>`). |
| `source.jpg` | `/repos/1/files/src/ledger/durations/parse.py?run=<PR 2 run>` | Chips under `def parse_duration`; then click `#L18 button.gt` and wait for `#line-tests-body .test-list` to show the tests popover. |
| `map.jpg` | `/repos/1/map` | Gap mode is the default. |
| `comment.jpg` | `/forge/outbox?method=upsert_pr_comment` | The rendered sticky comments. |

Times on pages are relative to `settings.now()`, so `COVERSET_FAKE_NOW=2026-10-11T14:00:00Z` pins
"3 min ago" texts for stable shots. Set it for the serving process only after the demo data
exists (runs executed under a pinned clock would get zero-length timelines).
