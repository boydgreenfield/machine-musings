"""A leader lease: one process per database runs the background loops.

The web process runs four loops besides serving requests: the run dispatcher, the retention gc,
the sandbox reaper and the warm-pool maintainer. Several replicas can serve one database; each loop
must still run in exactly one of them, or two dispatchers would race for the same queued runs and
two reapers would terminate each other's sandboxes.

The lease is a Postgres session-level advisory lock, ``pg_try_advisory_lock(key)`` with ``key``
a 64-bit hash of the lease's name (:func:`coverset.db.session.advisory_key`), taken on one
connection that the holder keeps checked out. There is no expiry to renew: the lock lives exactly
as long as that session, so a holder that crashes, is killed or loses its network releases it the
moment Postgres notices the dead connection: immediately on a clean exit (the process closes the
socket), and within the TCP keepalive bound otherwise. That bound is coverset's, not the OS's:
every connection asks the server for ``tcp_keepalives_idle=60``, ``interval=10`` and ``count=3``
(:data:`coverset.db.session.POSTGRES_SERVER_SETTINGS`), so a host that vanishes (VM crash, power
loss, a partition) loses the lease after about 90 s instead of Linux's default 2 h 11 min. Every
:attr:`LeaderLease.check_s` (30 s) the holder verifies its session still has the lock, and a
follower tries to take it, so a takeover completes within about two minutes of the holder vanishing;
:func:`run_while_leader` starts a loop when the lease is gained, cancels it when the lease is lost,
and starts it again after :data:`LOOP_RETRY_S` when it crashes (a loop that dies must never leave a
holder that keeps the lock and does nothing).

The lease is advisory: it decides who runs the loops, never correctness. A run is claimed with an
atomic ``UPDATE ... WHERE status = 'queued'`` whoever dispatches it, so a split-brain moment (an old
holder whose connection is dead but who has not noticed yet) can at worst have two dispatchers
*try*; one of them wins each run.

A transaction-mode pooler (PgBouncer ``pool_mode=transaction``, Neon's ``-pooler`` host) hands a
session's statements to different server connections, which breaks session advisory locks: use a
direct or session-mode connection (docs/postgres.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from coverset.db.session import advisory_key

__all__ = [
    "BACKGROUND_LEASE",
    "LEADER_EXTENSION",
    "LEASE_CHECK_S",
    "LOOP_RETRY_S",
    "LeaderLease",
    "LeaderStatus",
    "leader_status",
    "leader_status_on",
    "run_while_leader",
    "transaction_lock",
]

log = logging.getLogger(__name__)

LEADER_EXTENSION = "leader"
"""``app.extensions[LEADER_EXTENSION]`` holds the app's :class:`LeaderLease` (outside tests)."""
BACKGROUND_LEASE = "coverset:background"
"""The lease the web app's background loops share."""
LEASE_CHECK_S = 30.0
"""How often the holder verifies its lease and a follower tries to take it."""
LOOP_RETRY_S = 5.0
"""How long :func:`run_while_leader` waits before starting a loop again after it raised."""

_HELD_QUERY = text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
    "AND granted AND classid::bigint = :hi AND objid::bigint = :lo AND objsubid = 1"
)
_HOLDER_QUERY = text(
    "SELECT l.pid, a.application_name, host(a.client_addr), a.backend_start FROM pg_locks l "
    "LEFT JOIN pg_stat_activity a ON a.pid = l.pid WHERE l.locktype = 'advisory' AND l.granted "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND l.classid::bigint = :hi AND l.objid::bigint = :lo AND l.objsubid = 1"
)
"""Advisory locks are per database, and ``pg_locks`` lists every database's: without the
``database`` clause another installation's lease on the same server would show as this one's."""


def _key_parts(key: int) -> dict[str, int]:
    """``pg_locks`` shows a bigint advisory key as ``classid`` (high 32 bits) and ``objid`` (low)."""
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    return {"hi": unsigned >> 32, "lo": unsigned & 0xFFFF_FFFF}


