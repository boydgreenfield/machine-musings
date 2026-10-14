"""The treemap's view settings: module weight (full / thin / hidden), the ordered layout, the
baseline frame of a PR run, the stability measure, and how the map page and the overview read them.
"""

from __future__ import annotations

import html as html_lib
import random
import re
from pathlib import Path

import pytest
from quart.typing import TestClientProtocol as Client
from web_helpers import Demo, assert_csp_clean

from coverset.facts.queries import TreeNode
from coverset.web import repos
from coverset.web.render.treemap import (
    MAP_COOKIE,
    MODULE_WHY,
    Rect,
    TreeItem,
    anchor_to,
    build_tree,
    centre_moves,
    explicit_view_prefs,
    is_module,
    layout,
    ordered_split,
    reader_map_choice,
    render_legend,
    render_treemap,
    resolve_view,
    weigh_modules,
)

EPS = 1e-6


def _nodes() -> list[TreeNode]:
    return [
        TreeNode("src", "dir", 150, 100, 0.5),
        TreeNode("src/a.py", "file", 100, 70, 0.5),
        TreeNode("src/a.py::<module>", "function", 40, 40, None),
        TreeNode("src/a.py::f", "function", 50, 25, 0.25, func_key="f", def_line=10),
        TreeNode("src/a.py::g", "function", 10, 5, 0.5, func_key="g", def_line=30),
        TreeNode("src/b.py", "file", 50, 30, None),
        TreeNode("src/b.py::<module>", "function", 50, 30, None),
    ]


def _by_path(tree: TreeItem) -> dict[str, TreeItem]:
    out: dict[str, TreeItem] = {}

    def walk(item: TreeItem) -> None:
        for child in item.children:
            out[child.path] = child
            walk(child)

    walk(tree)
    return out


def test_full_keeps_module_statements() -> None:
    tree = build_tree(_nodes())
    assert weigh_modules(tree, "full") is tree
    assert _by_path(tree)["src/a.py::<module>"].size == 40


def test_thin_caps_module_code_at_a_tenth_of_the_file_functions() -> None:
    weighed = _by_path(weigh_modules(build_tree(_nodes()), "thin"))
    module = weighed["src/a.py::<module>"]
    assert module.size == pytest.approx(6.0)  # 10% of f + g (60 statements), less than its 40
    assert module.reduced and "drawn as 6 of 40 statements" in module.note
    assert module.statements == 40 and module.covered == 40  # the numbers in the hover text stay
    assert weighed["src/a.py"].size == pytest.approx(66.0)
    # a file of nothing but module-level code keeps a sliver (at least 1)
    assert weighed["src/b.py::<module>"].size == 1.0 and weighed["src/b.py"].size == 1.0
    # weighing twice changes nothing
    twice = _by_path(weigh_modules(weigh_modules(build_tree(_nodes()), "thin"), "thin"))
    assert {k: v.size for k, v in twice.items()} == {k: v.size for k, v in weighed.items()}


def test_thin_leaves_small_module_code_alone() -> None:
    nodes = [
        TreeNode("a.py", "file", 102, 102, None),
        TreeNode("a.py::<module>", "function", 2, 2, None),
        TreeNode("a.py::f", "function", 100, 100, None, func_key="f", def_line=3),
    ]
    module = _by_path(weigh_modules(build_tree(nodes), "thin"))["a.py::<module>"]
    assert module.size == 2 and not module.reduced and module.note == ""


def test_hidden_drops_module_leaves_and_notes_them_on_the_file() -> None:
    weighed = weigh_modules(build_tree(_nodes()), "hidden")
    found = _by_path(weighed)
    assert not any(is_module(item) for item in found.values())
    assert found["src/a.py"].size == 60
    assert found["src/a.py"].note == "module-level code: 40 statements, 100% ran (hidden from the map)"
    # b.py has only module-level code: it weighs nothing and is left out of the layout
    placed = {leaf.item.path for leaf in layout(weighed, Rect(0, 0, 600, 400))}
    assert "src/b.py" not in placed and "src/a.py::f" in placed
    svg = str(
        render_treemap(weighed, leaf_url=lambda i: "/l", dir_url=lambda i: "/d", module_weight="hidden")
    )
    assert "&lt;module&gt;" not in svg
    assert "src/a.py · 100 statements · lines 70% · module-level code: 40 statements" in html_lib.unescape(
        svg
    )


