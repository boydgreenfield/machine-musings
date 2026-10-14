"""coverset as an MCP server: the agent tools, two documentation resources, a run summary, three
playbooks.

:func:`build_server` wraps anything with the service surface (``label``, ``tools()``, ``call()``):
a local :class:`~coverset.agent.service.AgentService` (over a ``.coverset`` directory or the facts
store) or a :class:`~coverset.agent.remote.RemoteService` (a deployed coverset over HTTP). It uses
the MCP SDK's low-level :class:`mcp.server.lowlevel.Server` with explicit handlers rather than the
decorator API, because the tool schemas are data (:data:`coverset.agent.tools.TOOLS`) shared with
the HTTP API and the docs, not Python signatures.

What an agent gets:

* **tools**: every tool, annotated read-only, idempotent and closed-world (nothing here changes a
  repository or reaches the internet); a result is the tool's JSON both as ``structuredContent``
  and as indented text (for clients that only show text); a failure is an ``isError`` result whose
  text is the error JSON (``{"error": "ambiguous", "candidates": [...]}``), so the agent can recover;
* **resources**: ``coverset://docs/mcp`` and ``coverset://docs/input-classes`` (the Markdown pages,
  when this is a source checkout), ``coverset://run/summary`` (the default run's summary) and the
  template ``coverset://runs/{run}/summary``;
* **prompts**: the playbooks of :mod:`coverset.agent.prompts`;
* **instructions**: a paragraph telling the agent where to start and what never to claim.

:func:`serve_stdio` runs it on stdin/stdout (``coverset mcp serve``); nothing else may write to
stdout meanwhile, so logging goes to stderr.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final, Protocol

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server

from coverset import __version__
from coverset.agent.prompts import PLAYBOOKS, render
from coverset.agent.tools import ToolError, ToolSpec

__all__ = ["INSTRUCTIONS", "RESOURCES", "ServiceLike", "build_server", "serve_stdio"]

DOCS_DIR: Final = Path(__file__).resolve().parents[3] / "docs"
"""``docs/`` of a source checkout (a wheel does not ship it; the resources then say where to read it)."""

RESOURCES: Final = {
    "coverset://docs/mcp": (
        "mcp.md",
        "coverset MCP guide",
        "Setup, the tool reference, the playbooks, trust notes.",
    ),
    "coverset://docs/input-classes": (
        "input-classes.md",
        "Input classes",
        "How expected input classes are derived from declared types and observed shapes.",
    ),
}
SUMMARY_URI: Final = "coverset://run/summary"
SUMMARY_TEMPLATE: Final = "coverset://runs/{run}/summary"
_SUMMARY_RE: Final = re.compile(r"^coverset://runs/([A-Za-z0-9_-]{1,64})/summary$")

INSTRUCTIONS: Final = """\
coverset knows, for one test run, which lines and branches each test ran and which kinds of input \
(input classes such as email:multi-label-domain or total_cents:negative) each function's tests \
passed in. Start with get_run_summary; for a change use get_patch_coverage; for one function \
get_function, example_inputs and suggest_tests; to shrink a suite minimal_test_set and \
redundant_tests. Use the prompts review-tests-for-pr, bootstrap-tests-for-function and \
shrink-test-suite for the full playbooks. Removal and redundancy results are candidates, not \
verdicts (coverage cannot see assertions); missing input classes come from sampled shapes and mean \
"never observed", not "buggy"; example values are redacted shapes, never real data."""


class ServiceLike(Protocol):
    """What the server needs: :class:`~coverset.agent.service.AgentService` or
    :class:`~coverset.agent.remote.RemoteService`."""

    label: str

    async def tools(self) -> list[ToolSpec]:
        """The tools to list."""
        ...

    async def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one tool."""
        ...


_READ_ONLY: Final = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _text(data: Any) -> types.TextContent:
    return types.TextContent(type="text", text=json.dumps(data, indent=1, ensure_ascii=False, default=str))


def build_server(service: ServiceLike) -> Server[Any]:
    """The MCP server over ``service`` (see the module docstring)."""

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        specs = await service.tools()
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=spec.name,
                    title=spec.title,
                    description=spec.description,
                    input_schema=dict(spec.input_schema),
                    annotations=_READ_ONLY,
                )
                for spec in specs
            ]
        )

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        try:
            result = await service.call(params.name, dict(params.arguments or {}))
        except ToolError as exc:
            return types.CallToolResult(content=[_text(exc.to_json())], is_error=True)
        return types.CallToolResult(content=[_text(result)], structured_content=result)

    async def list_resources(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        resources = [
            types.Resource(uri=uri, name=title, description=description, mime_type="text/markdown")
            for uri, (_, title, description) in RESOURCES.items()
        ]
        resources.append(
            types.Resource(
                uri=SUMMARY_URI,
                name="Run summary",
                description=f"get_run_summary of the default run ({service.label}), as JSON.",
                mime_type="application/json",
            )
        )
        return types.ListResourcesResult(resources=resources)

    async def list_templates(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(
            resource_templates=[
                types.ResourceTemplate(
                    uri_template=SUMMARY_TEMPLATE,
                    name="Run summary by id",
                    description="get_run_summary of one run, as JSON.",
                    mime_type="application/json",
                )
            ]
        )

    async def read_resource(
        ctx: ServerRequestContext[Any], params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        uri = str(params.uri)
        if uri in RESOURCES:
            name = RESOURCES[uri][0]
            path = DOCS_DIR / name
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = f"docs/{name} is not part of this installation: read it in the coverset source tree."
            return types.ReadResourceResult(
                contents=[types.TextResourceContents(uri=uri, mime_type="text/markdown", text=text)]
            )
        run = None
        if uri != SUMMARY_URI:
            match = _SUMMARY_RE.fullmatch(uri)
            if match is None:
                raise ValueError(f"unknown resource {uri}")
            run = match.group(1)
        try:
            summary: Any = await service.call("get_run_summary", {"run": run} if run else {})
        except ToolError as exc:
            summary = exc.to_json()
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=uri, mime_type="application/json", text=json.dumps(summary, indent=1, default=str)
                )
            ]
        )

    async def list_prompts(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name=p.name,
                    title=p.title,
                    description=p.description,
                    arguments=[
                        types.PromptArgument(name=a.name, description=a.description, required=a.required)
                        for a in p.args
                    ],
                )
                for p in PLAYBOOKS.values()
            ]
        )

    async def get_prompt(
        ctx: ServerRequestContext[Any], params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        playbook = PLAYBOOKS.get(params.name)
        if playbook is None:
            raise ValueError(f"unknown prompt {params.name!r}; known: {', '.join(PLAYBOOKS)}")
        args = {k: str(v) for k, v in (params.arguments or {}).items() if v is not None}
        missing = [a.name for a in playbook.args if a.required and not args.get(a.name)]
        if missing:
            raise ValueError(f"prompt {playbook.name} needs the argument {missing[0]!r}")
        return types.GetPromptResult(
            description=playbook.description,
            messages=[
                types.PromptMessage(
                    role="user", content=types.TextContent(type="text", text=render(playbook.name, args))
                )
            ],
        )

    return Server(
        "coverset",
        version=__version__,
        title="coverset",
        description="Per-test coverage, input classes and test-suite economy for AI agents.",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_templates,
        on_read_resource=read_resource,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
    )


async def serve_stdio(service: ServiceLike) -> None:
    """Serve ``service`` over stdin/stdout until the client disconnects."""
    from mcp.server.stdio import stdio_server

    server = build_server(service)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
