"""The agent tools: names, descriptions and JSON Schemas, shared by every surface.

One table (:data:`TOOLS`) is the contract. The MCP server lists it verbatim, the HTTP API serves it
at ``GET /v1/agent`` (so ``coverset mcp serve --server`` can discover what a deployed instance
offers), and ``docs/mcp.md``'s tool reference is generated from it (``tests/agent`` keeps the page
in sync). The descriptions are what an agent reads before it calls anything, so they say what the
tool returns, when to use it, and the caveats that keep an agent honest: removal and redundancy
results are candidates, not verdicts; example values are redacted shapes, not data; input classes
come from sampled observations.

Arguments are validated here (:func:`validate_args`) against the same schemas, with a small
checker for the subset of JSON Schema the tools use (object, string, integer, boolean, enum,
array of strings, min/max), so a bad call fails the same way over stdio and over HTTP, and no
schema library is needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MAX_ROWS",
    "TOOLS",
    "ToolError",
    "ToolSpec",
    "tool_catalogue",
    "validate_args",
]

MAX_ROWS: Final = 200
"""No call returns more rows than this in any one list (pages continue with ``cursor``)."""
DEFAULT_LIMIT: Final = 50

CANDIDATES_NOT_VERDICTS: Final = (
    "Results are candidates, not verdicts: two tests that run the same lines and pass in the same "
    "input classes may still assert different things. Read both tests before recommending a removal, "
    "and never claim a test is useless because of these numbers alone."
)
SAMPLED: Final = (
    "Input classes come from sampled argument shapes: a class reported missing was never observed "
    "in the sampled calls, which is strong evidence but not proof that no test passes such a value."
)
REDACTED: Final = (
    "Example values are redacted reprs captured by the tracer (email local parts masked, secret-named "
    "parameters hidden, long strings cut). They show the kind of value a test passed, never raw data."
)


class ToolError(Exception):
    """A tool call that cannot be answered: ``code`` is a stable slug, ``status`` its HTTP status.

    ``details`` carries machine-readable context (for ``ambiguous``: the candidate functions), so an
    agent can recover without guessing.
    """

    STATUS: Final = {
        "invalid_argument": 400,
        "ambiguous": 400,
        "unauthorized": 401,
        "insufficient_scope": 403,
        "unknown_tool": 404,
        "not_found": 404,
        "rate_limited": 429,
        "unavailable": 503,
        "internal": 500,
    }

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    @property
    def status(self) -> int:
        """The HTTP status the API answers with."""
        return self.STATUS.get(self.code, 400)

    def to_json(self) -> dict[str, Any]:
        """``{"error": code, "message": ..., **details}`` (the HTTP body and the MCP error text)."""
        return {"error": self.code, "message": self.message, **self.details}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool: its name, a one-line title, the description an agent reads, and its input schema."""

    name: str
    title: str
    description: str
    input_schema: Mapping[str, Any]
    run_scoped: bool = True
    """Whether the tool reads one run (and so takes ``run`` and ``repo``)."""

    def to_json(self) -> dict[str, Any]:
        """The ``GET /v1/agent`` entry (also what ``--server`` mode re-lists over MCP)."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


# -- shared argument schemas -------------------------------------------------------------------------

_RUN: Final = {
    "type": "string",
    "maxLength": 64,
    "description": "A run id from list_runs. Omit it for the default run: the server's --run, else the "
    "newest completed default-branch baseline of `repo` (a local .coverset directory has one run, "
    "`local`). `latest` means the same as omitting it.",
}
_REPO: Final = {
    "type": "string",
    "maxLength": 300,
    "description": "Repository full name (owner/name) or numeric id from list_repos; picks the "
    "default run when `run` is omitted. Not needed with a local .coverset directory.",
}
_LIMIT: Final = {
    "type": "integer",
    "minimum": 1,
    "maximum": MAX_ROWS,
    "default": DEFAULT_LIMIT,
    "description": f"Rows per page (1 to {MAX_ROWS}).",
}
_CURSOR: Final = {
    "type": "string",
    "maxLength": 32,
    "description": "The `next_cursor` of a previous page of the same call; omit for the first page.",
}
_FUNCTION: Final = {
    "type": "string",
    "minLength": 1,
    "maxLength": 1000,
    "description": "Which function: a qualname (`validate_email`, `Invoice.total`), `path::qualname`, "
    "`path:line` (the innermost function whose lines contain it) or a 16-hex func_key. An ambiguous "
    "name fails with the candidates listed.",
}


def _schema(
    properties: Mapping[str, Any], required: tuple[str, ...] = (), *, run: bool = True
) -> dict[str, Any]:
    props = dict(properties)
    if run:
        props = {"run": _RUN, "repo": _REPO, **props}
    return {
        "type": "object",
        "properties": props,
        "required": list(required),
        "additionalProperties": False,
    }


# -- the tools ---------------------------------------------------------------------------------------

TOOLS: Final[dict[str, ToolSpec]] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "list_repos",
            "List repositories",
            "The repositories you may read, each with its default branch and newest run. Start here when "
            "talking to a deployed coverset; a local .coverset directory lists one pseudo-repository.",
            _schema({}, run=False),
            run_scoped=False,
        ),
        ToolSpec(
            "list_runs",
            "List runs",
            "Recent runs of one repository, newest first: id, kind (`pr` for any pull-request run, else the "
            "trigger: `baseline`, `push`, `manual`, `rerun`, `upload`; `local` for a local bundle), branch, "
            "PR number, head commit, status and verdict. Use a PR run's id with "
            "get_patch_coverage and minimal_test_set(scope='changed').",
            _schema(
                {
                    "repo": _REPO,
                    "kind": {
                        "type": "string",
                        "enum": ["any", "pr", "baseline"],
                        "default": "any",
                        "description": "Only pull-request runs, only default-branch baselines, or both.",
                    },
                    "limit": {**_LIMIT, "default": 20},
                },
                run=False,
            ),
            run_scoped=False,
        ),
        ToolSpec(
            "get_run_summary",
            "Run summary",
            "Headline facts of one run: identity (PR, branch, commit), verdict, test outcomes and total "
            "duration, the slowest tests, line and branch coverage, input-class totals, and for a PR run "
            "its patch coverage. Call it first to orient yourself; the `next_steps` field suggests which "
            "tool answers the obvious follow-up.",
            _schema({}),
        ),
        ToolSpec(
            "get_patch_coverage",
            "Patch coverage",
            "What a change touched and whether tests ran it: patch coverage (changed statements that "
            "ran), per-file missing lines as ranges, every changed function with its covering-test count, "
            "body line % and the input classes no test passed in, and the changed functions no test ran "
            "at all (`zero_test_functions`). For a PR run the diff is merge-base...head; for a local "
            ".coverset directory pass `base` (a git ref such as `origin/main`) and the diff is "
            "merge-base...working tree, so you can check a change before pushing. "
            "If `stale` is true, files changed after the tests ran: run `pytest --coverset` again. "
            + SAMPLED,
            _schema(
                {
                    "base": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "Local .coverset only: the git ref to diff the working tree against "
                        "(for example `origin/main`). Ignored for a facts-store run, whose PR base is known.",
                    },
                    "limit": _LIMIT,
                    "cursor": _CURSOR,
                }
            ),
        ),
        ToolSpec(
            "get_function",
            "Function details",
            "Everything coverset knows about one function in a run: its lines, body line and branch "
            "coverage with the lines that never ran and the branches taken one way only, its input "
            "classes per parameter (expected, observed with a redacted example, missing with a "
            "Hypothesis strategy), the tests that ran its body, which of them call it directly (from "
            "the test) and which reach it through other functions (callers listed). " + SAMPLED,
            _schema(
                {
                    "function": _FUNCTION,
                    "limit": {
                        **_LIMIT,
                        "default": 20,
                        "description": f"Covering tests and callers listed (fastest tests first; 1 to {MAX_ROWS}).",
                    },
                },
                ("function",),
            ),
        ),
        ToolSpec(
            "find_gaps",
            "Largest input-class gaps",
            "Functions that tests call but whose tests never pass in whole kinds of input, most missing "
            "classes first (or by ratio, or by body size). Each row names the missing classes. Use it to "
            "choose where a new test buys the most; follow up with get_function or suggest_tests. "
            "Functions no test called are left out unless `include_uncalled` is true. " + SAMPLED,
            _schema(
                {
                    "sort": {
                        "type": "string",
                        "enum": ["missing", "ratio", "size"],
                        "default": "missing",
                        "description": "`missing`: most missing classes; `ratio`: lowest observed/expected; "
                        "`size`: largest body first among functions with any gap.",
                    },
                    "path": {
                        "type": "string",
                        "maxLength": 1000,
                        "description": "Only functions whose file path starts with this prefix.",
                    },
                    "include_uncalled": {
                        "type": "boolean",
                        "default": False,
                        "description": "Also list functions no test called (every class is then missing).",
                    },
                    "limit": {**_LIMIT, "default": 20},
                    "cursor": _CURSOR,
                }
            ),
        ),
        ToolSpec(
            "get_covering_tests",
            "Tests covering a line or function",
            "The tests that executed a line (`path` + `line`), a function's body (`function`), or any line "
            "of a file (`path` alone), with each test's outcome and duration, fastest first. Import-time "
            "execution is not a test; `import_time` says whether module import also ran the line. Use it "
            "to pick the few tests to run after an edit.",
            _schema(
                {
                    "path": {
                        "type": "string",
                        "maxLength": 1000,
                        "description": "Repository-relative file path.",
                    },
                    "line": {"type": "integer", "minimum": 1, "description": "A line of `path`."},
                    "function": _FUNCTION,
                    "limit": _LIMIT,
                    "cursor": _CURSOR,
                }
            ),
        ),
        ToolSpec(
            "get_test",
            "Test details",
            "One test: outcome, duration, failure message (redacted, cut at 2,000 characters), the files "
            "and functions it ran (lines run out of the body's statements), the argument shapes it passed "
            "in (input classes reached, redacted examples, whether it called the function itself), and "
            "its outcome in recent runs (facts store only). Nodeids are exactly as the runner wrote them.",
            _schema(
                {
                    "nodeid": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4096,
                        "description": "The test id, e.g. `tests/test_parse.py::test_parse_duration[90s-90]`.",
                    }
                },
                ("nodeid",),
            ),
        ),
        ToolSpec(
            "example_inputs",
            "Redacted example inputs",
            "Example argument values per input class of one function, each with the test that passed it, "
            "for bootstrapping fixtures and new tests. " + REDACTED + " Missing classes come with the "
            "strategy to generate them instead. Read `how_to_use` before quoting any of it.",
            _schema(
                {
                    "function": _FUNCTION,
                    "param": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "Only this parameter (`return` for return values).",
                    },
                    "limit": _LIMIT,
                },
                ("function",),
            ),
        ),
        ToolSpec(
            "suggest_tests",
            "Suggest tests for missing input classes",
            "Property-based test material for the input classes no test passed in to one function: per "
            "missing class its description and a Hypothesis strategy (Python) or a fast-check arbitrary "
            "(TypeScript), plus a runnable-looking test skeleton. The skeleton never asserts anything "
            "specific: you must write the property (an invariant of the result) yourself. " + SAMPLED,
            _schema({"function": _FUNCTION}, ("function",)),
        ),
        ToolSpec(
            "minimal_test_set",
            "Minimal test set (set cover)",
            "A small set of tests that keeps every covered line, every branch arc (approximated: per-test "
            "arcs are not recorded, so an arc counts for a test that ran both its ends) and every observed "
            "input class of the scope, chosen by greedy weighted set cover (fewest seconds per newly "
            "covered fact, then a pass that drops any kept test the rest already cover). Returns the kept "
            "tests, the dropped ones with what each would lose (nothing, unless `budget_s` cut the cover "
            "short or the scope hides facts elsewhere), and `deselect_args`: lines for a pytest argument "
            "file (`pytest @deselect.txt`). scope `all` is the whole suite, `changed` a PR's changed "
            "functions, `path` one file or directory prefix. " + CANDIDATES_NOT_VERDICTS,
            _schema(
                {
                    "scope": {
                        "type": "string",
                        "enum": ["all", "changed", "path"],
                        "default": "all",
                        "description": "What must stay covered.",
                    },
                    "path": {
                        "type": "string",
                        "maxLength": 1000,
                        "description": "With scope `path`: a file path or directory prefix.",
                    },
                    "base": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "With scope `changed` on a local .coverset: the git ref to diff against.",
                    },
                    "facts": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["lines", "arcs", "classes"]},
                        "maxItems": 3,
                        "description": "Which facts must be kept (default all three).",
                    },
                    "budget_s": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Stop adding tests once the kept tests would take longer than this "
                        "many seconds (a best-coverage-within-budget set instead of a full cover).",
                    },
                    "keep_property_tests": {
                        "type": "boolean",
                        "default": True,
                        "description": "Always keep property-based tests (Hypothesis @given, fast-check): they "
                        "search inputs beyond what the sampled shapes record, so their recorded facts "
                        "understate them. Tests that did not pass are always kept.",
                    },
                    "deselect": {
                        "type": "string",
                        "enum": ["page", "all", "none"],
                        "default": "page",
                        "description": "What `deselect_args` lists: the dropped tests of this page (default, "
                        "small), all of them (up to 5,000; for writing the file), or nothing.",
                    },
                    "limit": _LIMIT,
                    "cursor": _CURSOR,
                }
            ),
        ),
        ToolSpec(
            "redundant_tests",
            "Redundant test candidates",
            "Tests that look redundant on coverage evidence: groups of tests with identical facts (same "
            "lines and input classes, the same data flow) and tests whose facts another single test "
            "fully contains (dominated). Each group suggests the fastest member to keep. "
            + CANDIDATES_NOT_VERDICTS,
            _schema({"limit": {**_LIMIT, "default": 20}, "cursor": _CURSOR}),
        ),
        ToolSpec(
            "test_tax",
            "Test tax",
            "What each test costs against what it alone covers: its statements (own plus helpers), "
            "assertions, fixtures and seconds against its unique facts (lines, branch arcs and input "
            "classes no other test reaches); leverage = unique facts per statement. For a PR run, "
            "`pr_tax` is the PR test tax: test statements the PR added per newly covered fact, flagged "
            "above the threshold (the 95-lines-to-check-one-environment-variable smell). `available` is "
            "false, with the reason, when the run has no economy (an older run). " + CANDIDATES_NOT_VERDICTS,
            _schema(
                {
                    "sort": {
                        "type": "string",
                        "enum": ["leverage", "cost", "statements", "unique", "duration"],
                        "default": "leverage",
                        "description": "`leverage` ascending (costliest per unique fact first), `cost` (ms per "
                        "unique fact), `statements`, `unique` facts, or `duration`.",
                    },
                    "only": {
                        "type": "string",
                        "enum": ["all", "assertion_free", "removable", "unmeasured"],
                        "default": "all",
                        "description": "Only tests without assertions, removable candidates, or tests whose "
                        "source could not be measured.",
                    },
                    "limit": {**_LIMIT, "default": 20},
                    "cursor": _CURSOR,
                }
            ),
        ),
        ToolSpec(
            "search",
            "Search functions, files and tests",
            "Case-insensitive substring search over function qualnames, measured file paths and test ids "
            "of one run. Use it to find the exact name another tool needs.",
            _schema(
                {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                        "description": "The substring.",
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["functions", "files", "tests"]},
                        "maxItems": 3,
                        "description": "What to search (default all three).",
                    },
                    "limit": {**_LIMIT, "default": 20},
                },
                ("query",),
            ),
        ),
        ToolSpec(
            "explain_shape_key",
            "Explain a shape key",
            "A shape key such as `str|ascii|kind:email/plain|len:11-100` in words: the value's type and "
            "each feature, plus whether it follows the version-1 grammar. Shape keys appear in get_test, "
            "get_function and example_inputs.",
            _schema(
                {
                    "key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 512,
                        "description": "The shape key.",
                    }
                },
                ("key",),
                run=False,
            ),
            run_scoped=False,
        ),
    )
}


def tool_catalogue() -> list[dict[str, Any]]:
    """Every tool as JSON (``GET /v1/agent``'s ``tools``)."""
    return [spec.to_json() for spec in TOOLS.values()]


# -- argument validation -----------------------------------------------------------------------------


def validate_args(name: str, args: Mapping[str, Any] | None) -> dict[str, Any]:
    """``args`` checked against ``name``'s schema, with defaults filled in.

    Raises :class:`ToolError` (``unknown_tool`` or ``invalid_argument``). Booleans are not accepted
    where integers are expected (JSON ``true`` is not ``1``), and integral floats are accepted as
    integers (some clients send ``20.0``).
    """
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError("unknown_tool", f"there is no tool {name!r}", {"tools": sorted(TOOLS)})
    if args is None:
        args = {}
    if not isinstance(args, Mapping):
        raise ToolError("invalid_argument", "arguments must be a JSON object")
    schema = spec.input_schema
    props: Mapping[str, Any] = schema["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError(
            "invalid_argument",
            f"{name} takes no argument {unknown[0]!r}",
            {"allowed": sorted(props)},
        )
    out: dict[str, Any] = {}
    for key, prop in props.items():
        if key in args and args[key] is not None:
            out[key] = _check(f"{name}.{key}", prop, args[key])
        elif "default" in prop:
            out[key] = prop["default"]
        else:
            out[key] = None
    missing = [key for key in schema.get("required", ()) if out.get(key) is None]
    if missing:
        raise ToolError("invalid_argument", f"{name} needs the argument {missing[0]!r}")
    return out


def _check(where: str, prop: Mapping[str, Any], value: Any) -> Any:
    kind = prop.get("type")
    if kind == "string":
        if not isinstance(value, str):
            raise ToolError("invalid_argument", f"{where} must be a string")
        if len(value) < prop.get("minLength", 0) or len(value) > prop.get("maxLength", 1 << 30):
            raise ToolError("invalid_argument", f"{where} has the wrong length")
        if "\x00" in value:
            raise ToolError("invalid_argument", f"{where} must not contain NUL")
    elif kind in ("integer", "number"):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ToolError("invalid_argument", f"{where} must be a number")
        if kind == "integer":
            if isinstance(value, float) and not value.is_integer():
                raise ToolError("invalid_argument", f"{where} must be an integer")
            value = int(value)
        if value < prop.get("minimum", float("-inf")) or value > prop.get("maximum", float("inf")):
            raise ToolError(
                "invalid_argument",
                f"{where} must be between {prop.get('minimum', '-inf')} and {prop.get('maximum', 'inf')}",
            )
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ToolError("invalid_argument", f"{where} must be true or false")
    elif kind == "array":
        if not isinstance(value, list) or len(value) > prop.get("maxItems", MAX_ROWS):
            raise ToolError("invalid_argument", f"{where} must be a short list")
        value = [_check(f"{where}[]", prop.get("items", {}), item) for item in value]
        return list(dict.fromkeys(value))
    if "enum" in prop and value not in prop["enum"]:
        raise ToolError("invalid_argument", f"{where} must be one of {', '.join(map(str, prop['enum']))}")
    return value