def test_render_marks_reduced_tiles_and_the_weight() -> None:
    svg = str(render_treemap(build_tree(_nodes()), leaf_url=lambda i: "/l", dir_url=lambda i: "/d"))
    assert 'data-modules="thin" data-layout="ordered"' in svg
    assert re.search(r'class="tm-leaf tm-module tm-reduced"', svg)
    lines = str(
        render_treemap(build_tree(_nodes()), mode="lines", leaf_url=lambda i: "/l", dir_url=lambda i: "/d")
    )
    assert re.search(r'class="tm-leaf tm-b5 tm-reduced"', lines)  # still coloured by its line %
    full = str(
        render_treemap(
            build_tree(_nodes()), leaf_url=lambda i: "/l", dir_url=lambda i: "/d", module_weight="full"
        )
    )
    assert "tm-reduced" not in full and 'data-modules="full"' in full


@pytest.mark.parametrize("mode", ["gap", "lines", "inputs"])
def test_legend_says_how_module_code_shows_and_why(mode: str) -> None:
    thin = str(render_legend(mode, "thin"))  # type: ignore[arg-type]
    assert "module-level code shown reduced" in thin and "tm-reduced" in thin
    assert html_lib.unescape(re.search(r'class="lg-module" title="([^"]+)"', thin).group(1)) == MODULE_WHY
    assert "module-level code hidden" in str(render_legend(mode, "hidden"))  # type: ignore[arg-type]
    full = str(render_legend(mode, "full"))  # type: ignore[arg-type]
    assert ("module-level code" in full) == (mode != "lines")
    assert "imports" in MODULE_WHY.lower() and "inflate" in MODULE_WHY


def test_ordered_split_tiles_the_rectangle_in_order() -> None:
    rng = random.Random(3)
    for _ in range(50):
        values = [rng.uniform(0.5, 50) for _ in range(rng.randint(1, 40))]
        box = Rect(0, 0, rng.uniform(50, 1200), rng.uniform(50, 700))
        rects = ordered_split(values, box)
        assert sum(r.area for r in rects) == pytest.approx(box.area)
        for value, rect in zip(values, rects, strict=True):
            assert rect.area == pytest.approx(box.area * value / sum(values))
            assert rect.x >= -EPS and rect.y >= -EPS
            assert rect.x + rect.w <= box.w + EPS and rect.y + rect.h <= box.h + EPS
    assert ordered_split([], Rect(0, 0, 10, 10)) == []


def test_ordered_layout_keeps_source_order_and_is_deterministic() -> None:
    tree = weigh_modules(build_tree(_nodes()), "full")
    first = layout(tree, Rect(0, 0, 900, 500))
    again = layout(tree, Rect(0, 0, 900, 500))
    assert [(leaf.item.path, leaf.rect) for leaf in first] == [(leaf.item.path, leaf.rect) for leaf in again]
    a_py = [leaf.item.path for leaf in first if leaf.item.path.startswith("src/a.py::")]
    assert a_py == ["src/a.py::<module>", "src/a.py::f", "src/a.py::g"]  # module first, then by def line
    squarified = [leaf.item.path for leaf in layout(tree, Rect(0, 0, 900, 500), algorithm="squarified")]
    assert set(squarified) == {leaf.item.path for leaf in first}


