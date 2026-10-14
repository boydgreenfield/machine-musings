"""Keep the README's numbers and tables equal to their sources.

Marked regions of ``README.md`` (and the coverset bullet of the repository's root README) are
generated:

* ``<!-- numbers:start -->`` ... ``<!-- numbers:end -->``: the numbers paragraph and its "Medians of"
  line, rendered from ``docs/numbers.json``;
* ``<!-- n:KEY -->VALUE<!-- /n -->`` anywhere in the README: one value from ``docs/numbers.json``;
* the line of ``../README.md`` that ends with ``<!-- coverset-bullet -->``: the root bullet;
* ``<!-- shapes:FUNC:start -->`` ... ``<!-- shapes:FUNC:end -->``: the output of
  ``coverset shapes FUNC --markdown`` on the repository's default branch (checked only with
  ``--data-dir``, a directory holding a seeded demo).

    uv run python scripts/check_readme.py                          # numbers only; exit 1 on a diff
    uv run python scripts/check_readme.py --data-dir <demo data>   # numbers and shapes tables
    uv run python scripts/check_readme.py --fix [--data-dir <dir>] # rewrite the regions in place

``--loose-examples`` ignores the example values of observed classes. The demo app's Hypothesis
profile is derandomized, so they should not differ; the flag is for a target suite whose property
tests draw fresh examples on every run.

After a re-measurement that updates ``docs/numbers.json``, ``--fix`` is all the README needs.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ROOT_README = ROOT.parent / "README.md"
NUMBERS = ROOT / "docs" / "numbers.json"
SHAPES_FUNCTIONS = ("validate_email", "parse_duration", "allocate_cents")
BULLET_MARK = "<!-- coverset-bullet -->"

NUMBERS_TEMPLATE = (
    "{demo_tests} tests in the demo app, {bench_tests} in a synthetic benchmark suite. A replayed "
    "`check_run.rerequested` webhook for the refunds PR reaches its first test {t_webhook_first_test} s "
    "later on a pooled Modal sandbox; a fresh sandbox restored from the trusted snapshot takes "
    "{t_create_snapshot} s, one from the env image {t_create_base} s, and building that image cold "
    "{t_image_cold} s. On the refunds PR the failing test runs {rank_coverset} of {pr1_tests} instead "
    "of {rank_file_order} in file order. Its failed result arrives {t_webhook_first_failed_result} s "
    "after the webhook and a retry confirms it at {t_verdict} s{verdict_where}, when the verdict is "
    "posted; file order would reach it after an estimated {t_first_failure_file_order_est} s on the same "
    "sandbox (estimated from the first test: the time to the first test result plus the median durations "
    "of the tests before it). {saving_clause} Fan-out is where larger suites save time: the benchmark "
    "suite takes {t_bench_1} s in one sandbox, {t_bench_16} s across 16 and {t_bench_32} s across 32, "
    "about ${cost_bench_16} per 16-shard run. Per-test coverage plus argument shapes cost {overhead_x}× "
    "plain pytest. The three functions below are at 100% line and branch coverage, and their tests never "
    "exercise {missing_classes} of {expected_classes} input classes."
)
SAVING = (
    "Running it first reports the failure {saving_s} s before file order would even reach it; the demo "
    "suite's tests take only {demo_suite_s} s in all, so that is most of what ordering can save here."
)
NO_SAVING = (
    "The demo suite's tests take {demo_suite_s} s in all, about as long as collecting and planning "
    "({t_webhook_collect} s), so on this suite the saving shows in the rank, not the clock."
)
"""The sentence after the estimate: the measured saving when the failed result beats the estimate."""
MEDIANS_TEMPLATE = (
    "Medians of {n_trials} runs on {bench_date} (the cold image build and the snapshot build are single "
    "runs), modal {modal_version}, Modal region {region} on the {plan_tier} plan, {cpu} CPU per sandbox; "
    "raw data in docs/benchmarks.md. The overhead is the median of {overhead_repeats} local runs of the "
    "demo suite; the input classes are demo data from {shapes_runs} local runs."
)
BULLET_TEMPLATE = (
    "- [`coverset/`](coverset/) — anticipatory CI for agent-written Python on Modal: tests start when a "
    "PR opens, likeliest failures first, and a data-flow overlay of the input classes each function's "
    "tests never tried. {demo_tests} demo tests, first failure in {t_verdict} s. " + BULLET_MARK
)

NUMBERS_BLOCK = re.compile(r"(<!-- numbers:start -->)\n(.*?)\n?(<!-- numbers:end -->)", re.DOTALL)
INLINE = re.compile(r"<!-- n:([a-z0-9_]+) -->(.*?)<!-- /n -->")
SHAPES_BLOCK = re.compile(
    r"(<!-- shapes:([A-Za-z0-9_.]+):start -->)\n(.*?)\n?(<!-- shapes:\2:end -->)", re.DOTALL
)


# -- rendering --------------------------------------------------------------------------------------------


def fmt(value: Any) -> str:
    """A number as the README prints it: ints with thousands separators, floats as stored (``5.0``)."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return repr(value) if abs(value) < 1e6 else f"{value:,.0f}"
    return str(value)


