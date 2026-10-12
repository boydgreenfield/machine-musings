"""Take the README screenshots: seven pages of the local demo and two pages of its static report,
light (``docs/<name>.jpg``) and dark (``docs/<name>-dark.jpg``).

Seeds a scratch demo with the LocalExecutor (``coverset demo --executor local --no-serve``), serves
it with ``coverset serve`` on a free loopback port with ``COVERSET_FAKE_NOW`` pinned on the serving
process only (so "3 min ago" texts are stable and the runs themselves keep real timelines), and drives
Chromium through Playwright at 1440×900, device scale 1. Full-size PNGs land in ``shots/``
(gitignored); the JPEGs are resized to 1200 px wide at quality 82, lowered stepwise until each is at
most 250 KB. The recipe per page is in docs/ui.md ("Screenshots").

The ``report`` group (``report.jpg``, ``report-source.jpg``, ``report-changes.jpg``) needs no server:
it builds the static report of PR #2's run with ``coverset report --run`` into
``<data-dir>/report-shots`` and PR #1's run compared with its ``main`` baseline (``--compare-run``)
into ``<data-dir>/report-changes-shots``, and opens their pages from ``file://`` (docs/report.md).

    uv run python scripts/screenshots.py                       # seed a temp demo, shoot everything
    uv run python scripts/screenshots.py --data-dir <dir>      # reuse <dir> when it holds a demo
    uv run python scripts/screenshots.py --only pr,map --scheme light
    uv run python scripts/screenshots.py --only report --data-dir <dir>   # the static report only

Needs a Playwright Chromium (``uv run playwright install chromium-headless-shell`` once).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SHOTS = ROOT / "shots"
VIEWPORT = {"width": 1440, "height": 900}
SOURCE_HEIGHT = 1000
OUT_WIDTH = 1200
QUALITY = 82
MIN_QUALITY = 50
MAX_BYTES = 250 * 1024
READY = 'body[data-ready="1"]'
SHOT_NAMES = ("pr", "run-live", "why-order", "shapes", "source", "map", "comment", "test", "repo")
REPORT_SHOT_NAMES = ("report", "report-source", "report-changes")
GROUPS = {"report": REPORT_SHOT_NAMES}
"""``--only`` names that stand for several shots."""
REPORT_LENS_TEST = "tests/test_windows.py::test_expires_at"
"""The test the report-source shot selects in the data-flow lens (it reaches parse.py through a caller)."""
REPORT_FILE_ROWS = 8
"""File-table rows kept under the treemap in report.jpg."""
VALIDATE_EMAIL_PATH = "src/ledger/accounts/emailcheck.py"
FAILING_TEST = "tests/test_refund.py::test_refund_partial"
"""The test whose page test.jpg shows (PR #1's failing test)."""
PARSE_PATH = "src/ledger/durations/parse.py"


@dataclass(frozen=True)
class DemoIds:
    """What the shots need from the demo database."""

    repo_id: int
    pr1_run: str
    pr2_run: str
    freeze_s: float
    fake_now: str


def log(message: str) -> None:
    print(f"[screenshots] {message}", flush=True)


def coverset_bin() -> str:
    """The ``coverset`` console script of this environment."""
    found = Path(sys.executable).with_name("coverset")
    if found.is_file():
        return str(found)
    which = shutil.which("coverset")
    if which is None:
        raise SystemExit("coverset is not installed in this environment: run `uv sync` first")
    return which


def child_env(data_dir: Path, **extra: str) -> dict[str, str]:
    """The environment for a coverset child: this one minus any COVERSET_* setting, plus ours."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERSET_")}
    env.update({"COVERSET_DATA_DIR": str(data_dir), "COVERSET_EXECUTOR": "local", **extra})
    return env


# -- the demo data ----------------------------------------------------------------------------------


def seed(data_dir: Path) -> None:
    """Run the local demo into ``data_dir`` (history, both PRs, the replayed webhook)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log(f"seeding the demo into {data_dir} (about 20 s)")
    logfile = data_dir / "screenshots-seed.log"
    with logfile.open("w", encoding="utf-8") as out:
        result = subprocess.run(
            [coverset_bin(), "demo", "--executor", "local", "--no-serve"],
            cwd=ROOT,
            env=child_env(data_dir),
            stdout=out,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise SystemExit(f"coverset demo failed (exit {result.returncode}); see {logfile}")


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def demo_ids(data_dir: Path) -> DemoIds:
    """Run ids, the replay freeze point and the pinned clock, from ``<data_dir>/coverset.db``."""
    con = sqlite3.connect(data_dir / "coverset.db")
    try:

        def pr_run(number: int) -> tuple[str, dict[str, Any]]:
            row = con.execute(
                "SELECT r.id, r.timeline FROM runs r JOIN pull_requests p ON p.id = r.pr_id "
                "WHERE p.number = ? ORDER BY r.created_at DESC LIMIT 1",
                (number,),
            ).fetchone()
            if row is None:
                raise SystemExit(f"no run of PR #{number} in {data_dir}: seed the demo first")
            return str(row[0]), json.loads(row[1] or "{}")

        pr1, timeline = pr_run(1)
        pr2, _ = pr_run(2)
        repo_id = int(con.execute("SELECT id FROM repos ORDER BY id LIMIT 1").fetchone()[0])
        newest = con.execute("SELECT MAX(created_at) FROM runs").fetchone()[0]
    finally:
        con.close()
    t0 = _ts(timeline.get("webhook_received")) or _ts(timeline.get("queued"))
    verdict = _ts(timeline.get("verdict"))
    if t0 is None or verdict is None:
        raise SystemExit(f"PR #1's run {pr1} has no verdict in its timeline")
    freeze = round((verdict - t0).total_seconds() + 0.1, 2)
    # Pin "now" a few minutes after the newest run, on a whole minute: relative times read "N min ago".
    last = datetime.fromisoformat(str(newest)).replace(tzinfo=UTC)
    now = (last + timedelta(minutes=4)).replace(second=0, microsecond=0)
    return DemoIds(repo_id, pr1, pr2, freeze, now.strftime("%Y-%m-%dT%H:%M:%SZ"))


# -- the server ---------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def serve(data_dir: Path, fake_now: str) -> Iterator[str]:
    """``coverset serve`` on a free port with the clock pinned; stopped (and its group killed) on exit."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    logfile = (data_dir / "screenshots-serve.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [coverset_bin(), "serve", "--bind", f"127.0.0.1:{port}"],
        cwd=ROOT,
        env=child_env(data_dir, COVERSET_FAKE_NOW=fake_now, COVERSET_PUBLIC_URL=base),
        stdout=logfile,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise SystemExit(f"coverset serve exited with {proc.returncode}; see {logfile.name}")
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=2) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            if time.monotonic() > deadline:
                raise SystemExit("coverset serve did not answer /healthz within 30 s")
            time.sleep(0.2)
        log(f"serving {data_dir} on {base} (COVERSET_FAKE_NOW={fake_now})")
        yield base
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        logfile.close()


# -- the browser ----------------------------------------------------------------------------------------


def launch(playwright: Any) -> Any:
    """Chromium from Playwright's cache; falls back to any cached build if the pinned one is missing."""
    try:
        return playwright.chromium.launch()
    except Exception as exc:  # the pinned browser build is not installed
        cache = Path.home() / "Library" / "Caches" / "ms-playwright"
        if not cache.is_dir():
            cache = Path.home() / ".cache" / "ms-playwright"
        candidates = sorted(cache.glob("chromium_headless_shell-*/*/chrome-headless-shell"), reverse=True)
        candidates += sorted(cache.glob("chromium-*/*/Chromium.app/Contents/MacOS/Chromium"), reverse=True)
        candidates += sorted(cache.glob("chromium-*/chrome-*/chrome"), reverse=True)
        if not candidates:
            raise SystemExit(
                f"no Chromium for Playwright ({exc}); run `uv run playwright install chromium-headless-shell`"
            ) from None
        log(f"default launch failed; using {candidates[0]}")
        return playwright.chromium.launch(executable_path=str(candidates[0]))


def wait_ready(page: Any, timeout_ms: float = 30_000) -> None:
    page.wait_for_selector(READY, state="attached", timeout=timeout_ms)


def rest_pointer(page: Any) -> None:
    """Park the mouse in the page's top-left corner, over nothing hoverable.

    Playwright keeps the pointer where the last click left it, across navigations: after the
    source shot clicks line 18's test count, the pointer sat over a treemap tile on the next page
    and its hover outline (the blue ring) ended up in map.jpg.
    """
    page.mouse.move(1, 1)


def goto(page: Any, url: str) -> None:
    page.goto(url, wait_until="load")
    rest_pointer(page)
    wait_ready(page)
    page.wait_for_timeout(200)


def function_key(page: Any, base: str, repo_id: int) -> str:
    """The func_key of ``validate_email`` from the PR #2 page's cards (``/prs/2/functions/<key>``)."""
    goto(page, f"{base}/repos/{repo_id}/prs/2")
    cards = page.eval_on_selector_all(
        "[hx-get*='/functions/'], a[href*='/functions/']",
        """els => els.map(e => [
            e.getAttribute('hx-get') || e.getAttribute('href'),
            (e.querySelector('.fn-name') || e).textContent,
        ])""",
    )
    for url, text in cards:
        if "/prs/2/functions/" in (url or "") and (text or "").strip() == "validate_email":
            return str(url).split("/functions/", 1)[1].split("?", 1)[0].split("/", 1)[0]
    raise SystemExit(f"no validate_email card on /repos/{repo_id}/prs/2")


def shoot_page(page: Any, path: Path) -> None:
    page.screenshot(path=str(path), full_page=False)


def take_shots(base: str, ids: DemoIds, schemes: list[str], only: set[str]) -> list[Path]:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(exist_ok=True)
    pngs: list[Path] = []
    with sync_playwright() as playwright:
        browser = launch(playwright)
        try:
            for scheme in schemes:
                context = browser.new_context(
                    viewport=VIEWPORT, device_scale_factor=1, color_scheme=scheme, reduced_motion="reduce"
                )
                page = context.new_page()
                page.set_default_timeout(30_000)
                func_key = function_key(page, base, ids.repo_id) if "shapes" in only else ""
                recipes = shot_recipes(base, ids, func_key)
                for name in SHOT_NAMES:
                    if name not in only:
                        continue
                    suffix = "" if scheme == "light" else "-dark"
                    target = SHOTS / f"{name}{suffix}.png"
                    log(f"{scheme}: {name}")
                    recipes[name](page, target)
                    pngs.append(target)
                context.close()
        finally:
            browser.close()
    return pngs


def shot_recipes(base: str, ids: DemoIds, func_key: str) -> dict[str, Callable[[Any, Path], None]]:
    rid = ids.repo_id

    def pr(page: Any, path: Path) -> None:
        goto(page, f"{base}/repos/{rid}/prs/2")
        shoot_page(page, path)

    def run_live(page: Any, path: Path) -> None:
        page.goto(f"{base}/runs/{ids.pr1_run}?replay=1&speed=0.25&freeze={ids.freeze_s}", wait_until="load")
        rest_pointer(page)
        page.wait_for_selector('[data-replay="frozen"]', state="attached", timeout=60_000)
        wait_ready(page)
        page.wait_for_timeout(300)
        shoot_page(page, path)

    def why_order(page: Any, path: Path) -> None:
        goto(page, f"{base}/runs/{ids.pr1_run}")
        panel = page.locator("#why-order")
        panel.scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        panel.screenshot(path=str(path))

    def shapes(page: Any, path: Path) -> None:
        goto(page, f"{base}/repos/{rid}/functions/{func_key}?run={ids.pr2_run}")
        shoot_page(page, path)

    def source(page: Any, path: Path) -> None:
        # The tests popover opens below line 18 only with 280 px free under it, and the page is too
        # short to scroll: a 100 px taller viewport keeps the chips under `def parse_duration` visible.
        page.set_viewport_size({"width": VIEWPORT["width"], "height": SOURCE_HEIGHT})
        try:
            goto(page, f"{base}/repos/{rid}/files/{PARSE_PATH}?run={ids.pr2_run}")
            page.locator("#L18 button.gt").click()
            page.wait_for_selector("#line-tests-body .test-list", state="attached")
            wait_ready(page)
            page.wait_for_timeout(250)
            # The popover scrolls inside a capped height, so its own bottom edge cuts a row in
            # half. End the shot just under the last row that is fully visible in it.
            bottom = page.evaluate(
                """() => {
                    const box = document.getElementById('line-tests');
                    const edge = Math.min(box.getBoundingClientRect().bottom, window.innerHeight);
                    let last = box.getBoundingClientRect().top;
                    for (const row of box.querySelectorAll('.test-list li')) {
                        const r = row.getBoundingClientRect();
                        if (r.bottom <= edge - 1) last = Math.max(last, r.bottom);
                    }
                    return last;
                }"""
            )
            height = min(SOURCE_HEIGHT, int(bottom) + 6)
            page.screenshot(
                path=str(path), clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height}
            )
        finally:
            page.set_viewport_size(VIEWPORT)

    def treemap(page: Any, path: Path) -> None:
        goto(page, f"{base}/repos/{rid}/map")
        # The whole treemap, down to the "Largest gaps" table, rather than a viewport cut through it.
        bottom = page.evaluate(
            "() => { const t = document.querySelector('.map-table');"
            " return t ? t.getBoundingClientRect().top + window.scrollY : document.body.scrollHeight; }"
        )
        height = max(VIEWPORT["height"], int(bottom) - 8)
        page.screenshot(
            path=str(path),
            full_page=True,
            clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height},
        )

    def comment(page: Any, path: Path) -> None:
        goto(page, f"{base}/forge/outbox?method=upsert_pr_comment")
        shoot_page(page, path)

    def test_page(page: Any, path: Path) -> None:
        # The failing test of PR #1: identity, result, history and its data flow, down to the end
        # of the "Functions it called" strips of the first function (the page is about 1330 px).
        goto(page, f"{base}/repos/{rid}/tests/by-nodeid?nodeid={FAILING_TEST}&run={ids.pr1_run}")
        page.wait_for_selector("#test-flow svg.flow-test", state="attached")
        bottom = page.evaluate(
            "() => { const s = document.querySelector('#test-flow');"
            " const r = s ? s.getBoundingClientRect() : null;"
            " return r ? r.bottom + window.scrollY : document.body.scrollHeight; }"
        )
        height = max(VIEWPORT["height"], int(bottom) + 16)
        page.screenshot(
            path=str(path),
            full_page=True,
            clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height},
        )

    def repo(page: Any, path: Path) -> None:
        # The repository overview (tiles, trend, changes, Gap thumbnail, largest gaps), down to
        # just under its section; the PR and run lists below it are left out.
        goto(page, f"{base}/repos/{rid}")
        bottom = page.evaluate(
            "() => { const s = document.querySelector('#overview');"
            " const r = s ? s.getBoundingClientRect() : null;"
            " return r ? r.bottom + window.scrollY : document.body.scrollHeight; }"
        )
        height = max(VIEWPORT["height"], int(bottom) + 12)
        page.screenshot(
            path=str(path),
            full_page=True,
            clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height},
        )

    return {
        "pr": pr,
        "run-live": run_live,
        "why-order": why_order,
        "shapes": shapes,
        "source": source,
        "map": treemap,
        "comment": comment,
        "test": test_page,
        "repo": repo,
    }


