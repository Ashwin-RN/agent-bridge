"""What the host model carries for the bridge: tool definitions, instructions and a job reply, each capped.

Measured as JSON with `ensure_ascii=False`, as hosts receive it. Before the five tools: 16,980 characters
of definitions (77 arguments), 3,492 of instructions, and about 1,800 for a finished job's reply.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_bridge.nesting import NOT_NESTED
from agent_bridge.server import NESTED_INSTRUCTIONS, SERVER_INSTRUCTIONS, create_server

from test_replies import _job
from test_surface import _settings

TOOLS_BUDGET = 9_000
INSTRUCTIONS_BUDGET = 1_600
REPLY_BUDGET = 1_400
# Codex keeps only the first 512 characters of a server's instructions.
CODEX_KEEPS = 512


async def test_the_five_tool_definitions_fit_their_budget(tmp_path: Path) -> None:
    mcp, manager = create_server(settings=_settings(tmp_path), nesting=NOT_NESTED)
    try:
        tools = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in await mcp.list_tools()]
    finally:
        await manager.shutdown()
    assert [t["name"] for t in tools] == ["run", "jobs", "cancel", "usage", "backends"]
    # Both the compact form and the spaced one a host may print.
    compact = len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    spaced = len(json.dumps(tools, ensure_ascii=False))
    assert max(compact, spaced) <= TOOLS_BUDGET, (compact, spaced)


def test_the_instructions_fit_and_their_first_512_characters_stand_alone() -> None:
    assert len(SERVER_INSTRUCTIONS) <= INSTRUCTIONS_BUDGET, len(SERVER_INSTRUCTIONS)
    # The opening paragraph is whole within what Codex keeps, and says everything a host must know.
    head = SERVER_INSTRUCTIONS.split("\n\n")[0]
    assert len(head) <= CODEX_KEEPS, len(head)
    for needed in (
        "`codex`", "`neon`", "`claude`",  # what the bridge runs
        "`run`", "`jobs`", "`cancel`", "`usage`", "`backends`",  # the five tools
        "returns at once", "collects",  # run answers at once, jobs collects
        "`completed` only means", "read its",  # a lifecycle status, and the reply is still read
        "quota=True", "before a fan-out",  # quota first
        "Default mode is read_only",
    ):
        assert needed in head, needed
    assert len(NESTED_INSTRUCTIONS) <= CODEX_KEEPS


REVIEW_LINE = "1. src/agent_bridge/jobs.py:1291 labels a Codex model it never observed.\n"


def test_a_full_job_reply_fits_its_budget() -> None:
    """A finished job with a 1,000-character reply, a label, a session and a token count."""
    response = (REVIEW_LINE * 14)[:1000]
    assert len(response) == 1000 and response.count("\n") == 13
    reply = _job(response=response).to_detail(now=time.time())
    size = len(json.dumps(reply, ensure_ascii=False, separators=(",", ":")))
    assert size <= REPLY_BUDGET, size