class LeaderLease:
    """The lease ``name`` on ``engine``'s database (see the module docstring).

    ``held`` is the current state; :meth:`acquire` makes one attempt, :meth:`check` verifies (or
    retries) and :meth:`keep` does that every ``check_s`` until stopped; :meth:`release` gives it
    up.
    """

    def __init__(
        self, engine: AsyncEngine, name: str = BACKGROUND_LEASE, *, check_s: float = LEASE_CHECK_S
    ) -> None:
        self.engine = engine
        self.name = name
        self.key = advisory_key(name)
        self.check_s = check_s
        self._connection: AsyncConnection | None = None
        self._held = asyncio.Event()
        self._lost = asyncio.Event()
        self._lock = asyncio.Lock()
        self._lost.set()

    @property
    def held(self) -> bool:
        """Whether this process holds the lease now."""
        return self._held.is_set()

    @property
    def role(self) -> str:
        """``leader`` or ``follower``, for logs and status pages."""
        return "leader" if self.held else "follower"

    def _set(self, held: bool, *, released: bool = False) -> None:
        """Record whether the lease is held and log the change (``released``: given up on purpose,
        at shutdown, which is not worth a warning)."""
        if held == self.held:
            return
        if held:
            self._lost.clear()
            self._held.set()
            log.info("leader lease %s acquired: this process runs the background tasks", self.name)
        else:
            self._held.clear()
            self._lost.set()
            if released:
                log.info("leader lease %s released: another replica may take it", self.name)
            else:
                log.warning(
                    "leader lease %s lost: background tasks stop here until it is regained", self.name
                )

    async def wait_held(self) -> None:
        """Return once the lease is held."""
        await self._held.wait()

    async def wait_lost(self) -> None:
        """Return once the lease is not held."""
        await self._lost.wait()

    async def acquire(self) -> bool:
        """One attempt to take the lease (True if this process holds it afterwards)."""
        async with self._lock:
            if self._connection is not None:
                return await self._verify()
            connection = await self.engine.connect()
            try:
                got = bool(
                    await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key})
                )
                # End the implicit transaction: the session lock outlives it, and an idle session
                # is not subject to idle_in_transaction_session_timeout.
                await connection.commit()
            except Exception:
                await _discard(connection)
                raise
            if got:
                self._connection = connection
            else:
                await connection.close()
            self._set(got)
            return got

    async def _verify(self) -> bool:
        """Whether the held connection still has the lock (dropping a dead connection)."""
        assert self._connection is not None
        try:
            count = await self._connection.scalar(_HELD_QUERY, _key_parts(self.key))
            await self._connection.commit()
            alive = bool(count)
        except Exception as exc:  # noqa: BLE001 - a dead session is a lost lease, not a crash
            log.warning("leader lease %s: its connection failed (%s)", self.name, type(exc).__name__)
            alive = False
        if not alive:
            connection, self._connection = self._connection, None
            await _discard(connection)
        self._set(alive)
        return alive

    async def check(self) -> bool:
        """Verify a held lease, or try to take a free one; returns :attr:`held`.

        Database errors count as "not held" and are logged, never raised: a follower simply tries
        again at the next check.
        """
        if self._connection is not None:
            async with self._lock:
                if self._connection is not None and await self._verify():
                    return True
        try:
            return await self.acquire()
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.warning("leader lease %s: cannot reach the database (%s)", self.name, type(exc).__name__)
            self._set(False)
            return False

    async def keep(self, stop: asyncio.Event) -> None:
        """:meth:`check` every :attr:`check_s` until ``stop`` is set (the renewal loop)."""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.check_s)
            if not stop.is_set():
                await self.check()

    async def release(self) -> None:
        """Give the lease up (unlock and close its connection); safe to call when not held."""
        async with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
                    await connection.commit()
                await _discard(connection)
            self._set(False, released=True)


