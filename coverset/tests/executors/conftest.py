"""A fake ``modal`` module for ModalExecutor unit tests (no network, no Modal account).

It models the slice of Modal 1.6 the executor uses, with the ``.aio`` calling convention:
``App.lookup``, the ``Image`` builder chain + ``build``/``from_id``, ``Sandbox.create/list`` and a
sandbox's ``exec`` (streamed byte chunks, exit codes, signals by ``COVERSET_EXEC_ID``),
``filesystem`` (an in-memory dict), ``snapshot_filesystem``, tags, ``poll`` and ``terminate``, plus
the exception classes the executor matches by name. Commands run by "exec" are answered by
``world.handler(cmd, env, workdir) -> ExecPlan``; ``tar -xzf`` from ``put_tree`` really extracts the
uploaded archive into the fake filesystem.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import itertools
import tarfile
import types
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


class _Method:
    """A callable with an ``.aio`` coroutine variant, like synchronicity-wrapped Modal methods."""

    def __init__(self, coro_fn: Callable[..., Any]) -> None:
        self.aio = coro_fn

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - sync use is a bug
        raise AssertionError("the executor must use the .aio API")


@dataclass
class ExecPlan:
    """How a fake exec behaves."""

    stdout: list[bytes] = field(default_factory=list)
    stderr: list[bytes] = field(default_factory=list)
    exit_code: int = 0
    hang: bool = False
    """Keep running (after writing the output) until signalled or the sandbox terminates."""
    ignore_term: bool = False
    endless_stdout: bytes | None = None
    """Write this chunk forever (until killed): for the stdout cap."""


class FakeStream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[bytes]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


class FakeProcess:
    def __init__(self, plan: ExecPlan, exec_id: str | None) -> None:
        self.plan = plan
        self.exec_id = exec_id
        self.stdout = FakeStream()
        self.stderr = FakeStream()
        self.returncode: int | None = None
        self.signals: list[int] = []
        self._done = asyncio.Event()
        self.wait = _Method(self._wait)
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        for chunk in self.plan.stdout:
            await self.stdout.queue.put(chunk)
            await asyncio.sleep(0)
        for chunk in self.plan.stderr:
            await self.stderr.queue.put(chunk)
            await asyncio.sleep(0)
        if self.plan.endless_stdout is not None:
            while not self._done.is_set():
                await self.stdout.queue.put(self.plan.endless_stdout)
                await asyncio.sleep(0)
            return
        if self.plan.hang:
            await self._done.wait()
            return
        self.finish(self.plan.exit_code)

    def finish(self, code: int) -> None:
        if self._done.is_set():
            return
        self.returncode = code
        self._done.set()
        self.stdout.queue.put_nowait(None)
        self.stderr.queue.put_nowait(None)

    def signal(self, signum: int) -> None:
        self.signals.append(signum)
        if signum == 15 and self.plan.ignore_term:
            return
        self.finish(128 + signum)

    async def _wait(self) -> int | None:
        await self._done.wait()
        return self.returncode


class FakeFilesystem:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self._sb = sandbox
        self.write_bytes = _Method(self._write_bytes)
        self.read_bytes = _Method(self._read_bytes)
        self.make_directory = _Method(self._make_directory)
        self.stat = _Method(self._stat)

    async def _write_bytes(self, data: bytes, path: str) -> None:
        self._sb.check_alive()
        parent = path.rsplit("/", 1)[0] or "/"
        if parent not in self.dirs:
            raise self._sb.world.exc.SandboxFilesystemNotFoundError(parent)
        self.files[path] = bytes(data)

    async def _read_bytes(self, path: str) -> bytes:
        self._sb.check_alive()
        if path not in self.files:
            raise self._sb.world.exc.SandboxFilesystemNotFoundError(path)
        return self.files[path]

    async def _make_directory(self, path: str, *, create_parents: bool = True) -> None:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            self.dirs.add("/" + "/".join(parts[:i]))

    async def _stat(self, path: str) -> Any:
        if path not in self.files:
            raise self._sb.world.exc.SandboxFilesystemNotFoundError(path)
        return types.SimpleNamespace(size=len(self.files[path]))


class FakeImage:
    def __init__(
        self, world: FakeWorld, steps: tuple[tuple[Any, ...], ...], object_id: str | None = None
    ) -> None:
        self.world = world
        self.steps = steps
        self.object_id = object_id
        self.build = _Method(self._build)

    def _then(self, *step: Any) -> FakeImage:
        return FakeImage(self.world, (*self.steps, step))

    def apt_install(self, *packages: str) -> FakeImage:
        return self._then("apt_install", packages)

    def env(self, values: dict[str, str]) -> FakeImage:
        return self._then("env", dict(values))

    def uv_pip_install(self, *packages: str, requirements: list[str] | None = None) -> FakeImage:
        content = [Path(path).read_text(encoding="utf-8") for path in requirements or []]
        return self._then("uv_pip_install", packages, tuple(requirements or ()), tuple(content))

    def add_local_file(self, local: str, remote: str, *, copy: bool = False) -> FakeImage:
        return self._then("add_local_file", local, remote, copy)

    def run_commands(self, *commands: str) -> FakeImage:
        return self._then("run_commands", commands)

    async def _build(self, app: Any) -> FakeImage:
        self.world.builds.append(self.steps)
        if self.world.build_error is not None:
            raise self.world.build_error
        self.object_id = f"im-built-{len(self.world.builds)}"
        return self


class FakeSandbox:
    def __init__(self, world: FakeWorld, image: FakeImage, kwargs: dict[str, Any]) -> None:
        self.world = world
        self.object_id = f"sb-{next(world.ids)}"
        self.image = image
        self.kwargs = kwargs
        self.tags = dict(kwargs.get("tags") or {})
        self.terminated = False
        self.filesystem = FakeFilesystem(self)
        self.processes: list[FakeProcess] = []
        self.execs: list[dict[str, Any]] = []
        self.exec = _Method(self._exec)
        self.terminate = _Method(self._terminate)
        self.poll = _Method(self._poll)
        self.set_tags = _Method(self._set_tags)
        self.get_tags = _Method(self._get_tags)
        self.snapshot_filesystem = _Method(self._snapshot)
        self.snapshot_kwargs: dict[str, Any] | None = None

    def check_alive(self) -> None:
        if self.terminated:
            raise self.world.exc.SandboxTerminatedError(self.object_id)

    async def _exec(
        self, *argv: str, workdir: str | None = None, timeout: int | None = None, text: bool = True
    ) -> FakeProcess:
        self.check_alive()
        record: dict[str, Any] = {"argv": list(argv), "workdir": workdir, "timeout": timeout, "text": text}
        self.execs.append(record)
        if workdir is not None and workdir not in self.filesystem.dirs:
            raise self.world.exc.ExecutionError(f"no such workdir {workdir}")
        if argv[:2] == ("python", "-c") and "COVERSET_EXEC_ID=" in argv[2]:
            signum, ids = int(argv[3]), set(argv[4:])
            record["signal"] = signum
            for proc in self.processes:
                if proc.exec_id in ids and proc.returncode is None:
                    proc.signal(signum)
            return FakeProcess(ExecPlan(stdout=[b"1\n"]), None)
        env: dict[str, str] = {}
        cmd = list(argv)
        if argv[:2] == ("env", "-i"):
            rest = list(argv[2:])
            while rest and "=" in rest[0]:
                key, value = rest.pop(0).split("=", 1)
                env[key] = value
            assert rest[:2] == ["sh", "-c"] and rest[3] == "sh", rest[:4]
            cmd = rest[4:]
        record["cmd"], record["env"] = cmd, env
        if cmd[:2] == ["sh", "-c"] and "tar -xzf" in cmd[2]:
            plan = self._extract(cmd[4], cmd[5])
        else:
            plan = self.world.handler(cmd, env, workdir) or ExecPlan()
        proc = FakeProcess(plan, env.get("COVERSET_EXEC_ID"))
        self.processes.append(proc)
        return proc

    def _extract(self, dest: str, archive: str) -> ExecPlan:
        data = self.filesystem.files.pop(archive, None)
        if data is None:
            return ExecPlan(stderr=[b"tar: no archive\n"], exit_code=2)
        for path in [p for p in self.filesystem.files if p.startswith(dest + "/")]:
            del self.filesystem.files[path]
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    handle = tar.extractfile(member)
                    assert handle is not None
                    target = f"{dest}/{member.name.lstrip('./')}"
                    self.filesystem.files[target] = handle.read()
                    parent = target.rsplit("/", 1)[0]
                    parts = parent.strip("/").split("/")
                    for i in range(1, len(parts) + 1):
                        self.filesystem.dirs.add("/" + "/".join(parts[:i]))
        self.filesystem.dirs.add(dest)
        return ExecPlan()

    async def _terminate(self, *, wait: bool = False) -> None:
        self.terminated = True
        for proc in self.processes:
            proc.finish(137)

    async def _poll(self) -> int | None:
        return 0 if self.terminated else None

    async def _set_tags(self, tags: dict[str, str]) -> None:
        self.tags = dict(tags)

    async def _get_tags(self) -> dict[str, str]:
        return dict(self.tags)

    async def _snapshot(self, timeout: int = 55, *, ttl: int | None = None) -> FakeImage:
        self.snapshot_kwargs = {"timeout": timeout, "ttl": ttl}
        image = FakeImage(
            self.world, ("snapshot", self.object_id), object_id=f"im-snap-{next(self.world.ids)}"
        )
        return image


class FakeWorld:
    """Shared state of the fake Modal backend (``world.Plan`` is :class:`ExecPlan`)."""

    Plan = ExecPlan

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.sandboxes: list[FakeSandbox] = []
        self.create_calls: list[dict[str, Any]] = []
        self.create_failures: list[BaseException] = []
        self.missing_images: set[str] = set()
        self.builds: list[tuple[tuple[Any, ...], ...]] = []
        self.build_error: BaseException | None = None
        self.lookups: list[dict[str, Any]] = []
        self.handler: Callable[[list[str], dict[str, str], str | None], ExecPlan | None] = lambda *a: None
        self.exc = _exceptions()

    def live(self) -> list[FakeSandbox]:
        return [sb for sb in self.sandboxes if not sb.terminated]

    def add_foreign(self, tags: dict[str, str]) -> FakeSandbox:
        """A sandbox created by someone else (another process or installation)."""
        sandbox = FakeSandbox(self, FakeImage(self, (), "im-x"), {"tags": tags})
        self.sandboxes.append(sandbox)
        return sandbox


def _exceptions() -> types.ModuleType:
    module = types.ModuleType("modal.exception")

    class Error(Exception):
        pass

    module.Error = Error  # type: ignore[attr-defined]
    for name in (
        "NotFoundError",
        "ResourceExhaustedError",
        "InternalFailure",
        "ExecutionError",
        "SandboxTerminatedError",
        "SandboxFilesystemNotFoundError",
    ):
        setattr(module, name, type(name, (Error,), {}))
    return module


def make_fake_modal(world: FakeWorld) -> types.ModuleType:
    """A module object standing in for ``modal``."""
    modal = types.ModuleType("modal")
    modal.exception = world.exc  # type: ignore[attr-defined]
    overrides: dict[str, str] = {}
    modal.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        config=types.SimpleNamespace(override_locally=lambda key, value: overrides.__setitem__(key, value))
    )
    modal.config_overrides = overrides  # type: ignore[attr-defined]

    @contextlib.contextmanager
    def enable_output() -> Iterator[None]:
        yield

    modal.enable_output = enable_output  # type: ignore[attr-defined]

    async def lookup(
        name: str, *, create_if_missing: bool = False, environment_name: str | None = None
    ) -> Any:
        world.lookups.append(
            {"name": name, "create_if_missing": create_if_missing, "environment_name": environment_name}
        )
        return types.SimpleNamespace(app_id="ap-fake", name=name)

    modal.App = types.SimpleNamespace(lookup=_Method(lookup))  # type: ignore[attr-defined]

    def debian_slim(python_version: str | None = None) -> FakeImage:
        return FakeImage(world, (("debian_slim", python_version),))

    def from_id(image_id: str) -> FakeImage:
        return FakeImage(world, (("from_id", image_id),), object_id=image_id)

    modal.Image = types.SimpleNamespace(debian_slim=debian_slim, from_id=from_id)  # type: ignore[attr-defined]

    async def create(
        *args: str, app: Any = None, image: FakeImage | None = None, **kwargs: Any
    ) -> FakeSandbox:
        call = {"args": args, "app": app, "image": image, **kwargs}
        world.create_calls.append(call)
        await asyncio.sleep(0)
        if world.create_failures:
            raise world.create_failures.pop(0)
        assert image is not None
        if image.object_id in world.missing_images:
            raise world.exc.NotFoundError(f"Image {image.object_id} not found")
        sandbox = FakeSandbox(world, image, kwargs)
        world.sandboxes.append(sandbox)
        return sandbox

    async def list_(
        *, app_id: str | None = None, tags: dict[str, str] | None = None
    ) -> AsyncIterator[FakeSandbox]:
        for sandbox in list(world.sandboxes):
            if sandbox.terminated:
                continue
            if tags and any(sandbox.tags.get(k) != v for k, v in tags.items()):
                continue
            yield sandbox

    modal.Sandbox = types.SimpleNamespace(create=_Method(create), list=_Method(list_))  # type: ignore[attr-defined]
    return modal


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


@pytest.fixture
def fake_modal(world: FakeWorld, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """The fake module, also installed as ``sys.modules["modal"]``; builder version env cleared."""
    module = make_fake_modal(world)
    monkeypatch.setitem(__import__("sys").modules, "modal", module)
    monkeypatch.setenv("MODAL_IMAGE_BUILDER_VERSION", "unset")  # so teardown restores the original state
    monkeypatch.delenv("MODAL_IMAGE_BUILDER_VERSION")
    return module
