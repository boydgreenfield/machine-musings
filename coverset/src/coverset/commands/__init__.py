"""Click commands of the ``coverset`` CLI that drive the orchestrator (registered by ``coverset.cli``).

Each command is small: argument parsing, settings, then a call into :mod:`coverset.orchestrator`,
:mod:`coverset.facts` or :mod:`coverset.demo`, where the logic lives.
"""

from __future__ import annotations

import click

from coverset.commands.demo import demo_command, seed_demo_command
from coverset.commands.facts import FACTS_DEBUG_COMMANDS, facts_query_command, shapes_command
from coverset.commands.invariants import invariants_scan_command
from coverset.commands.maintenance import (
    gc_command,
    invariants_check_command,
    modal_reap_command,
    repo_purge_command,
)
from coverset.commands.run import run_command, warm_command

__all__ = ["register_commands"]


def register_commands(
    cli: click.Group,
    *,
    facts: click.Group,
    invariants: click.Group,
    repo: click.Group,
    modal: click.Group,
) -> None:
    """Add (or replace placeholders with) the orchestrator's commands on the CLI groups."""
    cli.add_command(run_command, "run")
    cli.add_command(warm_command, "warm")
    cli.add_command(seed_demo_command, "seed-demo")
    cli.add_command(demo_command, "demo")
    cli.add_command(shapes_command, "shapes")
    cli.add_command(gc_command, "gc")
    facts.add_command(facts_query_command, "query")
    for name, command in FACTS_DEBUG_COMMANDS.items():
        facts.add_command(command, name)
    invariants.add_command(invariants_check_command, "check")
    invariants.add_command(invariants_scan_command, "scan")
    repo.add_command(repo_purge_command, "purge")
    modal.add_command(modal_reap_command, "reap")
    from coverset.commands.bundle import bundle_group

    cli.add_command(bundle_group, "bundle")
    from coverset.commands.report import report_command

    cli.add_command(report_command, "report")
