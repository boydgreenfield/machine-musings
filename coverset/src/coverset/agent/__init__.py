"""coverset for AI agents: one set of tools, served over MCP and over HTTP.

An agent (Claude Code, Cursor, a review bot) asks the questions a careful reviewer would: which
changed lines no test ran, which kinds of input a function's tests never pass in, what example
values look like (redacted), which tests could go without losing a single covered fact. The pieces:

* :mod:`coverset.agent.tools`: the tool table (names, descriptions, JSON Schemas), shared by every
  surface and by ``docs/mcp.md``;
* :mod:`coverset.agent.service`: :class:`AgentService`, every tool over a backend, answering plain
  JSON with caps, pages, redaction and caveats;
* :mod:`coverset.agent.backends`: a local ``.coverset`` directory or bundle, or the facts store;
* :mod:`coverset.agent.context`, :mod:`coverset.agent.cover`, :mod:`coverset.agent.changes`,
  :mod:`coverset.agent.economy`, :mod:`coverset.agent.explain`: one run's caches, the set-cover
  facts, the diff, the test-economy bridge, and words for shape keys;
* :mod:`coverset.agent.mcp_server` and :mod:`coverset.agent.prompts`: the MCP server and its
  three playbooks; :mod:`coverset.agent.remote`: the HTTP client ``--server`` mode uses;
  ``web/agent_api.py``: the ``/v1/agent`` HTTP API.
"""

from __future__ import annotations

from coverset.agent.tools import MAX_ROWS, TOOLS, ToolError, ToolSpec, validate_args

__all__ = ["MAX_ROWS", "TOOLS", "ToolError", "ToolSpec", "validate_args"]
