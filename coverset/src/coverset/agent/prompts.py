"""The playbooks: three MCP prompts that tell an agent how to use the tools well.

A tool list says what can be asked; a playbook says what to ask, in what order, how to word the
result and what never to claim. Each :class:`Playbook` renders to one user message with its
arguments filled in; ``docs/mcp.md`` prints the same text (a test keeps them identical), so a person
reviewing an agent's work can read exactly what the agent was told.

The three:

* ``review-tests-for-pr``: review a pull request's tests for reviewability: what the change needs
  tested, what it tests to death, what it misses.
* ``bootstrap-tests-for-function``: write the first good tests for one function from its input
  classes and redacted examples.
* ``shrink-test-suite``: propose a smaller suite with the same coverage facts (the set cover), as a
  reviewable plan rather than a deletion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

__all__ = ["PLAYBOOKS", "Playbook", "PlaybookArg", "render"]


@dataclass(frozen=True, slots=True)
class PlaybookArg:
    """One prompt argument."""

    name: str
    description: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class Playbook:
    """One prompt: its name, a description, its arguments and the template of its message."""

    name: str
    title: str
    description: str
    template: str
    args: tuple[PlaybookArg, ...] = field(default_factory=tuple)


NEVER_CLAIM: Final = """\
Never claim:
- that a test is useless, wrong or safe to delete because coverage facts say so: coverset sees what a
  test ran and passed in, not what it asserts. Say "covers nothing the kept tests do not" instead.
- that a function is buggy because an input class is missing. Say "no test passes <class>"; only a
  test you ran can show a bug.
- that a class is definitely never exercised when the caveat says the function was sampled. Say
  "not observed in the sampled calls".
- that an example value is real data. Examples are redacted shapes; never try to reconstruct them."""

PLAYBOOKS: Final[dict[str, Playbook]] = {
    p.name: p
    for p in (
        Playbook(
            name="review-tests-for-pr",
            title="Review the tests of a pull request",
            description="Review a PR's tests for reviewability: what the change needs tested, what it "
            "tests to death, what it misses. Produces a short review a human can act on.",
            args=(
                PlaybookArg("run", "The PR run id (list_runs kind=pr); omit for a local .coverset."),
                PlaybookArg(
                    "base", "Local .coverset only: the git ref the change is against (e.g. origin/main)."
                ),
            ),
            template="""\
Review the tests of this change with coverset. Run: {run}. Base: {base}.

Goal: a review that makes the change easier to merge. Reviewers read every added test line; a test
that re-checks what other tests already check costs review time and buys nothing, while the inputs
nobody tried are where the bugs are.

Fetch, in this order:
1. get_run_summary (run={run}). Note the verdict, failing tests and the patch numbers.
2. get_patch_coverage (run={run}, base={base}). Note zero_test_functions, files with missing_lines,
   and each changed function's missing input classes. If `stale` is non-empty, stop and ask for
   `pytest --coverset` to be re-run.
3. For each changed function with missing classes or no tests: get_function, then suggest_tests.
4. minimal_test_set (scope='changed', run={run}, base={base}). The dropped tests are the PR's tests
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

{never_claim}""",
        ),
        Playbook(
            name="bootstrap-tests-for-function",
            title="Bootstrap tests for one function",
            description="Write the first good tests for one function from its input classes, redacted "
            "examples and suggested strategies.",
            args=(
                PlaybookArg(
                    "function", "The function: qualname, path::qualname, path:line or func_key.", True
                ),
                PlaybookArg("run", "A run id; omit for the default run."),
            ),
            template="""\
Write tests for {function} with coverset's help. Run: {run}.

Fetch, in this order:
1. get_function (function={function}, run={run}): its body coverage, its input classes per parameter
   (observed with an example, missing with a strategy), the tests that already call it directly and
   the callers that reach it indirectly.
2. example_inputs (function={function}, run={run}): read `how_to_use` first. The examples show the
   shape of the values each class means; they are redacted (email local parts masked, long strings
   cut) and are not data to copy.
3. suggest_tests (function={function}, run={run}): a strategy per missing class and a skeleton.
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

{never_claim}""",
        ),
        Playbook(
            name="shrink-test-suite",
            title="Shrink a test suite (set cover)",
            description="Propose a smaller test suite that keeps every line, branch and input class the "
            "current one covers, as a reviewable plan.",
            args=(
                PlaybookArg("scope", "all, changed or path (default all)."),
                PlaybookArg("path", "With scope=path: a file or directory prefix."),
                PlaybookArg("budget_s", "Optional: a time budget in seconds for the kept tests."),
                PlaybookArg("run", "A run id; omit for the default run."),
            ),
            template="""\
Propose a smaller test suite with coverset. Scope: {scope}. Path: {path}. Budget: {budget_s} s. Run: {run}.

This is the set-cover problem: keep the fewest test-seconds that still reach every fact (line,
branch arc, input class) the suite reaches today. coverset solves it greedily; you turn it into a
plan a maintainer can trust.

Fetch, in this order:
1. get_run_summary (run={run}): suite size, duration, failing tests (always kept).
2. minimal_test_set (scope={scope}, path={path}, budget_s={budget_s}, run={run}): kept and dropped
   tests and what each dropped test would lose. Page through `dropped` with `cursor` if there are
   more; ask for deselect='all' once, at the end, for the whole deselect file.
3. redundant_tests (run={run}): equivalence groups (identical facts) and dominated tests. Groups are
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

{never_claim}""",
        ),
    )
}


UNSET: Final = "<default>"
"""How an argument the caller did not give is shown: the agent omits it in the tool call."""


def render(name: str, args: Mapping[str, str | None] | None = None) -> str:
    """The playbook's message with ``args`` filled in (unset ones read :data:`UNSET`, and the message
    says to omit them)."""
    playbook = PLAYBOOKS[name]
    given = args or {}
    values = {a.name: str(given.get(a.name) or UNSET) for a in playbook.args}
    if values.get("scope") == UNSET:
        values["scope"] = "all"
    body = playbook.template.format(never_claim=NEVER_CLAIM, **values)
    if UNSET in body:
        body += f"\n\nWherever an argument reads {UNSET}, leave it out of the tool call."
    return body
