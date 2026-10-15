"""Write ``fixtures/bundles/neutral-ts/``: a synthetic TypeScript neutral bundle (docs/neutral-bundle.md).

The repository is invented (``repo/``: three TypeScript files, eight functions) and so is the run:
no TypeScript was compiled or executed. The bundle is what a Vitest reporter following the spec
would write for twelve tests over those files, built here with ``coverset_model`` and written with
``write_bundle`` (plus ``links.jsonl``, written by hand to exercise that file). Everything is
deterministic: fixed timestamps, gzip without mtime, sorted rows, so a rerun rewrites the same bytes.

    uv run python scripts/make_neutral_fixture.py            # rewrite the fixture
    uv run python scripts/make_neutral_fixture.py --check    # exit 1 when the fixture is stale
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from coverset_model.io import pack, write_bundle
from coverset_model.neutral import (
    IMPORT_CONTEXT,
    CollectedItem,
    FrozenMap,
    FuncSampling,
    FunctionRegion,
    NeutralBundle,
    ShapeObs,
    StaticFile,
    TestRow,
    func_key,
)
from coverset_model.types import TypeDescriptor as T

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "bundles" / "neutral-ts"

PRICING = "src/cart/pricing.ts"
STRINGS = "src/util/strings.ts"
RATES = "src/shipping/rates.ts"

SOURCES = {
    PRICING: """\
export enum Currency {
  USD = "USD",
  EUR = "EUR",
}

export function applyDiscount(totalCents: number, percent: number): number {
  if (percent <= 0) {
    return totalCents;
  }
  const off = Math.round((totalCents * percent) / 100);
  return totalCents - off;
}

export function formatPrice(cents: number, currency: Currency): string {
  const units = (cents / 100).toFixed(2);
  return currency === Currency.EUR ? `${units} €` : `$${units}`;
}

export class Cart {
  private items = new Map<string, number>();

  addItem(sku: string, qty: number): void {
    if (qty <= 0) {
      throw new RangeError("qty must be positive");
    }
    this.items.set(sku, (this.items.get(sku) ?? 0) + qty);
  }
}
""",
    STRINGS: """\
export function slugify(title: string): string {
  return title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-");
}

export function parseQuantity(text?: string | null): number | undefined {
  if (text == null || text.trim() === "") {
    return undefined;
  }
  const n = Number.parseInt(text, 10);
  return Number.isNaN(n) ? undefined : n;
}

export function pad(value: string | number, width = 8): string {
  return String(value).padStart(width, " ");
}
""",
    RATES: """\
export type Zone = "domestic" | "eu" | "world";

const BASE: Record<Zone, number> = { domestic: 500, eu: 900, world: 1500 };

export function rateFor(zone: Zone, weightsGrams: number[]): number {
  const total = weightsGrams.reduce((sum, g) => sum + g, 0);
  const extra = Math.max(0, Math.ceil((total - 1000) / 500)) * 200;
  return BASE[zone] + extra;
}

