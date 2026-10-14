"""Fix-pass regressions: option forms, argument lifetimes, surrogates, tracer loss, early exits,
receivers, callers, code-object retention and the full-trace cap. Every run is a subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import warnings
from pathlib import Path

import pytest
from coverage.exceptions import CoverageWarning
from support import NONCE, PREFIX, contexts, events, read_jsonl, read_shapes

from pytest_coverset.coverage_ctx import CoverageContext


def _meta(out: Path) -> dict:
    return json.loads((out / "meta.json").read_text())


def _members(out: Path) -> list[str]:
    with tarfile.open(out / "bundle.tar.gz") as tar:
        return sorted(tar.getnames())


ALL_MEMBERS = sorted(
    [
        "coverage.sqlite",
        "coverage.json",
        "shapes.jsonl.gz",
        "results.jsonl",
        "collect.json",
        "durations.json",
        "invariant_links.jsonl",
        "meta.json",
    ]
)


# B1: value options as one word ------------------------------------------------------------------


def test_equals_form_keeps_the_ini_file_when_paths_live_outside_the_repo(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's argv shape: every value in `--opt=value` form, paths in a sibling `run/` dir."""
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    repo, run = pytester.path / "repo", pytester.path / "run"
    (repo / "tests").mkdir(parents=True)
    (run / "out").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\nxfail_strict = true\n")
    (repo / "tests" / "test_x.py").write_text(
        "import pytest\n\n@pytest.mark.xfail(reason='must fail when it passes')\ndef test_xpass():\n    pass\n"
    )
    (run / "full.txt").write_text("nothing.py\n")
    (run / "scores.json").write_text("{}")
    monkeypatch.chdir(repo)
    result = pytester.runpytest_subprocess(
        "tests/test_x.py",
        f"--coverset-out={run / 'out'}",
        f"--coverset-scores={run / 'scores.json'}",
        f"--coverset-full-trace={run / 'full.txt'}",
        "--coverset-shapes=on",
        f"--rootdir={repo}",
    )
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["configfile: pytest.ini", "*XPASS(strict)*"])
    assert _meta(run / "out")["configfile"] == str(repo / "pytest.ini")


# M1: no argument outlives `del` ------------------------------------------------------------------

LIFETIME_MODULE = """
import gc
import weakref


def drop_and_check(obj):
    ref = weakref.ref(obj)
    del obj
    gc.collect()
    return ref() is None


class Holder:
    def drop(self, obj):
        ref = weakref.ref(obj)
        del obj
        gc.collect()
        return ref() is None
"""


@pytest.mark.security
def test_arguments_die_when_the_function_deletes_them(pytester: pytest.Pytester) -> None:
    """On 3.12 frame.f_locals is a cached dict snapshot; the tracer must not leave it filled."""
    pytester.mkpydir("lifepkg")
    (pytester.path / "lifepkg" / "mod.py").write_text(LIFETIME_MODULE)
    pytester.makepyfile(
        test_life="""
        from lifepkg.mod import Holder, drop_and_check


        class Plain:
            pass


        # No `assert f(Plain())`: assertion rewriting would keep the argument alive for its message.
        def test_function_argument():
            result = drop_and_check(Plain())
            assert result is True


        def test_method_argument():
            result = Holder().drop(Plain())
            assert result is True
        """
    )
    result = pytester.runpytest_subprocess("--coverset-out", "out", "--coverset-source", "lifepkg")
    result.assert_outcomes(passed=2)
    shapes = read_shapes(pytester.path / "out")
    assert shapes.keys("drop_and_check", "obj") == {"obj:test_life.Plain"}
    assert shapes.keys("Holder.drop", "<receiver>") == {"obj:lifepkg.mod.Holder"}


# M2 + MINOR 4: lone surrogates and huge reasons --------------------------------------------------

SURROGATE_TESTS = """
import pytest


def test_failure_message():
    raise ValueError("bad name \\udcff here")


def test_skip_reason():
    pytest.skip("skipped because \\udcff")


@pytest.mark.xfail(reason="xfail \\udcff")
def test_xfail_reason():
    raise RuntimeError("expected")


def test_huge_skip_reason():
    pytest.skip("r" * 2_000_000)


@pytest.mark.invariant("inv-1")
def test_fine():
    pass
"""


