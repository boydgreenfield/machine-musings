"""The MCP server: tools, resources and prompts through a real MCP client.

Most tests connect the SDK's :class:`mcp.Client` to the server in-process; one launches
``coverset mcp serve`` as a subprocess and talks to it over stdio, the way Claude Code does. The
docs test keeps ``docs/mcp.md``'s generated tool reference and playbooks identical to what the
server tells an agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from agent_helpers import DemoApp
from click.testing import CliRunner
from mcp import Client, StdioServerParameters

from coverset.agent import docs
from coverset.agent.backends import LocalBackend
from coverset.agent.mcp_server import build_server
from coverset.agent.prompts import PLAYBOOKS, UNSET, render
from coverset.agent.service import AgentService
from coverset.agent.tools import TOOLS


@pytest.fixture
def server(demo_app: DemoApp):  # type: ignore[no-untyped-def]
    return build_server(AgentService(LocalBackend(demo_app.bundle, repo_root=demo_app.root)))


async def test_lists_every_tool_with_its_schema(server) -> None:  # type: ignore[no-untyped-def]
    async with Client(server) as client:
        listed = (await client.list_tools()).tools
    assert [t.name for t in listed] == list(TOOLS)
    for tool in listed:
        spec = TOOLS[tool.name]
        assert tool.description == spec.description
        assert tool.input_schema["properties"].keys() == spec.input_schema["properties"].keys()
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


async def test_calls_three_tools(server) -> None:  # type: ignore[no-untyped-def]
    async with Client(server) as client:
        summary = await client.call_tool("get_run_summary", {})
        function = await client.call_tool("get_function", {"function": "parse_duration"})
        gaps = await client.call_tool("find_gaps", {"limit": 2})
    assert not summary.is_error and summary.structured_content["tests"]["total"] == 97
    assert json.loads(summary.content[0].text) == summary.structured_content
    classes = {c["class"]: c for c in function.structured_content["input_classes"]["classes"]}
    assert classes["text:multi-letter-unit"]["observed"] is False
    assert classes["text:multi-letter-unit"]["strategy"] == 'st.sampled_from(["100ms", "2min", "3hr"])'
    assert len(gaps.structured_content["gaps"]) == 2


async def test_a_failed_call_is_an_error_result_the_agent_can_read(server) -> None:  # type: ignore[no-untyped-def]
    async with Client(server) as client:
        unknown = await client.call_tool("get_function", {"function": "parse_dur"})
        invalid = await client.call_tool("find_gaps", {"limit": 10_000})
    assert unknown.is_error
    body = json.loads(unknown.content[0].text)
    assert body["error"] == "not_found" and "src/ledger/durations/parse.py::parse_duration" in body["similar"]
    assert invalid.is_error and json.loads(invalid.content[0].text)["error"] == "invalid_argument"


async def test_resources(server) -> None:  # type: ignore[no-untyped-def]
    async with Client(server) as client:
        listed = {r.uri for r in (await client.list_resources()).resources}
        templates = (await client.list_resource_templates()).resource_templates
        guide = await client.read_resource("coverset://docs/mcp")
        classes = await client.read_resource("coverset://docs/input-classes")
        summary = await client.read_resource("coverset://run/summary")
        by_id = await client.read_resource("coverset://runs/local/summary")
        missing = await client.read_resource("coverset://runs/run_nope/summary")
    assert {"coverset://docs/mcp", "coverset://docs/input-classes", "coverset://run/summary"} <= {
        str(u) for u in listed
    }
    assert [t.uri_template for t in templates] == ["coverset://runs/{run}/summary"]
    assert guide.contents[0].text.startswith("# ")
    assert "input class" in classes.contents[0].text.lower()
    assert json.loads(summary.contents[0].text)["tests"]["total"] == 97
    assert json.loads(by_id.contents[0].text)["run"] == "local"
    assert json.loads(missing.contents[0].text)["error"] == "not_found"


async def test_prompts(server) -> None:  # type: ignore[no-untyped-def]
    async with Client(server) as client:
        listed = (await client.list_prompts()).prompts
        review = await client.get_prompt("review-tests-for-pr", {"base": "origin/main"})
        bootstrap = await client.get_prompt("bootstrap-tests-for-function", {"function": "allocate_cents"})
        shrink = await client.get_prompt("shrink-test-suite", {})
    assert [p.name for p in listed] == list(PLAYBOOKS)
    bootstrap_args = next(p for p in listed if p.name == "bootstrap-tests-for-function").arguments
    assert [(a.name, a.required) for a in bootstrap_args] == [("function", True), ("run", False)]
    review_text = review.messages[0].content.text
    assert "base=origin/main" in review_text and "Never claim" in review_text
    assert review_text == render("review-tests-for-pr", {"base": "origin/main"})
    assert "get_function (function=allocate_cents" in bootstrap.messages[0].content.text
    shrink_text = shrink.messages[0].content.text
    assert "Scope: all." in shrink_text and UNSET in shrink_text and "leave it out" in shrink_text


async def test_a_prompt_without_its_required_argument_fails(server) -> None:  # type: ignore[no-untyped-def]
    from mcp import MCPError

    async with Client(server) as client:
        with pytest.raises(MCPError):
            await client.get_prompt("bootstrap-tests-for-function", {})


async def test_stdio_transport(demo_app: DemoApp) -> None:
    """``coverset mcp serve`` as a subprocess, over stdio, as Claude Code runs it."""
    exe = Path(sys.executable).parent / "coverset"
    if not exe.is_file():
        pytest.skip("no coverset entry point next to this interpreter")
    params = StdioServerParameters(
        command=str(exe),
        args=["mcp", "serve", "--source", str(demo_app.bundle), "--repo-root", str(demo_app.root)],
        cwd=str(demo_app.root),
    )
    async with Client(params, read_timeout_seconds=120) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        result = await client.call_tool("suggest_tests", {"function": "allocate_cents"})
    assert names == list(TOOLS)
    assert not result.is_error
    assert "total_cents:negative" in {m["class"] for m in result.structured_content["missing"]}


def test_docs_are_in_sync_with_the_tools_and_playbooks() -> None:
    assert docs.DOC_PATH.is_file()
    assert docs.sync(docs.DOC_PATH, write=False), "run: uv run python -m coverset.agent.docs"
    text = docs.DOC_PATH.read_text(encoding="utf-8")
    for name in TOOLS:
        assert f"### `{name}`" in text
    for name in PLAYBOOKS:
        assert render(name).splitlines()[0] in text


def test_install_claude_code_prints_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.commands.mcp import mcp_group

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    local = runner.invoke(mcp_group, ["install-claude-code"])
    remote = runner.invoke(mcp_group, ["install-claude-code", "--server", "https://coverset.example.com"])
    insecure = runner.invoke(mcp_group, ["install-claude-code", "--server", "http://coverset.example.com"])
    assert list(tmp_path.iterdir()) == []
    assert local.exit_code == 0, local.output
    block = local.output.split("\n# or register", 1)[0].split("\n", 1)[1]
    entry = json.loads(block)["mcpServers"]["coverset"]
    assert entry["args"] == ["mcp", "serve", "--source", ".coverset"]
    assert "claude mcp add --scope project coverset -- " in local.output
    remote_entry = json.loads(remote.output.split("\n# or register", 1)[0].split("\n", 1)[1])["mcpServers"][
        "coverset"
    ]
    assert remote_entry["args"][-2:] == ["--server", "https://coverset.example.com"]
    assert remote_entry["env"] == {"COVERSET_TOKEN": "${COVERSET_TOKEN}"}, "the token is never written"
    # -e is variadic in Claude Code's CLI: the name must come first and `--` must end the env list.
    assert (
        "claude mcp add --scope project coverset -e 'COVERSET_TOKEN=${COVERSET_TOKEN}' -- " in remote.output
    )
    named = runner.invoke(
        mcp_group, ["install-claude-code", "--server", "https://coverset.example.com", "--name", "cs prod"]
    )
    assert (
        "claude mcp add --scope project 'cs prod' -e 'COVERSET_TOKEN=${COVERSET_TOKEN}' -- " in named.output
    )
    assert insecure.exit_code != 0 and "https" in insecure.output


def test_mcp_call_command(demo_app: DemoApp) -> None:
    from coverset.commands.mcp import mcp_group

    runner = CliRunner()
    ok = runner.invoke(
        mcp_group,
        ["call", "explain_shape_key", '{"key": "int|neg"}', "--source", str(demo_app.bundle)],
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["in_words"] == "an integer: negative"
    bad = runner.invoke(mcp_group, ["call", "find_gaps", '{"limit": 0}', "--source", str(demo_app.bundle)])
    assert bad.exit_code == 1
    both = runner.invoke(
        mcp_group, ["call", "list_repos", "--source", str(demo_app.bundle), "--server", "https://x.example"]
    )
    assert both.exit_code != 0 and "one of" in both.output
