"""Measure what pytest-coverset costs: plain pytest vs per-test coverage vs coverage plus argument shapes.

Runs the target suite (default: the plugin's own fixture project) ``--repeats`` times in each mode,
interleaving the modes so drift hits all three equally, inside a temporary copy of the target so
nothing is written into the source tree. Prints JSON with the median wall time of each mode
(``python -m pytest`` subprocess, interpreter start-up included) and ``overhead_x`` =
coverage+shapes / plain.

    uv run python packages/pytest-coverset/scripts/measure_overhead.py
    uv run python packages/pytest-coverset/scripts/measure_overhead.py --target ../demo-app --source ledger
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FIXTURE_PROJECT = Path(__file__).resolve().parent.parent / "tests" / "fixture_project"
IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".hypothesis", ".pytest_cache", "coverset-out*"
)
OK_EXIT_CODES = (0, 1)


def mode_args(mode: str, out_dir: Path, sources: list[str]) -> list[str]:
    """Extra pytest arguments for one mode."""
    if mode == "plain":
        return []
    args = ["--coverset-out", str(out_dir)]
    for source in sources:
        args += ["--coverset-source", source]
    return args + ["--coverset-shapes", "on" if mode == "coverage_shapes" else "off"]


def run_once(python: str, workdir: Path, args: list[str]) -> float:
    """Run pytest once and return its wall time in seconds."""
    command = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args]
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=workdir, capture_output=True, text=True, check=False)
    elapsed = time.perf_counter() - started
    if completed.returncode not in OK_EXIT_CODES:
        tail = "\n".join(completed.stdout.splitlines()[-20:] + completed.stderr.splitlines()[-20:])
        raise SystemExit(f"pytest exited {completed.returncode} for {' '.join(args) or 'plain'}:\n{tail}")
    return elapsed


def measure(
    target: Path, sources: list[str], repeats: int, python: str, extra: list[str]
) -> dict[str, object]:
    """Median wall times per mode for ``target``."""
    timings: dict[str, list[float]] = {"plain": [], "coverage": [], "coverage_shapes": []}
    with tempfile.TemporaryDirectory(prefix="coverset-overhead-") as tmp:
        workdir = Path(tmp) / "target"
        shutil.copytree(target, workdir, ignore=IGNORED)
        for round_number in range(repeats):
            for mode in timings:
                out_dir = Path(tmp) / f"out-{mode}-{round_number}"
                timings[mode].append(run_once(python, workdir, [*mode_args(mode, out_dir, sources), *extra]))
    medians = {mode: statistics.median(values) for mode, values in timings.items()}
    return {
        "plain_s": round(medians["plain"], 3),
        "coverage_s": round(medians["coverage"], 3),
        "coverage_shapes_s": round(medians["coverage_shapes"], 3),
        "overhead_x": round(medians["coverage_shapes"] / medians["plain"], 2),
        "coverage_x": round(medians["coverage"] / medians["plain"], 2),
        "repeats": repeats,
        "target": str(target),
        "python": subprocess.run(
            [python, "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "runs_s": {mode: [round(v, 3) for v in values] for mode, values in timings.items()},
    }


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target", type=Path, default=FIXTURE_PROJECT, help="project root to run pytest in")
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="--coverset-source value (repeatable; default fixturepkg)",
    )
    parser.add_argument("--repeats", type=int, default=3, help="runs per mode (median reported)")
    parser.add_argument(
        "--python", default=sys.executable, help="interpreter with pytest and pytest-coverset"
    )
    parser.add_argument("--output", type=Path, help="also write the JSON here")
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments (after --)")
    args = parser.parse_args(argv)
    sources = (
        args.source if args.source is not None else (["fixturepkg"] if args.target == FIXTURE_PROJECT else [])
    )
    result = measure(args.target.resolve(), sources, args.repeats, args.python, args.pytest_args)
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