@pytest.mark.security
def test_lone_surrogates_and_huge_reasons_keep_every_result(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8:backslashreplace")  # pytester decodes stdout strictly
    pytester.makepyfile(test_surrogates=SURROGATE_TESTS)
    pytester.makepyfile(test_broken_import='raise RuntimeError("bad \\udcff module")\n')
    result = pytester.runpytest_subprocess("--coverset-out", "out", "--continue-on-collection-errors")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    out = pytester.path / "out"
    meta = _meta(out)
    assert meta["errors"] == []
    rows = {row["nodeid"].split("::")[-1]: row for row in read_jsonl(out / "results.jsonl")}
    assert set(rows) == {
        "test_failure_message",
        "test_skip_reason",
        "test_xfail_reason",
        "test_huge_skip_reason",
        "test_fine",
    }
    assert "bad name ? here" in rows["test_failure_message"]["longrepr"]
    assert rows["test_skip_reason"]["reason"] == "skipped because ?"
    assert rows["test_xfail_reason"]["reason"] == "xfail ?"
    assert len(rows["test_huge_skip_reason"]["reason"].encode()) <= 8 * 1024
    test_events = [e for e in events(pytester) if e["kind"] == "test"]
    assert len(test_events) == 5
    raw = (pytester.path / "stdout").read_text(encoding="utf-8")
    assert max(len(line) for line in raw.split("\n") if line.startswith(PREFIX)) < 64 * 1024
    collect = json.loads((out / "collect.json").read_text())
    assert [e["nodeid"] for e in collect["errors"]] == ["test_broken_import.py"]
    assert "bad ? module" in collect["errors"][0]["longrepr"]
    assert read_jsonl(out / "invariant_links.jsonl") == [
        {"test_id": "test_surrogates.py::test_fine", "invariant_id": "inv-1"}
    ]
    assert _members(out) == ALL_MEMBERS
    assert not list(out.glob("*.tmp"))


# M3: a lost tracer is detected, recorded and repaired --------------------------------------------

TRACER_MODULE = """
def f(x):
    if x > 0:
        return 1
    return 0


def g(x):
    if x > 0:
        return 2
    return 3
"""

TRACER_TESTS = """
import sys
from tlpkg.m import f, g


def test_a():
    assert f(1) == 1


def test_kill():
    sys.settrace(None)
    assert f(-1) == 0


def test_b():
    assert g(1) == 2
"""


@pytest.mark.security
def test_lost_tracer_is_recorded_and_reinstalled(pytester: pytest.Pytester) -> None:
    pytester.mkpydir("tlpkg")
    (pytester.path / "tlpkg" / "m.py").write_text(TRACER_MODULE)
    pytester.makepyfile(test_t=TRACER_TESTS)
    result = pytester.runpytest_subprocess("--coverset-out", "out", "--coverset-source", "tlpkg")
    result.assert_outcomes(passed=3)
    out = pytester.path / "out"
    meta = _meta(out)
    assert meta["tracer_lost_at"] == "test_t.py::test_kill"
    assert meta["tracer_losses"] == [
        {"context": "test_t.py::test_kill", "found": "none", "reinstalled": True}
    ]
    assert any(e.startswith("coverage tracer lost: during test_t.py::test_kill") for e in meta["errors"])
    # test_kill lost its own coverage, but the tracer was back for test_b.
    assert "test_t.py::test_b" in contexts(out / "coverage.sqlite")
    assert "test_t.py::test_kill" not in contexts(out / "coverage.sqlite")


STACKED_COVERAGE_PLUGIN = """
import coverage

_cov = None


def pytest_configure(config):
    global _cov
    _cov = coverage.Coverage(data_file=".stacked-coverage", branch=True)
    _cov.start()


def pytest_unconfigure(config):
    if _cov is not None:
        _cov.stop()
"""


@pytest.mark.security
def test_a_coverage_object_stacked_on_top_is_recorded(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What pytest-cov does without --no-cov: a second Coverage pauses ours. Recorded, not repaired."""
    pytester.mkpydir("tlpkg")
    (pytester.path / "tlpkg" / "m.py").write_text(TRACER_MODULE)
    pytester.makepyfile(test_t=TRACER_TESTS.replace("sys.settrace(None)", "pass"))
    pytester.makepyfile(stackcov=STACKED_COVERAGE_PLUGIN)
    monkeypatch.setenv("PYTHONPATH", str(pytester.path))
    result = pytester.runpytest_subprocess(
        "-p", "stackcov", "--coverset-out", "out", "--coverset-source", "tlpkg"
    )
    result.assert_outcomes(passed=3)
    assert "self._collectors" not in result.stdout.str()
    meta = _meta(pytester.path / "out")
    assert meta["tracer_lost_at"] == "<import>"
    assert meta["tracer_losses"][0] == {
        "context": "<import>",
        "found": "coverage.CTracer",
        "reinstalled": False,
    }
    assert any("not re-installed" in e for e in meta["errors"])


def test_coverage_warnings_are_captured_not_raised(tmp_path: Path) -> None:
    ctx = CoverageContext(tmp_path, [])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with ctx._quiet():
            warnings.warn("Trace function changed, data is likely wrong", CoverageWarning, stacklevel=1)
    assert ctx.warnings == ["Trace function changed, data is likely wrong"]


# MINOR 3: a conftest that fails to import still yields a bundle ----------------------------------


def test_conftest_import_error_still_writes_the_bundle(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    (pytester.path / "tests").mkdir()
    (pytester.path / "tests" / "conftest.py").write_text('raise ImportError("conftest boom")\n')
    (pytester.path / "tests" / "test_ok.py").write_text("def test_ok():\n    pass\n")
    result = pytester.runpytest_subprocess("--coverset-out", "out")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    kinds = [e["kind"] for e in events(pytester)]
    assert kinds == ["bundle_written", "done"]
    assert events(pytester)[-1]["exit_code"] == 4
    meta = _meta(pytester.path / "out")
    assert meta["exit_code"] == 4
    assert any(e.startswith("loading conftest files:") and "conftest boom" in e for e in meta["errors"])
    assert _members(pytester.path / "out") == ALL_MEMBERS


def test_unreadable_scores_file_still_writes_the_bundle(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    pytester.makepyfile(test_one="def test_one():\n    pass\n")
    result = pytester.runpytest_subprocess("--coverset-out", "out", "--coverset-scores", "missing.json")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert [e["kind"] for e in events(pytester)][-2:] == ["bundle_written", "done"]
    assert _meta(pytester.path / "out")["exit_code"] == 4


# MINOR 9: Python 3.11 ----------------------------------------------------------------------------


def test_python_below_3_12_is_a_clear_usage_error() -> None:
    code = (
        "import sys, pytest\n"
        "sys.version_info = (3, 11, 9, 'final', 0)\n"
        "try:\n"
        "    import pytest_coverset.plugin\n"
        "except pytest.UsageError as exc:\n"
        "    print('USAGE', exc)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert proc.stdout.startswith("USAGE pytest-coverset needs Python 3.12 or later")


# MINOR 10: xdist is supported now: test_xdist.py --------------------------------------------


# MINOR 6 + 7: receivers and callers --------------------------------------------------------------

RECEIVER_MODULE = """
import dataclasses


@dataclasses.dataclass
class Money:
    cents: int

    def add(self, other, /, *rest, scale=1, **opts):
        return Money(self.cents + other.cents)

    @classmethod
    def zero(cls):
        return cls(0)

    @staticmethod
    def parse(self):
        return Money(int(self))


def outer():
    def inner(self, x):
        return x
    return inner("not-a-receiver", 1)


def gen_caller(items):
    return sum(helper(i) for i in items)


def list_caller(items):
    return [helper(i) for i in items]


def helper(i):
    return i


VALUE = helper(5)
"""


def test_receivers_need_an_owner_class_and_callers_skip_generator_frames(pytester: pytest.Pytester) -> None:
    pytester.mkpydir("rpkg")
    (pytester.path / "rpkg" / "r.py").write_text(RECEIVER_MODULE)
    pytester.makepyfile(
        test_r="""
        from rpkg.r import Money, gen_caller, list_caller, outer


        def test_methods():
            Money(1).add(Money(2), 3, 4, scale=2, flag=True)
            Money.zero()
            Money.parse("12")
            outer()
            gen_caller([1, 2])
            list_caller([3])
        """
    )
    pytester.runpytest_subprocess("--coverset-out", "out", "--coverset-source", "rpkg").assert_outcomes(
        passed=1
    )
    shapes = read_shapes(pytester.path / "out")
    money = "obj:rpkg.r.Money|dataclass"
    assert shapes.keys("Money.add", "<receiver>") == {money}
    assert shapes.keys("Money.zero", "<receiver>") == {money}
    assert shapes.params("Money.parse") == {"self", "return"}
    assert shapes.keys("Money.parse", "self") == {"str|ascii|digits|kind:numeric|len:2-10"}
    assert shapes.params("outer.<locals>.inner") == {"self", "x", "return"}
    assert shapes.keys("outer.<locals>.inner", "self") == {"str|ascii|len:11-100"}

    callers = shapes.header["callers"]
    named = {
        callers[row["c"]].get("qualname") or callers[row["c"]]["nodeid"] for row in shapes.rows("helper", "i")
    }
    assert named == {"<import>", "gen_caller", "list_caller"}
    assert not any(
        c["kind"] == "func" and (c["qualname"].endswith(">") or c["qualname"] == "<module>") for c in callers
    )


# MINOR 8: non-target code objects are not retained -----------------------------------------------


def test_non_target_code_objects_are_not_retained(pytester: pytest.Pytester) -> None:
    pytester.mkpydir("keep")
    (pytester.path / "keep" / "__init__.py").write_text("def target(x):\n    return x\n")
    pytester.makepyfile(
        test_mem="""
        import gc
        import weakref

        from keep import target


        def test_generated_functions_are_freed(request):
            tracer = request.config.pluginmanager.get_plugin("coverset-session").tracer
            refs = []
            for i in range(500):
                namespace = {}
                exec(compile(f"def f(x):\\n    return x + {i}\\n", f"<gen {i}>", "exec"), namespace)
                namespace["f"](i)
                refs.append(weakref.ref(namespace["f"].__code__))
                del namespace
            gc.collect()
            assert target(1) == 1
            assert sum(ref() is not None for ref in refs) == 0
            assert not any(name.startswith("<gen") for name in tracer._paths)
            assert [state.code.co_name for state in tracer._targets.values()] == ["target"]
        """
    )
    pytester.runpytest_subprocess("--coverset-out", "out").assert_outcomes(passed=1)


# MINOR 12: the full-trace safety cap -------------------------------------------------------------


def test_full_trace_functions_stop_at_the_safety_cap(project: pytest.Pytester) -> None:
    (project.path / "full.txt").write_text("src/fixturepkg/full.py\n")
    project.runpytest_subprocess(
        "--coverset-out",
        "out",
        "--coverset-source",
        "fixturepkg",
        "--coverset-full-trace",
        "full.txt",
        "--coverset-full-trace-cap",
        "100",
    ).assert_outcomes(passed=24)
    out = project.path / "out"
    shapes = read_shapes(out)
    full = shapes.sampling_of("hot_full")
    assert (full["calls"], full["truncated"], full["cap"]) == (100, True, "full")
    assert all(row["trunc"] for row in shapes.rows("hot_full"))
    assert shapes.sampling_of("hot")["cap"] == "sample"
    meta = _meta(out)
    assert meta["full_trace_cap"] == 100 and meta["functions_truncated"] == 2


def test_option_values_in_both_forms(project: pytest.Pytester) -> None:
    project.runpytest_subprocess(
        "--coverset-out=out", "--coverset-source=fixturepkg", "--coverset-shapes=off"
    ).assert_outcomes(passed=24)
    meta = _meta(project.path / "out")
    assert meta["sources"] == ["fixturepkg"] and meta["shapes"] == "off"
