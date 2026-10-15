# coverset for AI agents: the MCP server and `/v1/agent`

An agent that writes or reviews tests (Claude Code, Cursor, a review bot) can ask coverset the
questions a careful reviewer would ask:

- **Is this change tested?** Which changed lines and functions no test ran, before or after the push.
- **Tested with what?** Which kinds of input (input classes such as `email:multi-label-domain` or
  `total_cents:negative`) each function's tests passed in, and which they never did, with a
  Hypothesis strategy (or a fast-check arbitrary) for each missing one.
- **What do real inputs look like?** Redacted example values per input class, with the test that
  passed each one, for writing fixtures of the right shape.
- **Is this suite tested to death?** The smallest set of tests that keeps every covered line, branch
  and input class (a set cover), tests with identical data flows, tests another test fully
  contains, and what each test costs against what it alone covers.

The same fifteen tools are served three ways: over MCP on stdio for a local `.coverset` directory,
over MCP on stdio for a deployed coverset (the server calls its HTTP API), and as plain HTTP at
`/v1/agent`. Every answer is JSON with stable field names, at most 200 rows per list, and the
caveats that apply to it.

Contents: [Set up](#set-up) · [What the agent gets](#what-the-agent-gets) ·
[Tool reference](#tool-reference) · [Playbooks](#playbooks) ·
[Example transcripts](#example-transcripts) · [How it works](#how-it-works) ·
[Trust notes](#trust-notes) · [Limits](#limits)

## Set up

### Claude Code on your own checkout (no server)

Run the tests once with the plugin, then register the server:

```sh
pytest --coverset                       # writes .coverset/ (see docs/report.md)
coverset mcp install-claude-code        # prints the .mcp.json entry and the `claude mcp add` line
```

It prints (it never writes a file):

```json
{
  "mcpServers": {
    "coverset": {
      "command": "/path/to/coverset/.venv/bin/coverset",
      "args": ["mcp", "serve", "--source", ".coverset"]
    }
  }
}
```

Put that in `.mcp.json` at the root of the project (commit it to share it), or run the printed
`claude mcp add --scope project coverset -- ... mcp serve --source .coverset`. The command is the
absolute path of the `coverset` you ran, so it works from any project; teammates with coverset on
their `PATH` can use plain `coverset`. Restart Claude Code and ask, for example, "use coverset to
review the tests of my change against origin/main".

`--source` takes a `pytest --coverset` output directory, a `bundle.tar.gz` or a neutral bundle
(a TypeScript run from `coverset-vitest`, for instance). The server re-reads it whenever it changes,
so the agent can edit, run `pytest --coverset`, and ask again in one session. Sources (annotations,
and the work tree for diffs) are read from the git work tree the bundle sits in, only files git
tracks; `--repo-root DIR` names another. An archive or a neutral bundle is read under the strict
limits ingest uses unless you pass `--trusted` (the rules of [the static report](report.md)).

### A deployed coverset

The agent runs the server locally; the server calls the deployment's HTTP API with an API token:

1. Create a token with the `read` scope: on the settings page (Account → API tokens), or on the
   server host:

   ```sh
   coverset users tokens create --name claude-code --user YOUR_LOGIN --scopes read
   # or a token bound to one repository:
   coverset users tokens create --name review-bot --repo owner/name --scopes read
   ```

   The token (`cst_...`) is shown once. A personal token sees what you see (the repositories GitHub
   says you may read); a repository token sees one repository.
2. Export it as `COVERSET_TOKEN` in the environment Claude Code starts from, and register the server:

   ```sh
   coverset mcp install-claude-code --server https://coverset.example.com
   ```

   The printed entry passes `"env": {"COVERSET_TOKEN": "${COVERSET_TOKEN}"}`: Claude Code expands
   the variable when it starts the server, so the token itself is never written to `.mcp.json`.
   The printed CLI line is
   `claude mcp add --scope project coverset -e 'COVERSET_TOKEN=${COVERSET_TOKEN}' -- ... mcp serve --server ...`:
   the name comes before `-e` because `-e` takes several values and would swallow it.

`--server` must be `https://` (plain `http://` only for `localhost`). In this mode the tool list and
descriptions come from the server (`GET /v1/agent`), so a newer deployment's tools appear without
upgrading the laptop.

### On the server host itself

`coverset mcp serve --repo owner/name [--run RUN_ID]` reads the facts store of this machine's data
directory directly, as the operator (no per-repository authorization: whoever can run the command
can read the data directory anyway). `--run` sets the default run; without it, the newest completed
default-branch baseline.

### Checking a setup by hand

```sh
coverset mcp tools                                         # the tool names
coverset mcp call get_run_summary                          # ./.coverset, as the agent would see it
coverset mcp call get_function '{"function": "parse_duration"}' --source .coverset
coverset mcp call list_repos --server https://coverset.example.com   # with COVERSET_TOKEN set
```

### The HTTP API

`GET /v1/agent` describes every tool (name, title, description, JSON Schema). `POST
/v1/agent/<tool>` takes the arguments as a JSON object and answers the tool's JSON; `GET
/v1/agent/<tool>?arg=value` does the same from the query string (lists as `a,b`).

```sh
curl -s -H "Authorization: Bearer $COVERSET_TOKEN" https://coverset.example.com/v1/agent | jq '.tools[].name'
curl -s -H "Authorization: Bearer $COVERSET_TOKEN" -H 'Content-Type: application/json' \
  -d '{"function": "allocate_cents"}' https://coverset.example.com/v1/agent/suggest_tests
```

| Status | `error` | When |
|---|---|---|
| 200 | | the tool's JSON |
| 400 | `invalid_argument`, `ambiguous` | a bad argument (the message says which); an ambiguous function name lists `candidates` |
| 401 | `unauthorized` | no credentials, an unknown or revoked token |
| 403 | `insufficient_scope` | a token without `read`, `write` or `admin` (an `upload`-only CI token) |
| 404 | `not_found`, `unknown_tool` | no such tool, run, function or test, or a repository the caller cannot see (the same answer as one that does not exist) |
| 429 | `rate_limited` | more than 120 calls a minute for this token, user or admin; `Retry-After` says when |

Every error body is `{"error": ..., "message": ..., **details}`. The API is not a public route:
the access guard admits the admin token, a signed-in user's session (a browser `POST` also needs the
CSRF header) or a `cst_` token, and every repository a tool touches is checked with `read`
permission.

## What the agent gets

- **Fifteen tools**, read-only, idempotent and closed-world (the MCP annotations say so): nothing
  here changes a repository, a run or a file, and nothing reaches the internet.
- **Resources**: `coverset://docs/mcp` (this page), `coverset://docs/input-classes`
  ([input-classes.md](input-classes.md)), `coverset://run/summary` (the default run's summary) and
  the template `coverset://runs/{run}/summary`.
- **Prompts** (the [playbooks](#playbooks)): `review-tests-for-pr`, `bootstrap-tests-for-function`,
  `shrink-test-suite`.
- **Instructions**: one paragraph the client shows the model at connection time: where to start,
  and that removal results are candidates, missing classes mean "never observed" and examples are
  redacted.

A tool result arrives twice in the MCP message: as `structuredContent` (the JSON object) and as
indented JSON text for clients that only show text. A failed call is an error result whose text is
the error JSON, so the agent can read `candidates` or `similar` and try again.

Names an agent passes:

- **run**: a run id from `list_runs`; omit it for the default run (the server's `--run`, else the
  newest completed default-branch baseline; a local bundle has one run, `local`).
- **function**: `validate_email`, `Invoice.tax_cents`, `src/ledger/billing/allocate.py::allocate_cents`,
  `src/ledger/durations/parse.py:16` (the innermost function holding that line) or a 16-hex
  `func_key`. An exact qualname wins over a method of the same bare name; a name that still matches
  several functions fails with `ambiguous` and the candidates.
- **nodeid**: exactly as the test runner wrote it.

## Tool reference

Generated from the server's own tool definitions (`uv run python -m coverset.agent.docs` rewrites
this block; a test fails when it is stale).

<!-- tools:start -->

### `list_repos`: List repositories

The repositories you may read, each with its default branch and newest run. Start here when talking to a deployed coverset; a local .coverset directory lists one pseudo-repository.

No arguments.

### `list_runs`: List runs

Recent runs of one repository, newest first: id, kind (`pr` for any pull-request run, else the trigger: `baseline`, `push`, `manual`, `rerun`, `upload`; `local` for a local bundle), branch, PR number, head commit, status and verdict. Use a PR run's id with get_patch_coverage and minimal_test_set(scope='changed').

| Argument | Type | Default | Description |
|---|---|---|---|
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `kind` | `any` \| `pr` \| `baseline` | `any` | Only pull-request runs, only default-branch baselines, or both. |
| `limit` | integer 1-200 | `20` | Rows per page (1 to 200). |

### `get_run_summary`: Run summary

Headline facts of one run: identity (PR, branch, commit), verdict, test outcomes and total duration, the slowest tests, line and branch coverage, input-class totals, and for a PR run its patch coverage. Call it first to orient yourself; the `next_steps` field suggests which tool answers the obvious follow-up.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |

### `get_patch_coverage`: Patch coverage

What a change touched and whether tests ran it: patch coverage (changed statements that ran), per-file missing lines as ranges, every changed function with its covering-test count, body line % and the input classes no test passed in, and the changed functions no test ran at all (`zero_test_functions`). For a PR run the diff is merge-base...head; for a local .coverset directory pass `base` (a git ref such as `origin/main`) and the diff is merge-base...working tree, so you can check a change before pushing. If `stale` is true, files changed after the tests ran: run `pytest --coverset` again. Input classes come from sampled argument shapes: a class reported missing was never observed in the sampled calls, which is strong evidence but not proof that no test passes such a value.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `base` | string |  | Local .coverset only: the git ref to diff the working tree against (for example `origin/main`). Ignored for a facts-store run, whose PR base is known. |
| `limit` | integer 1-200 | `50` | Rows per page (1 to 200). |
| `cursor` | string |  | The `next_cursor` of a previous page of the same call; omit for the first page. |

### `get_function`: Function details

Everything coverset knows about one function in a run: its lines, body line and branch coverage with the lines that never ran and the branches taken one way only, its input classes per parameter (expected, observed with a redacted example, missing with a Hypothesis strategy), the tests that ran its body, which of them call it directly (from the test) and which reach it through other functions (callers listed). Input classes come from sampled argument shapes: a class reported missing was never observed in the sampled calls, which is strong evidence but not proof that no test passes such a value.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `function` | string | required | Which function: a qualname (`validate_email`, `Invoice.total`), `path::qualname`, `path:line` (the innermost function whose lines contain it) or a 16-hex func_key. An ambiguous name fails with the candidates listed. |
| `limit` | integer 1-200 | `20` | Covering tests and callers listed (fastest tests first; 1 to 200). |

### `find_gaps`: Largest input-class gaps

Functions that tests call but whose tests never pass in whole kinds of input, most missing classes first (or by ratio, or by body size). Each row names the missing classes. Use it to choose where a new test buys the most; follow up with get_function or suggest_tests. Functions no test called are left out unless `include_uncalled` is true. Input classes come from sampled argument shapes: a class reported missing was never observed in the sampled calls, which is strong evidence but not proof that no test passes such a value.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `sort` | `missing` \| `ratio` \| `size` | `missing` | `missing`: most missing classes; `ratio`: lowest observed/expected; `size`: largest body first among functions with any gap. |
| `path` | string |  | Only functions whose file path starts with this prefix. |
| `include_uncalled` | boolean | `false` | Also list functions no test called (every class is then missing). |
| `limit` | integer 1-200 | `20` | Rows per page (1 to 200). |
| `cursor` | string |  | The `next_cursor` of a previous page of the same call; omit for the first page. |

### `get_covering_tests`: Tests covering a line or function

The tests that executed a line (`path` + `line`), a function's body (`function`), or any line of a file (`path` alone), with each test's outcome and duration, fastest first. Import-time execution is not a test; `import_time` says whether module import also ran the line. Use it to pick the few tests to run after an edit.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `path` | string |  | Repository-relative file path. |
| `line` | integer |  | A line of `path`. |
| `function` | string |  | Which function: a qualname (`validate_email`, `Invoice.total`), `path::qualname`, `path:line` (the innermost function whose lines contain it) or a 16-hex func_key. An ambiguous name fails with the candidates listed. |
| `limit` | integer 1-200 | `50` | Rows per page (1 to 200). |
| `cursor` | string |  | The `next_cursor` of a previous page of the same call; omit for the first page. |

### `get_test`: Test details

One test: outcome, duration, failure message (redacted, cut at 2,000 characters), the files and functions it ran (lines run out of the body's statements), the argument shapes it passed in (input classes reached, redacted examples, whether it called the function itself), and its outcome in recent runs (facts store only). Nodeids are exactly as the runner wrote them.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `nodeid` | string | required | The test id, e.g. `tests/test_parse.py::test_parse_duration[90s-90]`. |

### `example_inputs`: Redacted example inputs

Example argument values per input class of one function, each with the test that passed it, for bootstrapping fixtures and new tests. Example values are redacted reprs captured by the tracer (email local parts masked, secret-named parameters hidden, long strings cut). They show the kind of value a test passed, never raw data. Missing classes come with the strategy to generate them instead. Read `how_to_use` before quoting any of it.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `function` | string | required | Which function: a qualname (`validate_email`, `Invoice.total`), `path::qualname`, `path:line` (the innermost function whose lines contain it) or a 16-hex func_key. An ambiguous name fails with the candidates listed. |
| `param` | string |  | Only this parameter (`return` for return values). |
| `limit` | integer 1-200 | `50` | Rows per page (1 to 200). |

### `suggest_tests`: Suggest tests for missing input classes

Property-based test material for the input classes no test passed in to one function: per missing class its description and a Hypothesis strategy (Python) or a fast-check arbitrary (TypeScript), plus a runnable-looking test skeleton. The skeleton never asserts anything specific: you must write the property (an invariant of the result) yourself. Input classes come from sampled argument shapes: a class reported missing was never observed in the sampled calls, which is strong evidence but not proof that no test passes such a value.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `function` | string | required | Which function: a qualname (`validate_email`, `Invoice.total`), `path::qualname`, `path:line` (the innermost function whose lines contain it) or a 16-hex func_key. An ambiguous name fails with the candidates listed. |

### `minimal_test_set`: Minimal test set (set cover)

A small set of tests that keeps every covered line, every branch arc (approximated: per-test arcs are not recorded, so an arc counts for a test that ran both its ends) and every observed input class of the scope, chosen by greedy weighted set cover (fewest seconds per newly covered fact, then a pass that drops any kept test the rest already cover). Returns the kept tests, the dropped ones with what each would lose (nothing, unless `budget_s` cut the cover short or the scope hides facts elsewhere), and `deselect_args`: lines for a pytest argument file (`pytest @deselect.txt`). scope `all` is the whole suite, `changed` a PR's changed functions, `path` one file or directory prefix. Results are candidates, not verdicts: two tests that run the same lines and pass in the same input classes may still assert different things. Read both tests before recommending a removal, and never claim a test is useless because of these numbers alone.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `scope` | `all` \| `changed` \| `path` | `all` | What must stay covered. |
| `path` | string |  | With scope `path`: a file path or directory prefix. |
| `base` | string |  | With scope `changed` on a local .coverset: the git ref to diff against. |
| `facts` | list of `lines` \| `arcs` \| `classes` |  | Which facts must be kept (default all three). |
| `budget_s` | number |  | Stop adding tests once the kept tests would take longer than this many seconds (a best-coverage-within-budget set instead of a full cover). |
| `keep_property_tests` | boolean | `true` | Always keep property-based tests (Hypothesis @given, fast-check): they search inputs beyond what the sampled shapes record, so their recorded facts understate them. Tests that did not pass are always kept. |
| `deselect` | `page` \| `all` \| `none` | `page` | What `deselect_args` lists: the dropped tests of this page (default, small), all of them (up to 5,000; for writing the file), or nothing. |
| `limit` | integer 1-200 | `50` | Rows per page (1 to 200). |
| `cursor` | string |  | The `next_cursor` of a previous page of the same call; omit for the first page. |

### `redundant_tests`: Redundant test candidates

Tests that look redundant on coverage evidence: groups of tests with identical facts (same lines and input classes, the same data flow) and tests whose facts another single test fully contains (dominated). Each group suggests the fastest member to keep. Results are candidates, not verdicts: two tests that run the same lines and pass in the same input classes may still assert different things. Read both tests before recommending a removal, and never claim a test is useless because of these numbers alone.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `limit` | integer 1-200 | `20` | Rows per page (1 to 200). |
| `cursor` | string |  | The `next_cursor` of a previous page of the same call; omit for the first page. |

### `test_tax`: Test tax

What each test costs against what it alone covers: its statements (own plus helpers), assertions, fixtures and seconds against its unique facts (lines, branch arcs and input classes no other test reaches); leverage = unique facts per statement. For a PR run, `pr_tax` is the PR test tax: test statements the PR added per newly covered fact, flagged above the threshold (the 95-lines-to-check-one-environment-variable smell). `available` is false, with the reason, when the run has no economy (an older run). Results are candidates, not verdicts: two tests that run the same lines and pass in the same input classes may still assert different things. Read both tests before recommending a removal, and never claim a test is useless because of these numbers alone.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `sort` | `leverage` \| `cost` \| `statements` \| `unique` \| `duration` | `leverage` | `leverage` ascending (costliest per unique fact first), `cost` (ms per unique fact), `statements`, `unique` facts, or `duration`. |
| `only` | `all` \| `assertion_free` \| `removable` \| `unmeasured` | `all` | Only tests without assertions, removable candidates, or tests whose source could not be measured. |
| `limit` | integer 1-200 | `20` | Rows per page (1 to 200). |
| `cursor` | string |  | The `next_cursor` of a previous page of the same call; omit for the first page. |

### `search`: Search functions, files and tests

Case-insensitive substring search over function qualnames, measured file paths and test ids of one run. Use it to find the exact name another tool needs.

| Argument | Type | Default | Description |
|---|---|---|---|
| `run` | string |  | A run id from list_runs. Omit it for the default run: the server's --run, else the newest completed default-branch baseline of `repo` (a local .coverset directory has one run, `local`). `latest` means the same as omitting it. |
| `repo` | string |  | Repository full name (owner/name) or numeric id from list_repos; picks the default run when `run` is omitted. Not needed with a local .coverset directory. |
| `query` | string | required | The substring. |
| `kinds` | list of `functions` \| `files` \| `tests` |  | What to search (default all three). |
| `limit` | integer 1-200 | `20` | Rows per page (1 to 200). |

### `explain_shape_key`: Explain a shape key

A shape key such as `str|ascii|kind:email/plain|len:11-100` in words: the value's type and each feature, plus whether it follows the version-1 grammar. Shape keys appear in get_test, get_function and example_inputs.

| Argument | Type | Default | Description |
|---|---|---|---|
| `key` | string | required | The shape key. |

<!-- tools:end -->

## Playbooks

A tool list says what can be asked. A playbook says what to ask, in what order, how to word the
result and what never to claim. Each is an MCP prompt; the text below is exactly what the agent
receives (generated, like the tool reference).

<!-- playbooks:start -->

### `review-tests-for-pr`: Review the tests of a pull request

Review a PR's tests for reviewability: what the change needs tested, what it tests to death, what it misses. Produces a short review a human can act on.

Arguments: `run`: The PR run id (list_runs kind=pr); omit for a local .coverset., `base`: Local .coverset only: the git ref the change is against (e.g. origin/main).

The message the agent receives (arguments left unset):

```text
Review the tests of this change with coverset. Run: <default>. Base: <default>.

Goal: a review that makes the change easier to merge. Reviewers read every added test line; a test
that re-checks what other tests already check costs review time and buys nothing, while the inputs
nobody tried are where the bugs are.

Fetch, in this order:
1. get_run_summary (run=<default>). Note the verdict, failing tests and the patch numbers.
2. get_patch_coverage (run=<default>, base=<default>). Note zero_test_functions, files with missing_lines,
   and each changed function's missing input classes. If `stale` is non-empty, stop and ask for
   `pytest --coverset` to be re-run.
3. For each changed function with missing classes or no tests: get_function, then suggest_tests.
4. minimal_test_set (scope='changed', run=<default>, base=<default>). The dropped tests are the PR's tests
   that add no line, branch or input class beyond the kept ones.
5. For each dropped test that the PR added: get_test on it and on its `covered_by` test, and read
   both test bodies in the diff. Only now decide whether it is duplication.

Write the review as three short sections:
- **Untested**: changed functions or lines no test ran, each with the input classes to add and the
  strategy from suggest_tests. This section comes first; it is what blocks a merge.
- **Gaps**: changed functions whose tests never pass in some input classes (name the classes).
- **Could be smaller**: added tests that duplicate another test's facts, each with the test it
  duplicates and what it would take to fold them (parametrize, one property test). Phrase every item
  as a question for the author ("Does X check anything Y does not?"), never as "delete X".

Keep it under 40 lines. Quote numbers exactly as the tools give them, with their caveats when you
quote a missing class or a dropped test.

Never claim:
- that a test is useless, wrong or safe to delete because coverage facts say so: coverset sees what a
  test ran and passed in, not what it asserts. Say "covers nothing the kept tests do not" instead.
- that a function is buggy because an input class is missing. Say "no test passes <class>"; only a
  test you ran can show a bug.
- that a class is definitely never exercised when the caveat says the function was sampled. Say
  "not observed in the sampled calls".
- that an example value is real data. Examples are redacted shapes; never try to reconstruct them.

Wherever an argument reads <default>, leave it out of the tool call.
```

### `bootstrap-tests-for-function`: Bootstrap tests for one function

Write the first good tests for one function from its input classes, redacted examples and suggested strategies.

Arguments: `function` (required): The function: qualname, path::qualname, path:line or func_key., `run`: A run id; omit for the default run.

The message the agent receives (arguments left unset):

```text
Write tests for <default> with coverset's help. Run: <default>.

Fetch, in this order:
1. get_function (function=<default>, run=<default>): its body coverage, its input classes per parameter
   (observed with an example, missing with a strategy), the tests that already call it directly and
   the callers that reach it indirectly.
2. example_inputs (function=<default>, run=<default>): read `how_to_use` first. The examples show the
   shape of the values each class means; they are redacted (email local parts masked, long strings
   cut) and are not data to copy.
3. suggest_tests (function=<default>, run=<default>): a strategy per missing class and a skeleton.
4. Read the function's source and its existing tests (the files named in step 1).

Then write tests, smallest first:
- one property-based test per parameter's missing classes, using the given strategies, asserting a
  property that must hold (round trip, conservation, ordering, a documented exception) rather than
  today's output;
- an example-based test only where a property is hard to state; build its inputs in the shape of the
  examples, with values you invent;
- no test for a class the existing tests already exercise unless it asserts something new.
Run the new tests. If one fails, report the failing input and the property; do not "fix" the test to
match the code without saying so.

Finish with: the classes now exercised, the ones still missing and why (for example: not reachable
through the public signature), and the commands you ran.

Never claim:
- that a test is useless, wrong or safe to delete because coverage facts say so: coverset sees what a
  test ran and passed in, not what it asserts. Say "covers nothing the kept tests do not" instead.
- that a function is buggy because an input class is missing. Say "no test passes <class>"; only a
  test you ran can show a bug.
- that a class is definitely never exercised when the caveat says the function was sampled. Say
  "not observed in the sampled calls".
- that an example value is real data. Examples are redacted shapes; never try to reconstruct them.

Wherever an argument reads <default>, leave it out of the tool call.
```

### `shrink-test-suite`: Shrink a test suite (set cover)

Propose a smaller test suite that keeps every line, branch and input class the current one covers, as a reviewable plan.

Arguments: `scope`: all, changed or path (default all)., `path`: With scope=path: a file or directory prefix., `budget_s`: Optional: a time budget in seconds for the kept tests., `run`: A run id; omit for the default run.

The message the agent receives (arguments left unset):

```text
Propose a smaller test suite with coverset. Scope: all. Path: <default>. Budget: <default> s. Run: <default>.

This is the set-cover problem: keep the fewest test-seconds that still reach every fact (line,
branch arc, input class) the suite reaches today. coverset solves it greedily; you turn it into a
plan a maintainer can trust.

Fetch, in this order:
1. get_run_summary (run=<default>): suite size, duration, failing tests (always kept).
2. minimal_test_set (scope=all, path=<default>, budget_s=<default>, run=<default>): kept and dropped
   tests and what each dropped test would lose. Page through `dropped` with `cursor` if there are
   more; ask for deselect='all' once, at the end, for the whole deselect file.
3. redundant_tests (run=<default>): equivalence groups (identical facts) and dominated tests. Groups are
   the best candidates for parametrization rather than deletion.
4. For the ten slowest dropped tests: get_test on the test and on its `covered_by` tests, then read
   their bodies. Keep any dropped test whose assertions check something its covering tests do not
   (a different exception, an output property, a side effect, a regression it was written for).

Deliver:
- a table: dropped test, seconds saved, the kept test(s) covering it, and your judgement after
  reading both (fold / parametrize / keep, with one line of why);
- the deselect file (from deselect_args, minus the tests you decided to keep) for a trial run:
  `pytest @deselect.txt`, then compare its coverage with `pytest --coverset` before anything is deleted;
- the totals: tests and seconds before and after, and the facts lost (should be zero without a budget).
Do not delete or edit test files unless asked; the output is a plan.

Never claim:
- that a test is useless, wrong or safe to delete because coverage facts say so: coverset sees what a
  test ran and passed in, not what it asserts. Say "covers nothing the kept tests do not" instead.
- that a function is buggy because an input class is missing. Say "no test passes <class>"; only a
  test you ran can show a bug.
- that a class is definitely never exercised when the caveat says the function was sampled. Say
  "not observed in the sampled calls".
- that an example value is real data. Examples are redacted shapes; never try to reconstruct them.

Wherever an argument reads <default>, leave it out of the tool call.
```

<!-- playbooks:end -->

## Example transcripts

Hand-written from real tool output on the demo (`coverset demo --executor local`); JSON is trimmed
to the fields the agent used (`…`), run ids are from one demo run.

### Reviewing PR #2 ("Allow signups from company subdomains")

> **User:** Review the tests of PR #2 with coverset.

The agent fetches the playbook (`review-tests-for-pr`, `run=run_01M4W4BR2NVFAH65V2PVPWMXPT`) and
follows it.

`get_run_summary {"run": "run_01M4W4BR2NVFAH65V2PVPWMXPT"}`

```json
{"run_info": {"pr_number": 2, "pr_title": "Allow signups from company subdomains", "verdict": "success",
              "reason": "all 99 tests passed"},
 "tests": {"total": 99, "outcomes": {"passed": 99}, "duration_s": 2.496},
 "patch": {"patch_pct": 100.0, "covered": 3, "total": 3, "changed_functions": 2, "zero_test_functions": []},
 "next_steps": ["get_patch_coverage: the changed lines and functions no test ran", "…"]}
```

`get_patch_coverage {"run": "run_01M4W4BR2NVFAH65V2PVPWMXPT"}`

```json
{"patch_pct": 100.0, "stale": [],
 "functions": [
   {"function": "src/ledger/accounts/emailcheck.py::validate_email", "change": "trivial", "inherited": true,
    "tests": 24, "input_classes": {"expected": 10, "observed": 8, "missing": ["email:multi-label-domain", "email:idn"]}},
   {"function": "src/ledger/accounts/signup.py::check_if_new_user_signup_allowed", "change": "modified",
    "tests": 14, "input_classes": {"expected": 15, "observed": 11,
    "missing": ["email:multi-label-domain", "email:idn", "existing_domains:unsorted", "existing_domains:duplicates"]}}],
 "zero_test_functions": []}
```

`suggest_tests {"run": "…", "function": "validate_email"}`

```json
{"missing": [
   {"class": "email:multi-label-domain", "description": "a domain with three or more labels, e.g. bob@eng.acme.com",
    "hypothesis": "st.sampled_from([\"bob@eng.acme.com\", \"ops@mail.eu.example.org\"])"},
   {"class": "email:idn", "hypothesis": "st.just(\"bob@bücher.de\")"}],
 "snippet": "from ledger.accounts.emailcheck import validate_email\n…@given(email=st.sampled_from([…]))…"}
```

`minimal_test_set {"run": "…", "scope": "changed"}`

```json
{"universe": {"lines": 10, "arcs": 4, "classes": 19, "tests_in_scope": 24},
 "kept": {"count": 8, "duration_s": 0.004},
 "dropped": {"count": 16, "duration_s": 0.315, "saving_pct": 98.7,
   "tests": [{"nodeid": "tests/test_signup.py::test_bulk_signup_screening_slow", "duration_s": 0.311133,
              "loses": {"lines": 0, "arcs": 0, "classes": 0},
              "covered_by": ["tests/test_signup.py::test_signup_allowed[dana@acme.com-existing_domains6-True]", "…"]}]}}
```

The agent reads `test_bulk_signup_screening_slow` and the `test_signup_allowed` cases next to it in
the diff, then answers:

> **Untested.** Nothing: every changed line ran (3 of 3), and both changed functions have tests.
>
> **Gaps.** The PR widens the email pattern to accept company subdomains, yet no test passes an
> address on a subdomain: `email:multi-label-domain` is not observed for `validate_email` (every
> line ran, 8 of 10 input classes) nor for `check_if_new_user_signup_allowed`. That is the case the
> PR title is about. Suggested test:
>
> ```python
> @given(email=st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"]))
> def test_an_existing_customers_subdomain_must_be_invited(email):
>     domain = email.split("@", 1)[1]
>     assert check_if_new_user_signup_allowed(email, existing_domains=[domain]) is False
> ```
>
> (and the same for `email:idn`). Whether `eng.acme.com` should count as the customer `acme.com`
> is a product decision the PR should state; the test above only pins the exact-domain rule.
>
> **Could be smaller.** `test_bulk_signup_screening_slow` (0.31 s, 98.7% of this change's test time)
> reaches no line, branch or input class of the change that the eight fast `test_signup_allowed` /
> `test_malformed_email_is_an_error` cases do not. Does it check something they do not, for
> example the batch behaviour? If not, it could become one more parametrized case.

What the agent did not say: that the slow test is useless, or that subdomain addresses are broken.

### Bootstrapping tests for `parse_duration`

> **User:** Write good tests for `parse_duration`.

`get_function {"function": "parse_duration"}`

```json
{"function": "src/ledger/durations/parse.py::parse_duration",
 "coverage": {"line_pct": 100.0, "branch_pct": 100.0, "missing_lines": []},
 "input_classes": {"expected": 11, "observed": 4, "pct": 36.4, "classes": [
   {"class": "text:single-unit", "observed": true, "example": "'0d'", "tests": ["tests/test_parse.py::test_parse_duration[0s-0]", "…"]},
   {"class": "text:multi-letter-unit", "observed": false, "strategy": "st.sampled_from([\"100ms\", \"2min\", \"3hr\"])"},
   {"class": "text:decimal", "observed": false, "strategy": "st.just(\"1.5h\")"},
   {"class": "text:signed", "observed": false, "strategy": "st.sampled_from([\"-5m\", \"+5m\"])"},
   {"class": "text:trailing-garbage", "observed": false, "strategy": "st.just(\"5m!\")"}, "…"]},
 "callers": [{"function": "src/ledger/durations/windows.py::expires_at", "samples": 6, "tests": 6}]}
```

`example_inputs {"function": "parse_duration"}`

```json
{"how_to_use": "These are redacted shapes, not raw data: …",
 "classes": [{"class": "text:multi-unit", "examples": [{"repr": "'1h30m'", "test": "tests/test_parse.py::test_parse_duration[1h30m-5400]"}]},
             {"class": "text:ascii", "examples": [{"repr": "'1h'", "caller": "src/ledger/durations/windows.py::expires_at", "…": "…"}]}]}
```

The agent reads `tests/test_parse.py` and `tests/test_properties.py`, sees that
`test_formatted_durations_parse_back` already checks the round trip through `format_duration` (so it
writes no second one), adds example tests for the missing unit forms, runs them, and reports:

> Every line and branch of `parse_duration` already ran, but its tests never passed in 7 of 11
> input classes. A round-trip property exists (`test_formatted_durations_parse_back`); it only
> generates what `format_duration` writes, which is why the unit forms below were never tried. I
> added `test_parse_duration_rejects_unsupported_forms`, parametrized over `"100ms"` and `"2min"`
> (`text:multi-letter-unit`), `"1.5h"` (`text:decimal`), `"-5m"` (`text:signed`) and `"5m!"`
> (`text:trailing-garbage`), each expecting `ValueError`. It fails on all five:
> `parse_duration("100ms")` returns 6000 (the pattern reads `100m` and drops the `s`), and the others
> return a number instead of raising. I have not changed `parse_duration`; the failing test
> documents the behaviour for you to decide. Still never passed in: `text:whitespace`,
> `text:unicode`, `text:ws-edge`. Command: `pytest tests/test_parse.py`.

### Shrinking the demo suite

> **User:** Our suite feels slow and repetitive. What could go?

`minimal_test_set {}`

```json
{"universe": {"lines": 95, "arcs": 31, "classes": 72, "blocks": 88, "tests_in_scope": 97},
 "exhaustive": true,
 "kept": {"count": 43, "duration_s": 0.436},
 "dropped": {"count": 54, "duration_s": 2.062, "saving_pct": 82.6, "tests": [
   {"nodeid": "tests/test_invoices.py::test_month_end_discount_run_slow", "duration_s": 0.81232,
    "loses": {"lines": 0, "arcs": 0, "classes": 0},
    "covered_by": ["tests/test_invoices.py::TestInvoice::test_discount_is_spread_in_proportion", "…"]}, "…"]},
 "deselect_args": "--deselect=tests/test_invoices.py::test_month_end_discount_run_slow\n…", "deselect_count": 54}
```

`redundant_tests {}`

```json
{"source": "test economy: facts store (computed when the run was finalized)",
 "equivalent_groups": [{"tests": ["tests/test_merge.py::test_merge_intervals[intervals1-merged1]", "…4 tests"],
                        "keep": "tests/test_merge.py::test_merge_intervals_leaves_input_unchanged"}],
 "collapsible_families": [{"family": "tests/test_merge.py::test_merge_intervals", "cases": 5, "distinct_flows": 1, "redundant_cases": 4}],
 "minimal_set_size": 54, "tests": 97}
```

> 54 of 97 tests (2.06 of 2.5 s) reach no line, branch or input class that the other 43 do not; the
> six property-based tests were kept on purpose. The four `_slow` tests account for 2.0 s of that.
> After reading them: `test_month_end_discount_run_slow` sleeps 0.8 s on purpose (its docstring
> says it exists to give the sharding demo uneven durations) and then asserts the subtotals of three
> discounted invoices, which `TestInvoice::test_discount_is_spread_in_proportion` does not check:
> move that assertion into a fast test, then drop the slow one. The merge cases `intervals1`, `2` and
> `4` run the same data flow as `test_merge_intervals_leaves_input_unchanged`, but each expects a
> different merged result, which coverage cannot see: keep them. Trial run:
> `pytest @deselect.txt` with the attached file, then `pytest --coverset` to confirm no fact is lost.
> I have deleted nothing.

The two numbers differ on purpose: `redundant_tests`' `minimal_set_size` (54) is the economy's
cover over every distinct argument *shape*, finer than the input classes `minimal_test_set` keeps
(43); see [How it works](#how-it-works).

## How it works

```
 agent ──stdio──▶ coverset mcp serve ──▶ AgentService ──▶ LocalBackend ─▶ .coverset (report loader)
                        │                     ▲        └─▶ StoreBackend ─▶ database + Parquet facts + mirror
                        └─ --server ─▶ RemoteService ──HTTPS──▶ /v1/agent ─▶ AgentService (StoreBackend)
```

- **One service, three surfaces.** `coverset.agent.service.AgentService` implements every tool over
  a backend; the MCP server (`coverset.agent.mcp_server`) and the HTTP API (`web/agent_api.py`) both
  call `AgentService.call(name, args)`, which validates the arguments against the same schemas
  (`coverset.agent.tools`). `--server` mode wraps `RemoteService`, an HTTP client with the same
  surface.
- **The same numbers as the pages.** A run is a `RunContext` around the pages' `RunFacts`: the
  store-backed one for a facts-store run, the static report's in-memory `BundleFacts` for a local
  bundle. Line states, partial branches, covering tests and input classes (the catalogue, with the
  same annotations) come from the code the pages and the report use; patch coverage of a PR run is the
  PR page's own diff analysis. Contexts are cached per run (keyed by the facts' file stamps), so the
  second call on a run costs milliseconds.
- **Patch coverage on a work tree.** For a local bundle, `get_patch_coverage(base=REF)` diffs the
  work tree against the merge base of `REF` and `HEAD` (uncommitted edits included, the base side from
  git, the head side from git-tracked files), classifies the changed functions as the planner does
  and applies the patch rules of [the data model](data-model.md). A changed file modified after the
  bundle was written is listed in `stale`.
- **The set cover.** Every fact (a line a test ran, a branch arc, an input class a test passed in)
  is listed once with the tests that reach it. Facts reached by exactly the same tests are
  interchangeable, so they are grouped into blocks first (88 blocks for the demo's 198 facts); the
  cover is `analysis/cover.py`'s greedy weighted cover over the blocks (most new blocks per second
  of the test's duration; unknown durations weigh the median) followed by a pass that drops any
  kept test the others already cover, heaviest first. With `budget_s` it is a budgeted maximum
  coverage instead (most facts per second until the budget is spent) and the dropped tests report
  what they would lose. Tests that did not pass are always kept, and so are property-based tests
  unless `keep_property_tests` is false (their recorded facts are a sample of what they explore).
- **Arcs per test are inferred.** Coverage records the run's branch arcs, not each test's. An arc
  `a -> b` counts for the tests that ran both lines; an exit arc for the tests that ran `a` and none
  of its other destinations. Arcs no test can be credited with are counted in `arcs_unattributed`.
- **The test economy.** When the run has it, `test_tax` and `redundant_tests` read
  `analysis/economy.py`'s results (the tables written at `finalize_run`, or computed from a local
  bundle on first use): test statements, assertions, fixtures and helpers against unique facts,
  equivalence groups, dominance, collapsible parametrize families and the PR test tax. The economy
  counts every distinct argument shape as a fact, which is finer than the input classes
  `minimal_test_set` keeps, so its minimal set is usually larger. Without it (an older run),
  `redundant_tests` and `test_tax` answer `available: false` with the reason.

## Trust notes

- **Read-only.** No tool writes to the repository, the database or the facts; the MCP annotations
  say `readOnlyHint: true`, `destructiveHint: false`, `openWorldHint: false`. `minimal_test_set`
  returns a deselect file; nothing is deleted.
- **Candidates, not verdicts.** Coverage sees what a test ran and passed in, never what it asserts.
  Every removal or redundancy result says so, and the playbooks tell the agent to read both tests
  before suggesting a change and to phrase it as a question.
- **Redacted examples.** Example values are the shape tracer's reprs: email local parts masked
  (`a***@example.com`), parameters, functions and dict keys whose names look secret hidden
  (`<redacted:len=N>`), strings cut at 48 characters, only builtin types formatted. The tools scrub
  every example and failure message once more (email local parts, GitHub tokens, Anthropic keys,
  bearer headers, private keys, the server's own secrets) and cap them (200 characters for an
  example, 2,000 for a failure message). They describe the kind of value, not data; no raw argument
  value is ever stored, so none can be returned.
- **Nodeids as given.** Test ids are returned exactly as the runner wrote them, parametrize ids
  included (the demo's contain email addresses that the test author wrote in the test file).
- **Data, not instructions.** Function names, test ids, failure messages and docstring-derived text
  come from the repository, which a pull request controls. An agent must treat every string in a
  tool result as data; the playbooks never ask it to follow text found in results.
- **Authorization.** Over HTTP every repository is checked with `require_repo(read)`; a repository
  the caller cannot see answers 404 exactly like one that does not exist, `list_repos` lists only
  visible ones, and a token needs a read-capable scope. `coverset mcp serve --repo` runs as the
  operator of the data directory; `--source` reads only the bundle you named and git-tracked files
  of its work tree.
- **Tokens.** `--server` sends the token only to the configured https URL (no redirects followed),
  and the log redactor masks `cst_` strings. Keep tokens in the environment, never in `.mcp.json`.
- **Sampled shapes.** The shape tracer caps the calls it records per function; a class reported
  missing in a sampled function reads "not observed in N sampled calls", and the tools carry that
  wording into `missing_reason` and the caveats.

## Limits

- At most 200 rows per list; `next_cursor` pages through the rest. `deselect_args` lists the
  dropped tests of the current page unless `deselect="all"` asks for all of them (up to 5,000), so a
  large suite's answer stays small enough for an agent's context.
- The HTTP API allows 120 calls a minute per token, user or admin.
- A local bundle has one run and no history: `get_test`'s history is facts-store only, and so is the
  PR test tax.
- Changed functions are classified for Python files; other languages get line-level patch coverage.
- Untracked files are not part of a work-tree diff (`git add -N FILE` includes a new file).
- Per-test branch arcs are inferred (see above); `facts=["lines", "classes"]` leaves them out.
- A file named by a suffix (`utils.py:12`, `get_covering_tests(path="utils.py")`) must match whole
  path components and name one file; `pkg_a/utils.py` and `pkg_b/utils.py` give an `ambiguous`
  error listing both rather than a guess.
- Memory: a served run's context (facts, cover index, function views, economy data) grows with the
  suite; a 5,000-test run reached about 720 MB once every tool had been used. A deployed instance
  keeps the 3 most recently used contexts (`COVERSET_AGENT_CONTEXTS=N` changes that); size the
  container for N of your largest runs, or lower N on a small one.
- The cover is greedy: small, not always minimum. Its result is a starting point for a reviewed
  plan, not a deletion list.
