"""The generated parts of ``docs/mcp.md``: the tool reference and the playbooks' text.

The page's hand-written prose sits around two marked blocks
(``<!-- tools:start -->``/``<!-- tools:end -->`` and ``<!-- playbooks:start -->``/
``<!-- playbooks:end -->``) whose content is generated from :data:`coverset.agent.tools.TOOLS` and
:data:`coverset.agent.prompts.PLAYBOOKS`, so the reference cannot drift from what an agent is
actually told. ``python -m coverset.agent.docs`` rewrites the blocks in place;
``python -m coverset.agent.docs --check`` exits 1 when they are stale (``tests/agent`` runs the
same check).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from coverset.agent.prompts import PLAYBOOKS, render
from coverset.agent.tools import TOOLS

__all__ = ["DOC_PATH", "playbooks_markdown", "sync", "tool_reference_markdown"]

DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "mcp.md"
_BLOCKS = {
    "tools": lambda: tool_reference_markdown(),
    "playbooks": lambda: playbooks_markdown(),
}


def _type(prop: Mapping[str, Any]) -> str:
    kind = prop.get("type", "any")
    if "enum" in prop:
        return " \\| ".join(f"`{v}`" for v in prop["enum"])
    if kind == "array":
        items = prop.get("items", {})
        inner = " \\| ".join(f"`{v}`" for v in items.get("enum", ())) or items.get("type", "any")
        return f"list of {inner}"
    if kind == "integer" and "maximum" in prop:
        return f"integer {prop.get('minimum', 0)}-{prop['maximum']}"
    return str(kind)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def tool_reference_markdown() -> str:
    """One section per tool: title, description, and an argument table."""
    out: list[str] = []
    for spec in TOOLS.values():
        out.append(f"### `{spec.name}`: {spec.title}")
        out.append("")
        out.append(spec.description)
        out.append("")
        props: Mapping[str, Any] = spec.input_schema.get("properties", {})
        required = set(spec.input_schema.get("required", ()))
        if not props:
            out.append("No arguments.")
            out.append("")
            continue
        out.append("| Argument | Type | Default | Description |")
        out.append("|---|---|---|---|")
        for name, prop in props.items():
            default = (
                "required" if name in required else (f"`{prop['default']}`" if "default" in prop else "")
            )
            if isinstance(prop.get("default"), bool):
                default = f"`{str(prop['default']).lower()}`"
            out.append(f"| `{name}` | {_type(prop)} | {default} | {_cell(prop.get('description', ''))} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def playbooks_markdown() -> str:
    """One section per playbook: description, arguments and the exact message an agent receives."""
    out: list[str] = []
    for playbook in PLAYBOOKS.values():
        out.append(f"### `{playbook.name}`: {playbook.title}")
        out.append("")
        out.append(playbook.description)
        out.append("")
        if playbook.args:
            out.append(
                "Arguments: "
                + ", ".join(
                    f"`{a.name}`{' (required)' if a.required else ''}: {a.description}" for a in playbook.args
                )
            )
            out.append("")
        out.append("The message the agent receives (arguments left unset):")
        out.append("")
        out.append("```text")
        out.append(render(playbook.name).rstrip())
        out.append("```")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _replace(text: str, name: str, body: str) -> str:
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    head, found, rest = text.partition(start)
    if not found or end not in rest:
        raise ValueError(f"docs/mcp.md has no {start} ... {end} block")
    _, _, tail = rest.partition(end)
    return f"{head}{start}\n\n{body}\n{end}{tail}"


def sync(path: Path = DOC_PATH, *, write: bool = True) -> bool:
    """Regenerate the marked blocks of ``path``; return True when it was already up to date."""
    text = path.read_text(encoding="utf-8")
    new = text
    for name, build in _BLOCKS.items():
        new = _replace(new, name, build())
    if new != text and write:
        path.write_text(new, encoding="utf-8")
    return new == text


def main(argv: list[str] | None = None) -> int:
    """``python -m coverset.agent.docs [--check] [PATH]``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path, default=DOC_PATH)
    parser.add_argument("--check", action="store_true", help="exit 1 when the generated blocks are stale")
    args = parser.parse_args(argv)
    fresh = sync(args.path, write=not args.check)
    if args.check and not fresh:
        print(
            f"{args.path}: the generated blocks are stale; run python -m coverset.agent.docs", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
