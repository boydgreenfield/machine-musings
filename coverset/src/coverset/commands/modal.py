"""``coverset modal smoke`` and ``coverset modal images``: live checks of the Modal setup.

``smoke`` proves the whole client-side path with one cheap sandbox: the token (``modal token info``
through the SDK), the ``coverset-runner`` App in environment ``COVERSET_MODAL_ENVIRONMENT``, a
``debian_slim`` image, one sandbox created with the production guards (CPU/memory requests and
limits, timeouts, no secrets, no OIDC token, network blocked, instance tags), ``python -V``, a file
written and read back through the filesystem API, and a SIGTERM delivered to a running exec the way
:meth:`~coverset.executors.modal_.ModalWorker.cancel` does it. The sandbox is terminated in a
``finally`` block whatever happens; the command exits non-zero with the error if any step fails.

``modal reap [--all]`` lives in :mod:`coverset.commands.maintenance` and works through
:meth:`~coverset.executors.modal_.ModalExecutor.reap`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import click

from coverset.commands.common import load
from coverset.config import Settings

__all__ = ["modal_images_command", "modal_smoke_command", "register_modal_commands"]

SMOKE_PYTHON = "3.13"
SMOKE_TIMEOUT_S = 300


@click.command("smoke")
@click.option(
    "--python", "python_version", default=SMOKE_PYTHON, show_default=True, help="debian_slim Python."
)
@click.option("-v", "--verbose", is_flag=True, help="Stream the image build log.")
def modal_smoke_command(python_version: str, verbose: bool) -> None:
    """Create one Modal sandbox, run python -V, write/read a file, signal an exec, terminate it."""
    settings = load()
    try:
        rows = asyncio.run(_smoke(settings, python_version, verbose=verbose))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"modal smoke failed: {type(exc).__name__}: {exc}") from None
    width = max(len(name) for name, _, _ in rows)
    for name, seconds, detail in rows:
        click.echo(f"{name.ljust(width)}  {seconds:7.2f} s  {detail}")
    click.echo("modal smoke: ok")


class _Steps:
    """Times each step; a failing step raises ClickException naming it."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, float, str]] = []

    async def __call__(
        self, name: str, action: Callable[[], Awaitable[Any]], describe: Callable[[Any], str]
    ) -> Any:
        started = time.perf_counter()
        try:
            value = await asyncio.wait_for(action(), timeout=SMOKE_TIMEOUT_S)
        except Exception as exc:
            self._print()
            raise click.ClickException(
                f"modal smoke failed at {name!r}: {type(exc).__name__}: {exc}"
            ) from None
        self.rows.append((name, time.perf_counter() - started, describe(value)))
        return value

    def _print(self) -> None:
        for name, seconds, detail in self.rows:
            click.echo(f"{name}  {seconds:.2f} s  {detail}", err=True)


