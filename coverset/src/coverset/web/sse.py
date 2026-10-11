"""Server-sent events for the run page: re-rendered HTML regions as named events.

One stream per page (``sse-connect`` on the run page), one named event per live region (``head``,
``timeline``, ``gantt``, ``failures``) whose data is that region's HTML, swapped with
``morph:innerHTML``; a final ``done`` event closes the client (``sse-close="done"``). Frames carry
``id: <seq>`` so a reconnect resumes with ``Last-Event-ID``.

* **Live**: stored events after ``after`` are polled every :data:`~coverset.web.api.POLL_INTERVAL_S`
  (the same cadence and terminal-status rules as the ``/v1`` stream, whose helpers this reuses);
  regions are re-rendered at most every :data:`RENDER_INTERVAL_S`; ``: ping`` every 15 s; the stream
  ends after ``done`` or :data:`~coverset.web.api.SSE_MAX_STREAM_S`.
* **Replay** (``?replay=1&speed=0.25``): the run's stored events are re-emitted with their original
  spacing divided by ``speed``; ``freeze=<seconds>`` stops the replay at that point of the run's own
  clock (screenshots of a run "frozen mid-replay"). A replay, like a live stream, ends after
  :data:`~coverset.web.api.SSE_MAX_STREAM_S` (``speed=0.01`` would otherwise hold a connection for
  hours); it then renders where it got to and sends ``done`` labelled ``stopped``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

from quart import render_template

from coverset.web.api import PING_INTERVAL_S, POLL_INTERVAL_S, SSE_MAX_STREAM_S, TERMINAL_STATUSES
from coverset.web.run_view import REGIONS, RunBundle, build_state, load_bundle, load_events, regions_context

__all__ = ["frame", "page_stream", "parse_speed"]

RENDER_INTERVAL_S = 0.15
MIN_SPEED = 0.01
MAX_SPEED = 100.0
MAX_REPLAY_GAP_S = 5.0
"""Longest pause between two replayed events (long idle stretches are compressed)."""


def parse_speed(value: str | None) -> float:
    """A replay speed in [0.01, 100] (default 1)."""
    try:
        speed = float(value) if value else 1.0
    except ValueError:
        return 1.0
    if speed != speed or speed <= 0:
        return 1.0
    return min(MAX_SPEED, max(MIN_SPEED, speed))


def frame(event: str, html: str, *, event_id: int | None = None) -> bytes:
    """One SSE frame; every line of ``html`` becomes its own ``data:`` line."""
    lines = [f"id: {event_id}"] if event_id is not None else []
    lines.append(f"event: {event}")
    lines.extend(f"data: {line}" for line in (html.splitlines() or [""]))
    return ("\n".join(lines) + "\n\n").encode()


async def _regions(context: dict[str, Any], event_id: int | None) -> list[bytes]:
    out = []
    for name in REGIONS:
        html = await render_template(f"runs/_{name}.html", **context)
        out.append(frame(name, html, event_id=event_id))
    return out


async def page_stream(
    session_factory: Callable[[], Any],
    run_id: str,
    *,
    after: int = 0,
    replay: bool = False,
    speed: float = 1.0,
    freeze: float | None = None,
) -> AsyncIterator[bytes]:
    """The run page's event stream (see the module docstring)."""
    yield b": coverset run page\n\n"
    if replay:
        async for chunk in _replay(session_factory, run_id, speed=speed, freeze=freeze, after=after):
            yield chunk
        return
    async for chunk in _live(session_factory, run_id, after=after):
        yield chunk


async def _replay(
    session_factory: Callable[[], Any], run_id: str, *, speed: float, freeze: float | None, after: int
) -> AsyncIterator[bytes]:
    async with session_factory() as session:
        bundle = await load_bundle(session, run_id)
        events = await load_events(session, run_id) if bundle is not None else []
    if bundle is None:
        return
    state = build_state(run_id, [e for e in events if e.seq <= after])
    final = events[-1].at if events else None
    origin = _origin_of(bundle)
    stop = origin + timedelta(seconds=freeze) if freeze is not None else None
    started = time.monotonic()
    replay_time = 0.0
    previous_at = None
    last_render = 0.0
    dirty = False
    cut = False
    for event in events:
        if event.seq <= after:
            previous_at = event.at
            continue
        if stop is not None and event.at > stop:
            break
        if previous_at is not None:
            gap = min(MAX_REPLAY_GAP_S, max(0.0, (event.at - previous_at).total_seconds()))
            replay_time += gap / speed
        previous_at = event.at
        delay = started + replay_time - time.monotonic()
        if delay > 0:
            if dirty:
                for chunk in await _regions(
                    regions_context(bundle, state, replay=True, final_clock=final), state.last_seq
                ):
                    yield chunk
                dirty = False
                last_render = time.monotonic()
            if time.monotonic() + delay - started > SSE_MAX_STREAM_S:
                cut = True
                break
            await asyncio.sleep(delay)
        state.apply(event.seq, event.kind, event.payload, event.at)
        dirty = True
        if time.monotonic() - last_render >= RENDER_INTERVAL_S:
            for chunk in await _regions(
                regions_context(bundle, state, replay=True, final_clock=final), state.last_seq
            ):
                yield chunk
            dirty = False
            last_render = time.monotonic()
    if stop is not None and (state.clock is None or state.clock < stop):
        state.clock = stop
    for chunk in await _regions(
        regions_context(bundle, state, replay=True, final_clock=final), state.last_seq
    ):
        yield chunk
    label = "stopped" if cut else "frozen" if stop is not None and state.done is None else "finished"
    yield frame("done", f'<span class="replay-status" data-replay="{label}">replay {label}</span>')


def _origin_of(bundle: RunBundle) -> Any:
    from coverset.web.render.timeline import milestones

    _, origin = milestones(bundle.run.timeline or {})
    return origin or bundle.run.created_at


async def _live(session_factory: Callable[[], Any], run_id: str, *, after: int) -> AsyncIterator[bytes]:
    async with session_factory() as session:
        bundle = await load_bundle(session, run_id)
        events = await load_events(session, run_id) if bundle is not None else []
    if bundle is None:
        return
    state = build_state(run_id, [e for e in events if e.seq <= max(after, 0)])
    pending = [e for e in events if e.seq > after]
    started = last_ping = time.monotonic()
    last_render = 0.0
    while True:
        for event in pending:
            state.apply(event.seq, event.kind, event.payload, event.at)
        changed = bool(pending)
        async with session_factory() as session:
            fresh = await load_bundle(session, run_id)
            pending = await load_events(session, run_id, after=state.last_seq)
        if fresh is not None:
            bundle = fresh
        finished = state.done is not None or (bundle.run.status in TERMINAL_STATUSES and not pending)
        now = time.monotonic()
        if (changed and now - last_render >= RENDER_INTERVAL_S) or finished:
            for chunk in await _regions(regions_context(bundle, state), state.last_seq):
                yield chunk
            last_render = now
        if finished:
            yield frame("done", '<span class="replay-status" data-replay="finished">run finished</span>')
            return
        if now - started >= SSE_MAX_STREAM_S:
            return
        if now - last_ping >= PING_INTERVAL_S:
            yield b": ping\n\n"
            last_ping = now
        if not pending:
            await asyncio.sleep(POLL_INTERVAL_S)