# -- the static report ------------------------------------------------------------------------------


def build_report_dir(data_dir: Path, ids: DemoIds) -> Path:
    """``coverset report --run <PR #2 run>`` into ``<data_dir>/report-shots`` (rewritten each time)."""
    out = data_dir / "report-shots"
    log(f"building the static report of PR #2's run into {out}")
    result = subprocess.run(
        [coverset_bin(), "report", "--run", ids.pr2_run, "--out", str(out)],
        cwd=ROOT,
        env=child_env(data_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"coverset report failed (exit {result.returncode}): {result.stderr[-2000:]}")
    return out


def build_changes_dir(data_dir: Path, ids: DemoIds) -> Path:
    """``coverset report --run <PR #1 run> --compare-run <the main baseline at its base>`` into
    ``<data_dir>/report-changes-shots`` (rewritten each time)."""
    con = sqlite3.connect(data_dir / "coverset.db")
    try:
        row = con.execute(
            "SELECT b.id FROM runs r JOIN runs b ON b.repo_id = r.repo_id AND b.head_sha = r.base_sha "
            "WHERE r.id = ? AND b.pr_id IS NULL ORDER BY b.created_at DESC LIMIT 1",
            (ids.pr1_run,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit(f"PR #1's run {ids.pr1_run} has no baseline run at its base commit")
    out = data_dir / "report-changes-shots"
    log(f"building the static report of PR #1's run against the main baseline {row[0]} into {out}")
    result = subprocess.run(
        [coverset_bin(), "report", "--run", ids.pr1_run, "--compare-run", str(row[0]), "--out", str(out)],
        cwd=ROOT,
        env=child_env(data_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"coverset report failed (exit {result.returncode}): {result.stderr[-2000:]}")
    return out


def take_report_shots(
    report: Path | None, schemes: list[str], only: set[str], changes: Path | None = None
) -> list[Path]:
    """The report's index (totals, treemap, the top of the file table), parse.py with the lens on
    and line 18's tests open, and the Changes page of PR #1 against main, from ``file://``."""
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(exist_ok=True)
    pngs: list[Path] = []
    with sync_playwright() as playwright:
        browser = launch(playwright)
        try:
            for scheme in schemes:
                context = browser.new_context(
                    viewport=VIEWPORT, device_scale_factor=1, color_scheme=scheme, reduced_motion="reduce"
                )
                page = context.new_page()
                page.set_default_timeout(30_000)
                suffix = "" if scheme == "light" else "-dark"
                if "report" in only and report is not None:
                    log(f"{scheme}: report")
                    target = SHOTS / f"report{suffix}.png"
                    report_index(page, report, target)
                    pngs.append(target)
                if "report-source" in only and report is not None:
                    log(f"{scheme}: report-source")
                    target = SHOTS / f"report-source{suffix}.png"
                    report_source(page, report, target)
                    pngs.append(target)
                if "report-changes" in only and changes is not None:
                    log(f"{scheme}: report-changes")
                    target = SHOTS / f"report-changes{suffix}.png"
                    report_changes(page, changes, target)
                    pngs.append(target)
                context.close()
        finally:
            browser.close()
    return pngs


def report_index(page: Any, report: Path, path: Path) -> None:
    """Totals, the Gap treemap and the first rows of the file table sorted by gap, largest first
    (a full-page clip)."""
    goto(page, (report / "index.html").as_uri())
    gap = page.locator("#files th button", has_text="Gap")
    gap.click()  # ascending
    gap.click()  # descending
    rest_pointer(page)
    bottom = page.evaluate(
        """(rows) => {
            const table = document.querySelector('#files table');
            if (!table) return document.body.scrollHeight;
            const body = table.tBodies[0].rows;
            const row = body[Math.min(rows, body.length) - 1];
            return row.getBoundingClientRect().bottom + window.scrollY;
        }""",
        REPORT_FILE_ROWS,
    )
    height = max(VIEWPORT["height"], int(bottom) + 1)
    page.screenshot(
        path=str(path), full_page=True, clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height}
    )


def report_source(page: Any, report: Path, path: Path) -> None:
    """parse.py with the data-flow lens on one test and line 18's tests popover open (below the line,
    so the chips under ``def parse_duration`` stay visible: a 1000 px tall viewport, as in source.jpg)."""
    page.set_viewport_size({"width": VIEWPORT["width"], "height": SOURCE_HEIGHT})
    try:
        goto(page, (report / "files" / f"{PARSE_PATH}.html").as_uri())
        index = page.evaluate(
            """(nodeid) => {
                const option = Array.from(document.querySelectorAll('#lens option'))
                    .find((o) => o.textContent.startsWith(nodeid + ' '));
                return option ? option.value : null;
            }""",
            REPORT_LENS_TEST,
        )
        if index is None:
            raise SystemExit(f"{REPORT_LENS_TEST} is not in parse.py's lens list")
        page.select_option("#lens", value=index)
        page.locator("#L18 button.gt").click()
        page.wait_for_selector("#line-tests-body .test-list", state="attached")
        page.wait_for_timeout(250)
        bottom = page.evaluate(
            """() => {
                const box = document.getElementById('line-tests');
                const edge = Math.min(box.getBoundingClientRect().bottom, window.innerHeight);
                let last = box.getBoundingClientRect().top;
                for (const row of box.querySelectorAll('.test-list li')) {
                    const r = row.getBoundingClientRect();
                    if (r.bottom <= edge - 1) last = Math.max(last, r.bottom);
                }
                return last;
            }"""
        )
        height = min(SOURCE_HEIGHT, int(bottom) + 6)
        page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height})
    finally:
        page.set_viewport_size(VIEWPORT)


def report_changes(page: Any, report: Path, path: Path) -> None:
    """The Changes page from the top through its tests table (a full-page clip, at most 1600 px)."""
    goto(page, (report / "changes.html").as_uri())
    bottom = page.evaluate(
        """() => {
            const end = document.querySelector('#tests-changed') || document.querySelector('main');
            return end.getBoundingClientRect().bottom + window.scrollY + 16;
        }"""
    )
    height = min(1600, max(VIEWPORT["height"], int(bottom)))
    page.screenshot(
        path=str(path), full_page=True, clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height}
    )


