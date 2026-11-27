"""Codex's own review of an uncommitted change, through the real CLI, in a throwaway repo.

Run from the checkout:  uv run python scripts/live-checks/smoke_review.py
The throwaway repo's one change turns `a + b` into `a - b` in a function named add. Passes when the job is
`completed` and the reply mentions the subtraction. Spends a little Codex quota.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

from _bridge import brief, reply_data, server_params, temp_dir
from mcp import ClientSession
from mcp.client.stdio import stdio_client


async def main() -> int:
    state = temp_dir("bridge-live-state-")
    repo = temp_dir("bridge-live-repo-")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "smoke@example.invalid")
    git("config", "user.name", "smoke")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    async with stdio_client(server_params(state)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            args = {"backend": "codex", "cwd": str(repo), "effort": "low", "review": {"scope": "uncommitted"},
                    "timeout_seconds": 280, "wait_seconds": 280}
            job = reply_data(await session.call_tool("run", args))
            while job.get("status") in {"queued", "running"} and job.get("job_id"):
                reply = reply_data(await session.call_tool("jobs", {"job_ids": [job["job_id"]], "wait_seconds": 60}))
                job = (reply.get("jobs") or [job])[0]
    text = (job.get("response") or "").lower()
    ok = job.get("status") == "completed" and ("subtract" in text or "a - b" in text or "minus" in text)
    print("PASS" if ok else "FAIL", "codex review", brief(job, response_chars=500))
    print("state folder:", state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
