"""``coverset mcp``: the MCP server for AI agents, and the snippet that registers it.

* ``coverset mcp serve`` serves the agent tools over stdio, from one of three sources:
  ``--source DIR`` (a ``pytest --coverset`` output directory, a ``bundle.tar.gz`` or a neutral
  bundle; the default is ``./.coverset``), ``--repo NAME [--run ID]`` (the facts store of this
  machine's data directory, as the operator: no per-repository authorization), or
  ``--server URL`` with an API token (a deployed coverset over its ``/v1/agent`` HTTP API, as the
  token's owner).
* ``coverset mcp install-claude-code`` prints (never writes) the ``.mcp.json`` entry and the
  equivalent ``claude mcp add`` command.
* ``coverset mcp tools`` lists the tools; ``coverset mcp call TOOL [JSON]`` runs one and prints
  its JSON (what the agent would see), for scripts and for checking a setup by hand.

The logic lives in :mod:`coverset.agent`.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

import click

__all__ = ["mcp_group"]

TOKEN_ENV = "COVERSET_TOKEN"


@click.group(name="mcp")
def mcp_group() -> None:
    """The MCP server for AI agents (Claude Code, Cursor, review bots)."""


def _source_options[F](fn: F) -> F:
    options = [
        click.option(
            "--source",
            type=click.Path(path_type=Path, exists=True),
            default=None,
            help="A pytest --coverset output directory, bundle.tar.gz or neutral bundle "
            "(default: ./.coverset when it exists).",
        ),
        click.option(
            "--repo-root",
            type=click.Path(path_type=Path, file_okay=False, exists=True),
            default=None,
            help="With --source: read sources (annotations, the work tree for diffs) here; default: the "
            "git work tree the source sits in. Only files git tracks are read.",
        ),
        click.option(
            "--trusted",
            is_flag=True,
            help="With --source: read an archive or a neutral bundle under the local limits.",
        ),
        click.option("--repo", "repo_spec", default=None, help="Serve this repository from the facts store."),
        click.option(
            "--run", "run_id", default=None, help="With --repo: the default run (else the newest baseline)."
        ),
        click.option("--server", default=None, help="A deployed coverset's URL (https://...)."),
        click.option(
            "--token",
            envvar=TOKEN_ENV,
            default=None,
            help=f"With --server: an API token with the read scope (or set {TOKEN_ENV}).",
        ),
    ]
    for option in reversed(options):
        fn = option(fn)  # type: ignore[assignment]
    return fn


def _service(
    source: Path | None,
    repo_root: Path | None,
    trusted: bool,
    repo_spec: str | None,
    run_id: str | None,
    server: str | None,
    token: str | None,
) -> tuple[Any, Any]:
    """The service for the chosen source and an async cleanup callable."""
    chosen = [
        name for name, value in (("--source", source), ("--repo", repo_spec), ("--server", server)) if value
    ]
    if len(chosen) > 1:
        raise click.UsageError(f"pass one of --source, --repo and --server, not {' and '.join(chosen)}")
    if server:
        from coverset.agent.remote import RemoteService

        try:
            remote = RemoteService(server, token or "")
        except ValueError as exc:
            raise click.UsageError(str(exc)) from None
        return remote, remote.aclose
    if repo_spec or run_id:
        return _store_service(repo_spec, run_id)
    from coverset.agent.backends import LocalBackend
    from coverset.agent.service import AgentService
    from coverset.agent.tools import ToolError

    if source is None:
        source = Path(".coverset")
        if not source.exists():
            raise click.UsageError(
                "no ./.coverset here: run `pytest --coverset` first, or pass --source/--repo/--server"
            )
    try:
        backend = LocalBackend(source, repo_root=repo_root, trusted=trusted)
    except ToolError as exc:
        raise click.UsageError(exc.message) from None

    async def nothing() -> None:
        return None

    return AgentService(backend), nothing


def _store_service(repo_spec: str | None, run_id: str | None) -> tuple[Any, Any]:
    from coverset.agent.backends import StoreBackend
    from coverset.agent.service import AgentService
    from coverset.commands.common import load
    from coverset.db.session import Database

    settings = load()
    db = Database.from_url(settings.database_url)
    service = AgentService(StoreBackend(settings, db, default_repo=repo_spec, default_run=run_id))

    async def close() -> None:
        await db.engine.dispose()

    return service, close


@mcp_group.command("serve")
@_source_options
def serve_command(
    source: Path | None,
    repo_root: Path | None,
    trusted: bool,
    repo_spec: str | None,
    run_id: str | None,
    server: str | None,
    token: str | None,
) -> None:
    """Serve the agent tools over stdio (stdout carries only MCP messages)."""
    import asyncio

    from coverset.agent.mcp_server import serve_stdio

    async def main() -> None:
        if repo_spec or run_id:
            from coverset.commands.common import load, prepare_db

            await prepare_db(load())
        service, close = _service(source, repo_root, trusted, repo_spec, run_id, server, token)
        try:
            await serve_stdio(service)
        finally:
            await close()

    asyncio.run(main())


@mcp_group.command("tools")
def tools_command() -> None:
    """List the tools and their one-line titles."""
    from coverset.agent.tools import TOOLS

    width = max(len(name) for name in TOOLS)
    for spec in TOOLS.values():
        click.echo(f"{spec.name.ljust(width)}  {spec.title}")


@mcp_group.command("call")
@click.argument("tool")
@click.argument("arguments", required=False, default="{}")
@_source_options
def call_command(
    tool: str,
    arguments: str,
    source: Path | None,
    repo_root: Path | None,
    trusted: bool,
    repo_spec: str | None,
    run_id: str | None,
    server: str | None,
    token: str | None,
) -> None:
    """Run one TOOL with ARGUMENTS (a JSON object) and print its JSON, as an agent would see it."""
    import asyncio

    from coverset.agent.tools import ToolError

    try:
        args = json.loads(arguments)
    except ValueError:
        raise click.UsageError(
            'ARGUMENTS must be a JSON object, e.g. \'{"function": "parse_duration"}\''
        ) from None

    async def main() -> dict[str, Any]:
        if repo_spec or run_id:
            from coverset.commands.common import load, prepare_db

            await prepare_db(load())
        service, close = _service(source, repo_root, trusted, repo_spec, run_id, server, token)
        try:
            return await service.call(tool, args)
        finally:
            await close()

    try:
        result = asyncio.run(main())
    except ToolError as exc:
        click.echo(json.dumps(exc.to_json(), indent=2, ensure_ascii=False), err=True)
        raise SystemExit(1) from None
    click.echo(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _executable() -> str:
    """The ``coverset`` entry point of this installation (absolute, so the snippet works from any
    project directory), else plain ``coverset``."""
    here = Path(sys.executable).parent / "coverset"
    if here.is_file():
        return str(here)
    return shutil.which("coverset") or "coverset"


@mcp_group.command("install-claude-code")
@click.option(
    "--source",
    default=".coverset",
    show_default=True,
    help="The bundle the server reads (relative to the project).",
)
@click.option("--server", default=None, help="Register a deployed coverset instead of a local bundle.")
@click.option("--name", default="coverset", show_default=True, help="The server's name in Claude Code.")
@click.option(
    "--command", "command", default=None, help="The coverset executable (default: this installation's)."
)
def install_claude_code_command(source: str, server: str | None, name: str, command: str | None) -> None:
    """Print the .mcp.json entry and the `claude mcp add` command (nothing is written)."""
    exe = command or _executable()
    args = ["mcp", "serve"]
    env: dict[str, str] = {}
    if server:
        from coverset.agent.remote import check_server_url

        try:
            server = check_server_url(server)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from None
        args += ["--server", server]
        env[TOKEN_ENV] = "${" + TOKEN_ENV + "}"
    else:
        args += ["--source", source]
    entry: dict[str, Any] = {"command": exe, "args": args}
    if env:
        entry["env"] = env
    snippet = {"mcpServers": {name: entry}}
    click.echo("# .mcp.json at the root of the project (commit it to share the server with the team):")
    click.echo(json.dumps(snippet, indent=2))
    click.echo("")
    click.echo("# or register it with the Claude Code CLI:")
    # The name goes before -e: Claude Code's -e/--env is variadic and would swallow the name
    # (and then the executable) as further NAME=value entries; `--` ends the env list.
    env_flags = f" -e '{TOKEN_ENV}=${{{TOKEN_ENV}}}'" if server else ""
    click.echo(
        f"claude mcp add --scope project {shlex.quote(name)}{env_flags} -- "
        + " ".join(shlex.quote(part) for part in [exe, *args])
    )
    if server:
        click.echo("")
        click.echo(
            f"# {TOKEN_ENV} must hold an API token with the read scope (never commit the token itself):"
        )
        click.echo(
            "#   create one on the settings page, or on the server: coverset users tokens create --name claude-code --user LOGIN --scopes read"
        )
    elif os.sep in exe:
        click.echo("")
        click.echo(f"# {exe} is this checkout's coverset; teammates may need their own path or `coverset`.")
