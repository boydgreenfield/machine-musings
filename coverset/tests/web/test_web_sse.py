"""The run page's event stream: first frame, live end-of-run frames, replay order and freezing."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from quart.typing import TestClientProtocol as Client
from web_helpers import Demo

from coverset.web.run_view import REGIONS
from coverset.web.sse import frame, parse_speed

FRAME = re.compile(r"(?:id: (?P<id>-?\d+)\n)?event: (?P<event>\w+)\n(?P<data>(?:data: .*\n)+)\n")


def frames(body: str) -> list[tuple[int | None, str, str]]:
    out = []
    for match in FRAME.finditer(body):
        data = "\n".join(line[6:] for line in match.group("data").splitlines())
        out.append((int(match.group("id")) if match.group("id") else None, match.group("event"), data))
    return out


def test_frame_splits_lines_into_data_fields() -> None:
    assert frame("gantt", "<svg>\n<rect/>\n</svg>", event_id=7) == (
        b"id: 7\nevent: gantt\ndata: <svg>\ndata: <rect/>\ndata: </svg>\n\n"
    )
    assert frame("done", "") == b"event: done\ndata: \n\n"


def test_parse_speed_is_clamped() -> None:
    assert parse_speed(None) == 1.0
    assert parse_speed("0.25") == 0.25
    assert parse_speed("nan") == 1.0
    assert parse_speed("-3") == 1.0
    assert parse_speed("1e9") == 100.0
    assert parse_speed("abc") == 1.0


async def test_finished_run_stream_renders_regions_then_done(client: Client, demo: Demo) -> None:
    response = await client.get(f"/runs/{demo.pr1_run}/events")
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache"
    body = await response.get_data(as_text=True)
    assert body.startswith(": coverset run page\n\n")
    events = frames(body)
    assert [e for _, e, _ in events] == [*REGIONS, "done"]
    gantt = dict((e, d) for _, e, d in events)["gantt"]
    assert "gt-fail" in gantt and "gt-pass" in gantt


async def test_live_stream_resumes_after_last_event_id(client: Client, demo: Demo) -> None:
    full = frames(await (await client.get(f"/runs/{demo.pr1_run}/events")).get_data(as_text=True))
    last = full[0][0]
    assert last is not None and last > 10
    resumed = await client.get(f"/runs/{demo.pr1_run}/events", headers={"Last-Event-ID": str(last)})
    events = frames(await resumed.get_data(as_text=True))
    assert events[-1][1] == "done"


async def test_replay_reemits_events_in_order(client: Client, demo: Demo) -> None:
    response = await client.get(f"/runs/{demo.pr1_run}/events?replay=1&speed=100")
    events = frames(await response.get_data(as_text=True))
    names = [e for _, e, _ in events]
    assert names[-1] == "done" and names.count("done") == 1
    body_frames = names[:-1]
    assert len(body_frames) % len(REGIONS) == 0 and len(body_frames) >= 2 * len(REGIONS)
    for start in range(0, len(body_frames), len(REGIONS)):
        assert body_frames[start : start + len(REGIONS)] == list(REGIONS)
    ids = [i for i, e, _ in events if e != "done"]
    assert ids == sorted(ids)
    heads = [d for _, e, d in events if e == "head"]
    reported = [int(m.group(1)) for d in heads if (m := re.search(r"<strong>(\d+)</strong>", d))]
    assert reported == sorted(reported) and reported[-1] == 100
    assert 'data-replay="finished"' in events[-1][2]


async def test_replay_freezes_mid_run(client: Client, demo: Demo) -> None:
    import json

    timeline = json.loads(demo.rows("SELECT timeline FROM runs WHERE id = :id", {"id": demo.pr1_run})[0][0])
    t0 = datetime.fromisoformat(timeline["queued"])
    first, last = (
        (datetime.fromisoformat(timeline[key]) - t0).total_seconds() for key in ("first_test", "finished")
    )
    freeze = round(first + (last - first) / 2, 2)  # mid-run on this machine, whatever its speed
    response = await client.get(f"/runs/{demo.pr1_run}/events?replay=1&speed=100&freeze={freeze}")
    events = frames(await response.get_data(as_text=True))
    assert events[-1][1] == "done" and 'data-replay="frozen"' in events[-1][2]
    heads = [d for _, e, d in events if e == "head"]
    final = int(re.search(r"<strong>(\d+)</strong>", heads[-1]).group(1))  # type: ignore[union-attr]
    assert 0 < final < 100
    assert 'data-key="finished"' not in [d for _, e, d in events if e == "timeline"][-1]


@pytest.mark.security
async def test_slow_replay_ends_after_the_stream_limit(
    client: Client, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.web import sse

    monkeypatch.setattr(sse, "SSE_MAX_STREAM_S", 0.3)
    started = time.monotonic()
    response = await client.get(f"/runs/{demo.pr1_run}/events?replay=1&speed=0.01")
    events = frames(await response.get_data(as_text=True))
    assert time.monotonic() - started < 5
    assert events[-1][1] == "done" and 'data-replay="stopped"' in events[-1][2]


async def test_runstate_blocks_from_events() -> None:
    from coverset.web.runstate import RunState

    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    state = RunState("run_x")
    state.apply(1, "shard", {"shard": 0, "status": "running", "tests": 4}, t0)
    for seq, (nodeid, outcome, offset) in enumerate(
        [("a", "passed", 1.0), ("b", "failed", 1.5), ("c", "passed", 2.5)], start=2
    ):
        state.apply(
            seq,
            "test",
            {"nodeid": nodeid, "outcome": outcome, "duration_s": 0.2, "shard": 0, "flaky": False},
            t0 + timedelta(seconds=offset),
        )
    state.apply(9, "test", {"nodeid": "b", "outcome": "passed", "shard": 0, "flaky": True, "retry": 1}, t0)
    blocks = state.blocks(t0)[0]
    assert [b.nodeid for b in blocks] == ["a", "b", "c"]
    assert blocks[1].start == blocks[0].end and blocks[2].start == blocks[1].end
    assert blocks[1].flaky and state.tests["b"].outcome == "passed"
    assert blocks[0].start >= 0.0 and blocks[0].end == 1.0
    assert state.counts["passed"] == 3 and state.reported == 3
