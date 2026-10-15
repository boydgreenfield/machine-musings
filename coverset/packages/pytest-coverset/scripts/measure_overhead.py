"""Measure what pytest-coverset costs, layer by layer: plain pytest, coverage.py alone, per-test
contexts, the shape tracer, the neutral export.

Runs the target suite (default: the plugin's own fixture project) ``--repeats`` times in each mode,
interleaving the modes so drift hits all of them equally, inside a temporary copy of the target so
nothing is written into the source tree. Prints JSON with the median wall time of each mode
(``python -m pytest`` subprocess, interpreter start-up included), each mode's multiplier over plain
pytest, and the median of the plugin's own timings from ``meta.json`` (context switches, the
finalisation steps, the neutral export).

Modes (``--modes``, default all five):

=================  ==============================================================================
``plain``          ``pytest``
``covpy``          ``coverage run --branch --source=...`` with ``COVERAGE_CORE=ctrace``: coverage.py
                   alone, one context for the whole run
``coverage``       the plugin with ``--coverset-shapes=off``: per-test contexts, results, the bundle
``coverage_shapes`` the plugin with the shape tracer (what the runner uses)
``neutral``        the plugin with the shape tracer and ``--coverset-neutral`` (what ``--coverset``
                   uses)
=================  ==============================================================================

``--workers N`` adds ``-n N`` (pytest-xdist) to every mode; ``covpy`` is skipped then (coverage
run does not follow xdist workers). ``--conftest-append FILE`` appends a file to
``tests/conftest.py`` of the copy (``stub_busy_conftest.py`` turns the synthetic bench suite's
``busy()`` into a no-op, so per-test overhead dominates).

    uv run python packages/pytest-coverset/scripts/measure_overhead.py
    uv run python packages/pytest-coverset/scripts/measure_overhead.py --target examples/demo-app --source ledger
    uv run python packages/pytest-coverset/scripts/measure_overhead.py --target examples/bench-suite \\
        --source benchsuite --conftest-append packages/pytest-coverset/scripts/stub_busy_conftest.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

FIXTURE_PROJECT = Path(__file__).resolve().parent.parent / "tests" / "fixture_project"
IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", ".hypothesis", ".pytest_cache", "coverset-out*", ".coverset"
)
OK_EXIT_CODES = (0, 1)
MODES = ("plain", "covpy", "coverage", "coverage_shapes", "neutral")


def mode_args(mode: str, out_dir: Path, sources: list[str]) -> list[str]:
    """Extra pytest arguments for one mode (``covpy`` wraps the command instead)."""
    if mode in ("plain", "covpy"):
        return []
    # `--opt=value` words, as the runner passes them: a separate path word could move pytest's rootdir.
    args = [f"--coverset-out={out_dir}"]
    args += [f"--coverset-source={source}" for source in sources]
    args.append("--coverset-shapes=" + ("off" if mode == "coverage" else "on"))
    if mode == "neutral":
        args.append("--coverset-neutral")
    return args


def command(python: str, mode: str, out_dir: Path, sources: list[str], extra: list[str]) -> list[str]:
    """The full command line of one run."""
    pytest_args = ["-q", "-p", "no:cacheprovider", *mode_args(mode, out_dir, sources), *extra]
    if mode == "covpy":
        source = ",".join(sources) or "."
        data = out_dir / "coverage.sqlite"
        return [
            python,
            "-m",
            "coverage",
            "run",
            "--branch",
            f"--source={source}",
            f"--data-file={data}",
            "-m",
            "pytest",
            *pytest_args,
        ]
    return [python, "-m", "pytest", *pytest_args]


def run_once(cmd: list[str], workdir: Path) -> float:
    """Run one command and return its wall time in seconds."""
    env = {**os.environ, "COVERAGE_CORE": "ctrace"}
    env.pop("COVERSET", None)
    env.pop("COVERSET_NEUTRAL", None)
    started = time.perf_counter()
    completed = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, check=False, env=env)
    elapsed = time.perf_counter() - started
    if completed.returncode not in OK_EXIT_CODES:
        tail = "\n".join(completed.stdout.splitlines()[-20:] + completed.stderr.splitlines()[-20:])
        raise SystemExit(f"exit {completed.returncode} for {' '.join(cmd)}:\n{tail}")
    return elapsed


def plugin_timings(out_dir: Path) -> dict[str, float]:
    """The plugin's own timings from one run's ``meta.json`` (flattened), plus the bundle sizes."""
    try:
        meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    timings = meta.get("timings") or {}
    flat: dict[str, float] = {
        key: float(value) for key, value in timings.items() if isinstance(value, int | float)
    }
    for key, value in (timings.get("finalize") or {}).items():
        flat[f"finalize.{key}"] = float(value)
    neutral = meta.get("neutral") or {}
    if isinstance(neutral.get("seconds"), int | float):
        flat["neutral_export_s"] = float(neutral["seconds"])
    xdist = meta.get("xdist") or {}
    if isinstance(xdist.get("merge_s"), int | float):
        flat["xdist_merge_s"] = float(xdist["merge_s"])
    for name in ("bundle.tar.gz", "neutral.tar.gz", "coverage.sqlite"):
        path = out_dir / name
        if path.is_file():
            flat[f"bytes.{name}"] = float(path.stat().st_size)
    return flat


def measure(
    target: Path,
    sources: list[str],
    repeats: int,
    python: str,
    extra: list[str],
    modes: tuple[str, ...] = MODES,
    workers: int | None = None,
    conftest_append: Path | None = None,
) -> dict[str, object]:
    """Median wall times per mode for ``target``."""
    if workers:
        extra = ["-n", str(workers), *extra]
        modes = tuple(m for m in modes if m != "covpy")
    timings: dict[str, list[float]] = {mode: [] for mode in modes}
    internals: dict[str, list[dict[str, float]]] = {mode: [] for mode in modes}
    with tempfile.TemporaryDirectory(prefix="coverset-overhead-") as tmp:
        workdir = Path(tmp) / "target"
        shutil.copytree(target, workdir, ignore=IGNORED)
        if conftest_append is not None:
            conftest = workdir / "tests" / "conftest.py"
            existing = conftest.read_text(encoding="utf-8") if conftest.is_file() else ""
            conftest.write_text(
                existing + "\n" + conftest_append.read_text(encoding="utf-8"), encoding="utf-8"
            )
        for round_number in range(repeats):
            for mode in modes:
                out_dir = Path(tmp) / f"out-{mode}-{round_number}"
                out_dir.mkdir()
                cmd = command(python, mode, out_dir, sources, extra)
                timings[mode].append(run_once(cmd, workdir))
                internals[mode].append(plugin_timings(out_dir))
    medians = {mode: statistics.median(values) for mode, values in timings.items()}
    plain = medians.get("plain")
    result: dict[str, Any] = {f"{mode}_s": round(value, 3) for mode, value in medians.items()}
    if plain:
        for mode, value in medians.items():
            if mode != "plain":
                result[f"{mode}_x"] = round(value / plain, 2)
        if "coverage_shapes" in medians:
            result["overhead_x"] = result["coverage_shapes_x"]
    result.update(
        repeats=repeats,
        workers=workers,
        target=str(target),
        python=subprocess.run(
            [python, "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        runs_s={mode: [round(v, 3) for v in values] for mode, values in timings.items()},
        plugin_timings={mode: _median_dicts(rows) for mode, rows in internals.items() if any(rows)},
    )
    return result


def _median_dicts(rows: list[dict[str, float]]) -> dict[str, float]:
    keys = sorted({key for row in rows for key in row})
    return {key: round(statistics.median(row[key] for row in rows if key in row), 6) for key in keys}


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
    parser.add_argument(
        "--modes", default=",".join(MODES), help=f"comma-separated subset of {', '.join(MODES)}"
    )
    parser.add_argument(
        "--workers", type=int, default=None, help="pytest-xdist workers (-n N) for every mode"
    )
    parser.add_argument(
        "--conftest-append", type=Path, default=None, help="append FILE to tests/conftest.py of the copy"
    )
    parser.add_argument("--output", type=Path, help="also write the JSON here")
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments (after --)")
    args = parser.parse_args(argv)
    sources = (
        args.source if args.source is not None else (["fixturepkg"] if args.target == FIXTURE_PROJECT else [])
    )
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    unknown = sorted(set(modes) - set(MODES))
    if unknown:
        parser.error(f"unknown modes: {', '.join(unknown)}")
    result = measure(
        args.target.resolve(),
        sources,
        args.repeats,
        args.python,
        args.pytest_args,
        modes=modes,
        workers=args.workers,
        conftest_append=args.conftest_append,
    )
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