async def _smoke(settings: Settings, python_version: str, *, verbose: bool) -> list[tuple[str, float, str]]:
    from coverset.executors.modal_ import ModalExecutor
    from coverset.executors.modal_images import build_and_record

    executor = ModalExecutor(settings, verbose=verbose)
    step = _Steps()
    terminate: Callable[[], Awaitable[None]] | None = None
    try:
        modal = executor.modal
        await step("token", lambda: token_info(modal), lambda info: info)
        app = await step(
            "app lookup",
            executor.app,
            lambda a: f"{getattr(a, 'app_id', '?')} ({settings.modal_environment}/coverset-runner)",
        )
        image = modal.Image.debian_slim(python_version=python_version)
        await step("image build", lambda: build_and_record(app, image, verbose=verbose, modal=modal), str)

        async def create() -> Any:
            nonlocal terminate
            sandbox, terminate = await executor.create_sandbox(image, role="smoke", repo="smoke", run="smoke")
            return sandbox

        sandbox = await step("sandbox create", create, lambda sb: f"{sb.object_id} (network blocked)")
        worker = executor.worker_for(sandbox)
        lines: list[str] = []

        async def python_v() -> str:
            result = await worker.run(
                ["python", "-V"],
                cwd="",
                env={},
                timeout_s=60,
                on_line=lambda t, _s: lines.append(t),
                stdout_cap=4096,
            )
            if result.exit_code != 0:
                raise RuntimeError(f"python -V exited {result.exit_code}: {lines}")
            return " ".join(lines).strip()

        await step("exec python -V", python_v, str)
        payload = f"coverset smoke {time.time():.0f}\n".encode()

        async def roundtrip() -> int:
            await worker.write_file("smoke/hello.txt", payload)
            back = await worker.get_file("smoke/hello.txt")
            if back != payload:
                raise RuntimeError(f"read back {back!r}, wrote {payload!r}")
            return len(back)

        await step("write+read file", roundtrip, lambda n: f"{n} bytes round-tripped")

        async def signal_exec() -> float:
            task = asyncio.get_running_loop().create_task(
                worker.run(["sleep", "120"], cwd="", env={}, timeout_s=150, stdout_cap=4096)
            )
            await asyncio.sleep(2.0)
            started = time.perf_counter()
            await worker.cancel()
            result = await asyncio.wait_for(task, timeout=30)
            if not result.cancelled or result.exit_code == 0:
                raise RuntimeError(f"exec not stopped by SIGTERM: {result}")
            return time.perf_counter() - started

        await step("SIGTERM an exec", signal_exec, lambda s: f"stopped {s:.2f} s after cancel()")
    finally:
        if terminate is not None:
            started = time.perf_counter()
            await terminate()
            step.rows.append(("terminate", time.perf_counter() - started, "sandbox terminated"))
        await executor.close()
    return step.rows


async def token_info(modal: Any) -> str:
    """``modal token info`` through the SDK: workspace, user and token id (never the secret).

    Runs the same RPC as the CLI on Modal's own event loop (``synchronizer.create_blocking``) in a
    thread: mixing the raw ``_Client`` with this loop would bind the shared client to the wrong loop.
    """
    try:
        from modal._utils.async_utils import synchronizer
        from modal.client import _Client
        from modal_proto import api_pb2
    except ImportError:
        await modal.Client.from_env.aio()
        return "authenticated"

    async def query() -> str:
        client = await _Client.from_env()
        response = await client._stub.TokenInfoGet(api_pb2.TokenInfoGetRequest())
        user = f", user {response.user_identity.username}" if response.HasField("user_identity") else ""
        return f"workspace {response.workspace_name or '?'}{user}, token {response.token_id}"

    return await asyncio.to_thread(synchronizer.create_blocking(query))


@click.command("images")
@click.option("--days", type=click.IntRange(1, 3650), default=30, show_default=True)
def modal_images_command(days: int) -> None:
    """List the env images this installation built and flag those unused for DAYS days.

    Modal has no API to delete an image; remove flagged ones by hand if you need to.
    """
    from coverset.executors.modal_ import ModalExecutor

    settings = load()
    executor = ModalExecutor(settings)
    records = executor.image_records()
    stale = {row["env_hash"] for row in executor.stale_images(days)}
    if not records:
        click.echo("no env images recorded")
        return
    for env_hash, record in sorted(records.items(), key=lambda kv: str(kv[1].get("last_used"))):
        flag = f"  unused for {days}+ days: delete manually" if env_hash in stale else ""
        click.echo(
            f"{record.get('image_id')}  python {record.get('python')}  env {env_hash[:16]}  "
            f"last used {record.get('last_used')}{flag}"
        )


def register_modal_commands(modal: click.Group) -> None:
    """Add ``smoke`` (replacing the placeholder) and ``images`` to the ``modal`` group."""
    modal.add_command(modal_smoke_command, "smoke")
    modal.add_command(modal_images_command, "images")