async def _discard(connection: AsyncConnection) -> None:
    """Close ``connection``, invalidating it first so a broken one never returns to the pool."""
    with contextlib.suppress(Exception):
        await connection.invalidate()
    with contextlib.suppress(Exception):
        await connection.close()


async def run_while_leader(
    lease: LeaderLease,
    loop: Callable[[], Awaitable[Any]],
    stop: asyncio.Event,
    *,
    label: str = "background task",
    retry_s: float = LOOP_RETRY_S,
) -> None:
    """Run ``loop()`` while ``lease`` is held, until ``stop`` is set.

    The loop starts when the lease is (or becomes) held and is cancelled when it is lost; it starts
    again on the next acquisition. A loop that raises is logged and started again after ``retry_s``
    (sooner if the lease is lost or ``stop`` is set meanwhile): the loops here only return once
    ``stop`` is set, and a crash during a takeover (the dispatcher's boot handling failing on the
    very Postgres blip that moved the lease) must not leave this process holding the lease while
    running nothing, with no follower able to take over. A loop that returns on its own ends this
    function.
    """
    while not stop.is_set():
        if not lease.held:
            await _first(lease.wait_held(), stop.wait())
            continue
        task = asyncio.ensure_future(loop())
        lost = asyncio.ensure_future(lease.wait_lost())
        stopped = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({task, lost, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (lost, stopped):
                waiter.cancel()
        if task.done():
            if task.cancelled() or task.exception() is None:
                return
            log.error(
                "%s failed; it starts again in %.0f s while this process holds leader lease %s",
                label,
                retry_s,
                lease.name,
                exc_info=task.exception(),
            )
            await _first(asyncio.sleep(retry_s), lease.wait_lost(), stop.wait())
            continue
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not stop.is_set():
            log.info("%s paused: leader lease %s lost", label, lease.name)


async def _first(*awaitables: Awaitable[Any]) -> None:
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True, slots=True)
class LeaderStatus:
    """Who holds a lease, as seen from any connection (``coverset deploy check``)."""

    name: str
    holder_pid: int | None = None
    application_name: str | None = None
    client_addr: str | None = None
    since: str | None = None

    @property
    def detail(self) -> str:
        """One line for a report."""
        if self.holder_pid is None:
            return f"{self.name}: free (no process runs the background tasks right now)"
        where = f" from {self.client_addr}" if self.client_addr else ""
        return f"{self.name}: held by backend pid {self.holder_pid} ({self.application_name or '?'}{where}) since {self.since}"


async def leader_status(engine: AsyncEngine, name: str = BACKGROUND_LEASE) -> LeaderStatus:
    """Who holds lease ``name`` on ``engine``'s database, without taking it."""
    async with engine.connect() as connection:
        return await leader_status_on(connection, name)


async def leader_status_on(connection: AsyncConnection, name: str = BACKGROUND_LEASE) -> LeaderStatus:
    """:func:`leader_status` on an open connection (``coverset deploy check`` reuses its probe's)."""
    row = (await connection.execute(_HOLDER_QUERY, _key_parts(advisory_key(name)))).first()
    if row is None:
        return LeaderStatus(name)
    pid, application, client, since = row
    return LeaderStatus(
        name,
        holder_pid=int(pid),
        application_name=application,
        client_addr=client,
        since=since.isoformat(timespec="seconds") if since is not None else None,
    )


async def transaction_lock(session: AsyncSession, name: str) -> None:
    """Hold the advisory lock ``name`` until ``session``'s transaction ends.

    For work that an in-process ``asyncio.Lock`` serialises within one replica and that must not
    interleave across replicas either (two uploads of one commit arriving at two replicas). Waits
    while another transaction holds it.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": advisory_key(name)})


def process_label() -> str:
    """``<host>:<pid>``, for log lines that say which replica holds the lease."""
    return f"{socket.gethostname()[:64] or 'localhost'}:{os.getpid()}"
