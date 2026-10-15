"""Write ``fixtures/bundles/neutral-rust/``: a real Rust capture of ``examples/rust-app`` and its bundle.

Unlike ``neutral-ts`` this fixture is not synthetic: ``capture/`` is what ``coverset rust run``
left behind for the example crate (cargo-nextest, LLVM source-based coverage, one profile per
test), with absolute paths made relative so it works from any checkout; ``repo/`` is a copy of the
crate's sources; ``bundle/`` and ``bundle.tar.gz`` are the neutral bundle the converter makes from
the capture. The converter's tests and the report test run against it without cargo.

    uv run python scripts/make_rust_fixture.py                 # run the crate (needs cargo-nextest
                                                               # and llvm-tools), rewrite everything
    uv run python scripts/make_rust_fixture.py --from-capture  # rebuild bundle/ from capture/ only
    uv run python scripts/make_rust_fixture.py --check         # exit 1 when bundle/ or repo/ is stale

Normalised so a rerun changes only what really changed: ``created_at`` is fixed, test start times
are rebased onto a fixed clock (their durations, rounded to the millisecond, are kept), and every
path in an export is relative to ``repo/``. The ``.profraw`` files are not kept: they are only
readable together with the test binaries that wrote them.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "examples" / "rust-app"
FIXTURE = ROOT / "fixtures" / "bundles" / "neutral-rust"
CREATED_AT = "2026-10-13T12:00:00Z"
T_START = 1_791_892_800.0
SOURCES = ("Cargo.toml", "Cargo.lock", "src", "tests")
MAX_BYTES = 200 * 1024


def copy_sources(target: Path) -> None:
    """``examples/rust-app``'s sources (no ``target/``, no ``.coverset/``) into ``target``."""
    target.mkdir(parents=True, exist_ok=True)
    for name in SOURCES:
        src = CRATE / name
        if src.is_dir():
            shutil.copytree(src, target / name)
        else:
            shutil.copy2(src, target / name)


def _relative_exports(capture: Path, crate_root: Path) -> None:
    """Rewrite ``SF:`` lines and digest ``file`` fields from absolute to ``repo/``-relative paths."""
    prefixes = sorted({str(crate_root) + "/", str(crate_root.resolve()) + "/"}, key=len, reverse=True)
    for lcov in sorted(capture.glob("exports/**/*.lcov")):
        text = lcov.read_text(encoding="utf-8")
        for prefix in prefixes:
            text = text.replace(f"SF:{prefix}", "SF:")
        lcov.write_text(text, encoding="utf-8")
    digest_path = capture / "exports" / "llvm-functions.json"
    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    for row in digest["functions"]:
        for prefix in prefixes:
            row["file"] = str(row["file"]).removeprefix(prefix)
    digest_path.write_text(json.dumps(digest, indent=0, sort_keys=True) + "\n", encoding="utf-8")


def _normalise_capture(capture: Path) -> None:
    doc = json.loads((capture / "capture.json").read_text(encoding="utf-8"))
    doc["root"] = "../repo"
    doc["created_at"] = CREATED_AT
    clock = T_START
    for test in doc["tests"]:
        if test.get("start") is None:
            continue
        duration = round(float(test.get("duration_s") or 0.0), 3)
        test["duration_s"] = duration
        test["start"] = round(clock, 3)
        test["stop"] = round(clock + duration, 3)
        clock += duration + 0.001
    (capture / "capture.json").write_text(
        json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def record(target: Path) -> None:
    """Run the crate with ``coverset rust run`` (nextest) and keep the normalised capture."""
    from coverset.lang.rust_llvm import run_rust

    with tempfile.TemporaryDirectory(prefix="coverset-rust-fixture-") as tmp:
        crate = Path(tmp) / "repo"
        copy_sources(crate)
        work = Path(tmp) / "work"
        run_rust(crate, out=Path(tmp) / "neutral", work=work, root=crate, runner="nextest", log=print)
        capture = target / "capture"
        shutil.rmtree(capture, ignore_errors=True)
        capture.mkdir(parents=True)
        shutil.copy2(work / "capture.json", capture / "capture.json")
        shutil.copytree(work / "exports", capture / "exports")
        _relative_exports(capture, crate)
    _normalise_capture(target / "capture")


def build(target: Path) -> None:
    """``repo/`` from the crate, then ``bundle/`` and ``bundle.tar.gz`` from ``capture/``."""
    from coverset.lang.rust_llvm import convert_capture, write_neutral
    from coverset_model.io import pack

    shutil.rmtree(target / "repo", ignore_errors=True)
    copy_sources(target / "repo")
    shutil.rmtree(target / "bundle", ignore_errors=True)
    (target / "bundle.tar.gz").unlink(missing_ok=True)
    bundle = convert_capture(target / "capture")
    write_neutral(bundle, target / "bundle", created_at=CREATED_AT)
    pack(target / "bundle", target / "bundle.tar.gz")


def _same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    return not mismatch and not errors and all(_same_tree(a / d, b / d) for d in cmp.common_dirs)


def size(target: Path) -> int:
    return sum(p.stat().st_size for p in target.rglob("*") if p.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare a rebuild with the committed fixture")
    mode.add_argument("--from-capture", action="store_true", help="rebuild bundle/ and repo/ from capture/")
    args = parser.parse_args(argv)
    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(FIXTURE / "capture", Path(tmp) / "capture")
            build(Path(tmp))
            for child in ("bundle", "repo"):
                if not _same_tree(Path(tmp) / child, FIXTURE / child):
                    print(
                        f"{FIXTURE.relative_to(ROOT)}/{child} is stale: run scripts/make_rust_fixture.py --from-capture"
                    )
                    return 1
            if (Path(tmp) / "bundle.tar.gz").read_bytes() != (FIXTURE / "bundle.tar.gz").read_bytes():
                print(f"{FIXTURE.relative_to(ROOT)}/bundle.tar.gz is stale")
                return 1
        print("fixture is up to date")
        return 0
    FIXTURE.mkdir(parents=True, exist_ok=True)
    if not args.from_capture:
        record(FIXTURE)
    build(FIXTURE)
    total = size(FIXTURE)
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({total:,} bytes)")
    if total > MAX_BYTES:
        print(f"warning: the fixture is over {MAX_BYTES:,} bytes")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