def ordinal(value: int) -> str:
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def cpu_phrase(cpu: str) -> str:
    """``"0.125-2.0"`` (request-limit) reads "up to 2"."""
    if "-" in cpu:
        upper = float(cpu.rsplit("-", 1)[1])
        return f"up to {upper:g}"
    return fmt(float(cpu))


def fields(numbers: Mapping[str, Any]) -> dict[str, str]:
    """Every placeholder value, formatted."""
    out = {key: fmt(value) for key, value in numbers.items() if not key.startswith("_")}
    out["rank_coverset"] = ordinal(int(numbers["rank_coverset"]))
    out["rank_file_order"] = ordinal(int(numbers["rank_file_order"]))
    out["cpu"] = cpu_phrase(str(numbers["cpu"]))
    out["verdict_where"] = "" if numbers.get("t_verdict_executor") == "modal" else " on the local executor"
    saving = float(numbers["t_first_failure_file_order_est"]) - float(
        numbers["t_webhook_first_failed_result"]
    )
    out["saving_s"] = fmt(round(saving, 1))
    out["saving_clause"] = (SAVING if saving > 0 else NO_SAVING).format_map(out)
    return out


def render_numbers(numbers: Mapping[str, Any]) -> str:
    values = fields(numbers)
    return NUMBERS_TEMPLATE.format_map(values) + "\n\n" + MEDIANS_TEMPLATE.format_map(values)


def render_bullet(numbers: Mapping[str, Any]) -> str:
    return BULLET_TEMPLATE.format_map(fields(numbers))


def load_numbers(path: Path = NUMBERS) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


# -- the shapes tables -----------------------------------------------------------------------------------


def coverset_bin() -> list[str]:
    found = Path(sys.executable).with_name("coverset")
    if found.is_file():
        return [str(found)]
    which = shutil.which("coverset")
    return [which] if which else [sys.executable, "-m", "coverset.cli"]


def shapes_markdown(func: str, data_dir: Path) -> str:
    """``coverset shapes FUNC --markdown`` on the default branch of the demo in ``data_dir``, whose
    database is ``COVERSET_DATABASE_URL`` (the one setting kept from this process's environment)."""
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("COVERSET_") or k == "COVERSET_DATABASE_URL"
    }
    env["COVERSET_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [*coverset_bin(), "shapes", func, "--markdown"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"coverset shapes {func} --markdown failed: {result.stderr.strip()[-2000:]}")
    return result.stdout.strip("\n")


COUNT_LINE = re.compile(r"^(\d+) of (\d+) input classes observed", re.MULTILINE)


def class_counts(table: str) -> tuple[int, int]:
    """(missing, expected) from a table's "N of M input classes observed" line."""
    match = COUNT_LINE.search(table)
    if match is None:
        raise ValueError("no 'N of M input classes observed' line in the table")
    observed, expected = int(match.group(1)), int(match.group(2))
    return expected - observed, expected


# -- checking and fixing ---------------------------------------------------------------------------------


@dataclass
class Finding:
    where: str
    expected: str
    actual: str

    def diff(self) -> str:
        lines = difflib.unified_diff(
            self.actual.splitlines(),
            self.expected.splitlines(),
            fromfile=f"{self.where} (README)",
            tofile=f"{self.where} (expected)",
            lineterm="",
        )
        return "\n".join(lines)


def _replace_numbers(text: str, numbers: Mapping[str, Any], findings: list[Finding]) -> str:
    match = NUMBERS_BLOCK.search(text)
    expected = render_numbers(numbers)
    if match is None:
        findings.append(Finding("README.md numbers block (markers missing)", expected, ""))
        return text
    if match.group(2) != expected:
        findings.append(Finding("README.md numbers block", expected, match.group(2)))
    return text[: match.start()] + f"{match.group(1)}\n{expected}\n{match.group(3)}" + text[match.end() :]


def _replace_inline(text: str, numbers: Mapping[str, Any], findings: list[Finding]) -> str:
    values = fields(numbers)

    def sub(match: re.Match[str]) -> str:
        key, actual = match.group(1), match.group(2)
        if key not in values:
            findings.append(Finding(f"README.md inline n:{key} (unknown key)", "<a numbers.json key>", key))
            return match.group(0)
        if actual != values[key]:
            findings.append(Finding(f"README.md inline n:{key}", values[key], actual))
        return f"<!-- n:{key} -->{values[key]}<!-- /n -->"

    return INLINE.sub(sub, text)


def _replace_bullet(text: str, numbers: Mapping[str, Any], findings: list[Finding]) -> str:
    expected = render_bullet(numbers)
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line.rstrip().endswith(BULLET_MARK)]
    if not hits:
        findings.append(Finding("../README.md coverset bullet (marker missing)", expected, ""))
        return text
    for i in hits:
        if lines[i] != expected:
            findings.append(Finding("../README.md coverset bullet", expected, lines[i]))
        lines[i] = expected
    return "\n".join(lines)