# -- JPEGs ---------------------------------------------------------------------------------------------


def to_jpeg(png: Path, out_dir: Path = DOCS) -> tuple[Path, int, int]:
    """``png`` resized to 1200 px wide (never upscaled) as a JPEG of at most 250 KB: (path, bytes, quality)."""
    from PIL import Image

    target = out_dir / f"{png.stem}.jpg"
    with Image.open(png) as image:
        rgb = image.convert("RGB")
        if rgb.width > OUT_WIDTH:
            height = round(rgb.height * OUT_WIDTH / rgb.width)
            rgb = rgb.resize((OUT_WIDTH, height), Image.Resampling.LANCZOS)
        quality = QUALITY
        while True:
            rgb.save(target, "JPEG", quality=quality, optimize=True, progressive=True)
            size = target.stat().st_size
            if size <= MAX_BYTES or quality <= MIN_QUALITY:
                return target, size, quality
            quality -= 4


# -- main -------------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--data-dir", type=Path, help="demo data dir (seeded when it has no coverset.db; default: a temp dir)"
    )
    parser.add_argument(
        "--only",
        default=",".join([*SHOT_NAMES, "report"]),
        help="comma-separated shot names (`report` stands for report, report-source and report-changes)",
    )
    parser.add_argument("--scheme", choices=["light", "dark", "both"], default="both")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data dir")
    args = parser.parse_args(argv)
    args.only = {name for x in args.only.split(",") if x for name in GROUPS.get(x, (x,))}
    unknown = args.only - set(SHOT_NAMES) - set(REPORT_SHOT_NAMES)
    if unknown:
        parser.error(f"unknown shots: {sorted(unknown)}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    temporary = args.data_dir is None
    data_dir = (args.data_dir or Path(tempfile.mkdtemp(prefix="coverset-shots-")) / "data").resolve()
    try:
        if not (data_dir / "coverset.db").is_file():
            seed(data_dir)
        ids = demo_ids(data_dir)
        log(f"PR #1 run {ids.pr1_run} (freeze {ids.freeze_s} s), PR #2 run {ids.pr2_run}")
        schemes = ["light", "dark"] if args.scheme == "both" else [args.scheme]
        pngs: list[Path] = []
        server_shots = args.only & set(SHOT_NAMES)
        if server_shots:
            with serve(data_dir, ids.fake_now) as base:
                pngs += take_shots(base, ids, schemes, server_shots)
        report_shots = args.only & set(REPORT_SHOT_NAMES)
        if report_shots:
            report = build_report_dir(data_dir, ids) if report_shots - {"report-changes"} else None
            changes = build_changes_dir(data_dir, ids) if "report-changes" in report_shots else None
            pngs += take_report_shots(report, schemes, report_shots, changes)
        for png in pngs:
            jpg, size, quality = to_jpeg(png)
            flag = "" if size <= MAX_BYTES else "  (over 250 KB at the lowest quality)"
            log(f"{jpg.relative_to(ROOT)}  {size / 1024:.0f} KB  q{quality}{flag}")
    finally:
        if temporary and not args.keep:
            shutil.rmtree(data_dir.parent, ignore_errors=True)
        elif temporary:
            log(f"kept {data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