def _big_tree(rng: random.Random, files: int = 60) -> list[TreeNode]:
    nodes: list[TreeNode] = []
    for d in range(4):
        sizes = []
        file_nodes: list[TreeNode] = []
        for f in range(files // 4):
            path = f"pkg/d{d}/f{f:02d}.py"
            funcs = [rng.randint(2, 60) for _ in range(rng.randint(1, 8))]
            sizes.append(sum(funcs))
            file_nodes.append(TreeNode(path, "file", sum(funcs), sum(funcs) // 2, 0.5))
            file_nodes += [
                TreeNode(
                    f"{path}::fn{i}", "function", s, s // 2, 0.5, func_key=f"{d}{f}{i}", def_line=i * 10 + 1
                )
                for i, s in enumerate(funcs)
            ]
        nodes.append(TreeNode(f"pkg/d{d}", "dir", sum(sizes), sum(sizes) // 2, 0.5))
        nodes += file_nodes
    return nodes


def _perturb(nodes: list[TreeNode], rng: random.Random) -> list[TreeNode]:
    out = []
    for node in nodes:
        if node.kind == "function" and rng.random() < 0.1:
            node = TreeNode(node.path, node.kind, max(1, round(node.statements * rng.uniform(0.9, 1.1))),
                            node.covered, node.input_cov, node.func_key, node.def_line)  # fmt: skip
        out.append(node)
    return out


def test_ordered_layout_moves_fewer_tiles_than_squarified_when_sizes_change_a_little() -> None:
    rng = random.Random(5)
    moved = {"ordered": 0, "squarified": 0}
    for _ in range(4):
        nodes = _big_tree(rng)
        before, after = build_tree(nodes), build_tree(_perturb(nodes, rng))
        for algorithm in moved:
            a = layout(weigh_modules(before, "thin"), Rect(0, 0, 1200, 620), algorithm=algorithm)  # type: ignore[arg-type]
            b = layout(weigh_modules(after, "thin"), Rect(0, 0, 1200, 620), algorithm=algorithm)  # type: ignore[arg-type]
            moved[algorithm] += centre_moves(a, b, 1200, 620)[0]
    assert moved["ordered"] < moved["squarified"]


def test_centre_moves_counts_function_tiles_beyond_the_threshold() -> None:
    def leaf(path: str, x: float) -> object:
        from coverset.web.render.treemap import Leaf

        return Leaf(TreeItem(path, path, "function", 1), Rect(x, 0, 10, 10), 1)

    before = [leaf("a", 0), leaf("b", 100), leaf("c", 200)]
    after = [leaf("a", 0), leaf("b", 104), leaf("c", 300), leaf("new", 0)]
    assert centre_moves(before, after, 1000, 100) == (1, 3)  # type: ignore[arg-type]


def test_anchor_keeps_the_baseline_frame_and_squeezes_a_new_function_into_its_file() -> None:
    base = weigh_modules(build_tree(_nodes()), "thin")
    grown = [
        *_nodes()[:3],
        TreeNode("src/a.py::f", "function", 58, 25, 0.25, func_key="f", def_line=10),  # grew by 8
        TreeNode("src/a.py::g", "function", 10, 5, 0.5, func_key="g", def_line=30),
        TreeNode("src/a.py::h", "function", 12, 0, None, func_key="h", def_line=50),  # new
        *_nodes()[5:],
    ]
    pr = weigh_modules(build_tree(grown), "thin")
    anchored, differs = anchor_to(pr, base)
    assert differs
    box = Rect(0, 0, 900, 500)
    frame = {leaf.item.path: leaf.rect for leaf in layout(base, box)}
    placed = {leaf.item.path: leaf.rect for leaf in layout(anchored, box)}
    assert placed["src/a.py"] == frame["src/a.py"] and placed["src/b.py"] == frame["src/b.py"]
    assert "src/a.py::h" in placed  # the new function takes a share of its file's frame
    a = frame["src/a.py"]
    h = placed["src/a.py::h"]
    assert a.x - EPS <= h.x and h.x + h.w <= a.x + a.w + EPS
    same, same_differs = anchor_to(base, base)
    assert not same_differs
    assert {leaf.item.path: leaf.rect for leaf in layout(same, box)} == frame
    # rendering an anchored tree keeps its frame (it is not weighed again)
    svg = str(
        render_treemap(anchored, width=900, height=500, leaf_url=lambda i: "/l", dir_url=lambda i: "/d")
    )
    drawn = re.search(
        r'aria-label="src/b\.py [^"]*"[^>]*><title>[^<]*</title><rect class="tm-file" '
        r'x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"',
        svg,
    )
    assert drawn
    b = frame["src/b.py"]
    assert [float(v) for v in drawn.groups()] == pytest.approx([b.x, b.y, b.w, b.h], abs=0.01)


def test_view_resolution_prefers_the_query_then_chosen_settings() -> None:
    choices = ("all", "missing")
    assert resolve_view("highlight", "missing", {"view.highlight": "all"}, choices, "all") == (
        "missing",
        "query",
    )
    assert resolve_view("highlight", "bogus", {"view.highlight": "missing"}, choices, "all") == (
        "missing",
        "prefs",
    )
    assert resolve_view("highlight", None, {}, choices, "all") == ("all", "default")
    assert resolve_view("highlight", None, None, choices, "all") == ("all", "default")
    # resolved settings at their defaults count as unset; a sources map says exactly
    resolved = {"view.highlight": "all", "view.map_mode": "lines", "view.theme": "auto"}
    assert explicit_view_prefs(resolved) == {"view.map_mode": "lines"}

    class Prefs(dict[str, str]):
        sources = {"view.highlight": "user", "view.map_mode": "default"}

    assert explicit_view_prefs(Prefs(resolved)) == {"view.highlight": "all"}
    assert explicit_view_prefs("nope") == {} and explicit_view_prefs(None) == {}


def test_the_reader_map_cookie_comes_after_the_settings_and_before_the_default() -> None:
    assert reader_map_choice("lines.hidden") == {"map_mode": "lines", "map_module_weight": "hidden"}
    assert reader_map_choice(".full") == {"map_module_weight": "full"}
    assert reader_map_choice("inputs.") == {"map_mode": "inputs"}
    for junk in (None, "", "<x>.<y>", "gap.thin" + "x" * 40, "LINES.FULL", "evil.<script>"):
        assert reader_map_choice(junk) == {}, junk  # only listed values ever reach a page
    assert reader_map_choice("<x>.full") == {"map_module_weight": "full"}  # each part on its own
    weights = ("full", "thin", "hidden")
    assert resolve_view("map_module_weight", None, {}, weights, "thin", "hidden") == ("hidden", "reader")
    assert resolve_view("map_module_weight", None, {}, weights, "thin", "bogus") == ("thin", "default")
    chosen = {"view.map_module_weight": "full"}
    assert resolve_view("map_module_weight", None, chosen, weights, "thin", "hidden") == ("full", "prefs")
    assert resolve_view("map_module_weight", "thin", chosen, weights, "thin", "hidden") == ("thin", "query")


# -- the pages ------------------------------------------------------------------------------------


async def test_the_reader_map_cookie_draws_the_first_request_and_the_overview(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    cookie = {"Cookie": f"{MAP_COOKIE}=lines.hidden"}
    page = await (await client.get(f"/repos/{demo.repo_id}/map", headers=cookie)).get_data(as_text=True)
    # source "reader", not "default": the head script has nothing to reload
    assert 'data-map-mode="lines" data-map-mode-from="reader"' in page
    assert 'data-modules="hidden" data-modules-from="reader"' in page
    assert 'class="treemap mode-lines"' in page
    overview = await (await client.get(f"/repos/{demo.repo_id}", headers=cookie)).get_data(as_text=True)
    thumb = overview.split('class="ov-thumb"', 1)[1].split("</svg>", 1)[0]
    assert 'data-modules="hidden"' in thumb and "&lt;module&gt;" not in thumb
    # the query and the scoped settings both come first
    page = await (await client.get(f"/repos/{demo.repo_id}/map?modules=full", headers=cookie)).get_data(
        as_text=True
    )
    assert 'data-modules="full" data-modules-from="query"' in page

    async def chosen() -> dict[str, str]:
        return {"view.map_mode": "inputs"}

    monkeypatch.setattr(repos, "request_view_prefs", chosen)
    page = await (await client.get(f"/repos/{demo.repo_id}/map", headers=cookie)).get_data(as_text=True)
    assert 'data-map-mode="inputs" data-map-mode-from="prefs"' in page
    assert 'data-modules="hidden" data-modules-from="reader"' in page
    junk = {"Cookie": f"{MAP_COOKIE}=evil.<script>"}
    page = await (await client.get(f"/repos/{demo.repo_id}/map", headers=junk)).get_data(as_text=True)
    assert 'data-modules="thin" data-modules-from="default"' in page and "<script>." not in page


def test_the_script_keeps_the_map_choice_in_a_cookie_not_a_reload() -> None:
    script = (Path(repos.__file__).parent / "static" / "app.js").read_text()
    assert 'document.cookie = "coverset-map=" + value + "; path=/; max-age=31536000; SameSite=Lax"' in script
    remember = script.split("function rememberMap", 1)[1].split("function cycle", 1)[0]
    assert "writeMapCookie(saved);" in remember
    assert "window.location.replace" not in script  # no second request for a saved choice


async def test_map_page_switches_module_weight_and_keeps_it_in_its_links(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}/map")).get_data(as_text=True)
    assert_csp_clean(page)
    assert 'data-modules="thin" data-modules-from="default"' in page  # the head's view meta
    assert re.search(r'data-map-modules="thin" aria-current="true"', page)
    assert page.count("data-map-mode=") >= 3 and "tm-reduced" in page
    hidden = await (await client.get(f"/repos/{demo.repo_id}/map?modules=hidden&layout=squarified")).get_data(
        as_text=True
    )
    assert 'data-modules="hidden" data-layout="squarified"' in hidden
    assert 'data-modules="hidden" data-modules-from="query"' in hidden
    assert "&lt;module&gt;" not in hidden.split("data-tm-box", 1)[1].split("</svg>", 1)[0]
    hrefs = re.findall(r'<a href="([^"]+)" data-map-mode=', hidden)
    assert len(hrefs) == 3
    for href in hrefs:
        assert "modules=hidden" in html_lib.unescape(href) and "layout=squarified" in html_lib.unescape(href)
    assert "module-level code hidden" in hidden


async def test_a_pr_run_map_uses_its_baseline_frame(client: Client, demo: Demo) -> None:
    page = await (await client.get(f"/repos/{demo.repo_id}/map?run={demo.pr1_run}")).get_data(as_text=True)
    assert "layout from baseline" in page and "data-frame" in page
    base = await (await client.get(f"/repos/{demo.repo_id}/map?run={demo.baseline_run}")).get_data(
        as_text=True
    )
    assert "layout from baseline" not in base

    def rects(html: str) -> dict[str, str]:
        out = {}
        for title, geometry in re.findall(
            r'<a [^>]*class="tm-group-link" aria-label="([^" ]+) [^"]*"[^>]*><title>[^<]*</title><rect class="tm-\w+" ([^/]+)/>',
            html,
        ):
            out[title] = geometry
        return out

    pr, frame = rects(page), rects(base)
    unchanged = [p for p in frame if p in pr and not p.startswith("src/ledger/billing")]
    assert unchanged and all(pr[p] == frame[p] for p in unchanged), "outside billing the frame is the same"


async def test_settings_choices_reach_the_map_and_the_overview(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def chosen() -> dict[str, str]:
        return {"view.map_mode": "lines", "view.map_module_weight": "hidden"}

    monkeypatch.setattr(repos, "request_view_prefs", chosen)
    page = await (await client.get(f"/repos/{demo.repo_id}/map")).get_data(as_text=True)
    assert 'data-map-mode="lines" data-map-mode-from="prefs"' in page
    assert 'class="treemap mode-lines"' in page and 'data-modules="hidden"' in page
    # a query parameter still wins over the settings
    page = await (await client.get(f"/repos/{demo.repo_id}/map?mode=gap")).get_data(as_text=True)
    assert 'data-map-mode="gap" data-map-mode-from="query"' in page
    overview = await (await client.get(f"/repos/{demo.repo_id}")).get_data(as_text=True)
    thumb = overview.split('class="ov-thumb"', 1)[1].split("</svg>", 1)[0]
    assert 'data-modules="hidden"' in thumb and "&lt;module&gt;" not in thumb


async def test_view_prefs_never_break_a_page(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken() -> dict[str, str]:
        return {"view.map_mode": "nonsense", "view.map_module_weight": 42}  # type: ignore[dict-item]

    monkeypatch.setattr(repos, "request_view_prefs", broken)
    response = await client.get(f"/repos/{demo.repo_id}/map")
    assert response.status_code == 200
    page = await response.get_data(as_text=True)
    assert 'data-map-mode="gap" data-map-mode-from="default"' in page
