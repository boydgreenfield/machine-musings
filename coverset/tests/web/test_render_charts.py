"""Layout invariants of the treemap, shape-grid cell semantics and the Gantt chart's classes."""

from __future__ import annotations

import itertools
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coverset.analysis.functions import index_functions
from coverset.analysis.shapes import Catalogue
from coverset.facts.queries import TreeNode
from coverset.lang.base import FuncSampling, ShapeObs
from coverset.web.facts_view import FunctionView
from coverset.web.render.shapegrid import render_grid, render_strip, shade_level
from coverset.web.render.timeline import render_gantt
from coverset.web.render.treemap import (
    Rect,
    _bucket,
    build_tree,
    layout,
    leaf_bucket,
    render_legend,
    render_treemap,
    squarify,
)
from coverset.web.runstate import Block

EPS = 1e-6


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.floats(min_value=0.5, max_value=1e4, allow_nan=False), min_size=1, max_size=40),
    st.floats(min_value=10, max_value=2000),
    st.floats(min_value=10, max_value=2000),
)
def test_squarify_tiles_the_rectangle(values: list[float], w: float, h: float) -> None:
    box = Rect(3.0, 7.0, w, h)
    rects = squarify(values, box)
    assert len(rects) == len(values)
    total = sum(values)
    for value, rect in zip(values, rects, strict=True):
        assert abs(rect.area - value / total * box.area) <= 1e-6 * box.area
        assert rect.x >= box.x - EPS and rect.y >= box.y - EPS
        assert rect.x + rect.w <= box.x + box.w + 1e-6 * box.w
        assert rect.y + rect.h <= box.y + box.h + 1e-6 * box.h
    assert abs(sum(r.area for r in rects) - box.area) <= 1e-6 * box.area
    for a, b in itertools.combinations(rects, 2):
        overlap_w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
        overlap_h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
        assert overlap_w <= 1e-6 * w or overlap_h <= 1e-6 * h


def _nodes() -> list[TreeNode]:
    return [
        TreeNode("src", "dir", 30, 20, 0.5),
        TreeNode("src/a", "dir", 20, 10, 0.5),
        TreeNode("src/a/x.py", "file", 20, 10, 0.5),
        TreeNode("src/a/x.py::<module>", "function", 4, 4, None),
        TreeNode("src/a/x.py::f", "function", 10, 6, 0.25, func_key="f", def_line=3),
        TreeNode("src/a/x.py::g", "function", 6, 0, None, func_key="g", def_line=20),
        TreeNode("src/b.py", "file", 10, 10, 1.0),
        TreeNode("src/b.py::h", "function", 10, 10, 1.0, func_key="h", def_line=1),
    ]


def test_tree_and_layout_nest_children_inside_parents() -> None:
    tree = build_tree(_nodes())
    assert tree.path == "src" and tree.statements == 30
    placed = layout(tree, Rect(0, 0, 600, 400))
    boxes = {leaf.item.path: leaf.rect for leaf in placed}
    assert set(boxes) == {
        "src/a",
        "src/a/x.py",
        "src/a/x.py::<module>",
        "src/a/x.py::f",
        "src/a/x.py::g",
        "src/b.py",
        "src/b.py::h",
    }
    for child, parent in (
        ("src/a/x.py::f", "src/a/x.py"),
        ("src/a/x.py", "src/a"),
        ("src/b.py::h", "src/b.py"),
    ):
        c, p = boxes[child], boxes[parent]
        assert (
            p.x - EPS <= c.x
            and c.x + c.w <= p.x + p.w + EPS
            and p.y - EPS <= c.y
            and c.y + c.h <= p.y + p.h + EPS
        )
    assert abs(boxes["src/a"].area + boxes["src/b.py"].area - 600 * 400) < 1e-6


def test_tree_root_narrows_to_a_directory() -> None:
    tree = build_tree(_nodes(), root="src/a")
    assert tree.path == "src/a" and [c.path for c in tree.children] == ["src/a/x.py"]
    assert build_tree(_nodes(), root="src/b.py").children[0].path == "src/b.py::h"


