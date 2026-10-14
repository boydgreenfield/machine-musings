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
    from coverset.commands.flows import flows_group

    cli.add_command(flows_group, "flows")
    from coverset.commands.rust import rust_group

    cli.add_command(rust_group, "rust")
    from coverset.commands.deploy_cmd import deploy_group
    from coverset.commands.doctor import doctor_group
    from coverset.commands.economy import economy_group
    from coverset.commands.mcp import mcp_group
    from coverset.commands.users import users_group

    cli.add_command(deploy_group, "deploy")
    cli.add_command(doctor_group, "doctor")
    cli.add_command(economy_group, "tests")
    cli.add_command(mcp_group, "mcp")
    cli.add_command(users_group, "users")
    from coverset.commands.settings_cmd import settings_group

    cli.add_command(settings_group, "settings")
    from coverset.commands.dev import dev_command

    cli.add_command(dev_command, "dev")
    from coverset.commands.db_cmd import db_group

    cli.add_command(db_group, "db")
    from coverset.commands.cache_cmd import cache_group

    cli.add_command(cache_group, "cache")
