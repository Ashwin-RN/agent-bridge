"""Shared setup for the live checks: start this checkout's bridge over stdio with a temp state folder."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters

REPO = Path(__file__).resolve().parents[2]
# The job line uses a middle dot: print it as UTF-8 whatever the console's code page is.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
NESTING_KEYS = ("AGENT_BRIDGE_PARENT_JOB", "AGENT_BRIDGE_PARENT_OWNER")


def bridge_env(state: Path, **extra: str) -> dict[str, str]:
    """This process's environment, as a fresh host window would have it, with the state in a temp folder."""
    env = dict(os.environ)
    for key in NESTING_KEYS:
        env.pop(key, None)
    env["AGENT_BRIDGE_STATE_DIR"] = str(state)
    env.update(extra)
    return env


def temp_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def server_params(state: Path, **extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "agent_bridge"], env=bridge_env(state, **extra), cwd=str(REPO)
    )


def reply_data(result: Any) -> dict[str, Any]:
    """A tool result as a dict, from its structured content or its text."""
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(data, dict):
        return data
    text = "".join(getattr(block, "text", "") for block in result.content)
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"raw": text[:600]}
    return parsed if isinstance(parsed, dict) else {"raw": text[:600]}


def brief(job: dict[str, Any], *, response_chars: int = 200) -> str:
    keys = ("line", "job_id", "status", "model", "model_source", "failure_reason", "error", "warning", "raw")
    out = {key: job[key] for key in keys if job.get(key) is not None}
    out["response"] = (job.get("response") or "")[:response_chars]
    tokens = (job.get("usage") or {}).get("total_tokens")
    if tokens is not None:
        out["total_tokens"] = tokens
    return json.dumps(out, ensure_ascii=False)