def mask_observed_examples(table: str) -> str:
    """``table`` with the example cell of every observed ("yes") row blanked.

    With property tests that draw fresh examples on every run, the shortest recorded example of a
    class they feed can change between runs; the classes, the observed column, the strategies of
    missing classes and the counts cannot. (The demo app's profile is derandomized.)
    """
    out = []
    for line in table.split("\n"):
        cells = line.split(" | ")
        if line.startswith("| `") and len(cells) == 3 and cells[1] == "yes":
            line = f"{cells[0]} | yes | ... |"
        out.append(line)
    return "\n".join(out)


def _replace_tables(
    text: str,
    tables: Mapping[str, str],
    numbers: Mapping[str, Any],
    findings: list[Finding],
    *,
    loose_examples: bool = False,
) -> str:
    found: set[str] = set()

    def sub(match: re.Match[str]) -> str:
        func, actual = match.group(2), match.group(3)
        found.add(func)
        if func not in tables:
            return match.group(0)
        same = (
            mask_observed_examples(actual) == mask_observed_examples(tables[func])
            if loose_examples
            else actual == tables[func]
        )
        if not same:
            findings.append(Finding(f"README.md shapes table {func}", tables[func], actual))
        return f"{match.group(1)}\n{tables[func]}\n{match.group(4)}"

    text = SHAPES_BLOCK.sub(sub, text)
    for func in sorted(set(tables) - found):
        findings.append(Finding(f"README.md shapes table {func} (markers missing)", tables[func], ""))
    missing = expected = 0
    for table in tables.values():
        m, e = class_counts(table)
        missing, expected = missing + m, expected + e
    if tables and (numbers.get("missing_classes"), numbers.get("expected_classes")) != (missing, expected):
        findings.append(
            Finding(
                "docs/numbers.json missing_classes/expected_classes vs the tables",
                f"{missing} of {expected}",
                f"{numbers.get('missing_classes')} of {numbers.get('expected_classes')}",
            )
        )
    return text


def check(
    *,
    data_dir: Path | None = None,
    fix: bool = False,
    readme: Path = README,
    root_readme: Path = ROOT_README,
    numbers_path: Path = NUMBERS,
    shapes: Callable[[str, Path], str] = shapes_markdown,
    loose_examples: bool = False,
) -> list[Finding]:
    """Compare (and with ``fix`` rewrite) the generated regions; returns what differed."""
    numbers = load_numbers(numbers_path)
    findings: list[Finding] = []
    text = readme.read_text(encoding="utf-8")
    new = _replace_numbers(text, numbers, findings)
    new = _replace_inline(new, numbers, findings)
    if data_dir is not None:
        tables = {func: shapes(func, data_dir) for func in SHAPES_FUNCTIONS}
        new = _replace_tables(new, tables, numbers, findings, loose_examples=loose_examples)
    root_text = root_readme.read_text(encoding="utf-8") if root_readme.is_file() else None
    new_root = _replace_bullet(root_text, numbers, findings) if root_text is not None else None
    if fix:
        if new != text:
            readme.write_text(new, encoding="utf-8")
        if new_root is not None and new_root != root_text:
            root_readme.write_text(new_root, encoding="utf-8")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="a seeded demo's data dir (its database: COVERSET_DATABASE_URL): also check the shapes tables",
    )
    parser.add_argument("--fix", action="store_true", help="rewrite the generated regions in place")
    parser.add_argument(
        "--loose-examples",
        action="store_true",
        help="ignore the example values of observed classes (for suites whose property tests are random)",
    )
    args = parser.parse_args(argv)
    findings = check(
        data_dir=args.data_dir.resolve() if args.data_dir else None,
        fix=args.fix,
        loose_examples=args.loose_examples,
    )
    if not findings:
        print("README regions match docs/numbers.json" + (" and coverset shapes" if args.data_dir else ""))
        return 0
    for finding in findings:
        print(finding.diff() or f"{finding.where}: expected {finding.expected!r}, found {finding.actual!r}")
        print()
    if args.fix:
        print(f"fixed {len(findings)} region(s)")
        return 0
    print(
        f"{len(findings)} region(s) differ; run `uv run python scripts/check_readme.py --fix`",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
