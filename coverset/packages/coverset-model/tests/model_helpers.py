"""A small neutral bundle built in memory, for the model package's tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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

PATH = "src/calc.ts"
T1, T2, T3 = (
    "tests/calc.test.ts > add > adds",
    "tests/calc.test.ts > add > negatives",
    "tests/calc.test.ts > div",
)
ADD = FunctionRegion(func_key(PATH, "add", 0), PATH, "add", 0, 1, 2, 4, "b1", "s1", ("a", "b"), "function")
DIV = FunctionRegion(func_key(PATH, "div", 0), PATH, "div", 0, 6, 7, 10, "b2", "s2", ("a", "b"), "function")


def small_bundle() -> NeutralBundle:
    """Two functions, three tests (one never ran), one partial branch, a capped function."""
    return NeutralBundle(
        meta={"runner": "vitest"},
        collected=(
            CollectedItem(T1, "tests/calc.test.ts", 3, (), ("add_commutes",), False),
            CollectedItem(T2, "tests/calc.test.ts", 7, ("property",), (), True),
            CollectedItem(T3, "tests/calc.test.ts", 11, (), (), False),
        ),
        tests=(
            TestRow(T1, "tests/calc.test.ts", "passed", 0.01, None, None, None, 1.0, 1.01),
            TestRow(
                T2, "tests/calc.test.ts", "failed", 0.02, "call", "abcd", "AssertionError: 1 != 2", 2.0, 2.02
            ),
        ),
        coverage={
            IMPORT_CONTEXT: {PATH: (1, 6)},
            T1: {PATH: (2, 3)},
            T2: {PATH: (2, 3)},
        },
        static={PATH: StaticFile(PATH, 10, (1, 2, 3, 6, 7, 8, 9), (), ((2, 3), (2, 4)))},
        run_arcs={PATH: ((2, 3),)},
        shapes=(
            ShapeObs(ADD.triple, "a", "int|pos", T1, 1, (T1,), False, "1", T1),
            ShapeObs(ADD.triple, "b", "int|neg", T2, 3, (T2,), True, "-1", T2),
            ShapeObs(ADD.triple, "return", "int|zero", (PATH, "div", 6), 1, (T2,), True, "0", T2),
        ),
        sampling=(FuncSampling(ADD.triple, 4, (T1, T2), True, "sample"),),
        durations={T1: 0.01, T2: 0.02},
        functions=(ADD, DIV),
        types=FrozenMap(
            {
                ADD.triple: FrozenMap(
                    {"a": T.scalar("int"), "b": T.union(T.scalar("int"), T.scalar("none")), "return": None}
                )
            }
        ),
    )


def rewrite_jsonl(path: Path, edit: Any) -> None:
    """Apply ``edit(rows) -> rows`` to a ``.jsonl`` file."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    path.write_text("".join(json.dumps(row) + "\n" for row in edit(rows)))