export function isFreeShipping(totalCents: number): boolean {
  return totalCents >= 5000;
}
""",
}

# Statements (Istanbul's statement map, first lines; declarations count, as module code), the
# possible branch arcs (each branch location to each target's first line) and the lines the
# module-level code ran at import time.
STATIC = {
    PRICING: (
        [1, 2, 3, 6, 7, 8, 10, 11, 14, 15, 16, 19, 20, 22, 23, 24, 26],
        [(7, 8), (7, 10), (23, 24), (23, 26)],
    ),
    STRINGS: ([1, 2, 8, 9, 10, 12, 13, 16, 17], [(9, 10), (9, 12)]),
    RATES: ([3, 5, 6, 7, 8, 11, 12], []),
}
IMPORT_LINES = {PRICING: [1, 2, 3, 6, 14, 19], STRINGS: [1, 8, 16], RATES: [3, 5, 11]}

INT, FLOAT, STR, NONE, BOOL = (
    T.scalar("int"),
    T.scalar("float"),
    T.scalar("str"),
    T.scalar("none"),
    T.scalar("bool"),
)
CURRENCY = T.enum("Currency", ("USD", "EUR"))

# qualname, path, def_line, body_start, end_line, kind, params, types (params and return)
FUNCTIONS = [
    ("applyDiscount", PRICING, 6, 7, 12, "function", ("totalCents", "percent"),
     {"totalCents": INT, "percent": FLOAT, "return": INT}),
    ("formatPrice", PRICING, 14, 15, 17, "function", ("cents", "currency"),
     {"cents": INT, "currency": CURRENCY, "return": STR}),
    ("Cart.addItem", PRICING, 22, 23, 27, "method", ("sku", "qty"), {"sku": STR, "qty": INT, "return": NONE}),
    ("slugify", STRINGS, 1, 2, 6, "function", ("title",), {"title": STR, "return": STR}),
    ("parseQuantity", STRINGS, 8, 9, 14, "function", ("text",),
     {"text": T.union(STR, NONE), "return": T.union(INT, NONE)}),
    ("pad", STRINGS, 16, 17, 18, "function", ("value", "width"),
     {"value": T.union(STR, FLOAT), "width": INT, "return": STR}),
    ("rateFor", RATES, 5, 6, 9, "function", ("zone", "weightsGrams"),
     {"zone": T.literal("domestic", "eu", "world"), "weightsGrams": T.list_of(INT), "return": INT}),
    ("isFreeShipping", RATES, 11, 12, 13, "function", ("totalCents",), {"totalCents": INT, "return": BOOL}),
]  # fmt: skip

P, S, R = "tests/pricing.test.ts", "tests/strings.test.ts", "tests/rates.test.ts"
# id, file, line, outcome, duration, failure (phase, message), property, executed lines by path
TESTS = [
    (f"{P} > applyDiscount > takes 10% off", P, 4, "passed", 0.004, None, False, {PRICING: [7, 10, 11]}),
    (f"{P} > applyDiscount > rounds half cents", P, 8, "passed", 0.003, None, False, {PRICING: [7, 10, 11]}),
    (f"{P} > formatPrice > formats euros", P, 14, "failed", 0.006,
     ("call", "AssertionError: expected '19.99 €' to be '€19.99'"), False, {PRICING: [15, 16]}),
    (f"{P} > Cart > adds an item", P, 21, "passed", 0.002, None, False, {PRICING: [20, 23, 26]}),
    (f"{P} > Cart > rejects a zero quantity", P, 26, "passed", 0.002, None, False, {PRICING: [20, 23, 24]}),
    (f"{S} > slugify > lower-cases and dashes", S, 4, "passed", 0.001, None, False, {STRINGS: [2]}),
    (f"{S} > parseQuantity > reads digits", S, 9, "passed", 0.001, None, False, {STRINGS: [9, 12, 13]}),
    (f"{S} > parseQuantity > returns undefined for blank input", S, 13, "passed", 0.001, None, False,
     {STRINGS: [9, 10]}),
    (f"{S} > pad > pads decimals", S, 18, "failed", 0.002,
     ("call", "AssertionError: expected '   7.5' to have length 5"), False, {STRINGS: [17]}),
    (f"{S} > pad > pads unicode", S, 22, "skipped", 0.0, None, False, {}),
    (f"{R} > rateFor > charges extra weight above a kilogram", R, 6, "passed", 0.412, None, True,
     {RATES: [6, 7, 8]}),
    (f"{R} > isFreeShipping > is free from 50 euros", R, 15, None, None, None, False, {}),
]  # fmt: skip
LINKS = [(TESTS[0][0], "discount_never_negative"), (TESTS[10][0], "shipping_cost_monotone")]
T_START = 1_791_720_000.0


def _hash(text: str) -> str:
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


def regions() -> list[FunctionRegion]:
    found = []
    for qualname, path, def_line, body_start, end_line, kind, params, _ in FUNCTIONS:
        lines = SOURCES[path].splitlines()
        signature = " ".join(line.strip() for line in lines[def_line - 1 : body_start - 1])
        body = "\n".join(line.strip() for line in lines[body_start - 1 : end_line])
        found.append(
            FunctionRegion(
                func_key(path, qualname, 0), path, qualname, 0, def_line, body_start, end_line,
                _hash(body), _hash(signature), params, kind,  # type: ignore[arg-type]
            )
        )  # fmt: skip
    return found


def shapes(by_name: dict[str, FunctionRegion]) -> tuple[list[ShapeObs], list[FuncSampling]]:
    """Observation rows (caller: the test) and one sampling row per traced function."""
    t = {
        name: TESTS[i][0]
        for i, name in enumerate(["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9", "t10", "t11", "t12"])
    }
    rows = [
        # function, param, shape key, samples, tests, example, example test
        ("applyDiscount", "totalCents", "int|pos", 2, ("t1", "t2"), "999", "t2"),
        ("applyDiscount", "percent", "int|pos", 1, ("t1",), "10", "t1"),
        ("applyDiscount", "percent", "float|pos", 1, ("t2",), "12.5", "t2"),
        ("applyDiscount", "return", "int|pos", 2, ("t1", "t2"), "874", "t2"),
        ("formatPrice", "cents", "int|pos", 1, ("t3",), "1999", "t3"),
        ("formatPrice", "currency", "str|ascii|len:2-10", 1, ("t3",), "Currency.EUR", "t3"),
        ("formatPrice", "return", "str|len:2-10|unicode", 1, ("t3",), "'19.99 €'", "t3"),
        ("Cart.addItem", "<receiver>", "obj:src.cart.pricing.Cart", 2, ("t4", "t5"), None, None),
        ("Cart.addItem", "sku", "str|ascii|digits|len:2-10", 2, ("t4", "t5"), "'SKU-1'", "t4"),
        ("Cart.addItem", "qty", "int|pos", 1, ("t4",), "2", "t4"),
        ("Cart.addItem", "qty", "int|zero", 1, ("t5",), "0", "t5"),
        ("Cart.addItem", "return", "NoneType", 1, ("t4",), "undefined", "t4"),
        ("slugify", "title", "str|ascii|len:11-100", 1, ("t6",), "'Hello World'", "t6"),
        ("slugify", "return", "str|ascii|len:11-100", 1, ("t6",), "'hello-world'", "t6"),
        ("parseQuantity", "text", "str|ascii|digits|kind:numeric|len:2-10", 1, ("t7",), "'42'", "t7"),
        ("parseQuantity", "text", "str|len:2-10|whitespace", 1, ("t8",), "'  '", "t8"),
        ("parseQuantity", "return", "int|pos", 1, ("t7",), "42", "t7"),
        ("parseQuantity", "return", "NoneType", 1, ("t8",), "undefined", "t8"),
        ("pad", "value", "float|pos", 1, ("t9",), "7.5", "t9"),
        ("pad", "width", "int|pos", 1, ("t9",), "6", "t9"),
        ("rateFor", "zone", "str|ascii|len:2-10", 2048, ("t11",), "'eu'", "t11"),
        ("rateFor", "weightsGrams", "list|elem:int|len:2-10|order:sorted", 1500, ("t11",), None, None),
        ("rateFor", "weightsGrams", "list|elem:int|len:1", 548, ("t11",), None, None),
        ("rateFor", "return", "int|pos", 2048, ("t11",), "900", "t11"),
    ]
    obs = [
        ShapeObs(
            func=by_name[func].triple,
            param=param,
            shape_key=key,
            caller=t[tests[0]],
            samples=samples,
            tests=tuple(t[x] for x in tests),
            truncated=func == "rateFor",
            example=example,
            example_test=t[example_test] if example_test else None,
        )
        for func, param, key, samples, tests, example, example_test in rows
    ]
    sampled = {
        "applyDiscount": (2, ("t1", "t2")),
        "formatPrice": (1, ("t3",)),
        "Cart.addItem": (2, ("t4", "t5")),
        "slugify": (1, ("t6",)),
        "parseQuantity": (2, ("t7", "t8")),
        "pad": (1, ("t9",)),
        "rateFor": (2048, ("t11",)),
    }
    sampling = [
        FuncSampling(
            by_name[name].triple,
            calls,
            tuple(t[x] for x in tests),
            truncated=name == "rateFor",
            cap="sample" if name == "rateFor" else None,
        )
        for name, (calls, tests) in sampled.items()
    ]
    return obs, sampling


def build() -> NeutralBundle:
    found = regions()
    by_name = {r.qualname: r for r in found}
    static = {
        path: StaticFile(path, len(SOURCES[path].splitlines()), tuple(statements), (), tuple(branches))
        for path, (statements, branches) in STATIC.items()
    }
    collected, results, durations = [], [], {}
    coverage: dict[str, dict[str, tuple[int, ...]]] = {
        IMPORT_CONTEXT: {p: tuple(v) for p, v in IMPORT_LINES.items()}
    }
    start = T_START
    for test_id, file, line, outcome, duration, failure, prop, lines in TESTS:
        markers = ("property",) if prop else ()
        collected.append(CollectedItem(test_id, file, line, markers, (), prop))
        if outcome is not None:
            phase, message = failure if failure else (None, None)
            sig = _hash(f"AssertionError\0{file}:{line}") if failure else None
            results.append(
                TestRow(test_id, file, outcome, duration, phase, sig, message, start, start + duration)
            )
            durations[test_id] = duration
            start = round(start + duration + 0.001, 6)
        if lines:
            coverage[test_id] = {path: tuple(v) for path, v in lines.items()}
    executed = {
        PRICING: ((7, 10), (23, 24), (23, 26)),  # (7, 8) never ran: line 7 is a partial branch
        STRINGS: ((9, 10), (9, 12)),
    }
    obs, sampling = shapes(by_name)
    types = FrozenMap({by_name[q].triple: FrozenMap(t) for q, *_, t in FUNCTIONS})
    return NeutralBundle(
        meta={
            "runner": "vitest",
            "runner_version": "3.2.4",
            "coverage": "node:inspector precise",
            "node": "22.11.0",
        },
        collected=tuple(collected),
        tests=tuple(results),
        coverage=coverage,
        static=static,
        run_arcs=executed,
        shapes=tuple(obs),
        sampling=tuple(sampling),
        durations=durations,
        functions=tuple(found),
        types=types,
    )


def write(target: Path) -> None:
    for child in ("bundle", "repo", "bundle.tar.gz"):
        path = target / child
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    for rel, text in SOURCES.items():
        path = target / "repo" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    bundle_dir = target / "bundle"
    write_bundle(
        build(),
        bundle_dir,
        manifest={
            "language": "typescript",
            "producer": {"name": "vitest-coverset-fixture", "version": "0.0.1"},
            "created_at": "2026-10-11T12:00:00Z",
            "shard": 0,
        },
    )
    links = "".join(json.dumps({"test": test, "invariant": inv}) + "\n" for test, inv in LINKS)
    (bundle_dir / "links.jsonl").write_text(links, encoding="utf-8")
    pack(bundle_dir, target / "bundle.tar.gz")


def _same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    return not mismatch and not errors and all(_same_tree(a / d, b / d) for d in cmp.common_dirs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with the committed fixture instead")
    args = parser.parse_args(argv)
    if not args.check:
        FIXTURE.mkdir(parents=True, exist_ok=True)
        write(FIXTURE)
        print(f"wrote {FIXTURE.relative_to(ROOT)}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        write(Path(tmp))
        for child in ("bundle", "repo"):
            if not _same_tree(Path(tmp) / child, FIXTURE / child):
                print(f"{FIXTURE.relative_to(ROOT)}/{child} is stale: run scripts/make_neutral_fixture.py")
                return 1
        if (Path(tmp) / "bundle.tar.gz").read_bytes() != (FIXTURE / "bundle.tar.gz").read_bytes():
            print(f"{FIXTURE.relative_to(ROOT)}/bundle.tar.gz is stale: run scripts/make_neutral_fixture.py")
            return 1
    print("fixture is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
