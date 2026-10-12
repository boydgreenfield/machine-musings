# The static report: `coverset report`

`coverset report` turns one test run into a folder of static HTML pages: the rich equivalent of
`coverage html`, for local testing, debugging and everyday use. It needs no server, no database and
no network: open `index.html` from disk (`file://`) or put the folder on any static host. It shows
what coverage.py shows (statements, lines run, partial branches) and what coverset adds: which
tests ran every line, what one test touched, and which kinds of input (input classes) each
function's tests passed in and which they never did.

![The report's overview: totals, the Gap treemap and the file table](report.jpg)

## Generating it

From a plain pytest run with the plugin, run from the repository (a git work tree):

```sh
pytest --coverset-out .coverset --coverset-source mypkg
coverset report .coverset                 # writes .coverset/report/, prints .coverset/report/index.html
coverset report .coverset --open          # and opens it in the browser
```

Without `--repo-root`, a `--coverset-out` directory reads its sources from the current directory, and
only when that is a git work tree that tracks the measured files (see
[what it trusts](#how-it-is-built-and-what-it-trusts)); elsewhere, pass `--repo-root`.

From a CI bundle (the `bundle.tar.gz` a shard uploads, or a neutral bundle directory or archive
from another language's reporter, recognised by its `manifest.json`):

```sh
coverset report bundle.tar.gz --repo-root ~/src/myrepo     # writes ./coverset-report/
```

From a run in the facts store (what `coverset serve` shows; sources come from the repository's
mirror at the run's head commit, exactly as the pages read them):

```sh
coverset report --run run_01M4P5Q4AJ0HJVV6MTJW890P24 [--repo coverset-demo/ledger] --out pr2-report
```

| Option | Meaning |
|---|---|
| `SOURCE` | a `--coverset-out` directory, a `bundle.tar.gz`, or a neutral bundle directory or `tar.gz` |
| `--run RUN_ID` | build from this run in the facts store instead (with `--repo NAME` to check which repository it belongs to) |
| `--out DIR` | where to write; default `SOURCE/report` for a directory source, else `./coverset-report` |
| `--repo-root PATH` | read source files from here (only the files git tracks there when it is a git work tree); default: for a `--coverset-out` directory the git-tracked files of the current directory when it is a git work tree, for an archive or a neutral bundle no source at all. Not with `--run` (an error) |
| `--compare BASE` | compare with this base and add a **Changes** page ([below](#comparing-with-a-base)); BASE is any source the report accepts (a `--coverset-out` directory, a `bundle.tar.gz`, a neutral bundle) |
| `--compare-run RUN_ID` | compare with this run in the facts store instead (with `SOURCE` or `--run`; with `--repo` it must belong to that repository too) |
| `--title TEXT` | the title on every page |
| `--open` | open `index.html` in the browser when done |

The command prints the path of `index.html` on stdout (the page counts go to stderr), so
`open "$(coverset report .coverset)"` works too. Regenerating with the same inputs writes
byte-identical files.

The output directory must be new, empty, or a previous report's: every report writes a
`.coverset-report` marker file at its top, and only a directory holding that marker is replaced.
Anything else is refused, so a mistyped `--out` never deletes someone's files. The pages are written
into a hidden staging directory next to `--out` (`.<name>.coverset-build-*`) that replaces it only
once the build succeeded: a build that fails leaves the previous report as it was.

When no source file was read (no `--repo-root` for an archive or a neutral bundle, or a current
directory that is not the repository), every page carries a "Sources not read" notice saying why,
and the command prints the same line on stderr; the coverage itself is all there.

## What the pages show

| Page | Contents |
|---|---|
| `index.html` | Totals (statements, lines run, branches taken, partial lines, functions and how many a test called, functions with input classes never exercised, tests by outcome); the treemap in **Gap** mode with a **Lines** toggle (the map's encoding, see [ui.md](ui.md#the-data-flow-overlay)); the file table, sortable by every column (statements, run %, never ran, partial, tests touching the file, gap = line % minus input-class %); the functions whose tests never passed in some expected class; every test with its outcome, duration, files touched and its failure message (expand it). |
| `files/<path>.html` | The source through the same renderer as the live source view: blue = ran, vermilion = never ran, dotted yellow = took one branch only (with "never jumped to line N"), sticky function headers, inlay chips under each `def` (filled = observed with its sampled calls, dashed = never exercised). The number in the gutter is how many tests ran the line; click it (or press `t`) for the list, each linking to its test page. Press `i` for the function's shape grid and its input-class table. The **data-flow lens** is above the code. |
| `functions/<key>.html` | The signature with its annotations (or the bundle's declared types), the body with line and branch coverage, the shape grid, the flow ribbon from test files through in-repo callers to parameters, the covering tests with their outcomes, every input class with its redacted example and the tests that passed it in, or for a missing class its Hypothesis strategy with a Copy button, a suggested Hypothesis test, and the sampling note. |
| `tests/<hash>.html` | One test: outcome, duration, failure message, the files it ran (statements it ran of each, linking to the file with the lens on this test), the functions it ran with body lines, and per parameter two strips: the input classes this test passed in next to the function's never-exercised ones, and the function's whole picture. |
| `changes.html` | Only with `--compare` / `--compare-run`: what this run gained or lost against the base ([below](#comparing-with-a-base)). The index then carries a tile ("Changes since base: n lines lost, m classes lost, k tests changed") linking to it, file pages mark newly uncovered and newly covered lines in the gutter, and a function page says in one line what changed for it. |
| `about.html` | Where the sources were read from and how many, producer, language, Python and coverage.py versions, tracer, the shape tracer's caps, what was dropped while reading the bundle (limits hit), the functions that hit the sample cap, and the bundle's meta verbatim (up to 200,000 characters). |

A file listed in the bundle but not read (missing under the repository root, not a regular file,
larger than 4 MiB, resolving outside the root, not tracked by git there, or no source read at all) is
shown as "source not available" with its coverage summary only (statements, the lines that never
ran, the partial branches, the tests that touched it); the about page counts the files read. Function
regions come from the sources (for Python) or from the bundle's own `functions.jsonl` (neutral
bundles), so a Python file without its source has no function pages.

Input classes are counted exactly as on the pages, with the same wording. The catalogue comes from
the type descriptors the bundle ships for the function whenever it ships them (a neutral bundle's
`functions.jsonl` `types`, or a converted Python bundle's), whether or not the source is there; only
a function without shipped descriptors gets `function_catalogue(region, source)` over the file's
annotations and enums. A class is "never exercised" only when no test of the function hit the shape
tracer's sample cap, else "not observed in N sampled calls (K tests hit the sample cap)"
([input-classes.md](input-classes.md#never-exercised-or-not-observed)).

A test's outcome is its result row's; a test that was collected but never ran reads "not run", and a
test that ran measured lines but has no result row reads "no result". A test page lists at most the
first 200 functions it ran (file order) and a function page at most 200 covering tests (by outcome,
then id), each with a "+N more" line, so the pages grow with tests plus functions rather than their
product; the per-line test lists in the pages' data blocks stay complete.

## Comparing with a base

```sh
coverset report .coverset --compare main-run/.coverset          # this run against a bundle
coverset report --run run_PR --compare-run run_MAIN --out pr-vs-main
coverset report bundle.tar.gz --repo-root . --compare-run run_MAIN   # mixing is allowed
```

![The Changes page of the demo's PR #1 against the main baseline at its base commit](report-changes.jpg)

The **Changes** page answers "what did this change gain or lose?": the totals as `base → now (±n)`
(statements, lines run and never ran, branches and branches taken, partial lines, functions and
functions called by a test, input classes observed and expected, tests and tests by outcome); per
file the lines newly uncovered and newly covered, linking to the line, files added and removed,
sorted by lines lost; per function the input classes lost and gained (by name), functions newly
without a covering test, functions new and removed; and the tests added, removed, with another
outcome (passed → failed, ...) or slower (more than 2× and 100 ms longer), linking to their pages.
The command prints `changes since base: n lines lost, m classes lost, k tests changed` on stderr.
"Tests changed" counts added, removed and other-outcome tests; slower ones are listed, not counted.

Only the base's facts are needed. Its sources are read under the same rules as `SOURCE`'s (with the
same `--repo-root`, or from the facts store's mirror at the base's head) and only to index its
Python functions and read their annotations; they are never shown. The pages show the current
sources. A base that is missing or cannot be read is an error naming the base, before anything is
written.

### What the Changes page compares

* **Matching.** Files are matched by path, functions by `(path, qualname, ordinal)`, tests by id.
  Line numbers are never matched across a file that changed.
* **Unchanged or changed file.** A file is *unchanged* when its statement and excluded-line sets are
  equal in both runs and, where the base's own text is known (a facts-store base, read from the
  mirror at its head), the two texts are equal. Two facts-store runs with different head commits
  whose text cannot be read at both count as changed. Two bundles read from the same local tree
  cannot be told apart by text, so there equal statements mean unchanged.
* **Lines of an unchanged file** are compared one to one: *newly uncovered* = a statement that ran in
  the base (under any test or at import) and not now; *newly covered* = the other way round.
* **Lines of a changed file** ("file changed") are compared only inside functions present in both
  runs whose `body_hash` (formatting-insensitive, see `analysis/functions.py`) is equal and whose body
  statements line up after shifting by the `def` line: "body unchanged, coverage changed", with the
  lines in this run's numbering. A function whose body changed is compared by counts ("body changed":
  body statements that ran, base → now). Module-level code of a changed file and new functions are
  not compared line by line; the page says how many statements were left out.
* **Whose function index.** A facts-store base's regions are its own (stored at ingest from its head),
  and so are the regions a neutral bundle ships. A Python bundle base indexed from the *local* tree
  borrows the current text: its regions are used only for files whose statements did not change, and
  never for a "body unchanged" claim; the functions of such a changed file are reported as not
  matchable, with a note.
* **Input classes.** Each side's catalogue is read the way its own report reads it (shipped
  descriptors win, else the source annotations). A class is *lost* when it was observed in the base,
  is still expected, and is not observed now; *gained* the other way round; a class observed in the
  base that is no longer expected (the declared types changed) is listed as "no longer expected",
  not lost. When this run hit the shape tracer's sample cap, the lost class's title says "not
  observed in N sampled calls" rather than "never exercised".
* **Different test sets.** When some tests that ran in the base did not run in this run (removed, or
  not selected: a PR run of a subset against a full baseline), the page says so at the top and lists
  them. A line, input class or covering test of the base whose base tests (or import time) all are
  among them is listed as **not re-run**, never as lost, and gets no gutter mark; only what both runs
  could have run counts as lost. A test "ran" when its outcome is anything but "not run".
* **Covering tests.** A function is *newly without a covering test* when some test ran its body in the
  base, none does now, and some of those base tests ran now.
* **Marks.** A file page marks newly uncovered lines `▾` and newly covered lines `▴` in the diff
  column, both in the legend; a function page shows one "Since base" line when something changed for
  it. The Changes page lists at most 500 rows per table and 120 line links per file and kind ("and N
  more").

## The data-flow lens

![parse.py with the lens on one test and line 18's tests open](report-source.jpg)

On a file page, the lens selector lists every test that ran a line of the file. Pick one and the
lines it did not run are dimmed: what is left is what that test touched, statement by statement,
including the module-level code it did not need. The line under the selector says how many lines it
ran. A test page links to each file it touched with the lens already on (`files/<path>.html#lens=<test
hash>`), so "what did this test exercise, and with which inputs" is two clicks: the test page's
strips for the inputs, the lens for the lines.

## Keys

| Key | Action |
|---|---|
| `j` / `k` | Next / previous line that never ran or took only one branch |
| `n` / `N` | Next / previous function with an input class no test passed in |
| `t` | Tests that ran the current line (again to close) |
| `i` | Show or hide the current function's shape grid |
| `[` / `]` | Previous / next file |
| `/` | Search files, functions and tests (arrows and Enter to pick, Escape to leave) |
| `?` | Keys help |

Keys are ignored while typing in a field and with Ctrl, Alt or Cmd held. Clicking a square of a
shape grid highlights its row in the input-class table under it. The theme button cycles auto →
light → dark (stored in `localStorage`, key `coverset-theme`); colours come from the pages'
stylesheet (`light-dark()` tokens, Okabe-Ito coverage colours, filled vs hollow dashed squares).

## How it is built, and what it trusts

The report reuses the pages' code rather than re-implementing it: the bundle becomes a
`RunFacts` (`coverset.report.facts.BundleFacts`, the pages' facts view over in-memory tables
instead of Parquet), so line states, partial branches, covering tests, function views and
`cap_hits` come from the same functions; the markup comes from `coverset.web.render` (source view,
shape grid, inlay chips, ribbon, treemap). `report.css` is the pages' `app.css` plus a few report
rules; `report.js` is one file with no dependencies.

Every string in a bundle is untrusted (test ids, failure messages, value reprs, paths, the meta, the
source text itself):

* text reaches HTML only through Jinja autoescaping or the render helpers; links pass `safe_href`,
  which during a report build also admits relative links made of plain segments ending in `.html`
  (with a fragment of `[A-Za-z0-9._~:=-]`, and any number of leading `../`, so the rule alone does
  not keep a link inside the report: the links the build writes are its own page names) and bare
  `#fragment`s; anything with a scheme, `//`, a backslash or a quote becomes `#`;
* per-page data (the tests behind each line) is a single `<script type="application/json">` block
  with `<`, `>` and `&` escaped; the search index is `search-index.js` (JSON, same escaping). There
  is no other inline script, and every page carries a Content-Security-Policy with no inline script,
  no `<base>` and no form submission. Served over HTTP it admits scripts and stylesheets only from
  the report's own origin; opened from disk, `file:` in the policy admits any local file, so there
  the guarantee is the escaping above (no bundle string becomes markup), and the policy only stops
  inline script;
* bidirectional control characters (Trojan Source) are written as visible `<U+202E>` markers on every
  page and in every data file;
* a path that is not a plain relative path of simple segments (`../x.py`, an absolute path, quotes,
  backslashes, a dot segment, a first segment `_`, which `files/_/` reserves) gets a hashed page name
  (`files/_/<hash>.html`), and so does a path whose page, or any directory on its way, would collide
  case-insensitively with an earlier page or directory (`x` next to `x.html/y`, `Src/a.ts` next to
  `src/b.ts`); a hashed name that is taken is re-salted; every write is checked to resolve inside the
  output directory;
* the bundle never chooses which local files are read: its `git_root`, `rootdir` and `repo_root`
  are ignored. With `--repo-root`, sources are read under it, and when it is a git work tree only the
  files git tracks there (both the path and the file a symlink resolves to); without it, a
  `--coverset-out` directory reads the git-tracked files of the current directory when that is a git
  work tree tracking at least one measured file, and an archive or a neutral bundle reads nothing.
  Every read is a plain relative path, symlinks resolved and required to stay inside, a regular file
  up to 4 MiB opened without blocking (a FIFO is skipped), decoded as UTF-8 with its byte-order mark
  dropped (a Python file's coding cookie is honoured). Git runs with no user or system config and
  with `core.fsmonitor` off;
* archives are extracted with the same hardened extractor as ingest (no links, no devices, size and
  member caps), and the bundle readers apply the untrusted-bundle limits of
  [security.md](security.md).

`tests/report/test_report_xss.py` feeds a bundle full of hostile strings and paths through every
page.

## Limits, and what differs from the live pages

* **One run, no history.** No flaky retries, no fail rates or p50 durations over runs, no
  "last failure", no quarantine: those need the database.
* **No pull-request diff.** No changed-line marks, no "Needs attention" cards, no patch coverage,
  no verdict, no "why this order", no AI review. The static report of a PR run shows that run's
  coverage of the head commit; `--compare` adds what its coverage gained or lost against a base run,
  not which lines the diff touched.
* **No invariants page.** Invariant links in the bundle are kept in the facts but not shown.
* Source files larger than 4 MiB are shown as "source not available". In repositories with more than
  400 measured files a file page lists the 200 files on either side of it (the overview lists all).
* A test page lists 200 functions and a function page 200 covering tests at most ("+N more").
* The about page shows the bundle's meta verbatim up to 200,000 characters, then says how long it was.
* The search covers file paths, function names and test ids, not file contents.
* `--run` reads the run's sources from the repository's mirror; when the mirror is gone the files
  are "source not available". `--repo-root` does not go with `--run`.

## Screenshots

`uv run python scripts/screenshots.py --only report --data-dir <demo data dir>` builds the report
of the demo's PR #2 run (`coverset report --run`) into `<data dir>/report-shots` and takes
`docs/report.jpg` (the overview, file table sorted by gap) and `docs/report-source.jpg` (parse.py
with the lens on `tests/test_windows.py::test_expires_at` and line 18's tests open); it also builds
PR #1's run compared with the `main` baseline at its base commit (`--run ... --compare-run ...`) into
`<data dir>/report-changes-shots` and takes `docs/report-changes.jpg` (the Changes page: the refund
tests added, one failing, the new `refund` function, `allocate_cents` gaining
`total_cents:negative`, nothing lost). All light and dark, from `file://` at 1440 px wide.