def test_leaf_buckets_per_mode() -> None:
    tree = build_tree(_nodes())
    leaves = {
        leaf.item.name: leaf.item
        for leaf in layout(tree, Rect(0, 0, 600, 400))
        if leaf.item.kind == "function"
    }
    assert leaf_bucket(leaves["g"], "gap")[0] == "tm-hatch"  # never called
    assert leaf_bucket(leaves["g"], "inputs")[0] == "tm-hatch"
    assert leaf_bucket(leaves["g"], "lines")[0] == "tm-b1"  # 0-20 is the first visible bin
    assert leaf_bucket(leaves["<module>"], "gap")[0] == "tm-module"  # outlined, never a gap colour
    assert leaf_bucket(leaves["<module>"], "inputs")[0] == "tm-module"
    assert leaf_bucket(leaves["<module>"], "lines")[0] == "tm-b5"
    cls, title = leaf_bucket(leaves["f"], "gap")  # 60% lines - 25% inputs = 35 points
    assert cls == "tm-g2" and "gap 35 points" in title
    assert leaf_bucket(leaves["h"], "gap")[0] == "tm-g1"


@pytest.mark.parametrize(
    ("value", "bucket"), [(0, 1), (19.9, 1), (20, 2), (39.9, 2), (40, 3), (60, 4), (80, 5), (100, 5)]
)
def test_buckets_have_round_edges(value: float, bucket: int) -> None:
    assert _bucket(value) == bucket


def test_legend_uses_round_edges_and_names_module_code() -> None:
    legend = str(render_legend("gap"))
    assert [m for m in re.findall(r"</svg>(\d+–\d+)</li>", legend)] == [
        "0–20",
        "20–40",
        "40–60",
        "60–80",
        "80–100",
    ]
    assert "module-level code" in legend and "never called" in legend and "no input classes" in legend


def test_treemap_is_a_group_of_labelled_links() -> None:
    svg = str(
        render_treemap(build_tree(_nodes()), leaf_url=lambda i: "/l", dir_url=lambda i: "/d", label="map")
    )
    assert 'role="group"' in svg and 'role="img"' not in svg
    links = re.findall(r"<a [^>]*>", svg)
    assert links and all("aria-label=" in link for link in links)


SOURCE = """\
def check(email: str, tries: int) -> str:
    return email
"""


def _view(observations: list[ShapeObs], sampling: FuncSampling | None = None) -> FunctionView:
    region = next(r for r in index_functions("m.py", SOURCE) if r.qualname == "check")
    cov = Catalogue().function_input_cov(
        region, observations, sampling, annotations={"email": "str", "tries": "int"}
    )
    samples: dict[str, int] = {}
    for obs in observations:
        for name in Catalogue().observed_classes([obs]):
            samples[name] = samples.get(name, 0) + obs.samples
    return FunctionView(
        region, cov, tuple(observations), samples, {"email": "str", "tries": "int"}, "str", (), 100.0, None
    )


def _obs(param: str, key: str, samples: int, *, truncated: bool = False) -> ShapeObs:
    return ShapeObs(
        ("m.py", "check", 1),
        param,
        key,
        "tests/t.py::test_a",
        samples,
        ("tests/t.py::test_a",),
        truncated,
        "'x'",
        "tests/t.py::test_a",
    )


def test_grid_cells_filled_versus_hollow() -> None:
    view = _view(
        [
            _obs("email", "str|ascii|kind:email/plain|len:11-100", 14),
            _obs("tries", "int|pos", 3),
        ]
    )
    cells = {c.name: c for c in view.cells()}
    assert cells["email:plain"].observed and cells["email:plain"].samples == 14
    assert not cells["email:multi-label-domain"].observed and cells["email:multi-label-domain"].samples == 0
    assert cells["tries:positive"].observed and not cells["tries:negative"].observed
    model = view.grid()
    assert [r.param for r in model.rows] == ["email", "tries"]
    assert model.header.startswith(f"Input classes {model.observed} of {model.expected}")
    html = str(render_grid(model, cell_url=lambda c: f"/c?cls={c.name}", target="t"))
    hollow = len(re.findall(r'class="sg-cell is-hollow"', html))
    filled = len(re.findall(r'class="sg-cell"', html))
    assert hollow == model.expected - model.observed and filled == model.observed
    assert "never exercised" in html
    strip = str(render_strip(view.cells()))
    assert strip.count("sg-hollow") == hollow and strip.count("sg-filled") == filled
    chips = view.chips()
    assert chips[0][0] == "email" and all(c.family == "email" for c in chips[0][2])


def test_grid_uses_sampling_wording_when_tests_hit_the_cap() -> None:
    obs = [_obs("email", "str|ascii|kind:email/plain|len:11-100", 2048, truncated=True)]
    sampling = FuncSampling(("m.py", "check", 1), 2048, ("tests/t.py::test_a",), True)
    model = _view(obs, sampling).grid()
    assert model.missing_reason == "not observed in 2048 sampled calls (1 test hit the sample cap)"
    html = str(render_grid(model, cell_url=lambda c: "/c", target="t"))
    assert "not observed in 2048 sampled calls" in html and "never exercised" not in html


def test_never_called_function_has_no_cells() -> None:
    model = _view([]).grid()
    assert model.expected == 0 and all(not r.cells for r in model.rows)
    assert all(r.note == "never called by a test" for r in model.rows)


def test_shade_levels_follow_log_samples() -> None:
    assert [shade_level(n) for n in (0, 1, 9, 10, 99, 100, 5000)] == [0, 1, 1, 2, 2, 3, 4]


def test_gantt_classes() -> None:
    lanes = {
        0: [Block("a", 0.0, 0.5, "passed"), Block("b", 0.5, 0.6, "failed")],
        1: [Block("c", 0.1, 0.4, "passed", flaky=True), Block("d", 0.4, 0.5, "skipped")],
    }
    html = str(render_gantt(lanes, span=1.0, queued={1: 5}, lane_bounds={0: (0.0, 0.6), 1: (0.1, None)}))
    html = html.split('<ul class="legend', 1)[0]
    assert html.count("gt-block gt-pass") == 1 and html.count("gt-block gt-fail") == 1
    assert html.count("gt-block gt-flaky") == 1 and html.count("gt-block gt-other") == 1
    assert "5 queued" in html and "shard 0" in html and "shard 1" in html
    fail = re.search(r'gt-block gt-fail" x="[\d.]+" y="[\d.]+" width="([\d.]+)"', html)
    assert fail and float(fail.group(1)) >= 6.0  # a failure never disappears into a sliver
    assert "No shard has reported" in str(render_gantt({}, span=1.0))


def test_treemap_tiles_carry_their_hover_text_in_data_info() -> None:
    svg = str(
        render_treemap(build_tree(_nodes()), leaf_url=lambda i: "/l", dir_url=lambda i: "/d", label="map")
    )
    links = re.findall(r"<a [^>]*>", svg)
    for link in links:
        label = re.search(r'aria-label="([^"]*)"', link)
        info = re.search(r'data-info="([^"]*)"', link)
        assert label and info and label.group(1) == info.group(1)
    infos = re.findall(r'class="tm-leaf-link" aria-label="[^"]*" data-info="([^"]*)"', svg)
    assert any("::f · " in i and "statements · lines 60%" in i and "input classes 25%" in i for i in infos)
    assert any("::g · " in i and "never called by a test" in i for i in infos)
    lines = str(
        render_treemap(build_tree(_nodes()), mode="lines", leaf_url=lambda i: "/l", dir_url=lambda i: "/d")
    )
    assert "x.py::g · 6 statements · lines 0% · never called by a test" in lines
